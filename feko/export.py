"""Generate native CAD geometry, not a triangulated SCUFF mesh import."""
import json
from pathlib import Path

from .config import validate_options
from antenna.feed import MODEL, metal_rectangles, planar_feed


def build_model_data(project):
    from gui.project import validate_project
    validate_project(project, for_run=True)
    options = validate_options(project.get("feko", {}), project)
    cells, a = project["cells"], project["cell_size_mm"]
    n = len(cells)
    rectangles = metal_rectangles(cells, a)
    r, c = project["feed_cell"]
    return dict(api_target="CADFEKO 2022.2 / 2024", units="mm", rectangles=rectangles,
                feed_position_mm=[(c+.5-n/2)*a, (n/2-r-.5)*a, 0.0],
                thickness_mm=project["thickness_mm"], epsilon_r=project["epsilon_r"],
                loss_tangent=project["loss_tangent"], frequency_hz=project["frequency_ghz"]*1e9,
                mesh_size_mm=project["mesh_size_mm"] or a/2, options=options,
                feed_model=MODEL, feed=planar_feed(cells, a, project["feed_cell"]),
                ground_model="infinite_PEC_at_minus_thickness",
                gain_definition="gain_not_realised_gain_upper_hemisphere")


def _lua_number(value):
    return format(value, ".17g")


def render_script(project):
    model = build_model_data(project)
    options = model["options"]
    numbers = {"a_mesh": model["mesh_size_mm"], "h": model["thickness_mm"],
               "eps_r": model["epsilon_r"], "tan_delta": model["loss_tangent"],
               "feed_x": model["feed_position_mm"][0], "feed_y": model["feed_position_mm"][1],
               "feed_width": model["feed"]["width_mm"], "z0": options["reference_ohm"],
               "frequency": model["frequency_hz"], "f_start": options["start_ghz"]*1e9,
               "f_stop": options["stop_ghz"]*1e9, "f_count": options["frequency_points"],
               "angle_step": options["angle_step_deg"]}
    parameters = [f"local {key} = {_lua_number(value)}" for key, value in numbers.items()]
    parameters += [f"local sweep = {str(options['sweep_enabled']).lower()}",
                   f"local compute_pattern = {str(options['compute_pattern']).lower()}",
                   f"local run_solver = {str(options['run_solver']).lower()}", "local pixels = {"]
    for rect in model["rectangles"]:
        parameters.append("  {" + ", ".join(_lua_number(rect[k]) for k in ("x", "y", "width", "depth")) + "},")
    parameters.append("}")
    template = Path(__file__).with_name("create_model.lua").read_text(encoding="utf-8")
    return template.replace("-- GENERATED_PARAMETERS", "\n".join(parameters), 1)


def export_model(project, directory):
    directory = Path(directory).resolve()
    model = build_model_data(project)
    script = render_script(project)
    if directory.exists() and (not directory.is_dir() or any(directory.iterdir())):
        raise FileExistsError(f"FEKO export directory must be empty: {directory}")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "create_model.lua").write_text(script, encoding="utf-8")
    (directory / "model.json").write_text(json.dumps(model, indent=2, ensure_ascii=False, allow_nan=False)+"\n", encoding="utf-8")
    return directory / "create_model.lua"
