"""Background experiment orchestration with incremental artifacts and partial summaries."""
import cProfile
import io
from pathlib import Path
import pstats
import time

from .config import validate, ExperimentCancelled
from .storage import Journal, read_json, write_json, write_csv
from .preprocessing import prepare
from .schur_backend import SchurFitnessBackend
from .feko_backend import FekoFitnessBackend
from .reporting import summary, plots, live_plot


def run_experiment(project, settings, directory, emit):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    settings = validate(settings, project)
    write_json(directory / "config.json", dict(schema_version=1, project=project, experiment=settings))
    journal = Journal(directory, emit)
    started = time.perf_counter()
    backends, setup, prep = [], {}, dict(elapsed_s=0, cache_hit=False)
    outcome = dict(kind=settings["kind"], pairs=[])
    status, error = "failed", None
    profile = cProfile.Profile()
    live_events = []
    visualization_s = 0.0
    current_additions = settings["kind"] == "accuracy" and settings["accuracy_mode"] == "current_additions"

    def cancel():
        if (directory / "cancel.flag").exists():
            raise ExperimentCancelled("Experiment cancelled; completed results retained")

    def progress(data):
        nonlocal visualization_s
        data["elapsed_s"] = time.perf_counter()-started
        journal.event("experiment_progress", **data)
        live_events.append(dict(data))
        if current_additions:
            return
        tick = time.perf_counter()
        path = live_plot(directory, live_events)
        visualization_s += time.perf_counter()-tick
        journal.event("live_plot", path=str(path))

    try:
        profile.enable()
        if settings["kind"] == "ga":
            from .ga import IMPLEMENTATION
            from .reference_ga import IMPLEMENTATION as REFERENCE, reference_metadata
            if settings["ga_implementation"] == REFERENCE:
                write_json(directory / "ga-reference.json", reference_metadata(settings["ga_reference"]))
            elif settings["ga_implementation"] != IMPLEMENTATION:
                raise ValueError("Select ThinWireMoM GA + LS or the separate binary GA implementation.")
        cancel()
        if current_additions:
            from .addition_benchmark import prepare_addition_cases
            tick = time.perf_counter()
            prepare_addition_cases(project, settings, directory, journal, cancel)
            setup["case_generation_s"] = time.perf_counter()-tick
        reference = Path(settings["ga_reference"])
        if reference.is_dir() and settings["ga_implementation"] != "thinwire_pixel_v1":
            from .storage import file_hash
            write_json(directory / "ga-reference.json", dict(path=str(reference),
                       files={p.name: file_hash(p) for p in reference.glob("*.m")},
                       adaptation="binary_tournament_v1 is the separately selected tournament implementation, not the ThinWireMoM reference adaptation."))
        tick = time.perf_counter()
        feko = FekoFitnessBackend(project, settings, directory, journal, cancel)
        setup["feko"] = time.perf_counter()-tick
        backends.append(feko)
        manifest, prep = prepare(project, settings, directory, journal, cancel)
        schur = SchurFitnessBackend(project, settings, directory, journal, cancel, manifest)
        setup["schur"] = schur.setup_s
        backends.append(schur)
        write_json(directory / "preprocessing.json", dict(prep, manifest=str(manifest), backend_setup_s=setup))
        if settings["kind"] == "accuracy":
            from .accuracy import run_accuracy
            outcome = run_accuracy(project, settings, directory, schur, feko, journal, cancel, progress)
        else:
            from .ga import run_ga
            outcome = run_ga(project, settings, directory, schur, feko, journal, cancel, progress)
        cancel()
        status = "completed"
    except ExperimentCancelled as exc:
        status, error = "cancelled", str(exc)
        journal.event("log", message=error)
    except Exception as exc:
        error = str(exc)
        journal.event("error", message=error)
    finally:
        cleanup_started = time.perf_counter()
        for backend in backends:
            if hasattr(backend, "close"):
                try:
                    backend.close()
                except Exception as exc:
                    journal.event("error", message=str(exc))
                    if status == "completed":
                        status, error = "failed", str(exc)
        cleanup_s = time.perf_counter()-cleanup_started
        profile.disable()
        profile.dump_stats(str(directory / "profile.pstats"))
        text = io.StringIO()
        pstats.Stats(profile, stream=text).strip_dirs().sort_stats("cumulative").print_stats(60)
        (directory / "profile.txt").write_text(text.getvalue(), encoding="utf-8")
        if not outcome.get("pairs") and (directory / "pairs.jsonl").is_file():
            import json
            outcome["pairs"] = [json.loads(line) for line in (directory / "pairs.jsonl").read_text().splitlines()]
        if current_additions and (directory / "addition-definition.json").is_file():
            from .addition_benchmark import save_addition_tables
            definition = read_json(directory / "addition-definition.json")
            save_addition_tables(directory, outcome.get("pairs", []), definition["spaces"], definition["frequencies_hz"])
        report = summary(backends, prep, setup, outcome, time.perf_counter()-started)
        report.update(status=status, error=error)
        report["live_visualization_s"] = visualization_s
        report["session_cleanup_s"] = cleanup_s
        records = [r for backend in backends for r in backend.records]
        write_json(directory / "summary.json", report)
        write_json(directory / "outcome.json", outcome)
        write_csv(directory / "evaluations.csv", [dict(candidate_id=r["candidate_id"], backend=r["backend"], cached=r["cached"],
                      fitness=r["fitness"], compute_s=r["compute_s"], original_compute_s=r["original_compute_s"],
                      online_s=r["online_s"], diagnostic_s=r["diagnostic_s"],
                      cells=r["cells"], rows=r["rows"], diagnostics=r["diagnostics"], context=r["context"]) for r in records])
        write_csv(directory / "summary.csv", [dict(solver=name, **values) for name, values in report["solvers"].items()])
        try:
            figures = plots(directory, outcome, report, records)
        except Exception as exc:
            journal.event("error", message=f"Plot export failed: {exc}")
            figures = []
            report["plot_error"] = str(exc)
            if status == "completed":
                status, report["status"] = "failed", "failed"
                error = report["error"] = f"Plot export failed: {exc}"
            write_json(directory / "summary.json", report)
        write_json(directory / "status.json", dict(status=status, error=error))
        journal.event("experiment_done", status=status, directory=str(directory), summary=report, plots=figures)
        emit("done", engine="experiment", status=status, directory=str(directory), summary=report,
             message={"completed": "Эксперимент завершён", "cancelled": "Эксперимент остановлен", "failed": "Ошибка эксперимента"}[status])
    return report
