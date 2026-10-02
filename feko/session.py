"""License-bounded, experiment-owned CADFEKO processes with sequential job queues."""
from contextlib import contextmanager
from pathlib import Path
import shutil
import subprocess
import threading
import time
import uuid

from .export import export_model
from .results import save_results
from .runner import read_status, validate_outputs, stop_tree, wait_for_saved_exit, STAGES
from .timing import available_solver_timing


class CadSession:
    def __init__(self, executable, directory):
        self.executable, self.directory = executable, Path(directory)
        self.directory.mkdir(parents=True)
        shutil.copyfile(Path(__file__).with_name("session.lua"), self.directory/"session.lua")
        self.process, self.log = None, None
        self.jobs = 0

    def state(self):
        try:
            value = (self.directory/"session_status.txt").read_text(encoding="utf-8").split("\n", 1)
            return value[0], value[1].strip() if len(value) > 1 else ""
        except OSError:
            return "", ""

    def check(self):
        state, detail = self.state()
        if state == "failed":
            raise RuntimeError("CADFEKO session failed: "+detail)
        code = self.process.poll()
        if code is not None:
            raise RuntimeError(f"CADFEKO session exited ({code}); see {self.directory/'cadfeko.log'}")

    def start(self, emit, cancel, timeout):
        if self.process is not None:
            self.check()
            return 0.0
        emit("stage", message="Запуск CADFEKO")
        self.log = (self.directory/"cadfeko.log").open("wb")
        started = time.perf_counter()
        self.process = subprocess.Popen([str(self.executable), "--run-script", str(self.directory/"session.lua"),
                                         "--non-interactive"], cwd=self.directory, stdout=self.log, stderr=subprocess.STDOUT)
        try:
            while self.state()[0] != "ready":
                cancel()
                self.check()
                if time.perf_counter()-started > timeout:
                    raise TimeoutError("CADFEKO session startup timed out")
                time.sleep(.05)
        except BaseException:
            self.close(force=True)
            raise
        emit("log", message=f"CADFEKO session ready: PID {self.process.pid}")
        return time.perf_counter()-started

    def request(self, text):
        temp = self.directory/"request-next.txt"
        temp.write_text(text, encoding="utf-8")
        temp.replace(self.directory/"request.txt")

    def run(self, project, directory, emit, cancel):
        start = time.perf_counter()
        directory = Path(directory).resolve()/"feko"
        emit("stage", message="Подготовка скрипта CADFEKO")
        script = export_model(project, directory)
        export_s = time.perf_counter()-start
        options = project["feko"]
        startup_s = self.start(emit, cancel, options["timeout_s"])
        # Paths are data read by Lua, never interpolated as executable Lua source.
        if any(ch in str(script) for ch in "\r\n"):
            raise ValueError("FEKO job path contains a newline")
        (directory/"cadfeko.log").write_text(f"Shared CADFEKO log: {self.directory/'cadfeko.log'}\n", encoding="utf-8")
        self.request(str(directory)+"\n"+str(script)+"\n")
        job_started = time.perf_counter()
        observations, previous = [], None
        try:
            while True:
                cancel()
                self.check()
                state = read_status(directory)
                if state:
                    status, stage, detail = state
                    if stage != previous:
                        observations.append(dict(stage=stage, elapsed_s=time.perf_counter()-job_started))
                        if stage in STAGES:
                            message = STAGES[stage]
                            if not options["compute_pattern"]:
                                message = message.replace("S11 и Gain", "S11")
                            emit("stage", message=message)
                    previous = stage
                    if status == "failed":
                        raise RuntimeError(detail or "CADFEKO job failed")
                    if status == "done":
                        if stage != "solved":
                            raise RuntimeError("CADFEKO job did not solve the antenna")
                        # Wait for CloseProject before queueing another model or stopping the process.
                        if self.state()[0] != "ready":
                            time.sleep(.05)
                            continue
                        files = validate_outputs(directory, True, options["compute_pattern"])
                        parsing_start = time.perf_counter()
                        rows = save_results(directory, options["compute_pattern"])
                        parse_s = time.perf_counter()-parsing_start
                        timings = dict(available_solver_timing(directory), export=export_s,
                                       cad_startup=startup_s, process_until_outputs=parsing_start-job_started,
                                       parsing=parse_s, saved_exit=0, total=time.perf_counter()-start)
                        reused = self.jobs > 0
                        self.jobs += 1
                        result = dict(status="solved", directory=str(directory), summary=rows, files=files,
                                      timings_s=timings, stage_observations=observations,
                                      session=dict(pid=self.process.pid, directory=str(self.directory), reused=reused))
                        emit("done", engine="feko", message="FEKO: расчёт завершён", **result)
                        return result
                if time.perf_counter()-job_started > options["timeout_s"]:
                    raise TimeoutError("FEKO job timed out; owned CADFEKO session stopped")
                time.sleep(.05)
        except BaseException:
            self.close(force=True)
            raise

    def close(self, force=False):
        try:
            if self.process is not None and self.process.poll() is None:
                if force:
                    stop_tree(self.process)
                else:
                    self.request("STOP\n")
                    wait_for_saved_exit(self.process)
        finally:
            if self.log is not None:
                self.log.close()
                self.log = None


class CadSessionPool:
    def __init__(self, executable, directory, maximum, cancel):
        self.executable, self.directory = executable, Path(directory)
        self.maximum, self.cancel = maximum, cancel
        self.sessions, self.idle = [], []
        self.lock = threading.Condition()

    @contextmanager
    def acquire(self):
        with self.lock:
            while not self.idle and len(self.sessions) >= self.maximum:
                self.cancel()
                self.lock.wait(.05)
            if self.idle:
                session = self.idle.pop()
            else:
                session = CadSession(self.executable, self.directory/uuid.uuid4().hex[:12])
                self.sessions.append(session)
        try:
            yield session
        finally:
            with self.lock:
                self.idle.append(session)
                self.lock.notify()

    def close(self):
        failures = []
        for session in self.sessions:
            try:
                session.close()
            except Exception as exc:
                failures.append(exc)
        if failures:
            raise RuntimeError("CADFEKO session cleanup failed: "+str(failures[0]))
