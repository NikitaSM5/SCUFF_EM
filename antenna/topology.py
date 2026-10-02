"""Fixed RWG design domain and block Schur updates of its active principal system."""
import copy
import json
from pathlib import Path
import time

import numpy as np

from .farfield import ZVAC
from .geometry import _validate_grid
from .solver import AntennaResult


def load_system(path):
    path = Path(path)
    with np.load(path, allow_pickle=False) as data:
        return AntennaResult(*(data[key].copy() for key in
                               ("M", "b", "x", "vertices_mm", "triangles", "rwg")),
                             json.loads(str(data["metadata_json"])), path.parent)


def triangle_cells(full):
    geometry = full.metadata["geometry"]
    n, a = len(geometry["cells"]), geometry["cell_size_mm"]
    centers = full.vertices_mm[full.triangles].mean(axis=1)
    rc = np.column_stack((np.floor(n/2-centers[:, 1]/a), np.floor(centers[:, 0]/a+n/2))).astype(int)
    if np.any(rc < 0) or np.any(rc >= n):
        raise ValueError("Full mesh extends outside the design grid")
    # A cell edit must never split the support triangle of a retained basis.
    xyz = full.vertices_mm[full.triangles]
    left = (rc[:, 1]-n/2)*a
    bottom = (n/2-rc[:, 0]-1)*a
    if (np.any(xyz[:, :, 0] < left[:, None]-a*1e-9) or
            np.any(xyz[:, :, 0] > left[:, None]+a+a*1e-9) or
            np.any(xyz[:, :, 1] < bottom[:, None]-a*1e-9) or
            np.any(xyz[:, :, 1] > bottom[:, None]+a+a*1e-9)):
        raise ValueError("Full mesh contains triangles crossing cell boundaries")
    return rc


def active_dofs(full, cells):
    geometry = full.metadata["geometry"]
    grid, _ = _validate_grid(cells, geometry["feed_cell"])
    if grid.shape != np.asarray(geometry["cells"]).shape:
        raise ValueError("Grid dimensions do not match the full matrix")
    rc = triangle_cells(full)
    metal = grid[rc[:, 0], rc[:, 1]].astype(bool)
    active = np.flatnonzero(metal[full.rwg[:, 4]] & metal[full.rwg[:, 5]])
    if not np.isin(np.flatnonzero(full.b), active).all():
        raise ValueError("The feed cell and its voltage-gap basis must remain active")
    return active, np.flatnonzero(metal)


def _checked_solve(matrix, rhs):
    if not np.isfinite(matrix).all() or np.linalg.cond(matrix) > 1e12:
        raise np.linalg.LinAlgError("Ill-conditioned Schur block")
    return np.linalg.solve(matrix, rhs)


def _state_checks(matrix, b, state):
    x, inverse = state["x"], state["Y"]
    if not np.isfinite(x).all() or not np.isfinite(inverse).all():
        raise np.linalg.LinAlgError("Non-finite topology state")
    residual = float(np.linalg.norm(matrix @ x-b)/max(np.linalg.norm(b), 1e-300))
    # Probe the inverse independently of the single port RHS, without an O(N^3) product.
    rng = np.random.default_rng(73421)
    probes = rng.standard_normal((len(x), 3)) + 1j*rng.standard_normal((len(x), 3))
    inverse_error = float(np.linalg.norm(matrix @ (inverse @ probes)-probes)/np.linalg.norm(probes))
    if residual > 1e-8 or inverse_error > 1e-8:
        raise np.linalg.LinAlgError("Topology update failed the residual check")
    return dict(relative_residual=residual, inverse_probe_error=inverse_error)


def update_solution(M, b, target, previous=None):
    """Return active indices, inverse and currents; no Hermitian/symmetry assumption.

    Each edit removes a block first, then adds a block through its Schur complement.
    Rebase on an unsafe intermediate block, failed residual or every 20 updates.
    """
    started = time.perf_counter()
    target = np.asarray(target, dtype=np.int64)
    if (target.ndim != 1 or len(target) == 0 or len(np.unique(target)) != len(target) or
            np.any(target < 0) or np.any(target >= len(M))):
        raise ValueError("Invalid active DOF indices")
    reason = None
    added = removed = 0
    steps = 0 if previous is None else int(previous["steps"])+1
    method = "initial_lu" if previous is None else "schur"
    try:
        if previous is None or steps >= 20:
            raise np.linalg.LinAlgError("Initial factorization" if previous is None else "Periodic rebase")
        old = previous["active"]
        Y, x = previous["Y"].copy(), previous["x"].copy()
        if Y.shape != (len(old), len(old)) or x.shape != (len(old),):
            raise ValueError("Invalid saved topology state dimensions")
        keep = np.flatnonzero(np.isin(old, target))
        drop = np.flatnonzero(~np.isin(old, target))
        added_idx = target[~np.isin(target, old)]
        removed, added = len(drop), len(added_idx)
        if removed:
            block = Y[np.ix_(drop, drop)]
            x = x[keep]-Y[np.ix_(keep, drop)] @ _checked_solve(block, x[drop])
            Y = Y[np.ix_(keep, keep)]-Y[np.ix_(keep, drop)] @ _checked_solve(block, Y[np.ix_(drop, keep)])
        active = old[keep]
        if added:
            cross = M[np.ix_(active, added_idx)]
            reverse = M[np.ix_(added_idx, active)]
            U, V = Y @ cross, reverse @ Y
            S = M[np.ix_(added_idx, added_idx)]-reverse @ U
            Sinv = _checked_solve(S, np.eye(added))
            new_x = Sinv @ (b[added_idx]-reverse @ x)
            x = np.concatenate((x-U @ new_x, new_x))
            Y = np.block([[Y+U @ Sinv @ V, -U @ Sinv], [-Sinv @ V, Sinv]])
            active = np.concatenate((active, added_idx))
        if not added and not removed:
            method = "unchanged"
        state = dict(active=active, Y=Y, x=x, steps=steps)
        checks = _state_checks(M[np.ix_(active, active)], b[active], state)
    except np.linalg.LinAlgError as exc:
        reason = str(exc)
        if previous is not None:
            method = "lu_rebase"
            removed = int(np.count_nonzero(~np.isin(previous["active"], target)))
            added = int(np.count_nonzero(~np.isin(target, previous["active"])))
        matrix = M[np.ix_(target, target)]
        # One factorization for both the inverse and the port solution.
        solution = np.linalg.solve(matrix, np.column_stack((np.eye(len(target)), b[target])))
        state = dict(active=target.copy(), Y=solution[:, :-1], x=solution[:, -1], steps=0)
        checks = _state_checks(matrix, b[target], state)
    state["checks"] = checks
    state["update"] = dict(method=method, added_dofs=added, removed_dofs=removed,
                           elapsed_s=time.perf_counter()-started, rebase_reason=reason)
    return state


def save_state(state, path, system_hash):
    with Path(path).open("xb") as stream:
        np.savez_compressed(stream, **{key: state[key] for key in ("active", "Y", "x", "steps")},
                            system_hash=np.array(system_hash))


def load_state(path, system_hash):
    with np.load(path, allow_pickle=False) as data:
        if str(data["system_hash"]) != system_hash:
            raise ValueError("Saved Schur state belongs to a different full matrix")
        return dict(active=data["active"].copy(), Y=data["Y"].copy(), x=data["x"].copy(), steps=int(data["steps"]))


def antenna_result(full, cells, state, directory):
    """Compact the retained triangles and remap RWG panel indices for postprocessing."""
    target, panels = active_dofs(full, cells)
    active = state["active"]
    if not np.array_equal(np.sort(active), target):
        raise ValueError("State does not match the selected metal")
    panel_map = np.full(len(full.triangles), -1, dtype=int)
    panel_map[panels] = np.arange(len(panels))
    rwg = full.rwg[active].copy()
    rwg[:, 4:6] = panel_map[rwg[:, 4:6]]
    metadata = copy.deepcopy(full.metadata)
    geometry = metadata["geometry"]
    geometry["cells"] = np.asarray(cells).tolist()
    geometry.pop("sha256", None)
    geometry["mesh"] = dict(triangles=len(panels), unknowns=len(active))
    current = (-ZVAC*full.b[active]) @ state["x"]
    if not np.isfinite(current) or abs(current) < 1e-300:
        raise ValueError("Zero or non-finite port current")
    impedance = 1/current
    metadata.update(input_impedance_ohm=[float(impedance.real), float(impedance.imag)],
                    port_current_A=[float(current.real), float(current.imag)], port_voltage_V=[1, 0],
                    unknowns=len(active), triangles=len(panels), checks=state["checks"],
                    topology_update=state["update"], timings_s=dict(assembly=0, rhs=0, lu=0, solve=state["update"]["elapsed_s"]))
    return AntennaResult(full.M[np.ix_(active, active)], full.b[active], state["x"], full.vertices_mm,
                         full.triangles[panels], rwg, metadata, Path(directory))


def write_subset_mesh(full, cells, path):
    """Export the identical retained mesh for independent native validation."""
    _, panels = active_dofs(full, cells)
    lines = ["$MeshFormat", "2.2 0 8", "$EndMeshFormat", "$Nodes", str(len(full.vertices_mm))]
    lines += [f"{i+1} {p[0]:.17g} {p[1]:.17g} {p[2]:.17g}" for i, p in enumerate(full.vertices_mm)]
    lines += ["$EndNodes", "$Elements", str(len(panels))]
    lines += [f"{i+1} 2 2 1 1 " + " ".join(str(v+1) for v in full.triangles[p]) for i, p in enumerate(panels)]
    lines += ["$EndElements", ""]
    Path(path).write_text("\n".join(lines), encoding="ascii")
