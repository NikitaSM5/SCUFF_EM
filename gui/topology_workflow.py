"""Persistent full-domain preparation, Schur edits and optional FEKO validation."""
import hashlib
import json
from pathlib import Path
import time

from antenna import Substrate, create_planar_antenna, solve_antenna, save_matrix, save_result
from antenna.topology import active_dofs, antenna_result, load_state, load_system, save_state, update_solution
from feko.config import validate_options
from gui.calculation import result_row, row_compute_time, save_comparison, write_json
from gui.topology_config import cache_signature, read_cache_manifest


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def prepare_cache(config, directory, emit, *, cancel=None):
    directory = Path(directory).resolve()
    signature = cache_signature(config)
    n = signature["n"]
    emit("stage", message="Полная матрица: триангуляция всей сетки")
    geometry = create_planar_antenna(
        [[1]*n for _ in range(n)], config["cell_size_mm"], config["feed_cell"], directory / "full/geometry",
        substrate=Substrate(config["thickness_mm"], config["epsilon_r"], config["loss_tangent"]),
        mesh_size_mm=config["mesh_size_mm"])
    unknowns = geometry.unknowns
    emit("mesh", **geometry.metadata["mesh"])
    # Dense matrix copies, LAPACK work and active inverse coexist during edits.
    memory_mib = 128*unknowns*unknowns/1024**2
    if unknowns > config["max_unknowns"] or memory_mib > 4096:
        raise ValueError(f"Полная сетка: {unknowns} неизвестных, рабочая память до {memory_mib:.0f} МиБ. "
                         f"Лимит неизвестных: {config['max_unknowns']}. Увеличьте шаг триангуляции "
                         "или лимит неизвестных (предел рабочей памяти этого режима: 4096 МиБ).")
    systems = []
    for index, frequency in enumerate(signature["frequencies_ghz"]):
        emit("stage", message=f"Полная матрица: SCUFF-EM, {frequency:g} ГГц")
        folder = directory / "full" / f"frequency-{index:04d}"
        result = solve_antenna(geometry, frequency, work_dir=folder / "calculation",
                               max_unknowns=config["max_unknowns"], timeout_s=config["timeout_s"],
                               verify_reference=config["verify_reference"], cancel=cancel)
        save_matrix(result.M, folder / "M.npy")
        path = save_result(result, folder / "system.npz")
        systems.append(dict(file=path.relative_to(directory).as_posix(), sha256=digest(path)))
        emit("log", message=f"Полная матрица {unknowns} × {unknowns}: сборка {result.metadata['timings_s']['assembly']:.3f} с")
    manifest = directory / "topology-cache.json"
    project = {k: v for k, v in config.items() if k not in ("topology_cache", "topology_state")}
    write_json(manifest, dict(schema_version=1, kind="scuff_topology_cache", signature=signature,
                             project=project, systems=systems, unknowns=unknowns, memory_estimate_mib=memory_mib))
    return manifest


def load_previous(path, cache_hash, count):
    if not path:
        return None
    path = Path(path).resolve()
    data = json.loads(path.read_text(encoding="utf-8"))
    if (data.get("schema_version") != 1 or data.get("cache_sha256") != cache_hash or
            len(data.get("states", [])) != count):
        raise ValueError("Предыдущее решение Шура не соответствует полной матрице.")
    for item in data["states"]:
        file = (path.parent / item["file"]).resolve()
        if not file.is_relative_to(path.parent) or digest(file) != item["sha256"]:
            raise ValueError("Файл предыдущего решения Шура изменён или повреждён.")
        item["path"] = file
    return data["states"]


def run_topology(config, directory, emit, *, prepare=False, compare=False):
    directory = Path(directory).resolve()
    options = validate_options(config.get("feko", {}), config)
    if compare:
        from feko.runner import find_cadfeko
        if find_cadfeko(options["cadfeko_exe"]) is None:
            raise RuntimeError("CADFEKO не найден. Укажите путь в параметрах FEKO.")
    manifest = prepare_cache(config, directory, emit) if prepare else Path(config["topology_cache"]).resolve()
    cache = read_cache_manifest(manifest)
    if cache["signature"] != cache_signature(config):
        raise ValueError("Параметры изменены. Рассчитайте полную матрицу заново.")
    cache_hash = digest(manifest)
    previous = load_previous(None if prepare else config.get("topology_state"), cache_hash, len(cache["systems"]))
    config = dict(config, topology_cache=str(manifest), topology_state=str(directory / "topology-state.json"))
    rows, states = [], []
    for index, entry in enumerate(cache["systems"]):
        started = time.monotonic()
        emit("stage", message=f"Шур: загрузка матрицы ({index+1}/{len(cache['systems'])})")
        file = manifest.parent / entry["file"]
        if digest(file) != entry["sha256"]:
            raise ValueError("Полная матрица изменена или повреждена.")
        full = load_system(file)
        tick = time.perf_counter()
        target, _ = active_dofs(full, config["cells"])
        mapping_s = time.perf_counter()-tick
        old = load_state(previous[index]["path"], entry["sha256"]) if previous else None
        emit("stage", message=f"Шур: {len(target)} активных неизвестных")
        state = update_solution(full.M, full.b, target, old)
        folder = directory / "scuff" / f"frequency-{index:04d}"
        folder.mkdir(parents=True)
        tick = time.perf_counter()
        result = antenna_result(full, config["cells"], state, folder)
        result.metadata["timings_s"].update(mapping=mapping_s, compaction=time.perf_counter()-tick)
        result.metadata["topology_cache"] = str(manifest)
        result.metadata["topology_cache_sha256"] = cache_hash
        save_result(result, folder / "system.npz")
        state_path = folder / "state.npz"
        save_state(state, state_path, entry["sha256"])
        states.append(dict(file=state_path.relative_to(directory).as_posix(), sha256=digest(state_path)))
        update = state["update"]
        emit("log", message=f"{update['method']}: +{update['added_dofs']} / -{update['removed_dofs']} DOF, "
             f"{update['elapsed_s']:.4f} с; невязка {state['checks']['relative_residual']:.2e}")
        rows.append(dict(result_row(result, config, folder, emit, started), topology_update=update))
    write_json(directory / "topology-state.json", dict(schema_version=1, cache_sha256=cache_hash, states=states))
    write_json(directory / "project.json", config)
    write_json(directory / "scuff/port-results.json", rows)
    data = save_comparison(directory, config, rows, [])
    emit("topology", cache=str(manifest), state=config["topology_state"], unknowns=cache["unknowns"])
    emit("comparison", data=data)
    if compare:
        from feko.runner import run_feko
        completed = []

        def feko_event(event, **fields):
            if event == "done":
                completed.append(fields)
            else:
                emit(event, **fields)

        config["feko"] = dict(options, run_solver=True)
        run_feko(config, directory, feko_event)
        if not completed or completed[0]["status"] != "solved":
            raise RuntimeError("FEKO не завершил расчёт")
        feko = completed[0]["summary"]
        if len(rows) != len(feko) or any(abs(a["frequency_hz"]-b["frequency_hz"]) > max(1, a["frequency_hz"]*1e-7)
                                        for a, b in zip(rows, feko)):
            raise RuntimeError("Частоты SCUFF-EM и FEKO не совпадают")
        data = save_comparison(directory, config, rows, feko,
                               compute_timings_s=dict(scuff=row_compute_time(rows),
                                                      feko=completed[0].get("timings_s", {}).get("compute")))
    emit("done", engine="topology", status="solved", message="Полная матрица готова" if prepare else "Расчёт завершён",
         comparison=data, pattern_path=str(directory / "feko/farfield.json") if compare and options["compute_pattern"] else None,
         cache=str(manifest), state=config["topology_state"], directory=str(directory))
