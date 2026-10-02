"""Exhaustive/sampled additions and a separately labelled incremental chain."""
from pathlib import Path
import time

from .domain import random_antenna, spaces, additions, incremental
from .metrics import compare_rows, discrepancy
from .scuff_backend import full_recomputation
from .storage import key, write_json, write_csv
from .timing import compute_speedup


def run_accuracy(project, settings, directory, schur, feko, journal, cancel, progress):
    if settings.get("accuracy_mode") == "current_additions":
        from .addition_benchmark import run_current_additions
        return run_current_additions(project, settings, directory, schur, feko, journal, cancel, progress)
    directory = Path(directory)
    base = random_antenna(len(project["cells"]), project["feed_cell"], settings["base_elements"],
                          settings["seed"], settings["connected"])
    definition = dict(base=base, seed=settings["seed"], spaces=spaces(base, settings["k_max"], settings["samples_per_k"],
                      settings["single_mode"], settings["multi_mode"]),
                      invalid_policy="record all sampled/enumerated masks; exclude invalid physical geometries explicitly")
    write_json(directory / "addition-definition.json", definition)
    raw_total = sum(item["planned"] for item in definition["spaces"])
    if raw_total > settings["exhaustive_threshold"] and not settings["allow_large_exhaustive"]:
        raise ValueError(f"Experiment has {raw_total} planned combinations; confirm the threshold override or choose sample mode")
    pairs, excluded = [], []
    total = 1+raw_total+(settings["k_max"] if settings["incremental"] else 0)
    counter = 0

    def evaluate(cells, k, added, group, schur_record=None, feko_record=None):
        nonlocal counter
        cancel()
        context = dict(k=k, added=added, group=group, case=counter)
        journal.event("stage", message=f"{group}: +{k}, case {counter+1}/{total}, Schur")
        a = schur_record or schur.evaluate(cells, force=group in ("base", "incremental"), context=context)
        journal.event("stage", message=f"{group}: +{k}, case {counter+1}/{total}, FEKO")
        b = feko_record or feko.evaluate(cells, force=group == "incremental", context=context)
        pair = dict(context, candidate_id=key(cells), cells=cells, schur=a, feko=b,
                    errors=compare_rows(a["rows"], b["rows"]), fitness_error=discrepancy(a["fitness"], b["fitness"], 1e-6),
                    speedup=compute_speedup(a, b), online_speedup=b["online_s"]/max(a["online_s"], 1e-9))
        if k == 0 or settings["diagnostic_scuff"]:
            journal.event("stage", message=f"{group}: +{k}, native SCUFF reassembly")
            native = full_recomputation(schur, cells, directory / "scuff-diagnostics" / f"case-{counter:06d}")
            pair["scuff_full"] = native
            pair["schur_vs_scuff"] = compare_rows(a["rows"], native["rows"])
            pair["scuff_vs_feko"] = compare_rows(native["rows"], b["rows"])
        pairs.append(pair)
        journal.append("pairs.jsonl", pair)
        counter += 1
        write_json(directory / "pairs.json", pairs)
        progress(dict(kind="accuracy", evaluated=counter, total=total, group=group, k=k,
                      best_cells=cells, best_fitness=a["fitness"], mean_fitness=None, median_fitness=None,
                      schur_stats=dict(schur.stats), feko_stats=dict(feko.stats)))
        return pair

    evaluate(base, 0, [], "base")
    base_id = key(base)
    # Keep base available as a parent; no independent case may chain off another addition case.
    base_state = schur.states.get(base_id)
    pending = []

    def flush():
        if not pending:
            return
        if settings["max_parallel_feko_jobs"] == 1:
            case = pending[0]
            if base_state:
                schur.states[base_id] = base_state
            schur.preferred_parent = base_id
            evaluate(case["cells"], case["k"], case["added"], "independent")
        else:
            contexts, quick = [], []
            for offset, case in enumerate(pending):
                if base_state:
                    schur.states[base_id] = base_state
                schur.preferred_parent = base_id
                context = dict(k=case["k"], added=case["added"], group="independent", case=counter+offset)
                contexts.append(context)
                quick.append(schur.evaluate(case["cells"], context=context))
            reference = feko.evaluate_many([case["cells"] for case in pending], contexts)
            for case, a, b in zip(pending, quick, reference):
                evaluate(case["cells"], case["k"], case["added"], "independent", a, b)
        pending.clear()

    for case in additions(base, project["feed_cell"], settings["k_max"], settings["samples_per_k"],
                          settings["single_mode"], settings["multi_mode"], settings["seed"], settings["connected"]):
        cancel()
        if not case["valid"]:
            excluded.append(case)
            journal.append("excluded.jsonl", dict(case, reason="invalid contact/connectivity"))
            counter += 1
            journal.event("log", message=f"Excluded invalid combination +{case['k']}: {case['added']}")
            continue
        pending.append(case)
        if len(pending) >= settings["max_parallel_feko_jobs"]:
            flush()
    flush()
    if settings["incremental"]:
        if base_state:
            schur.states[base_id] = base_state
        previous = base_id
        chain = list(incremental(base, project["feed_cell"], settings["k_max"], settings["seed"], settings["connected"]))
        if len(chain) < settings["k_max"]:
            journal.event("log", message=f"Incremental chain ended at {len(chain)}: no admissible next cell")
        for case in chain:
            schur.preferred_parent = previous
            # An incremental test must actually exercise updates, not return a cached independent case.
            old_cache = schur.settings["use_cache"]
            try:
                schur.settings["use_cache"] = False
                evaluate(case["cells"], case["k"], case["added"], "incremental")
            finally:
                schur.settings["use_cache"] = old_cache
            previous = key(case["cells"])
    schur.preferred_parent = None
    write_json(directory / "excluded.json", excluded)
    write_csv(directory / "cases.csv", [dict(candidate_id=p["candidate_id"], group=p["group"], k=p["k"],
               added=p["added"], schur_s=p["schur"]["compute_s"], feko_s=p["feko"]["compute_s"],
               schur_online_s=p["schur"]["online_s"], feko_online_s=p["feko"]["online_s"],
               schur_cached=p["schur"]["cached"], feko_cached=p["feko"]["cached"], speedup=p["speedup"],
               fitness_schur=p["schur"]["fitness"], fitness_feko=p["feko"]["fitness"], errors=p["errors"]) for p in pairs])
    return dict(kind="accuracy", pairs=pairs, excluded_count=len(excluded), definition=definition)
