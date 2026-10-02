"""Lossless NumPy files; never silently overwrite a previous calculation."""
import json
from pathlib import Path

import numpy as np


def save_matrix(matrix, path, *, overwrite=False):
    """Save a finite square complex matrix as .npy; load with np.load(path)."""
    matrix = np.asarray(matrix, dtype=np.complex128)
    if matrix.ndim != 2 or not matrix.shape[0] or matrix.shape[0] != matrix.shape[1]:
        raise ValueError("matrix must be nonempty and square")
    if not np.isfinite(matrix).all():
        raise ValueError("matrix contains NaN or Inf")
    path = Path(path).resolve()
    if path.suffix.lower() != ".npy":
        raise ValueError("Use a .npy file for save_matrix")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb" if overwrite else "xb") as stream:
        np.save(stream, matrix, allow_pickle=False)
    return path


def save_result(result, path, *, overwrite=False):
    """Save M, b, x, actual SCUFF mesh/RWG ordering and JSON metadata as .npz."""
    path = Path(path).resolve()
    if path.suffix.lower() != ".npz":
        raise ValueError("Use a .npz file for save_result")
    metadata = json.dumps(result.metadata, allow_nan=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb" if overwrite else "xb") as stream:
        np.savez_compressed(stream, M=result.M, b=result.b, x=result.x,
                            vertices_mm=result.vertices_mm, triangles=result.triangles,
                            rwg=result.rwg, metadata_json=np.array(metadata))
    return path
