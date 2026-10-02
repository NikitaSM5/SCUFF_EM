"""Opt-in real FEKO run: verify that saved results survive its automatic exit."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from feko.runner import run_feko


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("project", type=Path)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    project = json.loads(args.project.read_text(encoding="utf-8"))
    project["feko"] = dict(project.get("feko", {}), run_solver=True)
    owned, events = [], []
    start = subprocess.Popen

    def launch(*a, **kw):
        process = start(*a, **kw)
        owned.append(process)
        print("Owned CADFEKO PID:", process.pid, flush=True)
        return process

    def emit(event, **fields):
        events.append(dict(event=event, **fields))
        print(event, fields.get("message", ""), flush=True)

    with patch("feko.runner.subprocess.Popen", side_effect=launch):
        run_feko(project, args.directory, emit)
    assert len(owned) == 1, "Expected one CADFEKO process, with no forced taskkill fallback"
    assert owned[0].poll() == 0, "CADFEKO did not exit cleanly"
    assert events[-1]["status"] == "solved"
    for name in ("antenna.cfx", "summary.json", "summary.csv"):
        assert (args.directory / "feko" / name).stat().st_size > 0
    print("PASS: CADFEKO exited cleanly; model and results retained.", flush=True)


if __name__ == "__main__":
    main()
