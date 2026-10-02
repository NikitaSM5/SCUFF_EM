"""Run genuine SCUFF RF calculations and independently verify the exported system."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frequency-ghz", type=float, default=3.2)
    parser.add_argument("--out", type=Path,
                        default=Path("results") / datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("This test uses native Windows binaries. Run it with Windows Python.")
    source = ROOT / "library/source"
    build = ROOT / "build"
    binary_dir = ROOT / "library/bin"
    helper = ROOT / "test/bin/export_system.exe"
    rf_binary = binary_dir / "scuff-rf.exe"
    if not helper.is_file() or not rf_binary.is_file():
        parser.error("Missing native binaries. Run scripts/setup-windows.ps1.")
    os.environ["OPENBLAS_NUM_THREADS"] = "1"
    os.environ["OMP_NUM_THREADS"] = "1"
    import numpy as np
    from prepare_geometry import prepare

    if not np.isfinite(args.frequency_ghz) or args.frequency_ghz <= 0:
        parser.error("frequency must be finite and positive")
    out = (ROOT / args.out).resolve()
    if out.exists() and any(out.iterdir()):
        parser.error(f"Output directory is not empty: {out}. Choose a new --out.")
    out.mkdir(parents=True, exist_ok=True)
    geometry = prepare(source)
    sha = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    if sha != (build / "scuff_commit.txt").read_text().strip():
        raise RuntimeError("Source commit does not match build")
    geo = ROOT / "test/geometry/DipoleAntenna_Medium.scuffgeo"
    port = ROOT / "test/geometry/Dipole.ports"
    env = dict(os.environ)
    for key in list(env):
        if key.startswith("SCUFF_"):
            del env[key]
    env.update(OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", HOST=platform.node(),
               SCUFF_MESH_PATH=geo.parent.as_posix())
    env["PATH"] = str(binary_dir) + os.pathsep + env.get("PATH", "")
    summary = {"scuff_commit": sha, "timestamp_utc": datetime.now(timezone.utc).isoformat(),
               "geometry": geometry, "geometry_units": "mm", "port_current_A": [1.0, 0.0],
               "Z0_ohm": 50.0, "numpy_version": np.__version__, "python": sys.version,
               "platform": platform.platform(), "native_windows": True,
               "executable": str(helper), "threads": 1, "checks": {}}
    summary["windows_source_patches"] = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted((ROOT / "scripts/patches/windows").glob("*.patch"))}
    summary["windows_runtime"] = json.loads((build / "runtime.json").read_text())
    log = (out / "run.log").open("w", buffering=1)

    def report(message):
        print(message, flush=True)
        log.write(message + "\n")

    def run(command, cwd):
        command = [part.as_posix() if isinstance(part, Path) else str(part) for part in command]
        report("$ " + " ".join(map(str, command)))
        t = time.perf_counter()
        proc = subprocess.run(list(map(str, command)), cwd=cwd, env=env,
                              stdout=log, stderr=subprocess.STDOUT)
        if proc.returncode:
            raise RuntimeError(f"Command failed, exit={proc.returncode}; see {out / 'run.log'}")
        return time.perf_counter() - t

    def load_system(folder):
        matrix = np.loadtxt(folder / "M_real.csv", delimiter=",", ndmin=2) + 1j * np.loadtxt(folder / "M_imag.csv", delimiter=",", ndmin=2)
        def vector(name):
            a = np.loadtxt(folder / name, delimiter=",", ndmin=2)
            if a.shape[1] != 2:
                raise RuntimeError(f"Invalid vector format: {name}")
            return a[:, 0] + 1j * a[:, 1]
        return matrix, vector("b.csv"), vector("x.csv")

    def relative(a, b):
        return float(np.linalg.norm(a - b) / np.linalg.norm(b))

    try:
        wall = run([helper, geo, port, args.frequency_ghz, out], out)
        summary.update(json.loads((out / "helper.json").read_text()))
        summary["timings_s"]["helper_process_wall"] = wall
        m, b, x = load_system(out)
        checks = summary["checks"]
        checks["dimensions"] = m.shape == (x.size, x.size) and b.shape == x.shape
        checks["finite"] = bool(all(np.isfinite(a).all() for a in (m, b, x)))
        checks["nonzero_matrix_rhs"] = bool(np.linalg.norm(m) > 0 and np.linalg.norm(b) > 0)
        checks["lu_solve_success"] = summary["lu_info"] == summary["solve_info"] == 0
        checks["isolated_dipole"] = (summary["surfaces"] == 1 and summary["ports"] == 1
                                      and summary["triangles"] == geometry["selected_triangles"]
                                      and x.size == geometry["expected_unknowns"])
        if not all(checks.values()):
            raise RuntimeError(f"Invalid exported system: {checks}")
        residual = relative(m @ x, b)
        t = time.perf_counter()
        x_numpy = np.linalg.solve(m, b)
        summary["timings_s"]["numpy_solve"] = time.perf_counter() - t
        numpy_residual = relative(m @ x_numpy, b)
        difference = relative(x_numpy, x)
        summary.update(matrix_shape=list(m.shape), relative_residual=residual,
                       numpy_relative_residual=numpy_residual, numpy_solution_relative_difference=difference)
        checks["scuff_residual_lt_1e-8"] = residual < 1e-8
        checks["numpy_residual_lt_1e-8"] = numpy_residual < 1e-8
        checks["numpy_solution_agrees_lt_1e-8"] = difference < 1e-8
        t = time.perf_counter()
        np.savez(out / "system.npz", M=m, b=b, x=x)
        summary["timings_s"]["npz_export"] = time.perf_counter() - t
        with np.load(out / "system.npz") as archive:
            checks["npz_roundtrip_exact"] = all(np.array_equal(archive[k], a) for k, a in (("M", m), ("b", b), ("x", x)))
        reference = out / "reference"
        reference.mkdir()
        summary["timings_s"]["scuff_rf_reference_wall"] = run(
            [rf_binary, "--geometry", geo, "--portfile", port,
             "--frequency", args.frequency_ghz, "--ZParameters", "--SParameters"], reference)
        zdata = np.loadtxt(reference / "DipoleAntenna_Medium.zparms", ndmin=2)
        if zdata.shape != (1, 3) or not np.isclose(zdata[0, 0], args.frequency_ghz, rtol=1e-6):
            raise RuntimeError("Unexpected scuff-rf reference output")
        zin = complex(*summary["Z_in"])
        zref = complex(*zdata[0, 1:3])
        zerror = abs(zin - zref) / abs(zref)
        summary.update(scuff_rf_Z_in=[zref.real, zref.imag], scuff_rf_Z_relative_difference=zerror)
        checks["scuff_rf_impedance_agrees_lt_1e-6"] = zerror < 1e-6
        s11 = (zin - 50.0) / (zin + 50.0)
        checks["s11_formula"] = abs(s11 - complex(*summary["S11"])) < 1e-14
        checks["finite_impedance_s11"] = bool(np.isfinite([zin, s11]).all())
        repeat = out / "repeat"
        repeat.mkdir()
        summary["timings_s"]["repeat_helper_wall"] = run([helper, geo, port, args.frequency_ghz, repeat], repeat)
        mr, br, xr = load_system(repeat)
        zr = complex(*json.loads((repeat / "helper.json").read_text())["Z_in"])
        reproduction = {"M_relative_difference": relative(mr, m),
                        "b_relative_difference": relative(br, b),
                        "x_relative_difference": relative(xr, x),
                        "Z_relative_difference": abs(zr - zin) / abs(zin)}
        summary["reproducibility"] = reproduction
        checks["repeat_agrees_lt_1e-12"] = all(v < 1e-12 for v in reproduction.values())
        checks["repeat_residual_lt_1e-8"] = relative(mr @ xr, br) < 1e-8
        summary["sha256"] = {name: hashlib.sha256((out / name).read_bytes()).hexdigest()
                              for name in ("M_real.csv", "M_imag.csv", "b.csv", "x.csv", "system.npz")}
        report(f"SCUFF commit: {sha}\nM: {m.shape}, triangles: {summary['triangles']}, ports: {summary['ports']}")
        report(f"Z_in = {zin:.12g} ohm; S11 = {s11:.12g}")
        report(f"Residual = {residual:.6e}; NumPy residual = {numpy_residual:.6e}; solution difference = {difference:.6e}")
        report(f"Reference Z relative difference = {zerror:.6e}")
        report("Timings (seconds): " + json.dumps(summary["timings_s"]))
        report("M[:5,:5] =\n" + np.array2string(m[:5, :5], precision=7, max_line_width=140))
        report("Checks: " + json.dumps(checks))
        if not all(checks.values()):
            raise RuntimeError("Verification failed; see summary.json")
        summary["success"] = True
    except Exception as exc:
        summary["success"] = False
        summary["error"] = str(exc)
        report("FAILED: " + str(exc))
        raise
    finally:
        (out / "summary.json").write_text(json.dumps(summary, indent=2, allow_nan=False) + "\n")
        log.close()
    print(f"Results: {out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
