"""Copy the upstream inputs and audit the selected Gmsh 2.2 physical group."""
from collections import Counter
import hashlib
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "library/source/examples/YagiUdaAntennas"


def prepare(source=None):
    example = (Path(source) / "examples/YagiUdaAntennas") if source else EXAMPLE
    target = ROOT / "test/geometry"
    target.mkdir(exist_ok=True)
    files = [
        example / "scuffgeoFiles/DipoleAntenna_Medium.scuffgeo",
        example / "mshFiles/PlanarYUAntenna_Medium.msh",
        example / "portFiles/Dipole.ports",
    ]
    for source in files:
        dest = target / source.name
        if dest.exists() and dest.read_bytes() != source.read_bytes():
            raise RuntimeError(f"Refusing to overwrite changed input: {dest}")
        if not dest.exists():
            shutil.copyfile(source, dest)
    mesh = files[1].read_text().splitlines()
    assert mesh[mesh.index("$MeshFormat") + 1] == "2.2 0 8"
    start = mesh.index("$Nodes") + 1
    nodes = {}
    for line in mesh[start + 1:start + 1 + int(mesh[start])]:
        fields = line.split()
        nodes[int(fields[0])] = tuple(map(float, fields[1:]))
    start = mesh.index("$Elements") + 1
    selected, counts = [], Counter()
    for line in mesh[start + 1:start + 1 + int(mesh[start])]:
        fields = list(map(int, line.split()))
        if fields[1] == 2:
            n_tags = fields[2]
            assert n_tags >= 1
            tag = fields[3]
            counts[tag] += 1
            if tag == 1:
                selected.append(fields[3 + n_tags:])
    assert selected and all(len(t) == 3 for t in selected)
    used = {v for tri in selected for v in tri}
    xyz = [nodes[v] for v in used]
    lower = [min(p[k] for p in xyz) for k in range(3)]
    upper = [max(p[k] for p in xyz) for k in range(3)]
    assert lower[0] == upper[0] == 0.0
    edges = Counter(tuple(sorted((tri[i], tri[(i + 1) % 3])))
                    for tri in selected for i in range(3))
    assert all(n in (1, 2) for n in edges.values())
    gap = 2 * min(abs(p[2]) for p in xyz)
    metadata = {
        "geometry_units": "mm", "mesh_format": "Gmsh 2.2 ASCII",
        "selected_meshtag": 1, "mesh_triangles_by_tag": dict(counts),
        "selected_triangles": len(selected),
        "expected_unknowns": sum(n == 2 for n in edges.values()),
        "bounding_box_mm": [lower, upper], "gap_mm": gap,
        "width_mm": upper[1] - lower[1],
        "arm_length_mm": (upper[2] - lower[2] - gap) / 2,
        "tip_to_tip_mm": upper[2] - lower[2],
        "sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
    }
    (target / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(json.dumps(metadata, indent=2))
    return metadata


if __name__ == "__main__":
    prepare()
