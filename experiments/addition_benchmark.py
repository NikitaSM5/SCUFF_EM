"""Independent current-antenna additions, genuine solver timing and flat CSV tables."""
from concurrent.futures import ThreadPoolExecutor
import copy
from pathlib import Path

from .domain import sample_valid_additions
from .metrics import compare_rows, discrepancy, distribution
from .storage import key, read_json, write_csv, write_json
from .timing import compute_speedup


CASE_FIELDS = ["added_elements", "scuff_time_s", "feko_time_s", "s11_scuff_db", "s11_feko_db",
               "gmax_scuff_dbi", "gmax_feko_dbi", "case", "frequency_hz", "s11_error_abs",
               "s11_error_relative", "s11_error_db", "gmax_error_db", "gmax_error_relative",
               "schur_method", "fallback_reason", "candidate_id", "scuff_gain_warning", "scuff_gain_error"]
MEAN_FIELDS = ["added_elements", "scuff_time_mean_s", "feko_time_mean_s", "s11_scuff_mean_db",
               "s11_feko_mean_db", "gmax_scuff_mean_dbi", "gmax_feko_mean_dbi",
               "s11_error_db_mean", "gmax_error_db_mean", "cases", "frequency_hz"]


def comparison_rows(pair):
    diagnostics = pair["schur"].get("diagnostics", [])
    rows = []
    for index, (a, b, error) in enumerate(zip(pair["schur"]["rows"], pair["feko"]["rows"], pair["errors"])):
        diag = diagnostics[index] if index < len(diagnostics) else {}
        adb, bdb = a.get("s11_db"), b.get("s11_db")
        rows.append(dict(added_elements=pair["k"], scuff_time_s=pair["schur"].get("compute_s"),
                         feko_time_s=pair["feko"].get("compute_s"), s11_scuff_db=adb, s11_feko_db=bdb,
                         gmax_scuff_dbi=a.get("gain_peak_dbi"), gmax_feko_dbi=b.get("gain_peak_dbi"),
                         case=pair["case"], frequency_hz=a["frequency_hz"],
                         s11_error_abs=error["s11_complex"]["absolute"],
                         s11_error_relative=error["s11_complex"]["relative"],
                         s11_error_db=abs(adb-bdb) if adb is not None and bdb is not None else None,
                         gmax_error_db=error.get("gain_db_absolute"),
                         gmax_error_relative=error.get("gain_linear", {}).get("relative"),
                         schur_method=diag.get("method"), fallback_reason=diag.get("reason"),
                         candidate_id=pair["candidate_id"], scuff_gain_warning=a.get("gain_warning"),
                         scuff_gain_error=a.get("gain_error")))
    return rows


def save_addition_tables(directory, pairs, plans, frequencies):
    rows = [row for pair in pairs for row in comparison_rows(pair)]
    means = []
    for plan in plans:
        for frequency in frequencies:
            group = [row for row in rows if row["added_elements"] == plan["k"] and row["frequency_hz"] == frequency]
            def mean(field):
                return distribution([row.get(field) for row in group])["mean"]
            measured = [row for row in group if row["scuff_time_s"] is not None and row["feko_time_s"] is not None]
            means.append(dict(added_elements=plan["k"], scuff_time_mean_s=mean("scuff_time_s"),
                              feko_time_mean_s=mean("feko_time_s"), s11_scuff_mean_db=mean("s11_scuff_db"),
                              s11_feko_mean_db=mean("s11_feko_db"), gmax_scuff_mean_dbi=mean("gmax_scuff_dbi"),
                              gmax_feko_mean_dbi=mean("gmax_feko_dbi"), frequency_hz=frequency,
                              cases=len(group), requested_cases=plan["requested"], timed_cases=len(measured),
                              s11_valid_cases=sum(row["s11_error_db"] is not None for row in group),
                              gmax_valid_cases=sum(row["gmax_error_db"] is not None for row in group),
                              s11_error_abs_mean=mean("s11_error_abs"),
                              s11_error_relative_mean=mean("s11_error_relative"), s11_error_db_mean=mean("s11_error_db"),
                              gmax_error_db_mean=mean("gmax_error_db"),
                              gmax_error_relative_mean=mean("gmax_error_relative"),
                              schur_only_cases=sum(row["schur_method"] == "schur" for row in group),
                              lu_fallback_cases=sum(row["schur_method"] == "lu_fallback" for row in group)))
    directory = Path(directory)
    write_csv(directory / "addition-cases.csv", rows, fields=CASE_FIELDS)
    write_csv(directory / "addition-summary.csv", [{field: row[field] for field in MEAN_FIELDS} for row in means], fields=MEAN_FIELDS)
    return means


def prepare_addition_cases(project, settings, directory, journal, cancel):
    from gui.calculation import frequencies
    directory = Path(directory)
    base = copy.deepcopy(project["cells"])
    cases, plans = [], []
    definition = dict(mode="current_additions", base=base, seed=settings["seed"], spaces=plans, cases=cases,
                      connected=settings["connected"], k_max=settings["k_max"], samples_per_k=settings["samples_per_k"],
                      sampling_policy="Unique valid masks, exact requested count per K or fail before numerical setup. Small spaces are shuffled exhaustively; larger spaces use randomized constructive growth with exact constraint completion, not a uniform random sample.",
                      frequencies_hz=[f*1e9 for f in frequencies(project)],
                      timing_policy="Fresh solves only; each Schur variant starts from the same base factorization. Full-domain SCUFF assembly and base LU/Gain are setup, not update time. Times include impedance/Gain. FEKO runtime is read from .out. For a frequency sweep, time columns describe the entire sweep; physical results and errors are per frequency.",
                      error_policy="S11 complex absolute/relative and absolute dB difference; Gmax absolute dB and relative linear-Gain error. Summary errors are means of per-case errors, not differences of mean values.")
    for k in range(1, settings["k_max"]+1):
        cancel()
        journal.event("stage", message=f"Подготовка вариантов +{k}")
        selected, plan = sample_valid_additions(base, project["feed_cell"], k, settings["samples_per_k"],
                                               settings["seed"], settings["connected"], cancel=cancel)
        cases.extend(selected)
        plans.append(plan)
        write_json(directory / "addition-definition.json", definition)
        if plan["planned"] != plan["requested"]:
            raise ValueError(f"+{k}: требуется {plan['requested']} разных вариантов, но допустимых всего {plan['planned']}. "
                             "Уменьшите число вариантов или измените базовую антенну/условие связности. Расчёты не запущены.")
        journal.event("log", message=f"+{k}: подготовлено {plan['planned']}/{plan['requested']} разных допустимых вариантов")
    return definition


def run_current_additions(project, settings, directory, schur, feko, journal, cancel, progress):
    from gui.calculation import frequencies
    directory = Path(directory)
    file = directory / "addition-definition.json"
    definition = read_json(file) if file.is_file() else prepare_addition_cases(project, settings, directory, journal, cancel)
    if (definition["base"] != project["cells"] or definition["seed"] != settings["seed"] or
            definition.get("connected") != settings["connected"] or definition.get("k_max") != settings["k_max"] or
            definition.get("samples_per_k") != settings["samples_per_k"] or
            [plan["k"] for plan in definition["spaces"]] != list(range(1, settings["k_max"]+1)) or
            len(definition["cases"]) != settings["k_max"]*settings["samples_per_k"] or
            definition["frequencies_hz"] != [f*1e9 for f in frequencies(project)] or
            any(plan["planned"] != settings["samples_per_k"] for plan in definition["spaces"])):
        raise ValueError("Addition plan does not match the requested exact-count experiment")
    base, cases, plans = definition["base"], definition["cases"], definition["spaces"]
    pairs = []
    save_addition_tables(directory, pairs, plans, definition["frequencies_hz"])
    journal.event("stage", message="SCUFF: базовая антенна, факторизация LU")
    baseline = schur.evaluate(base, force=True, context=dict(role="base_setup", group="base", k=0))
    base_id = key(base)
    base_state = schur.states[base_id]
    write_json(directory / "addition-base.json", baseline)
    journal.event("log", message=f"Base LU + Gain: {baseline['compute_s']:.6g} s (excluded from update times)")
    counter = 0
    parallelism = settings["max_parallel_feko_jobs"]
    try:
        for start in range(0, len(cases), parallelism):
            batch = cases[start:start+parallelism]
            quick, contexts = [], []
            for offset, case in enumerate(batch):
                cancel()
                schur.states[base_id] = base_state
                schur.preferred_parent = base_id
                context = dict(k=case["k"], added=case["added"], group="independent", case=counter+offset+1)
                contexts.append(context)
                journal.event("stage", message=f"+{case['k']}: {context['case']}/{len(cases)}, Schur")
                quick.append(schur.evaluate(case["cells"], force=True, context=context))
            if parallelism == 1:
                journal.event("stage", message=f"+{batch[0]['k']}: {counter+1}/{len(cases)}, FEKO")
                reference = [feko.evaluate(batch[0]["cells"], force=True, context=contexts[0])]
            else:
                with ThreadPoolExecutor(max_workers=parallelism) as pool:
                    jobs = [pool.submit(feko.evaluate, case["cells"], force=True, context=context)
                            for case, context in zip(batch, contexts)]
                    reference = [job.result() for job in jobs]
            for case, context, a, b in zip(batch, contexts, quick, reference):
                pair = dict(context, candidate_id=key(case["cells"]), cells=case["cells"], schur=a, feko=b,
                            errors=compare_rows(a["rows"], b["rows"]),
                            fitness_error=discrepancy(a["fitness"], b["fitness"], 1e-6),
                            speedup=compute_speedup(a, b))
                pairs.append(pair)
                journal.append("pairs.jsonl", pair)
                counter += 1
                save_addition_tables(directory, pairs, plans, definition["frequencies_hz"])
                progress(dict(kind="accuracy", evaluated=counter, total=len(cases), group="independent", k=case["k"],
                              best_cells=case["cells"], best_fitness=a["fitness"], mean_fitness=None, median_fitness=None,
                              schur_stats=dict(schur.stats), feko_stats=dict(feko.stats)))
    finally:
        schur.preferred_parent = None
    write_json(directory / "pairs.json", pairs)
    return dict(kind="accuracy", pairs=pairs, excluded_count=0, definition=definition,
                base_setup_compute_s=baseline["compute_s"])
