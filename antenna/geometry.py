"""Conformal Gmsh meshes for zero-thickness PEC pixels in the z=0 plane."""
from collections import Counter
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
from threading import Lock

import numpy as np
from .feed import planar_feed

_GMSH_LOCK = Lock()
_INPUT_FILES = ("metal.msh", "antenna.scuffgeo", "feed.ports", "board.substrate")


def _positive(value, name):
    if isinstance(value, (bool, np.bool_)):
        raise ValueError(f"{name} must be a finite positive number")
    value = float(value)
    if not np.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return value


@dataclass(frozen=True)
class Substrate:
    """Infinite dielectric slab at -thickness_mm < z < 0; PEC ground below."""
    thickness_mm: float = 5.0
    epsilon_r: float = 4.4
    loss_tangent: float = 0.0

    def __post_init__(self):
        for name in ("thickness_mm", "epsilon_r"):
            object.__setattr__(self, name, _positive(getattr(self, name), name))
        loss = float(self.loss_tangent)
        if not np.isfinite(loss) or loss < 0:
            raise ValueError("loss_tangent must be finite and nonnegative")
        object.__setattr__(self, "loss_tangent", loss)


@dataclass(frozen=True)
class AntennaGeometry:
    directory: Path
    metadata: dict

    @property
    def scuffgeo(self):
        return self.directory / "antenna.scuffgeo"

    @property
    def port_file(self):
        return self.directory / "feed.ports"

    @property
    def substrate_file(self):
        return self.directory / "board.substrate"

    @property
    def unknowns(self):
        return self.metadata["mesh"]["unknowns"]

    def validate_files(self):
        for name in _INPUT_FILES:
            path = self.directory / name
            if not path.is_file() or _sha256(path) != self.metadata["sha256"][name]:
                raise ValueError(f"Geometry input changed or missing: {path}")


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_grid(cells, feed_cell):
    grid = np.asarray(cells)
    if grid.ndim != 2 or not grid.shape[0] or grid.shape[0] != grid.shape[1]:
        raise ValueError("cells must be a nonempty square n x n matrix")
    if grid.dtype.kind not in "buif" or not np.isin(grid, [0, 1]).all():
        raise ValueError("cells must contain only numeric 0 and 1")
    if (not isinstance(feed_cell, (tuple, list, np.ndarray)) or len(feed_cell) != 2 or any(isinstance(v, (bool, np.bool_)) or
            not isinstance(v, (int, np.integer)) for v in feed_cell)):
        raise ValueError("feed_cell must be (row, column), zero-based integer indices")
    row, col = map(int, feed_cell)
    if not (0 <= row < len(grid) and 0 <= col < len(grid)):
        raise ValueError("feed_cell is outside the grid")
    if grid[row, col] != 1:
        raise ValueError("The feed cell must contain metal (1)")
    # A diagonal-only contact is not a finite-width electrical connection.
    for r in range(len(grid) - 1):
        for c in range(len(grid) - 1):
            block = grid[r:r+2, c:c+2]
            if block.sum() == 2 and block[0, 0] == block[1, 1]:
                raise ValueError(f"Point-only metal contact near ({r}, {c}); add a bridge or a gap")
    return grid.astype(np.uint8), (row, col)


def _mesh_pixels(grid, a, feed, mesh_size, path):
    import gmsh

    with _GMSH_LOCK:
        if gmsh.isInitialized():
            raise RuntimeError("An external Gmsh session is active; finalize it before meshing")
        gmsh.initialize([], readConfigFiles=False)
        try:
            gmsh.option.setNumber("General.Terminal", 0)
            gmsh.option.setNumber("General.NumThreads", 1)
            gmsh.option.setNumber("Mesh.MshFileVersion", 2.2)
            gmsh.option.setNumber("Mesh.Binary", 0)
            gmsh.option.setNumber("Mesh.ElementOrder", 1)
            gmsh.option.setNumber("Mesh.Algorithm", 6)
            gmsh.option.setNumber("Mesh.RandomSeed", 1)
            gmsh.option.setNumber("Mesh.MeshSizeMin", mesh_size)
            gmsh.option.setNumber("Mesh.MeshSizeMax", mesh_size)
            gmsh.model.add("pixel_antenna")
            geo = gmsh.model.geo
            points, curves, surfaces = {}, {}, {}
            n = len(grid)

            def vertex(r, c):
                key = (r, c)
                if key not in points:
                    points[key] = geo.addPoint((c - n/2)*a, (n/2 - r)*a, 0, mesh_size)
                return points[key]

            def edge(p, q):
                key = tuple(sorted((p, q)))
                if key not in curves:
                    curves[key] = geo.addLine(*key)
                return curves[key] if p < q else -curves[key]

            for r, c in np.argwhere(grid):
                for half in (0, 1):
                    left, right = c+half/2, c+(half+1)/2
                    corners = [vertex(r+1, left), vertex(r+1, right), vertex(r, right), vertex(r, left)]
                    loop = geo.addCurveLoop([edge(corners[i], corners[(i+1) % 4]) for i in range(4)])
                    surfaces[int(r), int(c), half] = geo.addPlaneSurface([loop])
            port = planar_feed(grid, a, feed)
            feed_xyz = port["center_mm"]
            geo.synchronize()
            gmsh.model.addPhysicalGroup(2, list(surfaces.values()), 1, "metal")
            gmsh.model.mesh.generate(2)
            tags, coords, _ = gmsh.model.mesh.getNodes()
            vertices = np.asarray(coords).reshape(-1, 3)
            lookup = {int(tag): i for i, tag in enumerate(tags)}
            kinds, _, element_nodes = gmsh.model.mesh.getElements(2)
            if list(kinds) != [2]:
                raise RuntimeError("Expected only first-order triangles from Gmsh")
            triangles = np.array([lookup[int(tag)] for tag in element_nodes[0]], dtype=np.int64).reshape(-1, 3)
            stats = _audit_mesh(vertices, triangles, port, int(grid.sum()) * a*a)
            gmsh.write(str(path))
            return feed_xyz, stats, gmsh.__version__
        finally:
            gmsh.finalize()


def _audit_mesh(vertices, triangles, port, expected_area):
    xyz = vertices[triangles]
    signed_area = np.cross(xyz[:, 1] - xyz[:, 0], xyz[:, 2] - xyz[:, 0])[:, 2] / 2
    if not np.isfinite(vertices).all() or not (signed_area > 0).all():
        raise RuntimeError("Invalid or inconsistently oriented triangles")
    area = float(signed_area.sum())
    if not np.isclose(area, expected_area, rtol=1e-10, atol=1e-14):
        raise RuntimeError("Mesh area does not match the metal pixels")
    edges = Counter(tuple(sorted((int(t[i]), int(t[(i+1) % 3])))) for t in triangles for i in range(3))
    if any(count not in (1, 2) for count in edges.values()):
        raise RuntimeError("Non-manifold mesh")
    start, end = np.array(port["start_mm"]), np.array(port["end_mm"])
    tolerance = port["width_mm"]*1e-9
    port_edges = []
    for edge, count in edges.items():
        p = vertices[list(edge)]
        if (np.all(np.abs(p[:, 0]-start[0]) < tolerance) and
                np.all(p[:, 1] >= start[1]-tolerance) and np.all(p[:, 1] <= end[1]+tolerance)):
            if count != 2:
                raise RuntimeError("Voltage gap must separate two metal faces")
            port_edges.append(np.linalg.norm(p[1]-p[0]))
    if not np.isclose(sum(port_edges), port["width_mm"], rtol=1e-9):
        raise RuntimeError("Incomplete voltage-gap mesh seam")
    return {"vertices": len(vertices), "triangles": len(triangles),
            "unknowns": sum(count == 2 for count in edges.values()),
            "area_mm2": area, "port_edges": len(port_edges)}


def create_planar_antenna(cells, cell_size_mm, feed_cell, directory, *,
                          substrate=Substrate(), mesh_size_mm=None):
    """Mesh binary square pixels with a differential 1 V delta-gap feed.

    cells[row, column]: 1 = PEC metal, 0 = empty. Indices start at zero.
    Columns increase along +x, rows along -y; the grid is centered at (0,0,0).
    The source crosses the selected cell along y, feeding its +x half against
    its -x half. This is an ideal zero-width voltage gap, not a grounded probe.
    The infinite dielectric/ground are implicit, not meshed.
    directory must be empty. mesh_size_mm defaults to half the pixel side.
    """
    grid, feed = _validate_grid(cells, feed_cell)
    a = _positive(cell_size_mm, "cell_size_mm")
    h = _positive(a/2 if mesh_size_mm is None else mesh_size_mm, "mesh_size_mm")
    if not isinstance(substrate, Substrate):
        raise TypeError("substrate must be a Substrate instance")
    directory = Path(directory).resolve()
    if directory.exists() and (not directory.is_dir() or any(directory.iterdir())):
        raise FileExistsError(f"Geometry directory must be empty: {directory}")
    directory.mkdir(parents=True, exist_ok=True)
    xyz, stats, version = _mesh_pixels(grid, a, feed, h, directory / "metal.msh")
    port = planar_feed(grid, a, feed)
    (directory / "antenna.scuffgeo").write_text(
        "OBJECT Metal\n MESHFILE metal.msh\n MESHTAG 1\nENDOBJECT\n", encoding="ascii")
    (directory / "feed.ports").write_text(
        "DELTA_GAP " + " ".join(format(v, ".17g") for v in port["start_mm"]+port["end_mm"]) + "\n",
        encoding="ascii")
    # SCUFF uses exp(-i*omega*t), so a passive dielectric has Im(epsilon) >= 0.
    eps_imag = substrate.epsilon_r * substrate.loss_tangent
    (directory / "board.substrate").write_text(
        f"0 CONST_EPS_{substrate.epsilon_r:.17g}+{eps_imag:.17g}i\n"
        f"{-substrate.thickness_mm:.17g} GROUNDPLANE\n", encoding="ascii")
    metadata = {"schema_version": 1, "model": "pec_pixels_infinite_grounded_dielectric_delta_gap",
                "units": "mm", "cells": grid.tolist(), "cell_size_mm": a, "feed_cell": list(feed),
                "feed_position_mm": xyz, "feed": port, "substrate": asdict(substrate), "mesh_size_mm": h,
                "mesh": stats, "gmsh_version": version,
                "sha256": {name: _sha256(directory / name) for name in _INPUT_FILES}}
    (directory / "geometry.json").write_text(json.dumps(metadata, indent=2, allow_nan=False) + "\n")
    return AntennaGeometry(directory, metadata)


def load_geometry(directory):
    """Load a previously generated geometry and check its input file hashes."""
    directory = Path(directory).resolve()
    metadata = json.loads((directory / "geometry.json").read_text())
    if metadata.get("schema_version") != 1:
        raise ValueError("Unsupported geometry schema")
    geometry = AntennaGeometry(directory, metadata)
    geometry.validate_files()
    return geometry
