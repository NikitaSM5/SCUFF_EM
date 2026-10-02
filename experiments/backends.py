"""Fitness backend contract and shared evaluation accounting; no GA decisions."""
from abc import ABC, abstractmethod
import copy
from pathlib import Path
import threading
import time
import uuid

from .metrics import fitness
from .config import ExperimentCancelled
from .storage import CalculationCache, key, write_json
from gui.topology_config import cache_signature


class FitnessBackend(ABC):
    name = "abstract"

    def __init__(self, project, settings, directory, journal, cancel, fingerprint):
        self.project, self.settings = copy.deepcopy(project), dict(settings)
        self.directory, self.journal, self.cancel = Path(directory), journal, cancel
        self.cache = CalculationCache(settings["cache_dir"], self.name)
        self.fingerprint = fingerprint
        self.lock = threading.RLock()
        self.stats = dict(evaluations=0, cache_hits=0, failures=0, schur_updates=0, refactorizations=0, feko_runs=0)
        self.records = []
        self.memory = {}
        self.best = None
        self.epoch = time.perf_counter()

    def identity(self, cells):
        return dict(schema=1, solver=self.name, physics=cache_signature(self.project), cells=cells,
                    reference_ohm=self.project["feko"]["reference_ohm"], compute_pattern=self.project["feko"]["compute_pattern"],
                    angle_step_deg=self.project["feko"]["angle_step_deg"], fingerprint=self.fingerprint)

    def evaluate(self, cells, *, force=False, context=None):
        start = time.perf_counter()
        self.cancel()
        identity = self.identity(cells)
        cache_key = key(identity)
        candidate_id = key(cells)
        hit = False
        folder = None
        try:
            payload = None
            if self.settings["use_cache"] and not force:
                with self.lock:
                    payload = self.memory.get(cache_key)
                if payload is None:
                    payload = self.cache.get(identity)
                hit = payload is not None
            if payload is None:
                folder = self.directory / "evaluations" / self.name / (candidate_id[:12]+"-"+uuid.uuid4().hex[:8])
                folder.mkdir(parents=True)
                write_json(folder / "project.json", dict(self.project, cells=cells))
                payload = self.calculate(cells, folder)
                payload["directory"] = str(folder)
                payload["original_evaluation_s"] = time.perf_counter()-start
                self.cache.put(identity, payload)
                with self.lock:
                    self.memory[cache_key] = payload
            value = fitness(payload["rows"], self.settings["objective"])
            record = dict(candidate_id=candidate_id, cells=cells, backend=self.name, cached=hit,
                          fitness=value, evaluation_s=time.perf_counter()-start, context=context or {}, **payload)
            record["diagnostic_s"] = 0 if hit else payload.get("timings_s", {}).get("diagnostic_direct", 0)
            record["online_s"] = max(0, record["evaluation_s"]-record["diagnostic_s"])
            record["original_compute_s"] = payload.get("timings_s", {}).get("compute")
            record["compute_s"] = None if hit else record["original_compute_s"]
            record["started_s"] = start-self.epoch
            record["finished_s"] = record["started_s"]+record["evaluation_s"]
            with self.lock:
                self.stats["evaluations"] += 1
                self.stats["cache_hits"] += int(hit)
                self.records.append(record)
                if self.best is None or value > self.best["fitness"]:
                    self.best = record
            self.journal.append("evaluations.jsonl", record)
            self.journal.event("evaluation", backend=self.name, candidate_id=candidate_id, cached=hit,
                               fitness=value, evaluation_s=record["evaluation_s"], stats=dict(self.stats),
                               compute_s=record["compute_s"],
                               best_cells=self.best["cells"], best_fitness=self.best["fitness"],
                               mean_evaluation_s=sum(r["evaluation_s"] for r in self.records)/len(self.records),
                               mean_compute_s=sum(r["compute_s"] for r in self.records if r["compute_s"] is not None)/
                                    max(1, sum(r["compute_s"] is not None for r in self.records))
                                    if any(r["compute_s"] is not None for r in self.records) else None)
            return record
        except Exception as exc:
            if isinstance(exc, ExperimentCancelled):
                self.journal.append("interruptions.jsonl", dict(backend=self.name, candidate_id=candidate_id, context=context or {}))
                raise
            with self.lock:
                self.stats["failures"] += 1
            failure = dict(backend=self.name, candidate_id=candidate_id, cells=cells,
                           directory=str(folder) if folder else None, error=str(exc), context=context or {})
            self.journal.append("failures.jsonl", failure)
            self.journal.event("error", message=f"{self.name}: {exc}")
            raise

    def evaluate_many(self, cells, contexts=None):
        contexts = contexts or [{} for _ in cells]
        return [self.evaluate(mask, context=context) for mask, context in zip(cells, contexts)]

    @abstractmethod
    def calculate(self, cells, directory):
        pass
