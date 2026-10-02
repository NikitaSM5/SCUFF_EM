"""Cache compatibility checks shared by the GUI and its worker, without NumPy."""
import json
from pathlib import Path

from feko.config import validate_options


def cache_signature(config):
    options = validate_options(config.get("feko", {}))
    if options["sweep_enabled"]:
        first, last, count = options["start_ghz"], options["stop_ghz"], options["frequency_points"]
        frequencies = [first+(last-first)*i/(count-1) for i in range(count)]
    else:
        frequencies = [config["frequency_ghz"]]
    return dict(n=len(config["cells"]), cell_size_mm=config["cell_size_mm"],
                mesh_size_mm=config["mesh_size_mm"] or config["cell_size_mm"]/2,
                thickness_mm=config["thickness_mm"], epsilon_r=config["epsilon_r"],
                loss_tangent=config["loss_tangent"], feed_cell=list(config["feed_cell"]) if config["feed_cell"] is not None else None,
                frequencies_ghz=frequencies, feed_model="planar_delta_gap_voltage")


def read_cache_manifest(path):
    path = Path(path).resolve()
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or data.get("kind") != "scuff_topology_cache":
        raise ValueError("Выберите файл topology-cache.json полной матрицы.")
    if data.get("signature") != cache_signature(data["project"]):
        raise ValueError("Повреждены параметры полной матрицы.")
    if len(data["systems"]) != len(data["signature"]["frequencies_ghz"]):
        raise ValueError("Неполный набор частот в кэше.")
    for entry in data["systems"]:
        target = (path.parent / entry["file"]).resolve()
        if not target.is_relative_to(path.parent) or not target.is_file():
            raise ValueError("Файлы полной матрицы отсутствуют или находятся вне папки кэша.")
    return data
