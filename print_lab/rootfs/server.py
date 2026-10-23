"""Print Lab slice server (runs inside the toolchain image on the external drive).

Internal Supervisor network only. Endpoints:
  GET  /health
  POST /slice            {name, scad|url|model, profile?, overrides?}
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
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import admin
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


def check_camera():
    """Score a live frame, starting the printer's camera stream first if it isn't running."""
    p = printer()
    try:
        return DETECTOR.check(url=p.stream_url)
    except WatchError:
        p.kick_camera()
        time.sleep(2)
        return DETECTOR.check(url=p.stream_url)


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
    profile = str(req.get("profile", "pla_08"))
    if not NAME_RE.fullmatch(profile) or not (PROFILES / f"{profile}.ini").is_file():
        raise JobError(f"Unknown profile '{profile}'")

    if req.get("scad"):
        model = MODELS / f"{name}.scad"
        model.write_text(str(req["scad"]))
    elif req.get("url"):
        model = fetch(str(req["url"]), name)
    elif req.get("model"):
        model = MODELS / Path(str(req["model"])).name
        if not model.is_file():
            raise JobError(f"{model.name} is not in printlab/models")
    else:
        raise JobError("Give one of: scad, url, model")

    if model.suffix == ".scad":
        stl = WORK / f"{name}.stl"
        run(["openscad", "-o", str(stl), str(model)], timeout=600)
        model = stl

    args = ["prusa-slicer", "--export-gcode", "--load", str(PROFILES / f"{profile}.ini")]
    # overrides go in a second ini loaded on top of the profile: the CLI can't take on/off
    # settings as "--key 0", but an ini line handles every setting type the same way
    lines = []
    for k, v in (req.get("overrides") or {}).items():
        k, v = str(k), str(v)
        if not KEY_RE.fullmatch(k) or not VAL_RE.fullmatch(v):
            raise JobError(f"Bad override {k}={v}")
        lines.append(f"{k} = {v}")
    if lines:
        extra = WORK / f"{name}.overrides.ini"
        extra.write_text("\n".join(lines) + "\n")
        args += ["--load", str(extra)]
    out = GCODE / f"{name}.gcode"
    args += ["--center", "125,125", "--output", str(out), str(model)]
    try:
        run(args)
    except JobError:
        run(["xvfb-run", "-a", *args])  # some builds want a display even headless

    data = out.read_bytes()
    cut = data.find(b"; prusaslicer_config = begin")
    if cut > 0:
        data = data[:cut]
        out.write_bytes(data)
    if b"G9111 " not in data[:20000]:
        raise JobError("Sliced file is missing the Kobra start block (G9111) - wrong profile?")
    # "tune" carries calibration steps; top-level "temps" is kept for older callers
    tune = dict(req.get("tune") or {})
    if req.get("temps") and "temps" not in tune:
        tune["temps"] = req["temps"]
    applied, retracts_applied = [], []
    if tune.get("temps"):
        data, applied = apply_temps(data, tune["temps"])
    if tune.get("retracts"):
        base = float((req.get("overrides") or {}).get("retract_length")
                     or profile_value(profile, "retract_length") or 0)
        data, retracts_applied = apply_retracts(data, tune["retracts"], base)
    if applied or retracts_applied:
        out.write_bytes(data)
    tail = data[-6000:].decode(errors="ignore")

    def grab(label):
        m = re.search(rf"^; {re.escape(label)} = (.+)$", tail, re.M)
        return m.group(1).strip() if m else None

    return {"gcode": out.name, "size": len(data), "md5": hashlib.md5(data).hexdigest(),
            "estimated_time": grab("estimated printing time (normal mode)"),
            "filament_g": grab("filament used [g]"), "profile": profile,
            "layers": data.count(b";LAYER_CHANGE"), "temps_applied": applied,
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
        if path == "/files":
            def ls(folder, exts):
                rows = [{"name": p.name, "size": p.stat().st_size, "mtime": int(p.stat().st_mtime)}
                        for p in folder.iterdir() if p.is_file() and p.suffix.lower() in exts]
                return sorted(rows, key=lambda r: -r["mtime"])
            return self._send(200, {"models": ls(MODELS, MODEL_EXT), "gcode": ls(GCODE, (".gcode",))})
        if path == "/watch/last.jpg" and (DETECTOR.dir / "last.jpg").is_file():
            return self._send(200, raw=(DETECTOR.dir / "last.jpg").read_bytes(), ctype="image/jpeg")
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
                    result = DETECTOR.check(url=str(json.loads(self._body()).get("stream_url", "")))
            finally:
                DET_LOCK.release()
            print(f"[print_lab] check: score {result['score']} (best {result.get('view')}) "
                  f"views {result.get('views')} in {result.get('inference_s')}s "
                  f"frame {result.get('frame_size')}", flush=True)
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

    threading.Thread(target=_warm, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8099), Handler).serve_forever()
