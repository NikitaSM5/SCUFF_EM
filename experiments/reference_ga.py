"""Deterministic pixel adaptation of ThinWireMoM GA_app's GA + local-search loop."""
import copy
import math
from pathlib import Path
import random
import time

from .domain import random_antenna
from .metrics import distribution
from .reference_operators import offspring, selection, local_candidates, admissible
from .storage import file_hash, key, read_json, write_json
from .timing import compute_total

IMPLEMENTATION = "thinwire_pixel_v1"
SOURCE_FILES = ("GA_app.m", "GeneticAlgorithmOperators.m", "LocalSearchOperators.m", "Population.m",
                "WeightedPopulation.m", "GainCalculator.m", "AntennaGraph.m")
ADAPTATION = dict(
    source="GA_app.runOptimization + GeneticAlgorithmOperators + LocalSearchOperators",
    preserved="top-ranked all-pairs complementary crossover; offspring-only add/remove mutation; minimum-Hamming assignment; probabilistic crowding fc/(fc+fp); strict best-improvement local search (removal wins ties); LS/subpopulation GA/merge/full GA schedule",
    geometry="Wire edges replaced by four-neighbour metal cells, fixed feed cell. Shortest-path reconnection or feed-component pruning. Invalid point-contact masks/bounds revert explicitly for genetic operators and are excluded from local neighbourhoods.",
    initialization="Fixed-size connected pixel growth replaces wire space-colonization; generated once and shared by both backends.",
    scheduling="Fixed candidate-index LS order and first ceil(N*ls_ready_percent/100) candidates replace timing-dependent parfeval completion. No wall-clock-dependent GA decisions.",
    randomness="Pre-generated per-stage Python RNG seeds and crowding draws; deterministic per-pair and per-mutation substreams prevent geometry-dependent branching from shifting later operations. Same stream definition for both solvers, not bitwise MATLAB RNG/parfor equivalence.",
    assignment="SciPy linear_sum_assignment gives minimum total distance, like MATLAB matchpairs with prohibitive unmatched cost. Equal-cost assignment tie-breaking can differ.",
    selection="Crowding depends on actual nonnegative fitness ratios, not rankings alone. Equal rankings do not guarantee identical winners; positive scaling preserves probabilities.",
    fitness="reference_gain preserves Gmax*(1-((abs(Z)-Z0)/(abs(Z)+Z0))^2), using this project's planar Gain. This is NOT standard realised gain; complex S11 is retained separately. Multi-frequency extension takes the minimum.",
    iterations="generations is the number of outer GA_app iterations, plus initial population at generation 0",
)


def reference_metadata(path):
    path = Path(path)
    missing = [name for name in SOURCE_FILES if not (path/name).is_file()]
    if missing:
        raise ValueError("ThinWireMoM reference files missing: "+", ".join(missing))
    return dict(path=str(path.resolve()), files={name: file_hash(path/name) for name in SOURCE_FILES}, adaptation=ADAPTATION)


def definition(project, settings):
    rng = random.Random(settings["seed"])
    n, size = len(project["cells"]), settings["population_size"]
    early = max(1, min(size, math.ceil(size*settings["ls_ready_percent"]/100)))
    initial = [random_antenna(n, project["feed_cell"], settings["initial_elements"], rng.randrange(2**63), True)
               for _ in range(size)]

    def decisions():
        return dict(offspring_seed=rng.randrange(2**63), draws=[rng.random() for _ in range(size)])

    tape = [dict(early=[decisions() for _ in range(settings["subpopulation_ga_steps"])],
                 merge_draws=[rng.random() for _ in range(size)],
                 full=[decisions() for _ in range(settings["full_population_ga_steps"])])
            for _ in range(settings["generations"])]
    rate = math.ceil((1+math.sqrt(1+4*settings["child_fraction"]*size))/2)/size
    data = dict(schema_version=1, implementation=IMPLEMENTATION, n=n, feed_cell=list(project["feed_cell"]),
                initial_population=initial, decisions=tape, early_count=early, crossover_rate=rate,
                settings=copy.deepcopy(settings), reference=reference_metadata(settings["ga_reference"]))
    data["definition_id"] = key(data)
    return data


class Trajectory:
    def __init__(self, project, settings, experiment, backend, journal, cancel, progress, directory):
        self.project, self.settings, self.experiment = project, settings, experiment
        self.backend, self.journal, self.cancel, self.progress = backend, journal, cancel, progress
        self.directory = directory
        self.batches, self.generations, self.operations = [], [], []
        self.iteration = 0

    def evaluate(self, population, phase):
        self.cancel()
        contexts = [dict(generation=self.iteration, phase=phase, individual=i) for i in range(len(population))]
        records = self.backend.evaluate_many(population, contexts)
        self.batches.append(dict(population=copy.deepcopy(population), contexts=contexts))
        return records

    def step(self, population, decision, phase):
        children, notes = offspring([r["cells"] for r in population], [r["fitness"] for r in population],
                                    self.project, self.settings, decision["offspring_seed"], self.experiment["crossover_rate"])
        evaluated = self.evaluate(children, phase) if children else []
        survivors, crowding = selection(population, evaluated, decision["draws"])
        self.operations.append(dict(generation=self.iteration, phase=phase, offspring=notes, crowding=crowding))
        return survivors

    def local_search(self, record, index):
        for step in range(self.settings["local_search_depth"]):
            self.cancel()
            candidates, changes = local_candidates(record["cells"], self.project, self.settings)
            if not candidates:
                break
            evaluated = self.evaluate(candidates, f"ls-{index}-{step}")
            # Source: first best within each operation; removal wins add/removal ties.
            winner = max(range(len(evaluated)), key=lambda i: (evaluated[i]["fitness"], changes[i]["operation"] == "remove", -i))
            improved = evaluated[winner]["fitness"] > record["fitness"]
            self.operations.append(dict(generation=self.iteration, phase="local_search", individual=index,
                                        step=step, changes=changes, winner=winner, accepted=improved))
            if not improved:
                break
            record = evaluated[winner]
        return record

    def checkpoint(self, population, elapsed, online_s, compute_s):
        values = [r["fitness"] for r in population]
        stats = distribution(values)
        best = max(population, key=lambda r: r["fitness"])
        row = dict(generation=self.iteration, population=[r["cells"] for r in population], fitness=values,
                   best_fitness=stats["max"], mean_fitness=stats["mean"], median_fitness=stats["median"],
                   best_cells=best["cells"], candidate_ids=[r["candidate_id"] for r in population],
                   wall_s=elapsed, online_s=online_s, compute_s=compute_s, batch_end=len(self.batches))
        self.generations.append(row)
        trace = dict(schema_version=1, implementation=IMPLEMENTATION, n=len(self.project["cells"]),
                     feed_cell=list(self.project["feed_cell"]), definition=self.experiment,
                     generations=self.generations, batches=self.batches, operations=self.operations)
        write_json(self.directory/("ga-trace-"+self.backend.name+".json"), trace)
        self.journal.append("generations.jsonl", dict(backend=self.backend.name, **row))
        self.progress(dict(kind="ga", backend=self.backend.name, generation=self.iteration,
                           generations=self.settings["generations"], generation_s=elapsed, generation_compute_s=compute_s,
                           evaluated=self.iteration, total=self.settings["generations"],
                           best_fitness=stats["max"], mean_fitness=stats["mean"], median_fitness=stats["median"],
                           best_cells=best["cells"]))

    def run(self, replay=None):
        from .reporting import online_wall
        cursor, by_mask = 0, {}
        population = None
        for iteration in range(self.settings["generations"]+1):
            self.cancel()
            self.iteration = iteration
            start, offset = time.perf_counter(), len(self.backend.records)
            self.journal.event("stage", message=f"ThinWireMoM / {self.backend.name}: {iteration}/{self.settings['generations']}")
            if replay:
                gen = replay["generations"][iteration]
                for batch in replay["batches"][cursor:gen["batch_end"]]:
                    self.cancel()
                    records = self.backend.evaluate_many(batch["population"], batch["contexts"])
                    self.batches.append(copy.deepcopy(batch))
                    by_mask.update((r["candidate_id"], r) for r in records)
                cursor = gen["batch_end"]
                population = [by_mask[key(cells)] for cells in gen["population"]]
            elif iteration == 0:
                population = self.evaluate(self.experiment["initial_population"], "initial")
            else:
                tape = self.experiment["decisions"][iteration-1]
                early_count = self.experiment["early_count"]
                improved = [self.local_search(record, i) for i, record in enumerate(population[:early_count])]
                early = list(improved)
                for j, decision in enumerate(tape["early"]):
                    early = self.step(early, decision, f"early-ga-{j}")
                improved.extend(self.local_search(record, early_count+i) for i, record in enumerate(population[early_count:]))
                population, notes = selection(improved, early, tape["merge_draws"])
                self.operations.append(dict(generation=iteration, phase="merge", crowding=notes))
                for j, decision in enumerate(tape["full"]):
                    population = self.step(population, decision, f"full-ga-{j}")
            self.checkpoint(population, time.perf_counter()-start, online_wall(self.backend.records[offset:]),
                            compute_total(self.backend.records[offset:]))
        return read_json(self.directory/("ga-trace-"+self.backend.name+".json"))


def validate_replay(trace, project, settings):
    if trace.get("implementation") != IMPLEMENTATION or trace.get("n") != len(project["cells"]) or trace.get("feed_cell") != list(project["feed_cell"]):
        raise ValueError("ThinWireMoM replay algorithm/grid/feed mismatch")
    if len(trace["generations"]) != settings["generations"]+1:
        raise ValueError("ThinWireMoM replay iteration count mismatch")
    seen, cursor = set(), 0
    for generation in trace["generations"]:
        end = generation["batch_end"]
        if not isinstance(end, int) or not cursor <= end <= len(trace["batches"]):
            raise ValueError("Invalid replay batch boundary")
        for batch in trace["batches"][cursor:end]:
            if len(batch["population"]) != len(batch["contexts"]):
                raise ValueError("Replay contexts mismatch")
            for cells in batch["population"]:
                if len(cells) != len(project["cells"]) or not admissible(cells, project, settings):
                    raise ValueError("Invalid replay candidate")
                seen.add(key(cells))
        if len(generation["population"]) != settings["population_size"] or any(key(c) not in seen for c in generation["population"]):
            raise ValueError("Replay survivors were not evaluated")
        cursor = end
    if cursor != len(trace["batches"]):
        raise ValueError("Unconsumed replay batches")


def run_reference(project, settings, directory, schur, feko, journal, cancel, progress):
    from .ga import finish_ga
    experiment = definition(project, settings)
    supplied = read_json(settings["replay_file"]) if settings["replay_file"] else None
    if supplied:
        if settings["mode"] != "replay":
            raise ValueError("A saved trace requires replay mode")
        validate_replay(supplied, project, settings)
        experiment["initial_population"] = copy.deepcopy(supplied["generations"][0]["population"])
        experiment["replay_source_definition"] = supplied["definition"]
        experiment["definition_id"] = key({k: v for k, v in experiment.items() if k != "definition_id"})
    write_json(directory/"experiment-definition.json", experiment)
    write_json(directory/"initial-population.json", experiment["initial_population"])
    histories, traces, best = {}, {}, {}
    source = supplied
    for backend in (schur, feko):
        trajectory = Trajectory(project, settings, experiment, backend, journal, cancel, progress, directory)
        trace = trajectory.run(source if settings["mode"] == "replay" else None)
        histories[backend.name] = traces[backend.name] = trace["generations"]
        best[backend.name] = max(backend.records, key=lambda r: r["fitness"])
        if backend.name == "schur" and not supplied:
            source = trace
    return finish_ga(project, settings, directory, schur, feko, journal, histories, traces, best, experiment["definition_id"])
