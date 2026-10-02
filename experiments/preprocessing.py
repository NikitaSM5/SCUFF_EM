"""Reuse the existing SCUFF full-domain cache with solver/source fingerprints."""
from pathlib import Path
import time
import uuid

from gui.topology_config import cache_signature, read_cache_manifest
from gui.topology_workflow import prepare_cache
from .storage import CalculationCache, file_hash, key

ROOT = Path(__file__).resolve().parents[1]


def source_fingerprint():
    files = list((ROOT / "antenna").glob("*.py"))+list((ROOT / "feko").glob("*.py"))
    files += [ROOT / "experiments/schur_backend.py", ROOT / "experiments/feko_backend.py", ROOT / "gui/calculation.py"]
    files += [ROOT / "feko/create_model.lua", ROOT / "antenna/bin/export_planar.exe"]
    return key({str(p.relative_to(ROOT)): file_hash(p) for p in sorted(files)})


def prepare(project, settings, directory, journal, cancel):
    start = time.perf_counter()
    identity = dict(signature=cache_signature(project), sources=source_fingerprint())
    cache = CalculationCache(settings["cache_dir"], "full-matrix-index")
    found = cache.get(identity) if settings["use_cache"] else None
    manifest = Path(found["manifest"]) if found else None
    if manifest is None and project.get("topology_cache"):
        selected = Path(project["topology_cache"])
        data = read_cache_manifest(selected)
        if data["signature"] == identity["signature"]:
            # Original caches include the native executable fingerprint in every system metadata.
            import numpy as np
            import json
            with np.load(selected.parent / data["systems"][0]["file"], allow_pickle=False) as content:
                metadata = json.loads(str(content["metadata_json"]))
            if metadata["native_sha256"] == file_hash(ROOT / "antenna/bin/export_planar.exe"):
                manifest = selected
    hit = manifest is not None
    if manifest is None:
        cancel()
        folder = Path(settings["cache_dir"]) / "full-matrices" / (key(identity)[:16]+"-"+uuid.uuid4().hex[:8])
        folder.mkdir(parents=True)
        manifest = prepare_cache(project, folder, journal.event, cancel=cancel)
    data = read_cache_manifest(manifest)
    if data["signature"] != identity["signature"]:
        raise ValueError("Full matrix cache physics mismatch")
    artifacts = [dict(path=str(manifest.resolve()), sha256=file_hash(manifest))]
    for entry in data["systems"]:
        file = manifest.parent / entry["file"]
        if file_hash(file) != entry["sha256"]:
            raise ValueError("Full matrix cache checksum mismatch")
        artifacts.append(dict(path=str(file.resolve()), sha256=entry["sha256"]))
    cache.put(identity, dict(manifest=str(manifest.resolve()), artifacts=artifacts))
    elapsed = time.perf_counter()-start
    journal.event("log", message=f"SCUFF preprocessing: {elapsed:.3f} s; full-matrix cache {'hit' if hit else 'miss'}")
    return manifest, dict(elapsed_s=elapsed, cache_hit=hit, identity=identity, unknowns=data["unknowns"])
