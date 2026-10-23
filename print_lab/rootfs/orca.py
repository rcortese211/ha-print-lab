"""OrcaSlicer engine: Anycubic's own Kobra S1 profiles (built for the 0.8 mm nozzle by
tools/orca_profiles/build.py) sliced headless with the Orca CLI.

Overrides use PrusaSlicer's key names (what the bot, HA and older callers send); they are
translated here to Orca's keys and written into copies of the three profile files.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

ORCA = Path(os.environ.get("ORCA_APPRUN", "/opt/orcaslicer/AppRun"))
PROFILE_DIR = Path(__file__).resolve().parent / "profiles" / "orca"
QUALITIES = ("fast", "medium", "high")


class OrcaError(Exception):
    pass


def available() -> bool:
    return ORCA.is_file() and (PROFILE_DIR / "machine.json").is_file()


def _onoff(v):
    return "1" if str(v).strip().lower() in ("1", "true", "yes", "on") else "0"


# PrusaSlicer key -> (Orca key, value converter)
KEYS = {
    "layer_height": ("layer_height", str),
    "first_layer_height": ("initial_layer_print_height", str),
    "fill_density": ("sparse_infill_density", lambda v: v if str(v).endswith("%") else f"{v}%"),
    "fill_pattern": ("sparse_infill_pattern", str),
    "perimeters": ("wall_loops", str),
    "top_solid_layers": ("top_shell_layers", str),
    "bottom_solid_layers": ("bottom_shell_layers", str),
    "external_perimeter_speed": ("outer_wall_speed", str),
    "perimeter_speed": ("inner_wall_speed", str),
    "infill_speed": ("sparse_infill_speed", str),
    "ironing": ("ironing_type", lambda v: "top" if _onoff(v) == "1" else "no ironing"),
    "support_material": ("enable_support", _onoff),
    "support_material_auto": ("support_type", lambda v: "normal(auto)" if _onoff(v) == "1" else "normal(manual)"),
    "support_material_style": ("support_style", lambda v: {"grid": "grid", "snug": "snug"}.get(str(v), "default")),
    "brim_width": ("brim_width", str),
    "skirts": ("skirt_loops", str),
    "seam_position": ("seam_position", str),
    "retract_length": ("retraction_length", str),
    "temperature": ("nozzle_temperature", str),
    "first_layer_temperature": ("nozzle_temperature_initial_layer", str),
    "bed_temperature": ("textured_plate_temp", str),
    "first_layer_bed_temperature": ("textured_plate_temp_initial_layer", str),
}


def quality_for(overrides: dict) -> str:
    """Older callers pick quality by layer height; map that onto Anycubic's three processes."""
    try:
        lh = float(overrides.get("layer_height", 0.32))
    except ValueError:
        lh = 0.32
    return "fast" if lh >= 0.44 else "high" if lh <= 0.27 else "medium"


def _set(profiles: dict, key: str, value: str) -> None:
    for kind in ("process", "filament", "machine"):
        if key in profiles[kind]:
            old = profiles[kind][key]
            profiles[kind][key] = [value] * max(1, len(old)) if isinstance(old, list) else value
            return
    profiles["process"][key] = value


def slice_files(name: str, inputs: list[str], keep_positions: bool, quality: str,
                overrides: dict, out: Path, work: Path, timeout: int = 1800) -> dict:
    """Slice inputs (one object per file) into out. Returns Orca's result summary."""
    if quality not in QUALITIES:
        raise OrcaError(f"quality must be one of {', '.join(QUALITIES)}")
    profiles = {"machine": json.loads((PROFILE_DIR / "machine.json").read_text()),
                "process": json.loads((PROFILE_DIR / f"process_{quality}.json").read_text()),
                "filament": json.loads((PROFILE_DIR / "filament_pla.json").read_text())}
    applied = {}
    for k, v in overrides.items():
        ok, conv = KEYS.get(k, (k, str))
        val = conv(v)
        _set(profiles, ok, val)
        applied[ok] = val
        if k == "brim_width":
            _set(profiles, "brim_type", "outer_only" if float(v) > 0 else "no_brim")
    if profiles["process"].get("enable_support") == "1":
        # an 0.8 mm line can't make tree-support tips; Anycubic's own default is normal
        profiles["process"].setdefault("support_style", "grid")

    d = work / f"{name}.orca"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    for kind, data in profiles.items():
        (d / f"{kind}.json").write_text(json.dumps(data, indent=1))
    outdir = d / "out"
    outdir.mkdir()
    cmd = ["xvfb-run", "-a", str(ORCA), "--slice", "0", "--arrange", "0" if keep_positions else "1",
           "--load-settings", f"{d / 'machine.json'};{d / 'process.json'}",
           "--load-filaments", str(d / "filament.json"), "--outputdir", str(outdir), *inputs]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    result = {}
    try:
        result = json.loads((outdir / "result.json").read_text())
    except (OSError, ValueError):
        pass
    gcode = outdir / "plate_1.gcode"
    if p.returncode != 0 or not gcode.is_file():
        msg = result.get("error_string") or " | ".join((p.stdout + p.stderr).strip().splitlines()[-6:])
        raise OrcaError(f"OrcaSlicer couldn't slice it: {msg or 'no message'}")
    shutil.move(str(gcode), out)
    warn = "; ".join(w for w in (pl.get("warning_message") for pl in result.get("sliced_plates", [])) if w)
    return {"quality": quality, "overrides_applied": applied, "warnings": warn}
