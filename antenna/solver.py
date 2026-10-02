"""Native SCUFF-EM execution, with explicit resource limits and validation."""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

import numpy as np

from .geometry import AntennaGeometry, _positive

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class AntennaResult:
    M: np.ndarray
    b: np.ndarray
    x: np.ndarray
    vertices_mm: np.ndarray
    triangles: np.ndarray
    rwg: np.ndarray
    metadata: dict
    work_dir: Path


def _read_complex(path, shape):
    if path.stat().st_size != int(np.prod(shape)) * 16:
        raise RuntimeError(f"Invalid SCUFF export size: {path}")
    values = np.fromfile(path, dtype="<c16")
    if values.size != int(np.prod(shape)):
        raise RuntimeError(f"Incomplete SCUFF export: {path}")
    return values.reshape(shape)


def _relative(actual, expected):
    norm = np.linalg.norm(expected)
    return float(np.linalg.norm(actual - expected) / norm) if norm else float("inf")


def _verify(M, b, x, native, geometry):
    if not all(np.isfinite(array).all() for array in (M, b, x)):
        raise RuntimeError("SCUFF produced NaN or Inf")
    if not np.linalg.norm(M) or not np.linalg.norm(b):
        raise RuntimeError("Zero matrix or excitation vector")
    if native["triangles"] != geometry.metadata["mesh"]["triangles"]:
        raise RuntimeError("SCUFF read a different triangle count")
    mesh = geometry.metadata["mesh"]
    if native["port_edges"] != mesh.get("port_edges", mesh.get("feed_triangles")):
        raise RuntimeError("SCUFF feed does not match the geometry mesh")
    expected_feed = geometry.metadata.get("feed", {}).get("model")
    if expected_feed and native.get("feed_model") != expected_feed:
        raise RuntimeError("SCUFF excitation model does not match the geometry")
    residual = _relative(M @ x, b)
    checks = {"relative_residual": residual}
    if not np.isfinite(residual) or residual >= 1e-8:
        raise RuntimeError(f"SCUFF residual is too large: {residual}")
    return checks


def solve_antenna(geometry, frequency_ghz, *, work_dir=None, max_unknowns=2000,
                  timeout_s=600, verify_reference=False, cancel=None):
    """Return unfactorized M and b, x for the geometry's excitation (delta gap: 1 V).

    Only the metal has RWG unknowns; the infinite substrate and PEC ground
    enter via SCUFF's layered Green function. Work files and logs are retained.
    verify_reference repeats assembly/solution; legacy point ports additionally
    use the high-level scuffSolver API. This is not a physical accuracy test.
    """
    if os.name != "nt":
        raise RuntimeError("This installation requires native Windows Python")
    if not isinstance(geometry, AntennaGeometry):
        raise TypeError("geometry must come from create_planar_antenna or load_geometry")
    frequency = _positive(frequency_ghz, "frequency_ghz")
    timeout_s = _positive(timeout_s, "timeout_s")
    if isinstance(max_unknowns, bool) or not isinstance(max_unknowns, int) or max_unknowns < 1:
        raise ValueError("max_unknowns must be a positive integer")
    geometry.validate_files()
    n = geometry.unknowns
    if n > max_unknowns:
        raise ValueError(f"Mesh has {n} unknowns, limit is {max_unknowns}; use a coarser mesh. "
                         f"One dense complex matrix alone needs {16*n*n/1024**2:.1f} MiB.")
    binary = ROOT / "antenna/bin/export_planar.exe"
    if not binary.is_file():
        raise FileNotFoundError("Run scripts/setup-windows.ps1 to build export_planar.exe")
    runtime = json.loads((ROOT / "build/runtime.json").read_text())
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    if runtime.get(binary.name, {}).get("sha256") != digest:
        raise RuntimeError("Native binary does not match build/runtime.json; rebuild it")
    if work_dir is None:
        work_dir = ROOT / "results/planar" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    work_dir = Path(work_dir).resolve()
    if work_dir.exists() and (not work_dir.is_dir() or any(work_dir.iterdir())):
        raise FileExistsError(f"Calculation directory must be empty: {work_dir}")
    work_dir.mkdir(parents=True, exist_ok=True)
    env = {key: value for key, value in os.environ.items() if not key.startswith("SCUFF_")}
    env.update(OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1",
               SCUFF_MESH_PATH=geometry.directory.as_posix(), HOST="native-windows")
    env["PATH"] = str(ROOT / "library/bin") + os.pathsep + env.get("PATH", "")
    command = [str(binary), geometry.scuffgeo.as_posix(), geometry.port_file.as_posix(),
               geometry.substrate_file.as_posix(), format(frequency, ".17g"),
               work_dir.as_posix(), str(max_unknowns), str(int(verify_reference))]
    metadata = {"schema_version": 1, "frequency_ghz": frequency,
                "phasor_convention": "exp(-i*w*t)",
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "geometry": geometry.metadata, "command": command,
                "scuff_commit": (ROOT / "build/scuff_commit.txt").read_text().strip(),
                "native_sha256": digest, "numpy_version": np.__version__,
                "excitation": geometry.metadata.get("feed", {"model": "point_port", "current_a": 1.0}),
                "source_patches": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                   for p in sorted((ROOT / "scripts/patches/windows").glob("*.patch"))},
                "rwg_columns": ["v1", "v2", "q_plus", "q_minus", "panel_plus", "panel_minus"],
                "index_base": 0, "success": False}
    try:
        started = time.perf_counter()
        with (work_dir / "process.log").open("wb") as log:
            if cancel is None:
                process = subprocess.run(command, cwd=work_dir, env=env, stdout=log,
                                         stderr=subprocess.STDOUT, timeout=timeout_s)
            else:
                process = subprocess.Popen(command, cwd=work_dir, env=env, stdout=log, stderr=subprocess.STDOUT)
                try:
                    while process.poll() is None:
                        cancel()
                        if time.perf_counter()-started > timeout_s:
                            raise subprocess.TimeoutExpired(command, timeout_s)
                        time.sleep(.1)
                except BaseException:
                    process.kill()
                    process.wait(timeout=30)
                    raise
        if process.returncode:
            raise RuntimeError(f"SCUFF exited with {process.returncode}; see {work_dir / 'process.log'}")
        metadata["process_wall_s"] = time.perf_counter() - started
        native = json.loads((work_dir / "native.json").read_text())
        metadata.update(native)
        if native["unknowns"] != n or native["lu_info"] != 0 or native["solve_info"] != 0:
            raise RuntimeError("SCUFF dimensions or LAPACK status are inconsistent")
        M = _read_complex(work_dir / "M.bin", (n, n))
        b = _read_complex(work_dir / "b.bin", (n,))
        x = _read_complex(work_dir / "x.bin", (n,))
        metadata["checks"] = _verify(M, b, x, native, geometry)
        if verify_reference:
            reference = _read_complex(work_dir / "x_reference.bin", (n,))
            difference = _relative(x, reference)
            if not np.isfinite(difference) or difference >= 1e-8:
                raise RuntimeError("High-level scuffSolver reference disagrees")
            prefix = "repeat" if geometry.metadata.get("feed") else "high_level"
            metadata["checks"][prefix+"_solution_difference"] = difference
            reference_z = _read_complex(work_dir / "z_reference.bin", (1, 1))[0, 0]
            native_z = complex(*metadata["input_impedance_ohm"])
            z_difference = abs(native_z-reference_z) / max(abs(reference_z), 1e-12)
            if not np.isfinite(z_difference) or z_difference >= 1e-8:
                raise RuntimeError("High-level scuffSolver impedance disagrees")
            metadata["checks"][prefix+"_impedance_difference"] = z_difference
        vertices = np.loadtxt(work_dir / "vertices.csv", delimiter=",", ndmin=2)
        triangles = np.loadtxt(work_dir / "triangles.csv", delimiter=",", ndmin=2, dtype=np.int64)
        rwg = np.loadtxt(work_dir / "rwg.csv", delimiter=",", ndmin=2, dtype=np.int64)
        if rwg.shape != (n, 6):
            raise RuntimeError("Invalid RWG index map")
        metadata["success"] = True
        return AntennaResult(M, b, x, vertices, triangles, rwg, metadata, work_dir)
    except Exception as exc:
        metadata["error"] = str(exc)
        raise
    finally:
        (work_dir / "summary.json").write_text(json.dumps(metadata, indent=2, allow_nan=False) + "\n")
