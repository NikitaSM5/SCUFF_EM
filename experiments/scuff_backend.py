"""Optional full native SCUFF reassembly on the identical retained master triangles."""
import copy
from pathlib import Path
import shutil
import time

from antenna import solve_antenna, save_result
from antenna.geometry import AntennaGeometry, _sha256
from antenna.topology import active_dofs, write_subset_mesh
from gui.calculation import result_row


def full_recomputation(schur, cells, directory):
    directory = Path(directory)
    rows, timings, paths = [], [], []
    for index, full in enumerate(schur.full):
        started = time.monotonic()
        folder = directory / f"frequency-{index:04d}"
        mesh = folder / "geometry"
        mesh.mkdir(parents=True)
        # The full result retains the original geometry input hashes; locate via its saved command.
        original = Path(full.metadata["command"][1]).parent
        for name in ("antenna.scuffgeo", "feed.ports", "board.substrate"):
            shutil.copyfile(original / name, mesh / name)
        write_subset_mesh(full, cells, mesh / "metal.msh")
        metadata = copy.deepcopy(full.metadata["geometry"])
        active, panels = active_dofs(full, cells)
        metadata["cells"] = cells
        metadata["mesh"].update(unknowns=len(active), triangles=len(panels))
        metadata["sha256"] = {name: _sha256(mesh / name) for name in metadata["sha256"]}
        geometry = AntennaGeometry(mesh, metadata)
        result = solve_antenna(geometry, full.metadata["frequency_ghz"], work_dir=folder / "calculation",
                               max_unknowns=schur.project["max_unknowns"], timeout_s=schur.project["timeout_s"], cancel=schur.cancel)
        paths.append(str(save_result(result, folder / "system.npz")))
        rows.append(result_row(result, schur.project, folder, schur.journal.event, started))
        timings.append(time.monotonic()-started)
    return dict(rows=rows, total_s=sum(timings), system_files=paths)
