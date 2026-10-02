"""Explicit opt-in binary GA; no GA decision code was present in the supplied MATLAB folder.

    Tournament selection, uniform crossover, per-cell mutation, elitism. Invalid
    offspring revert to parent A and the rejection is recorded, without new RNG draws.
"""
import copy
import random
import time

from .domain import random_antenna, valid
from .metrics import distribution, compare_rows, discrepancy
from .storage import key, read_json, write_json
from .timing import compute_total

IMPLEMENTATION = "binary_tournament_v1"


def definition(project, settings):
    if settings["ga_implementation"] != IMPLEMENTATION:
        raise ValueError("В D:\\MIPT\\GA нет selection/crossover/mutation. Укажите референс или явно разрешите новый бинарный GA в настройках вкладки.")
    rng = random.Random(settings["seed"])
    n, count = len(project["cells"]), settings["population_size"]
    initial = [random_antenna(n, project["feed_cell"], rng.randint(settings["min_elements"], settings["max_elements"] or n*n),
                              rng.randrange(2**63), settings["connected"]) for _ in range(count)]
    decisions = []
    for generation in range(settings["generations"]-1):
        children = []
        for _ in range(count-settings["elitism"]):
            children.append(dict(tournament_a=rng.sample(range(count), settings["tournament_size"]),
                                 tournament_b=rng.sample(range(count), settings["tournament_size"]),
                                 crossover_draw=rng.random(), crossover_mask=[rng.random() < .5 for _ in range(n*n)],
                                 mutation_draws=[rng.random() for _ in range(n*n)]))
        decisions.append(children)
    result = dict(schema_version=1, implementation=IMPLEMENTATION, n=n, feed_cell=list(project["feed_cell"]),
                  initial_population=initial, decisions=decisions, settings=copy.deepcopy(settings),
                  semantics=dict(fitness="maximise; worst-frequency objective", generations="includes initial generation 0",
                                 crossover="uniform per cell", mutation="per cell, fixed feed excluded",
                                 invalid_offspring="revert to parent A, recorded; no extra random decisions"))
    result["definition_id"] = key(result)
    return result


def breed(population, fitness, decisions, project, settings):
    rank = sorted(range(len(population)), key=lambda i: (-fitness[i], i))
    children = [copy.deepcopy(population[i]) for i in rank[:settings["elitism"]]]
    trace = [dict(operation="elite", parent=i) for i in rank[:settings["elitism"]]]
    n = len(population[0])
    feed = tuple(project["feed_cell"])
    for decision in decisions:
        a = min(decision["tournament_a"], key=lambda i: (-fitness[i], i))
        b = min(decision["tournament_b"], key=lambda i: (-fitness[i], i))
        crossover = decision["crossover_draw"] < settings["crossover_probability"]
        child = copy.deepcopy(population[a])
        changed = []
        for r in range(n):
            for c in range(n):
                offset = r*n+c
                if crossover and decision["crossover_mask"][offset]:
                    child[r][c] = population[b][r][c]
                if (r, c) != feed and decision["mutation_draws"][offset] < settings["mutation_probability"]:
                    child[r][c] = 1-child[r][c]
                    changed.append([r, c])
        child[feed[0]][feed[1]] = 1
        admissible = valid(child, feed, connected=settings["connected"], min_elements=settings["min_elements"],
                           max_elements=settings["max_elements"] or n*n)
        if not admissible:
            child = copy.deepcopy(population[a])
        children.append(child)
        trace.append(dict(operation="offspring", parents=[a, b], crossover=crossover, mutations=changed,
                          rejected=not admissible, rejection_reason=None if admissible else "invalid physical mask or element bounds"))
    return children, trace


def run_ga(project, settings, directory, schur, feko, journal, cancel, progress):
    if settings["ga_implementation"] == "thinwire_pixel_v1":
        from .reference_ga import run_reference
        return run_reference(project, settings, directory, schur, feko, journal, cancel, progress)
    experiment = definition(project, settings)
    write_json(directory / "experiment-definition.json", experiment)
    write_json(directory / "initial-population.json", experiment["initial_population"])
    supplied = read_json(settings["replay_file"]) if settings["replay_file"] else None
    if supplied:
        if settings["mode"] != "replay":
            raise ValueError("A trace file requires Locked population replay mode")
        if supplied.get("n") != len(project["cells"]) or supplied.get("feed_cell") != list(project["feed_cell"]):
            raise ValueError("Replay trace grid/feed does not match this experiment")
        if len(supplied["generations"]) != settings["generations"]:
            raise ValueError("Replay generation count differs; no silent truncation is allowed")
        for generation in supplied["generations"]:
            if len(generation["population"]) != settings["population_size"] or any(not valid(c, project["feed_cell"], connected=settings["connected"],
                        min_elements=settings["min_elements"], max_elements=settings["max_elements"] or len(c)**2) for c in generation["population"]):
                raise ValueError("Invalid population in replay trace")
        experiment["initial_population"] = supplied["generations"][0]["population"]
        experiment["replay_source"] = settings["replay_file"]
        experiment["definition_id"] = key({k: v for k, v in experiment.items() if k != "definition_id"})
        write_json(directory / "experiment-definition.json", experiment)
        write_json(directory / "initial-population.json", experiment["initial_population"])
    histories, traces, best = {}, {}, {}
    for backend in (schur, feko):
        population = copy.deepcopy(experiment["initial_population"])
        generations, trace = [], []
        for generation in range(settings["generations"]):
            cancel()
            if settings["mode"] == "replay" and (backend.name == "feko" or supplied):
                source = supplied["generations"] if supplied else traces["schur"]
                population = copy.deepcopy(source[generation]["population"])
            started = time.perf_counter()
            journal.event("stage", message=f"GA {settings['mode']} / {backend.name}: generation {generation+1}/{settings['generations']}")
            records = backend.evaluate_many(population, [dict(generation=generation, individual=i) for i in range(len(population))])
            values = [r["fitness"] for r in records]
            winner = max(range(len(values)), key=lambda i: (values[i], -i))
            if backend.name not in best or values[winner] > best[backend.name]["fitness"]:
                best[backend.name] = records[winner]
            stats = distribution(values)
            summary = dict(generation=generation, best_fitness=stats["max"], mean_fitness=stats["mean"], median_fitness=stats["median"],
                           wall_s=time.perf_counter()-started, online_s=sum(r["online_s"] for r in records), compute_s=compute_total(records),
                           best_cells=records[winner]["cells"], candidate_ids=[r["candidate_id"] for r in records],
                           parallelism=settings["max_parallel_feko_jobs"] if backend.name == "feko" else 1)
            # Generation wall time, not summed worker time, is used for parallel runtime charts.
            if backend.name == "feko" and settings["max_parallel_feko_jobs"] > 1:
                summary["online_s"] = summary["wall_s"]
            generations.append(summary)
            item = dict(generation=generation, population=copy.deepcopy(population), fitness=values, operations=[])
            if generation < settings["generations"]-1 and not (settings["mode"] == "replay" and (backend.name == "feko" or supplied)):
                population, item["operations"] = breed(population, values, experiment["decisions"][generation], project, settings)
            trace.append(item)
            write_json(directory / ("ga-trace-"+backend.name+".json"), dict(n=len(project["cells"]), feed_cell=list(project["feed_cell"]),
                       definition_id=experiment["definition_id"], generations=trace))
            journal.append("generations.jsonl", dict(backend=backend.name, **summary))
            progress(dict(kind="ga", backend=backend.name, generation=generation+1, generations=settings["generations"],
                          evaluated=len(backend.records), total=settings["population_size"]*settings["generations"],
                          best_fitness=summary["best_fitness"], mean_fitness=summary["mean_fitness"], median_fitness=summary["median_fitness"],
                          best_cells=best[backend.name]["cells"], generation_s=summary["wall_s"],
                          generation_compute_s=summary["compute_s"],
                          schur_stats=dict(schur.stats), feko_stats=dict(feko.stats)))
        histories[backend.name], traces[backend.name] = generations, trace
    return finish_ga(project, settings, directory, schur, feko, journal, histories, traces, best, experiment["definition_id"])


def finish_ga(project, settings, directory, schur, feko, journal, histories, traces, best, definition_id):
    # Pair only identical geometries. Independent trajectories are never paired by index.
    def measured_by_mask(records):
        result = {}
        for record in records:
            ident = record["candidate_id"]
            if ident not in result or (result[ident].get("cached") and not record.get("cached")):
                result[ident] = record
        return result
    feko_by_mask = measured_by_mask(feko.records)
    schur_by_mask = measured_by_mask(schur.records)
    pairs = []
    for ident in sorted(schur_by_mask.keys() & feko_by_mask.keys()):
        a, b = schur_by_mask[ident], feko_by_mask[ident]
        pairs.append(dict(candidate_id=ident, cells=a["cells"], schur=a, feko=b,
                          errors=compare_rows(a["rows"], b["rows"]), fitness_error=discrepancy(a["fitness"], b["fitness"], 1e-6)))
    divergence = next((i for i, (a, b) in enumerate(zip(traces["schur"], traces["feko"])) if a["population"] != b["population"]), None)
    journal.event("stage", message="Independent FEKO validation of final best Schur candidate")
    validated = feko.evaluate(best["schur"]["cells"], force=True, context=dict(role="final_validation"))
    if settings["diagnostic_scuff"]:
        from .scuff_backend import full_recomputation
        write_json(directory / "best-scuff-full.json", full_recomputation(schur, best["schur"]["cells"], directory / "best-scuff-full"))
    write_json(directory / "best.json", dict(best=best, validated_best=validated))
    write_json(directory / "pairs.json", pairs)
    return dict(kind="ga", generations=histories, pairs=pairs, best=best, validated_best=validated,
                divergence_generation=divergence, definition_id=definition_id)
