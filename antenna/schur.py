"""Solve-operator Schur updates: LU factors and selected columns, never a full inverse."""
import time
import warnings

import numpy as np
from scipy.linalg import LinAlgWarning, lu_factor, lu_solve, get_lapack_funcs


class Factor:
    def __init__(self, matrix, condition_limit=1e12):
        self.size = len(matrix)
        self.rcond = 1.0
        if not self.size:
            self.lu = None
            return
        with warnings.catch_warnings():
            warnings.simplefilter("error", LinAlgWarning)
            try:
                self.lu = lu_factor(matrix)
            except (LinAlgWarning, ValueError) as exc:
                raise np.linalg.LinAlgError(str(exc)) from exc
        gecon = get_lapack_funcs("gecon", (self.lu[0],))
        self.rcond, info = gecon(self.lu[0], float(np.linalg.norm(matrix, 1)))
        if info or not np.isfinite(self.rcond) or self.rcond < 1/condition_limit:
            raise np.linalg.LinAlgError(f"Near-singular block: reciprocal condition estimate {self.rcond:.3e}")

    def solve(self, rhs):
        return np.array(rhs, dtype=complex, copy=True) if self.lu is None else lu_solve(self.lu, rhs)


class State:
    def __init__(self, active, factor, parent=None, *, mode="direct", U=None, C=None, keep=None, drop=None):
        self.active = np.asarray(active, dtype=np.int64)
        self.factor, self.parent, self.mode = factor, parent, mode
        self.U, self.C, self.keep, self.drop = U, C, keep, drop
        self.depth = parent.depth+1 if parent else 0

    def solve(self, rhs):
        if self.mode == "direct":
            return self.factor.solve(rhs)
        if self.mode == "add":
            n = len(self.parent.active)
            z = self.parent.solve(rhs[:n])
            y = self.factor.solve(rhs[n:]-self.C @ z)
            return np.concatenate((z-self.U @ y, y), axis=0)
        embedded = np.zeros((len(self.parent.active),)+rhs.shape[1:], dtype=complex)
        embedded[self.keep] = rhs
        z = self.parent.solve(embedded)
        return z[self.keep]-self.U[self.keep] @ self.factor.solve(z[self.drop])


def _changed(M, old, target, condition_limit):
    state = old
    keep = np.flatnonzero(np.isin(old.active, target))
    drop = np.flatnonzero(~np.isin(old.active, target))
    new = target[~np.isin(target, old.active)]
    if len(drop):
        columns = np.zeros((len(old.active), len(drop)), dtype=complex)
        columns[drop, np.arange(len(drop))] = 1
        U = old.solve(columns)
        state = State(old.active[keep], Factor(U[drop], condition_limit), old,
                      mode="remove", U=U, keep=keep, drop=drop)
    if len(new):
        C = M[np.ix_(new, state.active)]
        U = state.solve(M[np.ix_(state.active, new)])
        S = M[np.ix_(new, new)]-C @ U
        state = State(np.concatenate((state.active, new)), Factor(S, condition_limit), state,
                      mode="add", U=U, C=C)
    return state, len(new), len(drop)


def solve_topology(M, b, target, previous=None, *, refactor_interval=20,
                   condition_limit=1e12, residual_tolerance=1e-8, max_changed_fraction=.5):
    """Update a principal subsystem. Rebase logged on depth, large edit or numerical failure."""
    start = time.perf_counter()
    target = np.asarray(target, dtype=np.int64)
    if target.ndim != 1 or not len(target) or len(set(target)) != len(target) or np.any(target < 0) or np.any(target >= len(M)):
        raise ValueError("Invalid active indices")
    if refactor_interval < 1 or condition_limit <= 1 or not 0 < max_changed_fraction <= 1:
        raise ValueError("Invalid Schur stability settings")
    old_ids = previous.active if previous else np.array([], dtype=int)
    added = int(np.sum(~np.isin(target, old_ids)))
    removed = int(np.sum(~np.isin(old_ids, target)))
    method, reason = "schur", None

    def checked(state):
        rhs = b[state.active]
        x = state.solve(rhs)
        matrix = M[np.ix_(state.active, state.active)]
        residual = float(np.linalg.norm(matrix @ x-rhs)/max(np.linalg.norm(rhs), 1e-300))
        if not np.isfinite(x).all() or not np.isfinite(residual) or residual > residual_tolerance:
            raise np.linalg.LinAlgError(f"Residual {residual:.3e} exceeds tolerance")
        return x, residual

    try:
        if previous is None:
            raise np.linalg.LinAlgError("initial_factorization")
        if added+removed == 0:
            method = "unchanged"
            state = previous
        else:
            if previous.depth+bool(added)+bool(removed) >= refactor_interval:
                raise np.linalg.LinAlgError("periodic_refactorization")
            if added+removed > max_changed_fraction*max(len(target), len(old_ids)):
                raise np.linalg.LinAlgError("large_edit_direct_solve")
            state, _, _ = _changed(M, previous, target, condition_limit)
        x, residual = checked(state)
    except np.linalg.LinAlgError as exc:
        reason = str(exc)
        method = "initial_lu" if previous is None else "lu_fallback"
        state = State(target, Factor(M[np.ix_(target, target)], condition_limit))
        x, residual = checked(state)
    diagnostics = dict(method=method, reason=reason, added_dofs=added, removed_dofs=removed,
                       depth=state.depth, block_rcond_estimate=float(state.factor.rcond),
                       relative_residual=residual, solve_s=time.perf_counter()-start)
    return state, x, diagnostics
