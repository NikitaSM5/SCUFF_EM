"""Full FEKO evaluations, persistent physics cache and license-bounded concurrency."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import time

from feko.runner import find_cadfeko, run_feko
from feko.session import CadSessionPool
from .backends import FitnessBackend
from .preprocessing import source_fingerprint
from .storage import file_hash, key


class FekoFitnessBackend(FitnessBackend):
    name = "feko"

    def __init__(self, project, settings, directory, journal, cancel):
        executable = find_cadfeko(project["feko"]["cadfeko_exe"])
        if executable is None:
            raise FileNotFoundError("CADFEKO не найден. Укажите путь в параметрах FEKO.")
        binaries = [executable]+[p for p in (executable.parent / "runfeko.exe", executable.parent / "feko.exe") if p.is_file()]
        fingerprint = dict(sources=source_fingerprint(), binaries={str(p): file_hash(p) for p in binaries})
        super().__init__(project, settings, directory, journal, cancel, fingerprint)
        self.project["feko"].update(cadfeko_exe=str(executable), run_solver=True)
        self.sessions = CadSessionPool(executable, self.directory/"cadfeko-sessions",
                                       settings["max_parallel_feko_jobs"], cancel) if settings.get("reuse_cadfeko", True) else None

    def calculate(self, cells, directory):
        done = []

        def event(kind, **data):
            if kind == "done":
                done.append(data)
            else:
                self.journal.event(kind, **data)

        with self.lock:
            self.stats["feko_runs"] += 1
        if self.sessions:
            with self.sessions.acquire() as session:
                session.run(dict(self.project, cells=cells), directory, event, self.cancel)
        else:
            run_feko(dict(self.project, cells=cells), directory, event, cancel=self.cancel)
        if not done or done[0]["status"] != "solved":
            raise RuntimeError("FEKO did not finish a full solve")
        result = done[0]
        files = list((directory / "feko").glob("*.s1p"))+list((directory / "feko").glob("*.ffe"))
        artifacts = [dict(path=str(p), sha256=file_hash(p)) for p in files]
        timestamps = []
        timing_file = directory / "feko/stage_times.tsv"
        if timing_file.is_file():
            for line in timing_file.read_text().splitlines():
                stage, timestamp = line.split("\t")
                timestamps.append((stage, float(timestamp)))
        stages = {a[0]: b[1]-a[1] for a, b in zip(timestamps, timestamps[1:])}
        return dict(rows=result["summary"], timings_s=result["timings_s"], artifacts=artifacts,
                    diagnostics=dict(stage_wall_seconds=stages, stage_resolution_s=1,
                                     stage_observations=result["stage_observations"], session=result.get("session")))

    def close(self):
        started = time.perf_counter()
        try:
            if self.sessions:
                self.sessions.close()
        finally:
            self.cleanup_s = time.perf_counter()-started

    def evaluate_many(self, cells, contexts=None):
        contexts = contexts or [{} for _ in cells]
        if self.settings["max_parallel_feko_jobs"] == 1:
            return super().evaluate_many(cells, contexts)
        # Deduplicate in-flight work as well as completed disk-cache entries.
        with ThreadPoolExecutor(max_workers=self.settings["max_parallel_feko_jobs"]) as pool:
            futures = {}
            for mask, context in zip(cells, contexts):
                ident = key(mask)
                if ident not in futures:
                    futures[ident] = pool.submit(self.evaluate, mask, context=context)
            results = {ident: future.result() for ident, future in futures.items()}
        seen = set()
        output = []
        for mask, context in zip(cells, contexts):
            ident = key(mask)
            if ident in seen:
                # A duplicate is still accounted as an evaluation request/cache hit.
                output.append(self.evaluate(mask, context=context))
            else:
                output.append(results[ident])
                seen.add(ident)
        return output
