"""Read FEKO's own elapsed solver time, independently of launcher/startup time."""
import math
from pathlib import Path
import re


NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[EeDd][+-]?\d+)?"
ROW = re.compile(r"^\s*(.*?)\s+("+NUMBER+r")\s+("+NUMBER+r")\s*$")


def read_solver_timing(path):
    sections, current = [], None
    for line in Path(path).read_text(encoding="utf-8-sig", errors="replace").splitlines():
        if "SUMMARY OF REQUIRED TIMES IN SECONDS" in line:
            current = {}
        elif current is not None:
            match = ROW.match(line)
            if match:
                label, cpu, wall = match.groups()
                values = dict(cpu_s=float(cpu.replace("D", "E")), runtime_s=float(wall.replace("D", "E")))
                if not all(math.isfinite(v) and v >= 0 for v in values.values()):
                    raise ValueError("Invalid FEKO solver timing")
                current[label.strip().rstrip(":")] = values
                if label.strip() == "total times:":
                    sections.append(current)
                    current = None
    if not sections:
        raise ValueError("FEKO .out contains no complete solver timing summary")
    # The last complete summary is cumulative across the solved configurations/frequencies.
    stages = sections[-1]
    total = stages["total times"]
    return dict(compute=total["runtime_s"], solver_cpu=total["cpu_s"],
                assembly=stages.get("Calculation of matrix elements", {}).get("runtime_s", 0),
                linear_solve=stages.get("Solution of the system of linear equations", {}).get("runtime_s", 0),
                far_field=stages.get("Calculation of far field", {}).get("runtime_s", 0),
                timing_source="FEKO .out runtime (last complete cumulative summary)",
                timing_resolution_s=.001, solver_stages=stages)


def available_solver_timing(directory):
    path = Path(directory)/"antenna.out"
    if not path.is_file():
        return dict(compute=None, timing_error="FEKO antenna.out missing")
    try:
        return read_solver_timing(path)
    except ValueError as exc:
        return dict(compute=None, timing_error=str(exc))
