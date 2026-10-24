"""Keep the ACE purge speed in printer.custom.cfg matched to the fitted nozzle.

GoKlipper reads [filament_hub] sweep_speed only at startup, so this runs once per boot:
it waits for GoKlipper, reads extruder.Nozzle_diameter, and rewrites the managed block if
the nozzle differs from the one the block was written for. A rewrite takes effect on the
next power-cycle; until then a warning is logged and shown on the screen.
"""
import json
import re
import shutil
import socket
import time

CFG = "/userdata/app/gk/printer_data/config/printer.custom.cfg"
SOCK = "/tmp/unix_uds1"
BEGIN = "# BEGIN nozzle-purge (managed by the nozzle-purge Rinkhals app, do not edit)"
END = "# END nozzle-purge"

# purge speed in mm/s of 1.75 mm filament (x2.405 = mm3/s)
SWEEP_SPEED = {0.2: 2, 0.4: 7, 0.6: 10, 0.8: 13}


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def query(method, params):
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(5)
    s.connect(SOCK)
    s.sendall(json.dumps({"id": 1, "method": method, "params": params}).encode() + b"\x03")
    buf = b""
    while not buf.endswith(b"\x03"):
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
    s.close()
    return json.loads(buf.rstrip(b"\x03"))


def nozzle_diameter():
    for _ in range(60):
        try:
            res = query("objects/query", {"objects": {"extruder": None}})
            value = res["result"]["status"]["extruder"].get("Nozzle_diameter")
            if value:
                return round(float(value), 1)
        except Exception:
            pass
        time.sleep(5)
    return None


def block_for(diameter):
    speed = SWEEP_SPEED.get(diameter, min(SWEEP_SPEED.values()))
    return "\n".join([
        BEGIN,
        "# nozzle: %.1f" % diameter,
        "[filament_hub]",
        "sweep_speed: %s" % speed,
        END,
    ])


def main():
    diameter = nozzle_diameter()
    if diameter is None:
        log("could not read the nozzle diameter; leaving the config alone")
        return
    with open(CFG) as f:
        text = f.read()
    match = re.search(re.escape(BEGIN) + r".*?" + re.escape(END), text, re.S)
    current = match.group(0) if match else None
    wanted = block_for(diameter)
    if current == wanted:
        log("nozzle %.1f: purge settings already match" % diameter)
        return
    backup = "%s.bak-nozzle-%s" % (CFG, time.strftime("%Y%m%d-%H%M%S"))
    shutil.copy(CFG, backup)
    if current:
        text = text.replace(current, wanted)
    else:
        text = text.rstrip("\n") + "\n\n" + wanted + "\n"
    with open(CFG, "w") as f:
        f.write(text)
    log("nozzle %.1f: purge settings rewritten (backup %s); power-cycle to apply" % (diameter, backup))
    if current:
        try:
            query("gcode/script", {"script": "M117 Nozzle changed: power-cycle to apply purge speed"})
        except Exception:
            pass


if __name__ == "__main__":
    main()
