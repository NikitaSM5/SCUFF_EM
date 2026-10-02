"""Versioned experiment definitions. Values are independent of GUI widget types."""
from pathlib import Path


class ExperimentCancelled(Exception):
    pass


def defaults(kind="accuracy"):
    return dict(schema_version=1, kind=kind, seed=42, objective="reference_gain" if kind == "ga" else "reflection", connected=kind == "ga",
                population_size=8, generations=3, mutation_probability=.1, crossover_probability=.8,
                tournament_size=3, elitism=1, min_elements=1, max_elements=0, mode="replay",
                base_elements=1, k_max=2, samples_per_k=100 if kind == "accuracy" else 10,
                accuracy_mode="current_additions", single_mode="sample" if kind == "accuracy" else "exhaustive", multi_mode="sample",
                exhaustive_threshold=500, allow_large_exhaustive=False, incremental=kind == "ga",
                refactor_interval=20, condition_limit=1e12, max_changed_fraction=.5,
                state_cache_size=8, max_parallel_feko_jobs=1, use_cache=True, reuse_cadfeko=True,
                diagnostic_direct=True, diagnostic_scuff=False, cache_dir="", replay_file="",
                ga_reference="D:/MIPT/ThinWireMoM/ThinWireMoM", ga_implementation="thinwire_pixel_v1",
                initial_elements=1, child_fraction=.1, offspring_mutation_rate=1.0,
                remove_connect_probability=1.0, remove_method_probability=.3, crossover_connect_probability=1.0,
                local_search_depth=1, ls_ready_percent=50, subpopulation_ga_steps=1, full_population_ga_steps=1)


def validate(settings, project):
    data = dict(defaults(settings.get("kind", "accuracy")), **settings)
    if data["kind"] == "accuracy" and settings and "accuracy_mode" not in settings:
        data["accuracy_mode"] = "random_base"
    current_additions = data["kind"] == "accuracy" and data["accuracy_mode"] == "current_additions"
    if current_additions:
        data.update(single_mode="sample", multi_mode="sample", incremental=False, objective="reflection",
                    max_changed_fraction=1.0, refactor_interval=20, diagnostic_direct=False, diagnostic_scuff=False,
                    allow_large_exhaustive=True)
    n = len(project["cells"])
    for key, low, high in (("seed", 0, 2**31-1), ("population_size", 2, 10000), ("generations", 1, 10000),
                           ("tournament_size", 1, 10000), ("elitism", 0, 9999), ("min_elements", 1, n*n),
                           ("max_elements", 0, n*n), ("base_elements", 1, n*n), ("k_max", 1, max(1, n*n-1)),
                           ("samples_per_k", 1, 10000000), ("exhaustive_threshold", 1, 10000000),
                           ("refactor_interval", 1, 100), ("state_cache_size", 1, 64), ("max_parallel_feko_jobs", 1, 16)):
        if (data["kind"] == "ga" and key in ("base_elements", "k_max", "samples_per_k", "exhaustive_threshold")) or (current_additions and key == "base_elements"):
            continue
        if type(data[key]) is not int or not low <= data[key] <= high:
            raise ValueError(f"Invalid {key}: {low} .. {high}")
    for key, low, high in (("mutation_probability", 0, 1), ("crossover_probability", 0, 1),
                           ("max_changed_fraction", .01, 1), ("condition_limit", 100, 1e15)):
        if type(data[key]) not in (int, float) or not low <= data[key] <= high:
            raise ValueError(f"Invalid {key}")
    if data["ga_implementation"] == "binary_tournament_v1" and (data["elitism"] >= data["population_size"] or data["tournament_size"] > data["population_size"]):
        raise ValueError("Elitism must be smaller than population; tournament must not exceed population")
    if data["min_elements"] > (data["max_elements"] or n*n):
        raise ValueError("Element count bounds are inconsistent")
    if data["kind"] == "accuracy" and data["accuracy_mode"] not in ("current_additions", "random_base"):
        raise ValueError("Unknown accuracy mode")
    if current_additions:
        from .domain import valid
        if not valid(project["cells"], project["feed_cell"], connected=data["connected"]):
            raise ValueError("Текущая антенна не соответствует условиям геометрии или связности.")
        if data["k_max"] > n*n-sum(map(sum, project["cells"])):
            raise ValueError("K превышает число пустых клеток текущей антенны.")
        if not project["feko"]["compute_pattern"]:
            raise ValueError("Для сравнения Gmax включите «Считать ДН и Gain».")
    elif data["kind"] == "accuracy" and data["base_elements"]+data["k_max"] > n*n:
        raise ValueError("Base elements + K exceeds the number of grid cells")
    if data["kind"] not in ("ga", "accuracy") or data["mode"] not in ("independent", "replay"):
        raise ValueError("Unknown experiment kind/mode")
    if data["objective"] not in ("reflection", "gain", "realized_gain", "reference_gain"):
        raise ValueError("Unknown objective")
    if any(data[key] not in ("exhaustive", "sample") for key in ("single_mode", "multi_mode")):
        raise ValueError("Unknown addition sampling mode")
    for key in ("connected", "incremental", "use_cache", "reuse_cadfeko", "diagnostic_direct", "diagnostic_scuff", "allow_large_exhaustive"):
        if type(data[key]) is not bool:
            raise ValueError(f"Invalid {key}")
    if data["objective"] != "reflection" and not project["feko"]["compute_pattern"]:
        raise ValueError("Для выбранной цели включите «Считать ДН и Gain».")
    if data["kind"] == "ga" and data["ga_implementation"] == "thinwire_pixel_v1":
        if data["objective"] == "reflection":
            raise ValueError("ThinWireMoM требует неотрицательную fitness: выберите Fitness ThinWireMoM, Gain или realised Gain.")
        if not data["connected"]:
            raise ValueError("ThinWireMoM сохраняет связность с портом; включите связность.")
        for name, low, high in (("initial_elements", data["min_elements"], data["max_elements"] or n*n),
                                ("local_search_depth", 0, 1000), ("ls_ready_percent", 1, 100),
                                ("subpopulation_ga_steps", 0, 1000), ("full_population_ga_steps", 0, 1000)):
            if type(data[name]) is not int or not low <= data[name] <= high:
                raise ValueError(f"Invalid {name}: {low} .. {high}")
        for name in ("child_fraction", "offspring_mutation_rate", "remove_connect_probability",
                     "remove_method_probability", "crossover_connect_probability"):
            if type(data[name]) not in (int, float) or not 0 <= data[name] <= 1:
                raise ValueError(f"Invalid {name}: 0 .. 1")
    if not data["cache_dir"]:
        data["cache_dir"] = str(Path(project["output_dir"]).resolve() / "benchmark-cache")
    return data
