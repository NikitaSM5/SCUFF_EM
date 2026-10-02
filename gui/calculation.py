"""Sequential solver workflow; no Qt objects or mutable editor state."""
import csv
import json
from pathlib import Path
import time

from antenna.parameters import from_impedance
from antenna.feed import MODEL, planar_feed
from feko.config import validate_options


def frequencies(config):
    options = validate_options(config.get("feko", {}))
    if not options["sweep_enabled"]:
        return [config["frequency_ghz"]]
    first, last, count = options["start_ghz"], options["stop_ghz"], options["frequency_points"]
    return [first+(last-first)*i/(count-1) for i in range(count)]


def write_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False)+"\n", encoding="utf-8")


def result_row(result, config, output, emit, started, *, persist_gain=True, gain_data=None):
    compute_started = time.perf_counter()
    from antenna.farfield import calculate_gain
    frequency = result.metadata["frequency_ghz"]
    options = validate_options(config.get("feko", {}))
    impedance = complex(*result.metadata["input_impedance_ohm"]).conjugate()
    row = from_impedance(impedance, frequency*1e9, options["reference_ohm"])
    if not row["passive"]:
        emit("log", message=f"SCUFF-EM, {frequency:g} ГГц: |S11| > 1; портовый результат не прошёл проверку пассивности.")
    gain = dict(gain_peak_dbi=None)
    if options["compute_pattern"]:
        emit("stage", message=f"SCUFF-EM: Gain, {frequency:g} ГГц")
        try:
            gain = calculate_gain(result, options["angle_step_deg"])
        except (ValueError, RuntimeError) as exc:
            gain = dict(gain_peak_dbi=None, gain_error=str(exc))
            emit("log", message=f"SCUFF-EM: Gain не определён: {exc}")
        if gain.get("radiation_efficiency_upper", 0) > 1.02:
            gain["gain_warning"] = "radiated_power_exceeds_accepted_power"
            emit("log", message="SCUFF-EM: излучённая мощность превышает принятую; проверьте пространственную и угловую сетки.")
        postprocess_s = time.perf_counter()-compute_started
        if gain_data is not None:
            gain_data.update(gain)
        if persist_gain:
            write_json(Path(output) / "gain.json", gain)
    else:
        postprocess_s = time.perf_counter()-compute_started
    native = result.metadata["timings_s"]
    compute_s = sum(native.get(key, 0) for key in ("assembly", "rhs", "lu", "solve", "mapping", "compaction"))+postprocess_s
    return dict(row, elapsed_s=time.monotonic()-started, unknowns=result.M.shape[0],
                compute_s=compute_s,
                **{k: v for k, v in gain.items() if k != "gain_linear_points"},
                checks=result.metadata["checks"])


def run_scuff(config, directory, emit, frequency_list=None):
    from antenna import Substrate, create_planar_antenna, solve_antenna, save_matrix, save_result
    directory = Path(directory)
    emit("stage", message="SCUFF-EM: геометрия и сетка")
    geometry = create_planar_antenna(
        config["cells"], config["cell_size_mm"], config["feed_cell"], directory / "geometry",
        substrate=Substrate(config["thickness_mm"], config["epsilon_r"], config["loss_tangent"]),
        mesh_size_mm=config["mesh_size_mm"])
    emit("mesh", **geometry.metadata["mesh"])
    values = frequency_list or [config["frequency_ghz"]]
    rows = []
    for index, frequency in enumerate(values):
        emit("stage", message=f"SCUFF-EM: {frequency:g} ГГц ({index+1}/{len(values)})")
        output = directory if len(values) == 1 else directory / f"frequency-{index:04d}"
        output.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        result = solve_antenna(geometry, frequency, work_dir=output / "calculation",
                               max_unknowns=config["max_unknowns"], timeout_s=config["timeout_s"],
                               verify_reference=config["verify_reference"])
        save_matrix(result.M, output / "M.npy")
        save_result(result, output / "system.npz")
        rows.append(result_row(result, config, output, emit, started))
        write_json(directory / "port-results.json", rows)
    return dict(unknowns=result.M.shape[0], checks=result.metadata["checks"],
                timings_s=result.metadata["timings_s"], directory=str(directory), summary=rows)


def row_compute_time(rows):
    values = [row.get("compute_s") for row in rows]
    return sum(values) if values and all(value is not None for value in values) else None


def save_comparison(directory, config, scuff, feko, *, compute_timings_s=None):
    data = dict(project=config, phasor_convention="exp(+j*w*t)", scuff=scuff, feko=feko,
                feed_models=dict(scuff=MODEL, feko=MODEL),
                feed=planar_feed(config["cells"], config["cell_size_mm"], config["feed_cell"]))
    if compute_timings_s is not None:
        data["compute_timings_s"] = compute_timings_s
    write_json(Path(directory) / "comparison.json", data)
    with (Path(directory) / "comparison.csv").open("w", newline="", encoding="utf-8") as stream:
        fields = ["solver", "frequency_hz", "reference_ohm", "s11_db", "s11_magnitude", "s11_phase_deg",
                  "resistance_ohm", "reactance_ohm", "vswr", "reflected_power_percent", "gain_peak_dbi", "passive",
                  "run_compute_s"]
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for solver, rows in (("SCUFF-EM", scuff), ("FEKO", feko)):
            timing_key = "scuff" if solver == "SCUFF-EM" else "feko"
            writer.writerows(dict(row, solver=solver, run_compute_s=(compute_timings_s or {}).get(timing_key))
                             for row in rows)
    return data


def run_comparison(config, directory, emit):
    from feko.runner import find_cadfeko, run_feko
    options = validate_options(config.get("feko", {}), config)
    if find_cadfeko(options["cadfeko_exe"]) is None:
        raise RuntimeError("CADFEKO не найден. Укажите путь в параметрах FEKO.")
    config = dict(config, feko=dict(options, run_solver=True))
    directory = Path(directory)
    scuff = run_scuff(config, directory / "scuff", emit, frequencies(config))["summary"]
    data = save_comparison(directory, config, scuff, [])
    emit("comparison", data=data)
    completed = []

    def feko_event(event, **fields):
        if event == "done":
            completed.append(fields)
        else:
            emit(event, **fields)

    run_feko(config, directory, feko_event)
    if not completed or completed[0]["status"] != "solved":
        raise RuntimeError("FEKO не завершил расчёт")
    feko = completed[0]["summary"]
    if len(scuff) != len(feko) or any(abs(a["frequency_hz"]-b["frequency_hz"]) > max(1, a["frequency_hz"]*1e-7)
                                     for a, b in zip(scuff, feko)):
        raise RuntimeError("Частоты SCUFF-EM и FEKO не совпадают")
    data = save_comparison(directory, config, scuff, feko,
                           compute_timings_s=dict(scuff=row_compute_time(scuff),
                                                  feko=completed[0].get("timings_s", {}).get("compute")))
    emit("done", engine="compare", status="solved", message="Расчёт завершён", directory=str(directory),
         comparison=data, pattern_path=str(directory / "feko/farfield.json") if options["compute_pattern"] else None)
