"""Print Lab slice server (runs inside the toolchain image on the external drive).

Internal Supervisor network only. Endpoints:
  GET  /health
  POST /slice            {name, scad|url|model, profile?, overrides?}
  GET  /gcode/<name>
  GET  /profiles
  PUT  /profiles/<name>  (body: ini text)
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LAB = Path(os.environ.get("PRINTLAB_DIR", "/lab"))
MODELS, GCODE, PROFILES, WORK = (LAB / d for d in ("models", "gcode", "profiles", "work"))
NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,60}")
KEY_RE = re.compile(r"[a-z0-9_]{1,60}")
VAL_RE = re.compile(r"[A-Za-z0-9_.,%+ -]{0,60}")
MODEL_EXT = (".stl", ".3mf", ".obj", ".step", ".stp", ".scad")
MAX_DOWNLOAD = 200 * 1024 * 1024
LOCK = threading.Lock()


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
    for k, v in (req.get("overrides") or {}).items():
        k, v = str(k), str(v)
        if not KEY_RE.fullmatch(k) or not VAL_RE.fullmatch(v):
            raise JobError(f"Bad override {k}={v}")
        args += [f"--{k.replace('_', '-')}", v]
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
    tail = data[-6000:].decode(errors="ignore")

    def grab(label):
        m = re.search(rf"^; {re.escape(label)} = (.+)$", tail, re.M)
        return m.group(1).strip() if m else None

    return {"gcode": out.name, "size": len(data), "md5": hashlib.md5(data).hexdigest(),
            "estimated_time": grab("estimated printing time (normal mode)"),
            "filament_g": grab("filament used [g]"), "profile": profile,
            "layers": data.count(b";LAYER_CHANGE")}


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

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/health":
            free = shutil.disk_usage(LAB)
            return self._send(200, {"ok": True, "prusaslicer": version(["prusa-slicer", "--help"]),
                                    "openscad": version(["openscad", "--version"]),
                                    "drive_free_gb": round(free.free / 1e9, 1),
                                    "profiles": sorted(p.stem for p in PROFILES.glob("*.ini"))})
        if path == "/profiles":
            return self._send(200, {"profiles": sorted(p.stem for p in PROFILES.glob("*.ini"))})
        m = re.fullmatch(r"/gcode/([A-Za-z0-9_-]{1,60}\.gcode)", path)
        if m and (GCODE / m.group(1)).is_file():
            return self._send(200, raw=(GCODE / m.group(1)).read_bytes(), ctype="text/plain")
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if urllib.parse.urlsplit(self.path).path != "/slice":
            return self._send(404, {"error": "not found"})
        try:
            req = json.loads(self._body())
            if not LOCK.acquire(timeout=600):
                raise JobError("Another slice is still running")
            try:
                self._send(200, slice_job(req))
            finally:
                LOCK.release()
        except (JobError, ValueError, subprocess.TimeoutExpired) as err:
            self._send(400, {"error": str(err)})

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
    ThreadingHTTPServer(("0.0.0.0", 8099), Handler).serve_forever()
