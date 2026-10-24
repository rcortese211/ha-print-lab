"""Build Print Lab's Kobra S1 OrcaSlicer profiles from Anycubic's own Orca profiles.

    ANYCUBIC=<OrcaSlicer>/resources/profiles/Anycubic python3 build.py <out dir>

writes <out dir>/<nozzle>/{machine,process_fast,process_medium,process_high,filament_pla}.json
for each nozzle Print Lab supports.

0.4 mm: Anycubic's own Kobra S1 0.4 machine, PLA and 0.20 mm process; fast and high come from
        the S1 Max 0.4 processes (0.28 mm and 0.12 mm), the S1 has no others.
0.8 mm: the S1 0.4 machine with the nozzle-dependent values of the S1 Max 0.8 profile, and the
        S1 Max 0.8 processes and PLA (same S1-family hotend), with the S1's exhaust-fan values.

The Orca CLI only accepts a process whose compatible_printers names the machine's "system" name,
so these are marked from=system (the CLI then uses each profile's own name). "nil" values mean
"use the default" in the GUI but are rejected by the CLI, so they're dropped.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from flatten import flat  # noqa: E402

S1_04 = "Anycubic Kobra S1 0.4 nozzle"
NOZZLES = {
    "0.4": {
        "machine_from": None,
        "procs": {"fast": "0.28mm Standard @Anycubic Kobra S1 Max 0.4 nozzle",
                  "medium": "0.20mm Standard @Anycubic Kobra S1 0.4 nozzle",
                  "high": "0.12mm Standard @Anycubic Kobra S1 Max 0.4 nozzle"},
        "filament": "Anycubic PLA @Anycubic Kobra S1 0.4 nozzle",
    },
    "0.8": {
        "machine_from": "Anycubic Kobra S1 Max 0.8 nozzle",
        "procs": {"fast": "0.48mm Standard @Anycubic Kobra S1 Max 0.8 nozzle",
                  "medium": "0.32mm Standard @Anycubic Kobra S1 Max 0.8 nozzle",
                  "high": "0.24mm Standard @Anycubic Kobra S1 Max 0.8 nozzle"},
        "filament": "Anycubic PLA @Anycubic Kobra S1 Max 0.8 nozzle",
    },
}
SYSTEM = {"inherits": "", "from": "system"}


def no_nil(d):
    return {k: v for k, v in d.items() if v != "nil" and v != ["nil"]}


def build(nozzle, spec):
    name = f"Anycubic Kobra S1 {nozzle} nozzle (Print Lab)"
    m = flat("machine", S1_04)
    if spec["machine_from"]:
        other = flat("machine", spec["machine_from"])
        for k in ("nozzle_diameter", "max_layer_height", "min_layer_height", "wipe_distance", "nozzle_volume"):
            m[k] = other[k]
    m.update(name=name, printer_settings_id=name, printer_variant=nozzle, default_print_profile="",
             default_filament_profile=[], default_bed_type="Textured PEI Plate", **SYSTEM)
    m.pop("setting_id", None)
    out = {"machine": m}
    for q, pname in spec["procs"].items():
        p = flat("process", pname)
        p.update(compatible_printers=[name], name=f"Print Lab {q} {nozzle}",
                 print_settings_id=f"Print Lab {q} {nozzle}", curr_bed_type="Textured PEI Plate", **SYSTEM)
        p.pop("setting_id", None)
        out[f"process_{q}"] = p
    f = flat("filament", spec["filament"])
    s1 = flat("filament", "Anycubic PLA @Anycubic Kobra S1 0.4 nozzle")
    for k in ("activate_air_filtration", "during_print_exhaust_fan_speed", "complete_print_exhaust_fan_speed"):
        f[k] = s1[k]
    f.update(compatible_printers=[name], name=f"Print Lab PLA {nozzle}",
             filament_settings_id=[f"Print Lab PLA {nozzle}"], **SYSTEM)
    f.pop("setting_id", None)
    out["filament_pla"] = f
    return out


def main(dest):
    for nozzle, spec in NOZZLES.items():
        d = os.path.join(dest, nozzle)
        os.makedirs(d, exist_ok=True)
        for k, v in build(nozzle, spec).items():
            with open(os.path.join(d, f"{k}.json"), "w") as fh:
                json.dump(no_nil(v), fh, indent=1, sort_keys=True)
        print("wrote", d)


if __name__ == "__main__":
    main(sys.argv[1])
