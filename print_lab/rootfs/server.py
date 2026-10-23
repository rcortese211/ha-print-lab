"""Print Lab slice server (runs inside the toolchain image on the external drive).

Internal Supervisor network only. Endpoints:
  GET  /health
  POST /slice            {name, scad|url|model|models, profile?, overrides?}
                         models: [{url|model, count}] arranges several models on one plate
  GET  /preview/<name>.png  top-down picture of a sliced plate
  GET  /gcode/<name>
  GET  /profiles
  GET  /files            models and sliced G-code on the drive, newest first
  PUT  /profiles/<name>  (body: ini text)
  POST /detect           {stream_url} or a JPEG body; returns the failure score
  GET  /watch/last.jpg   the frame the last check looked at
  /admin/...             maintenance, only on a standalone machine (see admin.py)
  /printer/...           direct printer control, standalone only, needs the admin token:
    GET  status | files      POST config {host} | stage {gcode} | start {confirm, filename?}
    POST stop | pause | resume | check (camera frame + failure score)
  POST /slice with "stage": true also puts the result on the printer (needs the token)
"""
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time

import numpy as np
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import admin
import calib
import mesh
import orca
import plate
import plateguard
import watch
from printer import Printer, PrinterError
from watch import Detector, WatchError

LAB = Path(os.environ.get("PRINTLAB_DIR", "/lab"))
MODELS, GCODE, PROFILES, WORK = (LAB / d for d in ("models", "gcode", "profiles", "work"))
NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,60}")
KEY_RE = re.compile(r"[a-z0-9_]{1,60}")
VAL_RE = re.compile(r"[A-Za-z0-9_.,%+ -]{0,60}")
MODEL_EXT = (".stl", ".3mf", ".obj", ".step", ".stp", ".scad")
MAX_DOWNLOAD = 200 * 1024 * 1024
LOCK = threading.Lock()
DET_LOCK = threading.Lock()
DETECTOR = Detector(LAB)
PRINTER_CFG = LAB / "printer.json"
STAGED = LAB / "staged.json"
_PRINTER = None


def printer():
    global _PRINTER
    try:
        host = json.loads(PRINTER_CFG.read_text()).get("host")
    except (OSError, ValueError):
        host = None
    if not host:
        raise JobError("No printer address set yet (POST /printer/config)")
    if _PRINTER is None or _PRINTER.host != host:
        _PRINTER = Printer(host)
    return _PRINTER


def read_staged():
    try:
        return json.loads(STAGED.read_text())
    except (OSError, ValueError):
        return {}


def stage_gcode(name, extra=None):
    """Put a sliced file from the drive on the printer and remember it as the staged one."""
    path = GCODE / Path(str(name)).name
    if not path.is_file() or path.suffix != ".gcode":
        raise JobError(f"Print Lab has no {path.name}")
    result = printer().stage(path.name, path.read_bytes())
    STAGED.write_text(json.dumps({**result, **(extra or {}),
                                  "staged_at": time.strftime("%Y-%m-%d %H:%M:%S")}))
    return result


CALIBRATION = LAB / "camera_calibration.json"
GUARD = plateguard.PlateGuard(CALIBRATION, GCODE)
CAL_STATE = {"running": False, "last": None}


def calibration_run():
    """Film the calibration print (a frame every 2 s once printing starts), then work out the camera."""
    CAL_STATE.update(running=True, last="waiting for the printer to start printing")
    run_dir = LAB / "calibration" / time.strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    p = printer()
    # the printer reports idle as soon as it has read the file, while it is still holding the
    # bed at each height, so film for a fixed time once printing starts
    shoot_for = 90 + sum(secs for _z, secs in calib.DWELLS) + 60
    try:
        t0, state = time.time(), None
        while time.time() - t0 < 25 * 60 and state != "printing":
            time.sleep(3)
            try:
                st = p.status()
                state = (st.get("job") or {}).get("state") or st.get("state")
            except PrinterError as err:
                print(f"[calibration] status: {err}", flush=True)
        if state != "printing":
            raise ValueError("the calibration print never started")
        CAL_STATE["last"] = "filming"
        try:
            p.kick_camera()
        except PrinterError:
            pass
        started = time.time()
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-rw_timeout", "15000000",
                        "-i", p.stream_url, "-t", str(shoot_for), "-vf", "fps=1/2", "-q:v", "3",
                        str(run_dir / "%05d.jpg")], capture_output=True, timeout=shoot_for + 120)
        files = sorted(run_dir.glob("[0-9]*.jpg"))
        (run_dir / "times.json").write_text(json.dumps([[2 * i, f.name] for i, f in enumerate(files)]))
        print(f"[calibration] filmed {len(files)} frames in {round(time.time() - started)} s", flush=True)
        solve_run(run_dir)
    except Exception as err:  # noqa: BLE001
        print(f"[calibration] failed: {type(err).__name__}: {err}", flush=True)
        CAL_STATE["last"] = f"failed: {err}"
    finally:
        CAL_STATE["running"] = False


def solve_run(run_dir: Path) -> None:
    """Work out the camera from a run's frames; save it, a contact sheet and an overlay."""
    if (run_dir / "times.json").is_file():
        times = json.loads((run_dir / "times.json").read_text())
    else:                         # older runs: frames are named by seconds since the run began
        times = [[int(f.stem), f.name] for f in sorted(run_dir.glob("[0-9]*.jpg"))]
    frames = [(t, run_dir / name) for t, name in times]
    print(f"[calibration] {len(frames)} frames from {run_dir.name}; solving", flush=True)
    CAL_STATE["last"] = "solving"
    calib.contact_sheet([f for _t, f in frames], LAB / "calibration" / "latest_sheet.jpg")
    try:
        cal = calib.solve(frames)
    except ValueError as err:
        CAL_STATE["last"] = f"failed: {err}"
        print(f"[calibration] failed: {err}", flush=True)
        return
    cal["run"] = run_dir.name
    CALIBRATION.write_text(json.dumps(cal))
    if cal.get("P"):
        calib.draw_overlay(Path(cal["frame"]), calib.Camera(cal["P"], cal.get("k1", 0.0)),
                           LAB / "calibration" / "latest_overlay.jpg", cal["frame_bed_z"])
    msg = (f"camera calibrated: {cal['points']} points at bed heights {cal['heights']}, "
           f"error {cal['rms_px']} px (worst {cal['max_px']}), lens k1 {cal.get('k1')}; "
           f"views {cal.get('views')}; matched {cal.get('matched')}" + (f"; {cal['note']}" if cal.get("note") else ""))
    print(f"[calibration] {msg}", flush=True)
    CAL_STATE["last"] = msg


def resolve_if_stale() -> None:
    """On start: re-solve the newest calibration run if the saved result is from older code."""
    runs = sorted(p for p in (LAB / "calibration").glob("2*") if any(p.glob("[0-9]*.jpg"))) \
        if (LAB / "calibration").is_dir() else []
    if not runs:
        return
    _cam, cal = calib.load(CALIBRATION)
    if cal and cal.get("version") == calib.VERSION and cal.get("run") == runs[-1].name:
        return
    try:
        solve_run(runs[-1])
    except Exception as err:  # noqa: BLE001
        print(f"[calibration] re-solve failed: {type(err).__name__}: {err}", flush=True)


def checked(url):
    """Score a live frame: the failure model (zoomed onto the parts when the camera is
    calibrated) plus the plate check (plastic where no part should be). If the camera has
    shown the same picture for several checks (a frozen stream), restart it and look again;
    still frozen is an error, so a dead feed never passes for a clean print."""
    status = plan = None
    try:
        status = printer().status()
    except (JobError, PrinterError):
        pass
    try:
        plan = GUARD.plan(status)
    except Exception as err:  # noqa: BLE001  the plate check must never break the basic one
        print(f"[print_lab] plate plan failed: {type(err).__name__}: {err}", flush=True)
    crops = None
    if plan is not None and plan.crop:
        crops = watch.parse_crops(os.environ.get("WATCH_CROPS", watch.DEFAULT_CROPS)) + [plan.crop]
    result = DETECTOR.check(url=url, crops=crops)
    if result.get("frozen"):
        print("[print_lab] camera looks frozen; restarting it", flush=True)
        try:
            printer().kick_camera()
        except (JobError, PrinterError) as err:
            print(f"[print_lab] couldn't restart the camera: {err}", flush=True)
        time.sleep(3)
        result = DETECTOR.check(url=url, crops=crops)
        result["camera_restarted"] = True
        if result.get("frozen"):
            raise WatchError("The camera feed is frozen (same picture for several checks), even after a restart")
    if plan is not None:
        try:
            extra = GUARD.analyse(DETECTOR.dir / "last.jpg", plan)
        except Exception as err:  # noqa: BLE001
            extra = {"plate": f"error: {type(err).__name__}: {err}"}
        result.update(extra)
        result["ml_score"] = result["score"]
        result["score"] = round(max(result["score"], extra.get("plate_score") or 0.0), 3)
        if (extra.get("plate_score") or 0) >= 0.2:
            DETECTOR._keep(DETECTOR.dir / "last.jpg", result["score"], False)
    return result


def check_camera():
    """Score a live frame, starting the printer's camera stream first if it isn't running."""
    p = printer()
    try:
        return checked(p.stream_url)
    except WatchError as err:
        if "frozen" in str(err):
            raise
        p.kick_camera()
        time.sleep(2)
        return checked(p.stream_url)


class JobError(Exception):
    pass


def run(cmd, timeout=900):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        tail = (p.stderr or p.stdout or "").strip().splitlines()[-12:]
        raise JobError(f"{os.path.basename(cmd[0])} failed: " + " | ".join(tail))
    return p


def version(cmd):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return (out.stdout or out.stderr).strip().splitlines()[0]
    except Exception as err:  # noqa: BLE001
        return f"unavailable ({err})"


def model_input(item, fallback_name):
    """A model file for one plate entry: downloaded, from the models folder, or OpenSCAD."""
    if item.get("url"):
        url = str(item["url"])
        stem = os.path.splitext(os.path.basename(urllib.parse.urlsplit(url).path))[0]
        stem = re.sub(r"[^A-Za-z0-9_-]+", "_", urllib.parse.unquote(stem)).strip("_-")[:50] or fallback_name
        model = fetch(url, stem)
    elif item.get("model"):
        model = MODELS / Path(str(item["model"])).name
        if not model.is_file():
            raise JobError(f"{model.name} is not in printlab/models")
    else:
        raise JobError("Each model needs a url or a model name")
    if model.suffix == ".scad":
        stl = WORK / f"{model.stem}.stl"
        run(["openscad", "-o", str(stl), str(model)], timeout=600)
        model = stl
    return model


def fetch(url, name):
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ("http", "https"):
        raise JobError("Only http(s) links are supported")
    ext = os.path.splitext(p.path.lower())[1]
    if ext not in MODEL_EXT:
        raise JobError(f"Link must point at one of: {', '.join(MODEL_EXT)}")
    dest = MODELS / f"{name}{ext}"
    req = urllib.request.Request(url, headers={"User-Agent": "PrintLab/0.1"})
    with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
        total = 0
        while chunk := r.read(1 << 16):
            total += len(chunk)
            if total > MAX_DOWNLOAD:
                raise JobError("Download is larger than 200 MB")
            f.write(chunk)
    return dest


def slice_job(req):
    name = str(req.get("name", ""))
    if not NAME_RE.fullmatch(name):
        raise JobError("name must be 1-60 letters, digits, - or _")
    # OrcaSlicer (Anycubic's own S1 profiles) is the default since the Oct 2026 cube test; the
    # camera calibration print stays on PrusaSlicer, whose output it was built and tested on
    default = "prusa" if (req.get("tune") or {}).get("camera_calibration") else "orca"
    engine = str(req.get("slicer") or os.environ.get("PRINTLAB_SLICER", default)).lower()
    if engine not in ("prusa", "orca"):
        raise JobError("slicer must be prusa or orca")
    if engine == "orca" and not orca.available():
        raise JobError("OrcaSlicer isn't installed on the Pi")
    profile = str(req.get("profile", "pla_08"))
    if engine == "prusa" and (not NAME_RE.fullmatch(profile) or not (PROFILES / f"{profile}.ini").is_file()):
        raise JobError(f"Unknown profile '{profile}'")

    inputs = []
    if req.get("models"):
        items = req["models"]
        if not isinstance(items, list) or not 1 <= len(items) <= 12:
            raise JobError("models must be a list of 1-12 entries")
        for i, item in enumerate(items):
            count = int(item.get("count", 1))
            if not 1 <= count <= 50:
                raise JobError("count must be 1-50")
            inputs += [str(model_input(item, f"{name}_{i + 1}"))] * count
        if len(inputs) > 100:
            raise JobError("That's more than 100 pieces on one plate")
    elif req.get("scad"):
        model = MODELS / f"{name}.scad"
        model.write_text(str(req["scad"]))
        inputs = [str(model_input({"model": model.name}, name))]
    elif req.get("url"):
        inputs = [str(fetch(str(req["url"]), name))]
    elif req.get("model"):
        inputs = [str(model_input({"model": req["model"]}, name))]
    else:
        raise JobError("Give one of: scad, url, model, models")

    # orientation + layout: models we can read get rested on their best face (unless the
    # request says orient: false; OpenSCAD models are ours and already oriented), several
    # pieces are laid out by us and sliced as one object (the slicer's own arrange crashes in
    # some PrusaSlicer builds)
    out = GCODE / f"{name}.gcode"
    pieces = len(inputs)
    orientation, supports = {}, False
    readable = all(Path(p).suffix.lower() in (".stl", ".3mf", ".obj") for p in inputs)
    want_orient = req.get("orient", True) is not False
    if readable and (pieces > 1 or (want_orient and not str(inputs[0]).startswith(str(WORK)))):
        try:
            meshes = {}
            for p in dict.fromkeys(inputs):
                tris = mesh.load(Path(p))
                if want_orient and not str(p).startswith(str(WORK)):
                    tris, info = mesh.orient(tris)
                    orientation[Path(p).name] = info
                    supports |= info["needs_supports"]
                meshes[p] = tris
            laid = mesh.arrange([meshes[p] for p in inputs])
        except mesh.MeshError as err:
            raise JobError(f"Couldn't lay out the plate: {err}") from err
        if engine == "orca":
            # one file per piece, kept where we laid it: Orca makes each its own object, so a
            # single failed piece can be cancelled (EXCLUDE_OBJECT) without stopping the plate
            for old in WORK.glob(f"{name}.p*.stl"):
                old.unlink()
            inputs = []
            for i, part in enumerate(laid):
                part_stl = WORK / f"{name}.p{i + 1:02d}.stl"
                mesh.write_stl(part_stl, [part])
                inputs.append(str(part_stl))
        else:
            plate_stl = WORK / f"{name}.plate.stl"
            mesh.write_stl(plate_stl, laid)
            inputs = [str(plate_stl)]
        layout = ["--dont-arrange"]
    else:
        layout = ["--center", "125,125"]

    # "quality" + "quality_overrides" come from the bot's presets: PrusaSlicer gets the preset as
    # overrides on its one tuned profile, Orca picks Anycubic's own process for that quality
    quality = str(req.get("quality") or "")
    overrides = {}
    if engine == "prusa":
        overrides.update({str(k): str(v) for k, v in (req.get("quality_overrides") or {}).items()})
    overrides.update({str(k): str(v) for k, v in (req.get("overrides") or {}).items()})
    if supports and "support_material" not in overrides:
        # grid supports: the organic style's tips can't be thinner than a 0.8 mm nozzle's line
        overrides.update({"support_material": "1", "support_material_auto": "1",
                          "support_material_style": "grid"})
    for k, v in overrides.items():
        if not KEY_RE.fullmatch(k) or not VAL_RE.fullmatch(v):
            raise JobError(f"Bad override {k}={v}")
    out.unlink(missing_ok=True)
    orca_info = None
    if engine == "orca":
        try:
            orca_info = orca.slice_files(name, inputs, layout == ["--dont-arrange"],
                                         quality or orca.quality_for(overrides), overrides, out, WORK)
        except (orca.OrcaError, subprocess.TimeoutExpired, ValueError) as err:
            raise JobError(f"{err}" + (f" (orientation: {json.dumps(orientation)})" if orientation else "")) from err
    else:
        # overrides go in a second ini loaded on top of the profile: the CLI can't take on/off
        # settings as "--key 0", but an ini line handles every setting type the same way
        args = ["prusa-slicer", "--export-gcode", "--load", str(PROFILES / f"{profile}.ini")]
        if overrides:
            extra = WORK / f"{name}.overrides.ini"
            extra.write_text("".join(f"{k} = {v}\n" for k, v in overrides.items()))
            args += ["--load", str(extra)]
        args += layout
        args += ["--output", str(out), *inputs]
        try:
            p = run(args)
        except JobError:
            p = run(["xvfb-run", "-a", *args])  # some builds want a display even headless
        if not out.is_file():
            tail = " | ".join((p.stdout + p.stderr).strip().splitlines()[-6:])
            raise JobError(f"The slicer finished without making G-code: {tail or 'no message'}"
                           + (f" (orientation: {json.dumps(orientation)})" if orientation else ""))

    data = out.read_bytes()
    cut = data.find(b"; prusaslicer_config = begin") if engine == "prusa" else -1
    if cut > 0:
        data = data[:cut]
        out.write_bytes(data)
    if b"G9111 " not in data[:20000]:
        raise JobError("Sliced file is missing the Kobra start block (G9111) - wrong profile?")
    segs = plate.segments(data)
    if segs is not None and not plate.fits(segs):
        lo_x, hi_x, lo_y, hi_y = plate.extents(segs)
        out.unlink(missing_ok=True)
        raise JobError(f"It doesn't fit on the bed: that layout needs about {hi_x - lo_x:.0f} x {hi_y - lo_y:.0f} mm "
                       f"(the bed is 250 x 250). Try fewer copies or split it into two plates.")
    # "tune" carries calibration steps; top-level "temps" is kept for older callers
    tune = dict(req.get("tune") or {})
    if req.get("temps") and "temps" not in tune:
        tune["temps"] = req["temps"]
    applied, retracts_applied = [], []
    if tune.get("temps"):
        data, applied = apply_temps(data, tune["temps"])
    if tune.get("retracts"):
        base = float((req.get("overrides") or {}).get("retract_length")
                     or (orca.retraction_length() if engine == "orca"
                         else profile_value(profile, "retract_length")) or 0)
        data, retracts_applied = apply_retracts(data, tune["retracts"], base)
    if tune.get("camera_calibration"):
        data = calib.postprocess(data)
        out.write_bytes(data)
    elif applied or retracts_applied:
        out.write_bytes(data)
    cfg = data.rfind(b"; CONFIG_BLOCK_START")      # Orca: the summary sits just before its config
    tail = (data[max(0, cfg - 6000):cfg] if cfg > 0 else data[-6000:]).decode(errors="ignore")

    def grab(label):
        m = re.search(rf"^; {re.escape(label)} = (.+)$", tail, re.M)
        return m.group(1).strip() if m else None

    return {"gcode": out.name, "size": len(data), "md5": hashlib.md5(data).hexdigest(),
            "estimated_time": grab("estimated printing time (normal mode)"),
            "filament_g": grab("filament used [g]"),
            "profile": profile if engine == "prusa" else f"orca {orca_info['quality']}",
            "slicer": engine, "orca": orca_info,
            "layers": data.count(b";LAYER_CHANGE"), "pieces": pieces, "orientation": orientation,
            "supports": overrides.get("support_material") == "1",
            "preview": f"/preview/{out.stem}.png", "temps_applied": applied,
            "retracts_applied": retracts_applied}


def profile_value(profile, key):
    for line in (PROFILES / f"{profile}.ini").read_text().splitlines():
        k, _, v = line.partition("=")
        if k.strip() == key:
            return v.strip()
    return None


E_RE = re.compile(rb"^(G1(?: [^E;]\S*)*) E(-?[0-9.]+)(.*)$")


def apply_retracts(data, steps, base):
    """Retraction tower: change the retraction length at given heights.

    steps is [[from_z_mm, length_mm], ...]. Needs relative E. Every retraction
    move (negative E, including the part done during a wipe) and every plain
    un-retract (E only, no X/Y) is scaled by length / base from that height up.
    """
    if base <= 0:
        raise JobError("retracts: the profile has no retraction to scale")
    try:
        steps = sorted((float(z), float(l)) for z, l in steps)
    except (TypeError, ValueError) as err:
        raise JobError("retracts must be a list of [height_mm, length_mm]") from err
    if len(steps) > 40 or any(not 0 <= l <= 4 or z < 0 for z, l in steps):
        raise JobError("retracts: up to 40 steps, 0-4 mm, heights 0 or more")
    if b"M83" not in data[:20000] and b"relative extrusion" not in data[:20000]:
        raise JobError("retracts: the G-code does not use relative extrusion")
    out, applied, i, k = [], [], 0, None
    for line in data.split(b"\n"):
        if i < len(steps) and line.startswith(b";Z:"):
            z = float(line[3:].strip())
            while i < len(steps) and z + 1e-6 >= steps[i][0]:
                k = steps[i][1] / base
                applied.append([round(z, 2), steps[i][1]])
                i += 1
        if k is not None and line.startswith(b"G1 "):
            m = E_RE.match(line)
            if m:
                e = float(m.group(2))
                xy = b" X" in m.group(1) or b" Y" in m.group(1)
                if e < 0 or not xy:
                    line = m.group(1) + b" E%.5f" % (e * k) + m.group(3)
        out.append(line)
    if i < len(steps):
        raise JobError(f"retracts: the print never reaches {steps[i][0]} mm")
    return b"\n".join(out), applied


def apply_temps(data, temps):
    """Temperature tower: set the nozzle temperature at given heights.

    temps is [[from_z_mm, celsius], ...]. An M104 goes in right after the first
    layer marker at or above each height, so the change lands before that layer
    prints.
    """
    try:
        steps = sorted((float(z), int(t)) for z, t in temps)
    except (TypeError, ValueError) as err:
        raise JobError("temps must be a list of [height_mm, temperature]") from err
    if len(steps) > 40 or any(not 170 <= t <= 280 or z < 0 for z, t in steps):
        raise JobError("temps: up to 40 steps, 170-280 C, heights 0 or more")
    lines, applied, i = data.split(b"\n"), [], 0
    out = []
    for line in lines:
        out.append(line)
        if i < len(steps) and line.startswith(b";Z:"):
            z = float(line[3:].strip())
            temp = None
            while i < len(steps) and z + 1e-6 >= steps[i][0]:
                temp = steps[i][1]
                i += 1
            if temp is not None:
                out.append(b"M104 S%d ; temp tower" % temp)
                applied.append([round(z, 2), temp])
    if i < len(steps):
        raise JobError(f"temps: the print never reaches {steps[i][0]} mm")
    return b"\n".join(out), applied


class Handler(BaseHTTPRequestHandler):
    server_version = "PrintLab/0.1"

    def _send(self, code, obj=None, raw=None, ctype="application/json"):
        body = raw if raw is not None else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self, limit=4 * 1024 * 1024):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise JobError("Request too large")
        return self.rfile.read(n)

    def _admin(self, method):
        parts = urllib.parse.urlsplit(self.path)
        if not admin.enabled():
            return self._send(404, {"error": "not found"})
        if not admin.authorised(self.headers.get("X-Admin-Token")):
            return self._send(403, {"error": "forbidden"})
        action = parts.path[len("/admin/"):]
        query = urllib.parse.parse_qs(parts.query)
        if method == "GET" and action == "status":
            return self._send(200, admin.status(LAB))
        if method == "GET" and action == "logs":
            return self._send(200, admin.logs(int((query.get("lines") or ["200"])[0]),
                                              (query.get("unit") or [admin.SERVICE])[0]))
        if method == "POST" and action == "bot":
            try:
                return self._send(200, admin.set_bot(LAB, json.loads(self._body(64 * 1024))))
            except (ValueError, JobError) as err:
                return self._send(400, {"error": str(err)})
        if method == "POST" and action in ("update", "restart", "reboot"):
            return self._send(200, getattr(admin, action)())
        self._send(404, {"error": "not found"})

    def _printer(self, method):
        if not admin.enabled():
            return self._send(404, {"error": "not found"})
        if not admin.authorised(self.headers.get("X-Admin-Token")):
            return self._send(403, {"error": "forbidden"})
        action = urllib.parse.urlsplit(self.path).path[len("/printer/"):]
        try:
            body = json.loads(self._body(64 * 1024) or b"{}") if method == "POST" else {}
            if method == "GET" and action == "status":
                return self._send(200, {**printer().status(), "staged": read_staged() or None})
            if method == "GET" and action == "calibration":
                _p, cal = calib.load(CALIBRATION)
                return self._send(200, {**CAL_STATE, "calibration": cal})
            if method == "GET" and action == "files":
                return self._send(200, {"files": printer().files()})
            if method == "POST" and action == "config":
                host = str(body.get("host", ""))
                if not re.fullmatch(r"[A-Za-z0-9.-]{1,64}", host):
                    raise JobError("host must be an IP address or host name")
                PRINTER_CFG.write_text(json.dumps({"host": host}))
                return self._send(200, {"ok": True, "host": host})
            if method == "POST" and action == "stage":
                with LOCK:
                    return self._send(200, stage_gcode(body.get("gcode", ""), {"source": "print_lab"}))
            if method == "POST" and action == "start":
                if body.get("confirm") is not True:
                    raise JobError("Starting a print needs confirm: true")
                staged = read_staged()
                if not staged.get("filename"):
                    raise JobError("Nothing is staged")
                if body.get("filename") and body["filename"] != staged["filename"]:
                    raise JobError("That file is not the staged one")
                result = printer().start(staged, bool(body.get("auto_leveling", True)))
                if staged["filename"].startswith(calib.NAME) and not CAL_STATE["running"]:
                    threading.Thread(target=calibration_run, daemon=True).start()
                    result["calibrating"] = True
                staged["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                STAGED.write_text(json.dumps(staged))
                return self._send(200, result)
            if method == "POST" and action in ("stop", "pause", "resume"):
                return self._send(200, printer().control(action))
            if method == "POST" and action == "check":
                if not DET_LOCK.acquire(timeout=60):
                    raise WatchError("Another check is still running")
                try:
                    return self._send(200, check_camera())
                finally:
                    DET_LOCK.release()
            self._send(404, {"error": "not found"})
        except (JobError, PrinterError, WatchError, ValueError) as err:
            self._send(400, {"error": str(err)})
        except Exception as err:  # noqa: BLE001
            self._send(500, {"error": f"{type(err).__name__}: {err}"})

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith("/admin/"):
            return self._admin("GET")
        if path.startswith("/printer/"):
            return self._printer("GET")
        if path == "/health":
            free = shutil.disk_usage(LAB)
            return self._send(200, {"ok": True, "prusaslicer": version(["prusa-slicer", "--help"]),
                                    "openscad": version(["openscad", "--version"]),
                                    "drive_free_gb": round(free.free / 1e9, 1),
                                    "detector": DETECTOR.status,
                                    "profiles": sorted(p.stem for p in PROFILES.glob("*.ini"))})
        if path == "/profiles":
            return self._send(200, {"profiles": sorted(p.stem for p in PROFILES.glob("*.ini"))})
        if path in ("/calibration/latest_sheet.jpg", "/calibration/latest_overlay.jpg"):
            f = LAB / path.lstrip("/")
            if f.is_file():
                return self._send(200, raw=f.read_bytes(), ctype="image/jpeg")
            return self._send(404, {"error": "not found"})
        if path == "/frames":
            if not admin.authorised(self.headers.get("X-Admin-Token")):
                return self._send(403, {"error": "forbidden"})
            kept = sorted(DETECTOR.history.glob("*/*.jpg"), reverse=True)[:200] if DETECTOR.history.is_dir() else []
            return self._send(200, {"frames": [f"{p.parent.name}/{p.name}" for p in kept]})
        m = re.fullmatch(r"/frames/(\d{4}-\d{2}-\d{2})/([0-9]{6}-[0-9.]+(?:-frozen|-calibration)?\.jpg)", path)
        if m:          # single frames are viewable like /watch/last.jpg; the listing needs the token
            f = DETECTOR.history / m.group(1) / m.group(2)
            if f.is_file():
                return self._send(200, raw=f.read_bytes(), ctype="image/jpeg")
            return self._send(404, {"error": "not found"})
        if path == "/files":
            def ls(folder, exts):
                rows = [{"name": p.name, "size": p.stat().st_size, "mtime": int(p.stat().st_mtime)}
                        for p in folder.iterdir() if p.is_file() and p.suffix.lower() in exts]
                return sorted(rows, key=lambda r: -r["mtime"])
            return self._send(200, {"models": ls(MODELS, MODEL_EXT), "gcode": ls(GCODE, (".gcode",))})
        if path == "/watch/last.jpg" and (DETECTOR.dir / "last.jpg").is_file():
            return self._send(200, raw=(DETECTOR.dir / "last.jpg").read_bytes(), ctype="image/jpeg")
        m = re.fullmatch(r"/preview/([A-Za-z0-9_-]{1,60})\.png", path)
        if m and (GCODE / f"{m.group(1)}.gcode").is_file():
            src = GCODE / f"{m.group(1)}.gcode"
            png = WORK / f"{m.group(1)}.preview.png"
            if not png.is_file() or png.stat().st_mtime < src.stat().st_mtime:
                segs = plate.segments(src.read_bytes())
                if segs is None:
                    return self._send(404, {"error": "nothing to draw"})
                png.write_bytes(plate.render(segs))
            return self._send(200, raw=png.read_bytes(), ctype="image/png")
        m = re.fullmatch(r"/gcode/([A-Za-z0-9_-]{1,60}\.gcode)", path)
        if m and (GCODE / m.group(1)).is_file():
            return self._send(200, raw=(GCODE / m.group(1)).read_bytes(), ctype="text/plain")
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if urllib.parse.urlsplit(self.path).path.startswith("/admin/"):
            return self._admin("POST")
        if urllib.parse.urlsplit(self.path).path.startswith("/printer/"):
            return self._printer("POST")
        if urllib.parse.urlsplit(self.path).path == "/detect":
            return self._detect()
        if urllib.parse.urlsplit(self.path).path != "/slice":
            return self._send(404, {"error": "not found"})
        try:
            req = json.loads(self._body())
            if req.get("stage") and not admin.authorised(self.headers.get("X-Admin-Token")):
                return self._send(403, {"error": "staging on the printer needs the admin token"})
            if not LOCK.acquire(timeout=600):
                raise JobError("Another slice is still running")
            try:
                result = slice_job(req)
                if req.get("stage"):
                    result = {"sliced": result, "staged": stage_gcode(
                        result["gcode"], {"estimated_time": result.get("estimated_time"),
                                          "filament_g": result.get("filament_g"), "source": "print_lab"})}
                self._send(200, result)
            finally:
                LOCK.release()
        except (JobError, PrinterError, ValueError, subprocess.TimeoutExpired) as err:
            self._send(400, {"error": str(err)})

    def _detect(self):
        try:
            if not DET_LOCK.acquire(timeout=60):
                raise WatchError("Another check is still running")
            try:
                if (self.headers.get("Content-Type") or "").startswith("image/jpeg"):
                    result = DETECTOR.check(jpeg=self._body(8 * 1024 * 1024))
                else:
                    url = str(json.loads(self._body()).get("stream_url", ""))
                    try:
                        # HA still sends the stock camera address; with Rinkhals on the printer
                        # the camera lives elsewhere, and the printer object knows where
                        p = printer()
                        if url in p.camera_urls():
                            url = p.stream_url
                    except Exception:  # noqa: BLE001
                        pass
                    result = checked(url)
            finally:
                DET_LOCK.release()
            print(f"[print_lab] check: score {result['score']} (best {result.get('view')}) "
                  f"views {result.get('views')} in {result.get('inference_s')}s "
                  f"frame {result.get('frame_size')} change {result.get('frame_change')} "
                  f"plate {result.get('plate')} {result.get('plate_score')} debris {result.get('debris_px')}",
                  flush=True)
            self._send(200, result)
        except (WatchError, JobError, ValueError, subprocess.TimeoutExpired) as err:
            self._send(400, {"error": str(err)})
        except Exception as err:  # noqa: BLE001  model/runtime trouble: report, don't die
            self._send(500, {"error": f"{type(err).__name__}: {err}"})

    def do_PUT(self):
        m = re.fullmatch(r"/profiles/([A-Za-z0-9_-]{1,60})", urllib.parse.urlsplit(self.path).path)
        if not m:
            return self._send(404, {"error": "not found"})
        try:
            text = self._body(512 * 1024).decode()
            if "G9111" not in text:
                raise JobError("Profile has no Kobra start block (G9111)")
            (PROFILES / f"{m.group(1)}.ini").write_text(text)
            self._send(200, {"saved": m.group(1)})
        except (JobError, UnicodeDecodeError) as err:
            self._send(400, {"error": str(err)})

    def log_message(self, fmt, *args):
        print("[print_lab] " + fmt % args, flush=True)


if __name__ == "__main__":
    for d in (MODELS, GCODE, PROFILES, WORK):
        d.mkdir(parents=True, exist_ok=True)
    def _warm():
        try:
            DETECTOR.ensure_model()
        except Exception as err:  # noqa: BLE001
            DETECTOR.status = f"error: {err}"
            print(f"[print_lab] detector not ready: {err}", flush=True)

    try:
        DETECTOR.tidy_history()
    except OSError as err:
        print(f"[print_lab] couldn't tidy the frame history: {err}", flush=True)
    threading.Thread(target=_warm, daemon=True).start()
    threading.Thread(target=resolve_if_stale, daemon=True).start()
    class DualStackServer(ThreadingHTTPServer):
        """Answer on IPv4 and IPv6: printlab.local can resolve to either."""
        address_family = socket.AF_INET6

        def server_bind(self):
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            super().server_bind()

    try:
        server = DualStackServer(("::", 8099), Handler)
    except OSError:                       # no IPv6 on this machine
        server = ThreadingHTTPServer(("0.0.0.0", 8099), Handler)
    server.serve_forever()
