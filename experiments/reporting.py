"""Scientific summaries and raster plots, including cold/warm and preprocessing costs."""
from pathlib import Path

import numpy as np

from .metrics import distribution
from .storage import write_json, write_csv
from .timing import compute_value, compute_total, compute_speedup


def online_wall(records):
    intervals = sorted((r["started_s"], r["started_s"]+r["online_s"]) for r in records if "started_s" in r)
    if len(intervals) != len(records):
        return sum(r["online_s"] for r in records)
    total, end = 0.0, float("-inf")
    for start, stop in intervals:
        total += max(0, stop-max(start, end))
        end = max(stop, end)
    return total


def summary(backends, preprocessing, setup, outcome, wall_s):
    result = dict(wall_s=wall_s, preprocessing=preprocessing, backend_setup_s=setup, solvers={},
                  timing_policy="Primary compute_s: FEKO cumulative runtime from .out; Schur topology solve + impedance/Gain. Excludes CADFEKO startup, CAD meshing, Python I/O, cache lookup, direct diagnostics. Cache hits have no measured compute_s. online_s/end_to_end_s retained as separate whole-workflow measurements. Parallel compute_s is summed solver work, not wall time.",
                  relative_error_policy="abs(S-R)/max(abs(R),floor); complex S11 floor=1e-3; Z floor=1 ohm; linear Gain floor=1e-6; fitness floor=1e-6")
    for backend in backends:
        records = [r for r in backend.records if r.get("context", {}).get("role") not in ("final_validation", "base_setup")]
        cold = [value for r in records if (value := compute_value(r)) is not None]
        online = online_wall(records)
        base_setup_s = sum(r["evaluation_s"] for r in backend.records if r.get("context", {}).get("role") == "base_setup")
        result["solvers"][backend.name] = dict(backend.stats, online_s=online,
                  evaluation_work_s=sum(r["online_s"] for r in records),
                  end_to_end_s=online+base_setup_s+setup.get(backend.name, 0)+getattr(backend, "cleanup_s", 0)+(preprocessing["elapsed_s"] if backend.name == "schur" else 0),
                  compute_s=compute_total(records), compute_time=distribution(cold),
                  compute_timing_missing=sum(not r["cached"] and compute_value(r) is None for r in records),
                  evaluation_time=distribution(cold), cold_evaluation_time=distribution(cold),
                  whole_evaluation_time=distribution([r["online_s"] for r in records]),
                  cache_hit_rate=backend.stats["cache_hits"]/max(1, backend.stats["evaluations"]),
                  diagnostic_s=sum(r["diagnostic_s"] for r in records),
                  best_fitness=max((r["fitness"] for r in records), default=None))
        result["solvers"][backend.name].update(evaluations=len(records), cache_hits=sum(r["cached"] for r in records),
                  cache_hit_rate=sum(r["cached"] for r in records)/max(1, len(records)),
                  feko_runs=sum(not r["cached"] for r in records) if backend.name == "feko" else 0)
    result["validation_runtime_s"] = sum(r["evaluation_s"] for backend in backends for r in backend.records
                                        if r.get("context", {}).get("role") == "final_validation")
    result["validation_timing_policy"] = "Mandatory final FEKO validation is reported separately, not charged to the FEKO GA trajectory"
    result["base_setup_compute_s"] = sum(r.get("compute_s") or 0 for backend in backends for r in backend.records
                                        if r.get("context", {}).get("role") == "base_setup")
    pairs = outcome.get("pairs", [])
    result["paired_geometries"] = len(pairs)
    result["errors"] = {metric: {kind: distribution([row[metric][kind] for p in pairs for row in p["errors"] if metric in row])
                                      for kind in ("absolute", "relative")}
                        for metric in ("s11_complex", "impedance", "reflected_power", "gain_linear")}
    result["fitness_error"] = {kind: distribution([p["fitness_error"][kind] for p in pairs]) for kind in ("absolute", "relative")}
    cold_pairs = [p for p in pairs if not p["schur"]["cached"] and not p["feko"]["cached"]]
    result["cold_pair_speedup"] = distribution([compute_speedup(p["schur"], p["feko"]) for p in cold_pairs])
    s = result["solvers"].get("schur", {})
    f = result["solvers"].get("feko", {})
    complete_timing = not (s.get("compute_timing_missing", 0) or f.get("compute_timing_missing", 0))
    result["overall_compute_speedup"] = (f.get("compute_s", 0)/s["compute_s"]
        if complete_timing and s.get("compute_s", 0) > 0 and f.get("compute_time", {}).get("count", 0) else None)
    result["overall_online_speedup"] = f.get("online_s", 0)/max(s.get("online_s", 0), 1e-9)
    result["overall_end_to_end_speedup"] = f.get("end_to_end_s", 0)/max(s.get("end_to_end_s", 0), 1e-9)
    result["divergence_generation"] = outcome.get("divergence_generation")
    result["validated_best"] = outcome.get("validated_best")
    result["excluded_count"] = outcome.get("excluded_count", 0)
    diagnostics = [d for backend in backends if backend.name == "schur" for r in backend.records if not r["cached"] for d in r["diagnostics"]]
    result["schur_vs_direct_current_error"] = distribution([d.get("current_relative_error_vs_direct") for d in diagnostics])
    result["schur_residual"] = distribution([d["relative_residual"] for d in diagnostics])
    result["schur_block_rcond"] = distribution([d["block_rcond_estimate"] for d in diagnostics])
    result["native_scuff_diagnostic_s"] = sum(p.get("scuff_full", {}).get("total_s", 0) for p in pairs)
    return result


def live_plot(directory, events):
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib import pyplot as plt
    fig, ax = plt.subplots(figsize=(7, 3.5))
    for backend in ("schur", "feko"):
        rows = [e for e in events if e.get("backend", "schur") == backend]
        if rows:
            ax.plot([e.get("generation", e["evaluated"]) for e in rows], [e["best_fitness"] for e in rows], "o-", label=backend)
    ax.set(xlabel="Generation / evaluated cases", ylabel="Fitness (maximise)")
    ax.legend(); ax.grid(alpha=.2); fig.tight_layout()
    path = Path(directory) / "live-plot.png"
    temp = path.with_name("live-plot-next.png")
    fig.savefig(temp, dpi=110)
    plt.close(fig)
    temp.replace(path)
    return path


def plots(directory, outcome, report, records):
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib import pyplot as plt
    directory = Path(directory)
    folder = directory / "plots"
    folder.mkdir(exist_ok=True)
    names = []

    def save(name, figure):
        figure.tight_layout()
        figure.savefig(folder / (name+".png"), dpi=130)
        plt.close(figure)
        names.append(name+".png")

    colors = dict(schur="#1976b5", feko="#d34b36")

    def mean_available(values):
        values = [v for v in values if v is not None]
        return float(np.mean(values)) if values else float("nan")

    generations = outcome.get("generations", {})
    if generations:
        fig, axes = plt.subplots(1, 3, figsize=(13, 4))
        for backend, rows in generations.items():
            for metric, style in (("best_fitness", "-"), ("mean_fitness", "--"), ("median_fitness", ":")):
                axes[0].plot([r["generation"] for r in rows], [r[metric] for r in rows], style, color=colors[backend], label=f"{backend} {metric.split('_')[0]}")
            times = np.array([r.get("compute_s", float("nan")) for r in rows])
            x = [r["generation"] for r in rows]
            axes[1].plot(x, times, "o-", color=colors[backend], label=backend)
            axes[2].plot(x, np.cumsum(times), "o-", color=colors[backend], label=backend)
        for ax, title, label in zip(axes, ("Fitness convergence", "Solver computation per generation", "Cumulative solver computation"), ("Fitness (maximise)", "Seconds", "Seconds")):
            ax.set(xlabel="Generation", ylabel=label, title=title)
            ax.legend(fontsize=7)
            ax.grid(alpha=.2)
        save("ga-convergence-runtime", fig)
        fig, axes = plt.subplots(1, 2, figsize=(9, 4))
        for backend, rows in generations.items():
            x = [r["generation"] for r in rows]
            times = np.array([r["online_s"] for r in rows])
            axes[0].plot(x, times, "o-", color=colors[backend], label=backend)
            prep = report["preprocessing"]["elapsed_s"] if backend == "schur" else 0
            axes[1].plot(x, np.cumsum(times)+prep+report["backend_setup_s"].get(backend, 0), "o-", color=colors[backend], label=backend)
        for ax, title in zip(axes, ("Whole evaluation time per generation", "Whole workflow cumulative time")):
            ax.set(title=title, xlabel="Generation", ylabel="Seconds"); ax.legend(); ax.grid(alpha=.2)
        save("ga-workflow-runtime", fig)
    pairs = outcome.get("pairs", [])
    if pairs:
        fig, axes = plt.subplots(1, 3, figsize=(13, 4))
        for ax, metric in zip(axes, ("fitness", "s11", "gain")):
            a, b = [], []
            for p in pairs:
                if metric == "fitness":
                    a.append(p["feko"]["fitness"]); b.append(p["schur"]["fitness"])
                else:
                    field = "s11_magnitude" if metric == "s11" else "gain_peak_dbi"
                    for s, f in zip(p["schur"]["rows"], p["feko"]["rows"]):
                        if s.get(field) is not None and f.get(field) is not None:
                            a.append(f[field]); b.append(s[field])
            if a:
                ax.scatter(a, b, s=18)
                low, high = min(a+b), max(a+b)
                pad = max(abs(high-low)*.05, abs(high)*.001, 1e-6)
                ax.plot([low-pad, high+pad], [low-pad, high+pad], "k--", linewidth=1)
            ax.set(xlabel="FEKO", ylabel="Schur", title=metric+" (identical masks)")
            ax.grid(alpha=.2)
        save("paired-scatter", fig)
        by_group = {}
        for p in pairs:
            by_group.setdefault(p.get("group", "ga"), []).append(p)
        for group, group_pairs in by_group.items():
            ks = sorted({p.get("k", 0) for p in group_pairs})
            fig, axes = plt.subplots(2, 3, figsize=(13, 7))
            groups = [[p for p in group_pairs if p.get("k", 0) == k] for k in ks]
            for backend in ("schur", "feko"):
                means = [mean_available([compute_value(p[backend]) for p in values]) for values in groups]
                axes[0, 0].plot(ks, means, "o-", color=colors[backend], label=backend)
            axes[0, 0].set(title="Solver computation (cache hits excluded)", ylabel="Seconds", xlabel="Added cells")
            axes[0, 0].legend()
            axes[0, 1].plot(ks, [mean_available([compute_speedup(p["schur"], p["feko"]) for p in g]) for g in groups], "o-")
            axes[0, 1].set(title="Solver computation speedup", ylabel="FEKO / Schur", xlabel="Added cells")
            errors = [[max(row["s11_complex"]["relative"] for row in p["errors"]) for p in g] for g in groups]
            for metric in ("mean", "median", "p95", "max"):
                axes[0, 2].plot(ks, [distribution(e)[metric] for e in errors], "o-", label=metric)
            axes[0, 2].set(title="Complex S11 relative error", xlabel="Added cells")
            axes[0, 2].legend(fontsize=8)
            axes[1, 0].plot(ks, [np.mean([max(row["s11_complex"]["absolute"] for row in p["errors"]) for p in g]) for g in groups], "o-")
            axes[1, 0].set(title="Complex S11 absolute error", xlabel="Added cells")
            measured = [p for p in group_pairs if compute_value(p["schur"]) is not None]
            axes[1, 1].scatter([compute_value(p["schur"]) for p in measured], [max(r["s11_complex"]["relative"] for r in p["errors"]) for p in measured], s=18)
            axes[1, 1].set(title="Computation / error", xlabel="Schur compute seconds", ylabel="Relative complex S11 error")
            axes[1, 2].boxplot(errors, tick_labels=[str(k) for k in ks])
            axes[1, 2].set(title="Error distribution", xlabel="Added cells")
            for ax in axes.flat:
                ax.grid(alpha=.2)
            save("accuracy-"+group, fig)
            for k, values in zip(ks, groups):
                fig, axes = plt.subplots(1, 3, figsize=(13, 4))
                for ax, field, title in zip(axes, ("s11_magnitude", "resistance_ohm", "gain_peak_dbi"),
                                           ("S11 magnitude", "Resistance (ohm)", "Gain max (dBi)")):
                    xy = [(f[field], s[field]) for p in values
                          for s, f in zip(p["schur"]["rows"], p["feko"]["rows"])
                          if s.get(field) is not None and f.get(field) is not None]
                    if xy:
                        x, y = np.asarray(xy).T
                        ax.scatter(x, y, s=18)
                        low, high = min(x.min(), y.min()), max(x.max(), y.max())
                        pad = max((high-low)*.05, abs(high)*.001, 1e-6)
                        ax.plot([low-pad, high+pad], [low-pad, high+pad], "k--", linewidth=1)
                    ax.set(xlabel="FEKO", ylabel="Schur", title=f"{title}, K={k}")
                    ax.grid(alpha=.2)
                save(f"scatter-{group}-k-{k:03d}", fig)
        fig, axes = plt.subplots(1, 2, figsize=(9, 4))
        for backend in ("schur", "feko"):
            online = np.array([online_wall([p[backend] for p in pairs[:i+1]]) for i in range(len(pairs))])
            axes[0].plot(online, label=backend)
            axes[1].plot(online+report["backend_setup_s"].get(backend, 0)+(report["preprocessing"]["elapsed_s"] if backend == "schur" else 0), label=backend)
        for ax, title in zip(axes, ("Online cumulative time", "End-to-end cumulative time")):
            ax.set(title=title, xlabel="Paired case", ylabel="Seconds"); ax.legend(); ax.grid(alpha=.2)
        save("paired-cumulative-time", fig)
        fig, ax = plt.subplots(figsize=(7, 4))
        for backend in ("schur", "feko"):
            ax.plot([compute_total([p[backend] for p in pairs[:i+1]]) for i in range(len(pairs))], label=backend)
        ax.set(title="Cumulative solver computation", xlabel="Paired case", ylabel="Seconds")
        ax.legend(); ax.grid(alpha=.2)
        save("paired-compute-time", fig)
    fig, axes = plt.subplots(1, 2, figsize=(9, 4))
    for backend in ("schur", "feko"):
        cold = [value for r in records if r["backend"] == backend and (value := compute_value(r)) is not None]
        warm = [r["online_s"] for r in records if r["backend"] == backend and r["cached"]]
        for ax, values in zip(axes, (cold, warm)):
            if values:
                ax.hist(values, bins=min(15, max(1, len(values))), alpha=.5, label=backend, color=colors[backend])
    for ax, title in zip(axes, ("Solver computation time", "Cache lookup time")):
        ax.set(title=title, xlabel="Seconds", ylabel="Count")
        if ax.patches:
            ax.legend()
    save("evaluation-time", fig)
    best = outcome.get("best", {})
    if best:
        fig, axes = plt.subplots(1, len(best), figsize=(4*len(best), 4), squeeze=False)
        for ax, (backend, record) in zip(axes.flat, best.items()):
            ax.imshow(record["cells"], cmap="Blues", vmin=0, vmax=1, interpolation="nearest")
            ax.set(title=f"{backend}: {record['fitness']:.6g}", xlabel="Column", ylabel="Row")
        save("best-geometries", fig)
    write_json(directory / "plots.json", names)
    return names
