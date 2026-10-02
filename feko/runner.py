"""Run an owned CADFEKO instance and follow the generated script's status."""
import os
from pathlib import Path
import shutil
import subprocess
import time

from .config import validate_options
from .export import export_model
from .results import save_results
from .timing import available_solver_timing

STAGES = {"geometry": "FEKO: создание металла и порта", "substrate": "FEKO: подложка и земля",
          "requests": "FEKO: настройка S11 и Gain", "mesh": "FEKO: построение сетки",
          "solver": "FEKO: расчёт S11 и Gain"}


def installed_roots():
    if os.name != "nt":
        return []
    import winreg
    roots = []
    key_name = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, key_name, 0, winreg.KEY_READ | view) as key:
                    for index in range(winreg.QueryInfoKey(key)[0]):
                        try:
                            with winreg.OpenKey(key, winreg.EnumKey(key, index)) as entry:
                                name = winreg.QueryValueEx(entry, "DisplayName")[0]
                                if "feko" in name.lower():
                                    location = winreg.QueryValueEx(entry, "InstallLocation")[0]
                                    if location:
                                        roots.append(Path(location))
                        except OSError:
                            continue
            except OSError:
                continue
    return list(dict.fromkeys(roots))


def find_cadfeko(configured=""):
    if configured.strip():
        path = Path(os.path.expandvars(configured)).expanduser()
        if path.name.lower() not in ("cadfeko.exe", "cadfeko"):
            raise ValueError("Укажите именно cadfeko.exe в параметрах FEKO.")
        return path.resolve() if path.is_file() else None
    located = shutil.which("cadfeko.exe") or shutil.which("cadfeko")
    if located:
        return Path(located).resolve()
    roots = [Path(os.environ[key]) for key in ("FEKO_HOME", "ALTAIR_HOME") if os.environ.get(key)]
    roots.extend(installed_roots())
    for base in (os.environ.get("ProgramFiles", "C:/Program Files"), "C:/Altair"):
        parent = Path(base) if base == "C:/Altair" else Path(base) / "Altair"
        if parent.is_dir():
            roots.append(parent)
            roots.extend(sorted((p for p in parent.iterdir() if p.is_dir()), reverse=True))
    for root in roots:
        for suffix in ("bin/cadfeko.exe", "feko/bin/cadfeko.exe", "altair/feko/bin/cadfeko.exe"):
            candidate = root / suffix
            if candidate.is_file():
                return candidate.resolve()
    return None


def read_status(directory):
    try:
        fields = (Path(directory) / "feko_status.txt").read_text(encoding="utf-8").split("\n", 2)
    except (OSError, UnicodeError):
        return None
    if len(fields) != 3 or fields[0] not in ("running", "failed", "done"):
        return None
    return fields[0], fields[1], fields[2].strip()


def validate_outputs(directory, solved, compute_pattern=True):
    directory = Path(directory)
    files = [p for p in directory.iterdir() if p.is_file() and p.stat().st_size > 0]
    project = directory / "antenna.cfx"
    if project not in files:
        raise RuntimeError("CADFEKO не создал antenna.cfx. См. cadfeko.log.")
    if solved:
        for suffix in ((".s1p", ".ffe") if compute_pattern else (".s1p",)):
            if not any(p.suffix.lower() == suffix for p in files):
                raise RuntimeError(f"FEKO завершился, но файл {suffix} не найден. Проверьте solver.log и .out.")
    return [p.name for p in files if p.suffix.lower() in (".cfx", ".s1p", ".ffe", ".bof", ".out")]


def stop_tree(process):
    if process.poll() is None:
        if os.name == "nt":
            subprocess.run([str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/taskkill.exe"),
                            "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=False)
        else:
            process.terminate()
        process.wait(timeout=30)


def wait_for_saved_exit(process):
    """Allow the script's Application:Exit() to finish; only clean up our instance."""
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        stop_tree(process)


def run_feko(project, directory, emit, *, cancel=None):
    total_started = time.monotonic()
    directory = Path(directory).resolve() / "feko"
    options = validate_options(project.get("feko", {}), project)
    emit("stage", message="Подготовка скрипта CADFEKO")
    script = export_model(project, directory)
    export_s = time.monotonic()-total_started
    executable = find_cadfeko(options["cadfeko_exe"])
    if executable is None:
        emit("done", engine="feko", status="prepared", directory=str(directory),
             message="Скрипт готов. CADFEKO не найден.", files=[script.name, "model.json"])
        return
    emit("log", message=f"CADFEKO: {executable}")
    emit("stage", message="Запуск CADFEKO")
    command = [str(executable), "--run-script", str(script)]
    if options["run_solver"]:
        command.append("--non-interactive")
    with (directory / "cadfeko.log").open("wb") as log:
        process = subprocess.Popen(command, cwd=directory,
                                   stdout=log, stderr=subprocess.STDOUT)
        started = time.monotonic()
        previous = None
        observations = []
        try:
            while True:
                if cancel is not None:
                    cancel()
                state = read_status(directory)
                if state is not None:
                    status, stage, detail = state
                    if stage != previous:
                        observations.append(dict(stage=stage, elapsed_s=time.monotonic()-started))
                    if stage != previous and stage in STAGES:
                        message = STAGES[stage]
                        if not options["compute_pattern"]:
                            message = message.replace("S11 и Gain", "S11")
                        emit("stage", message=message)
                    previous = stage
                    if status == "failed":
                        raise RuntimeError(detail or "Ошибка скрипта CADFEKO")
                    if status == "done":
                        solved = options["run_solver"]
                        if stage != ("solved" if solved else "model_created"):
                            raise RuntimeError("Некорректный итоговый статус CADFEKO")
                        files = validate_outputs(directory, solved, options["compute_pattern"])
                        parse_started = time.monotonic()
                        summary = save_results(directory, options["compute_pattern"]) if solved else []
                        parse_s = time.monotonic()-parse_started
                        exit_started = time.monotonic()
                        if solved:
                            files += ["summary.json", "summary.csv"]
                            if options["compute_pattern"]:
                                files += ["farfield.json"]
                            wait_for_saved_exit(process)
                        emit("done", engine="feko", status=stage, directory=str(directory), files=files,
                             summary=summary,
                             timings_s=dict(available_solver_timing(directory), export=export_s, process_until_outputs=parse_started-started,
                                            parsing=parse_s, saved_exit=time.monotonic()-exit_started,
                                            total=time.monotonic()-total_started,
                                            first_status_observed=observations[0]["elapsed_s"] if observations else None),
                             stage_observations=observations,
                             message="FEKO: расчёт завершён" if solved else "Модель FEKO создана")
                        return
                code = process.poll()
                if code is not None:
                    raise RuntimeError(f"CADFEKO закрылся до завершения скрипта (код {code}). См. cadfeko.log.")
                if time.monotonic()-started > options["timeout_s"]:
                    raise TimeoutError("Истёк тайм-аут FEKO; запущенное дерево процессов остановлено.")
                time.sleep(.25)
        except BaseException:
            stop_tree(process)
            raise
