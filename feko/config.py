"""FEKO-only settings; old SCUFF projects need no migration."""
import math

ANGLE_STEPS = (1, 2, 5, 10, 15, 30)


def default_options():
    return dict(cadfeko_exe="", reference_ohm=50.0,
                sweep_enabled=False, start_ghz=2.0, stop_ghz=4.0, frequency_points=51,
                angle_step_deg=5, run_solver=True, compute_pattern=True, timeout_s=3600)


def validate_options(options, project=None):
    if not isinstance(options, dict):
        raise ValueError("FEKO settings must be an object")
    values = dict(default_options(), **options)
    values.pop("feed_diameter_mm", None)  # Accepted only for loading legacy projects.
    for key, lo, hi in (("reference_ohm", 0.001, 100000),
                        ("start_ghz", 0.000001, 10000), ("stop_ghz", 0.000001, 10000)):
        v = values[key]
        if type(v) not in (int, float) or not math.isfinite(v) or not lo <= v <= hi:
            raise ValueError(f"Invalid FEKO {key}: expected {lo} .. {hi}")
    for key, lo, hi in (("frequency_points", 2, 501), ("timeout_s", 10, 86400)):
        if type(values[key]) is not int or not lo <= values[key] <= hi:
            raise ValueError(f"Invalid FEKO {key}")
    if type(values["angle_step_deg"]) is not int or values["angle_step_deg"] not in ANGLE_STEPS:
        raise ValueError("Invalid FEKO far-field angular step")
    for key in ("sweep_enabled", "run_solver", "compute_pattern"):
        if type(values[key]) is not bool:
            raise ValueError(f"Invalid FEKO {key}")
    if not isinstance(values["cadfeko_exe"], str):
        raise ValueError("CADFEKO path must be a string")
    if values["sweep_enabled"] and values["stop_ghz"] <= values["start_ghz"]:
        raise ValueError("FEKO: конец диапазона должен быть больше начала.")
    return values
