"""OrcaSlicer engine: Anycubic's own Kobra S1 profiles, one set per nozzle size (built by
tools/orca_profiles/build.py into profiles/orca/<nozzle>/), sliced headless with the Orca CLI.

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


def nozzle_key(nozzle) -> str:
    """0.4, "0.40", 0.4000001 -> "0.4"."""
    return f"{float(nozzle):.1f}"


def supported_nozzles() -> list[str]:
    if not PROFILE_DIR.is_dir():
        return []
    return sorted(d.name for d in PROFILE_DIR.iterdir() if (d / "machine.json").is_file())


def profile_dir(nozzle) -> Path:
    d = PROFILE_DIR / nozzle_key(nozzle)
    if not (d / "machine.json").is_file():
        have = ", ".join(f"{n} mm" for n in supported_nozzles()) or "none"
        raise OrcaError(f"No Print Lab profile for a {nozzle_key(nozzle)} mm nozzle yet (have: {have})")
    return d


def available(nozzle=None) -> bool:
    if not ORCA.is_file():
        return False
    return bool(supported_nozzles()) if nozzle is None else nozzle_key(nozzle) in supported_nozzles()


def _first(v):
    return v[0] if isinstance(v, list) else v


def retraction_length(nozzle) -> float:
    return float(_first(json.loads((profile_dir(nozzle) / "machine.json").read_text())
                        .get("retraction_length", ["0"])))


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


def quality_for(overrides: dict, nozzle) -> str:
    """Older callers pick quality by layer height: take the process with the nearest one."""
    d = profile_dir(nozzle)
    heights = {q: float(_first(json.loads((d / f"process_{q}.json").read_text())["layer_height"]))
               for q in QUALITIES}
    try:
        lh = float(overrides["layer_height"])
    except (KeyError, ValueError):
        return "medium"
    return min(heights, key=lambda q: abs(heights[q] - lh))


def _set(profiles: dict, key: str, value: str) -> None:
    for kind in ("process", "filament", "machine"):
        if key in profiles[kind]:
            old = profiles[kind][key]
            profiles[kind][key] = [value] * max(1, len(old)) if isinstance(old, list) else value
            return
    profiles["process"][key] = value


def slice_files(name: str, inputs: list[str], keep_positions: bool, quality: str,
                overrides: dict, out: Path, work: Path, nozzle, filaments: list | None = None,
                filament_ids: list[int] | None = None, timeout: int = 1800) -> dict:
    """Slice inputs (one object per file) into out with the profiles for this nozzle.

    filaments: colours in order ({color, material, temp?}); filament_ids: the colour (1-based)
    of each input. One colour per object; the printer's ACE does the swaps (no prime tower,
    the firmware purges into its chute). Returns Orca's result summary."""
    if quality not in QUALITIES:
        raise OrcaError(f"quality must be one of {', '.join(QUALITIES)}")
    pdir = profile_dir(nozzle)
    profiles = {"machine": json.loads((pdir / "machine.json").read_text()),
                "process": json.loads((pdir / f"process_{quality}.json").read_text()),
                "filament": json.loads((pdir / "filament_pla.json").read_text())}
    applied = {}
    for k, v in overrides.items():
        ok, conv = KEYS.get(k, (k, str))
        val = conv(v)
        _set(profiles, ok, val)
        applied[ok] = val
        if k == "brim_width":
            _set(profiles, "brim_type", "outer_only" if float(v) > 0 else "no_brim")
    if profiles["process"].get("enable_support") == "1":
        # grid supports: tree tips are too fine for the big nozzles, and it's Anycubic's default
        profiles["process"].setdefault("support_style", "grid")

    d = work / f"{name}.orca"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    fil_files = [d / "filament.json"]
    if filaments and len(filaments) > 1:
        # several colours: one filament file each, no prime tower (the ACE purges at the chute)
        profiles["process"]["enable_prime_tower"] = "0"
        fil_files = []
        for i, f in enumerate(filaments, 1):
            fil = json.loads(json.dumps(profiles["filament"]))
            fil["name"] = f"{fil['name']} {i}"
            fil["filament_settings_id"] = [fil["name"]]
            fil["filament_colour"] = [f["color"].upper()]
            if f.get("temp"):
                fil["nozzle_temperature"] = [str(f["temp"])]
                fil["nozzle_temperature_initial_layer"] = [str(f["temp"])]
            fil_files.append(d / f"filament_{i}.json")
            fil_files[-1].write_text(json.dumps(fil, indent=1))
    elif filaments:
        profiles["filament"]["filament_colour"] = [filaments[0]["color"].upper()]
        if filaments[0].get("temp"):
            profiles["filament"]["nozzle_temperature"] = [str(filaments[0]["temp"])]
            profiles["filament"]["nozzle_temperature_initial_layer"] = [str(filaments[0]["temp"])]
    for kind, data in profiles.items():
        if kind == "filament" and len(fil_files) > 1:
            continue
        (d / f"{kind}.json").write_text(json.dumps(data, indent=1))
    outdir = d / "out"
    outdir.mkdir()
    cmd = ["xvfb-run", "-a", str(ORCA), "--slice", "0", "--arrange", "0" if keep_positions else "1",
           "--load-settings", f"{d / 'machine.json'};{d / 'process.json'}",
           "--load-filaments", ";".join(str(f) for f in fil_files), "--outputdir", str(outdir)]
    if filaments and len(filaments) > 1 and filament_ids:
        if len(filament_ids) != len(inputs):
            raise OrcaError("internal: one colour per piece needed")
        cmd += ["--load-filament-ids", ",".join(str(int(i)) for i in filament_ids)]
    cmd += inputs
    # cwd: Orca drops log files in the working directory
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=d)
    result = {}
    try:
        result = json.loads((outdir / "result.json").read_text())
    except (OSError, ValueError):
        pass
    gcode = outdir / "plate_1.gcode"
    if p.returncode != 0 or not gcode.is_file():
        print(f"[orca] failed: {' '.join(cmd)}\n{(p.stdout + p.stderr)[-3000:]}", flush=True)
        msg = result.get("error_string") or " | ".join((p.stdout + p.stderr).strip().splitlines()[-6:])
        raise OrcaError(f"OrcaSlicer couldn't slice it: {msg or 'no message'}")
    shutil.move(str(gcode), out)
    warn = "; ".join(w for w in (pl.get("warning_message") for pl in result.get("sliced_plates", [])) if w)
    return {"quality": quality, "nozzle": nozzle_key(nozzle),
            "layer_height": _first(profiles["process"].get("layer_height")),
            "overrides_applied": applied, "warnings": warn}
