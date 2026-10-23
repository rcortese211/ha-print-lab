"""Build Print Lab's Kobra S1 0.8 mm OrcaSlicer profiles from Anycubic's own Orca profiles.
# The Orca CLI only accepts a process whose compatible_printers names the machine's "system" name,
# so these are marked from=system (the CLI then uses each profile's own name).

Machine: Anycubic Kobra S1 (0.4) with the nozzle-dependent values of the S1 Max 0.8 profile.
Process/filament: Anycubic's S1 Max 0.8 nozzle profiles (same S1-family hotend and nozzle),
with the S1's own exhaust-fan values.
"""
import json, sys, os
sys.path.insert(0, os.path.dirname(__file__))
from flatten import flat
NAME = "Anycubic Kobra S1 0.8 nozzle (Print Lab)"

m = flat("machine", "Anycubic Kobra S1 0.4 nozzle")
m8 = flat("machine", "Anycubic Kobra S1 Max 0.8 nozzle")
for k in ("nozzle_diameter", "max_layer_height", "min_layer_height", "wipe_distance", "nozzle_volume"):
    m[k] = m8[k]
m.update(name=NAME, printer_settings_id=NAME, printer_variant="0.8", inherits="", **{"from": "system"},
         default_print_profile="", default_filament_profile=[])
m.pop("setting_id", None)
procs = {"high": "0.24mm Standard @Anycubic Kobra S1 Max 0.8 nozzle",
         "medium": "0.32mm Standard @Anycubic Kobra S1 Max 0.8 nozzle",
         "fast": "0.48mm Standard @Anycubic Kobra S1 Max 0.8 nozzle"}
out = {"machine": m}
for q, n in procs.items():
    p = flat("process", n)
    p.update(compatible_printers=[NAME], inherits="", **{"from": "system"},
             name=f"Print Lab {q} 0.8", print_settings_id=f"Print Lab {q} 0.8")
    p.pop("setting_id", None)
    out[f"process_{q}"] = p
f = flat("filament", "Anycubic PLA @Anycubic Kobra S1 Max 0.8 nozzle")
f4 = flat("filament", "Anycubic PLA @Anycubic Kobra S1 0.4 nozzle")
for k in ("activate_air_filtration", "during_print_exhaust_fan_speed", "complete_print_exhaust_fan_speed"):
    f[k] = f4[k]
f.update(compatible_printers=[NAME], inherits="", **{"from": "system"},
         name="Print Lab PLA 0.8", filament_settings_id=["Print Lab PLA 0.8"])
f.pop("setting_id", None)
out["filament_pla"] = f
os.makedirs(sys.argv[1], exist_ok=True)
for k, v in out.items():
    json.dump(v, open(os.path.join(sys.argv[1], f"{k}.json"), "w"), indent=1, sort_keys=True)
print("wrote", list(out))
