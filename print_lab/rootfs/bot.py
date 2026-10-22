#!/usr/bin/env python3
"""Print Lab Telegram bot (runs on the Pi next to Print Lab).

A fallback way to check on and drive the printer from Telegram. It only answers the
chat ids in its config, and never starts or stops anything without a "yes".

  /status        printer state, progress, temperatures, failure score, camera frame
  /files         what Print Lab has (sliced G-code and models)
  /print <name>  stage a Print Lab file on the printer (re-slices a model if needed)
  a link         download a model file, slice it and stage it
  /stop /pause /resume
  yes / no       confirm or cancel the pending action

Everything goes through Print Lab on this machine, which talks to the printer directly,
so the bot keeps working when Home Assistant is down.

Config: $PRINTLAB_DIR/bot.json, written by POST /admin/bot:
  {"telegram_token": "...", "chat_ids": [123]}
The bot waits until the file exists and restarts itself (via systemd) when it changes.
"""
import html
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

LAB = Path(os.environ.get("PRINTLAB_DIR", "/var/lib/printlab"))
CONFIG = LAB / "bot.json"
LAB_URL = os.environ.get("PRINTLAB_URL", "http://127.0.0.1:8099")
MODEL_EXT = (".stl", ".3mf", ".obj", ".step", ".stp")
TOKEN = os.environ.get("PRINTLAB_ADMIN_TOKEN", "")
CONFIRM_FOR = 15 * 60
LINK_RE = re.compile(r"https?://\S+")

HELP = (
    "Print Lab bot\n\n"
    "/status - printer state and a camera frame\n"
    "/files - files Print Lab has\n"
    "/print <name> - put a Print Lab file on the printer\n"
    "Send a link to an STL/3MF/OBJ/STEP file to slice and stage it\n"
    "/pause  /resume  /stop\n"
    "Nothing starts or stops until you reply yes."
)


def log(*args):
    print("[bot]", *args, flush=True)


class Err(Exception):
    pass


def http(method, url, body=None, headers=None, timeout=30, raw=False):
    headers = dict(headers or {})
    data = None
    if isinstance(body, bytes):
        data = body
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = r.read()
    except urllib.error.HTTPError as err:
        text = err.read()[:600].decode(errors="ignore")
        try:
            j = json.loads(text)
            text = j.get("message") or j.get("error") or j.get("description") or text
        except ValueError:
            pass
        raise Err(f"{text.strip() or 'error'} (HTTP {err.code})") from err
    except (urllib.error.URLError, OSError) as err:
        host = urllib.parse.urlsplit(url).hostname
        raise Err(f"can't reach {host} ({getattr(err, 'reason', err)})") from err
    return out if raw else json.loads(out or b"null")


class Telegram:
    def __init__(self, token):
        self.base = f"https://api.telegram.org/bot{token}/"

    def api(self, method, params=None, timeout=30):
        r = http("POST", self.base + method, params or {}, timeout=timeout)
        if not r.get("ok"):
            raise Err(r.get("description", "Telegram error"))
        return r["result"]

    def send(self, chat, text):
        for i in range(0, max(len(text), 1), 4000):
            self.api("sendMessage", {"chat_id": chat, "text": text[i:i + 4000],
                                     "disable_web_page_preview": True})

    def photo(self, chat, jpg, caption=""):
        b = uuid.uuid4().hex
        head = (f'--{b}\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n{chat}\r\n'
                f'--{b}\r\nContent-Disposition: form-data; name="caption"\r\n\r\n{caption[:1000]}\r\n'
                f'--{b}\r\nContent-Disposition: form-data; name="photo"; filename="printer.jpg"\r\n'
                f'Content-Type: image/jpeg\r\n\r\n').encode()
        http("POST", self.base + "sendPhoto", head + jpg + f"\r\n--{b}--\r\n".encode(),
             {"Content-Type": f"multipart/form-data; boundary={b}"}, timeout=60)


class Lab:
    """Print Lab on this machine (slicing, files, camera check and the printer itself)."""
    h = {"X-Admin-Token": TOKEN}

    def get(self, path, timeout=30, raw=False):
        return http("GET", LAB_URL + path, headers=self.h, timeout=timeout, raw=raw)

    def post(self, path, body=None, timeout=60):
        return http("POST", LAB_URL + path, body or {}, self.h, timeout)


def safe_name(text):
    name = re.sub(r"[^A-Za-z0-9_-]+", "_", text).strip("_-")[:50]
    return name or f"model_{int(time.time())}"


def duration(minutes):
    try:
        m = round(float(minutes))
    except (TypeError, ValueError):
        return None
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m"


def find_model_link(url):
    """A direct model link as-is; for a web page, the first model file it links to."""
    path = urllib.parse.urlsplit(url).path.lower()
    if path.endswith(MODEL_EXT):
        return url
    try:
        page = http("GET", url, headers={"User-Agent": "Mozilla/5.0 PrintLab"}, timeout=30, raw=True)
    except Err:
        return None
    text = html.unescape(page[:3_000_000].decode(errors="ignore"))
    for m in re.finditer(r"""https?://[^\s"'<>]+?\.(?:3mf|stl|obj|step|stp)(?:\?[^\s"'<>]*)?""", text, re.I):
        return m.group(0)
    return None


class Bot:
    def __init__(self, cfg):
        self.tg = Telegram(cfg["telegram_token"])
        self.lab = Lab()
        self.chats = {int(c) for c in cfg["chat_ids"]}
        self.pending = {}          # chat -> (action, arg, since)
        self.job = None            # thread doing a slice/stage

    # ------------------------------------------------------------- dispatch
    def handle(self, msg):
        chat = msg.get("chat", {}).get("id")
        sender = msg.get("from", {}).get("id")
        if chat not in self.chats or (sender is not None and sender not in self.chats and chat != sender):
            log(f"ignored a message from {sender} in {chat}")
            return
        text = (msg.get("text") or "").strip()
        if not text:
            return
        word = text.split()[0].lower().split("@")[0]
        rest = text[len(text.split()[0]):].strip()
        try:
            if word in ("/start", "/help"):
                self.tg.send(chat, HELP)
            elif word == "/status":
                self.status(chat)
            elif word == "/files":
                self.files(chat)
            elif word == "/print":
                self.background(chat, self.stage_name, rest) if rest else \
                    self.tg.send(chat, "Which one? /print <name> - see /files")
            elif word in ("/stop", "/pause", "/resume"):
                self.ask(chat, word[1:], None,
                         {"stop": "Stop the current print? It can't be resumed. Reply yes to stop.",
                          "pause": "Pause the print? Reply yes.",
                          "resume": "Resume the print? Reply yes."}[word[1:]])
            elif word in ("yes", "y", "/yes"):
                self.confirm(chat)
            elif word in ("no", "n", "/no", "/cancel", "cancel"):
                self.tg.send(chat, "Cancelled." if self.pending.pop(chat, None) else "Nothing was waiting.")
            elif LINK_RE.search(text):
                self.background(chat, self.stage_link, LINK_RE.search(text).group(0))
            else:
                self.tg.send(chat, "Not sure what you mean. /help lists what I can do.")
        except Err as err:
            self.tg.send(chat, f"That didn't work: {err}")

    def background(self, chat, fn, arg):
        if self.job and self.job.is_alive():
            self.tg.send(chat, "Still busy with the last request; give it a minute.")
            return

        def run():
            try:
                fn(chat, arg)
            except Err as err:
                self.tg.send(chat, f"That didn't work: {err}")
            except Exception as err:  # noqa: BLE001
                log("job crashed:", repr(err))
                self.tg.send(chat, f"Something broke: {type(err).__name__}")

        self.job = threading.Thread(target=run, daemon=True)
        self.job.start()

    def ask(self, chat, action, arg, question):
        self.pending[chat] = (action, arg, time.time())
        self.tg.send(chat, question)

    def confirm(self, chat):
        action, arg, since = self.pending.pop(chat, (None, None, 0))
        if action is None:
            self.tg.send(chat, "Nothing is waiting for a yes.")
            return
        if time.time() - since > CONFIRM_FOR:
            self.tg.send(chat, "That was too long ago; ask again.")
            return
        if action == "start":
            r = self.lab.post("/printer/start", {"confirm": True, "filename": arg}, timeout=120) or {}
            self.tg.send(chat, f"Started {arg}." if r.get("printer_busy") else
                         f"Sent start for {arg}; the printer hasn't reported busy yet. /status in a minute.")
        elif action in ("stop", "pause", "resume"):
            r = self.lab.post(f"/printer/{action}") or {}
            note = " (the printer ignores stop while it's still levelling/heating - try again once it prints)" \
                if action == "stop" else ""
            self.tg.send(chat, f"Sent {action}{'' if r.get('acknowledged') else ' (no acknowledgement yet)'}.{note}")

    # ------------------------------------------------------------- commands
    def status(self, chat):
        try:
            st = self.lab.get("/printer/status")
        except Err as err:
            lines = [f"Can't reach the printer: {err}"]
            try:
                h = self.lab.get("/health", timeout=10)
                lines.append(f"Print Lab is up (detector {h.get('detector')}, {h.get('drive_free_gb')} GB free).")
            except Err as e2:
                lines.append(f"Print Lab isn't answering either: {e2}")
            self.tg.send(chat, "\n".join(lines))
            return
        state = st.get("state")
        job = st.get("job") or {}
        lines = [f"Printer: {job.get('state') or state}"]
        if job:
            lines.append(f"File: {job.get('filename')}")
            lines.append(f"Progress: {job.get('progress')}%  layer {job.get('curr_layer')}/{job.get('total_layers')}")
            left = duration(job.get("remain_time"))
            if left:
                lines.append(f"Time left: {left}")
        lines.append(f"Nozzle {st.get('nozzle_c')}°C, bed {st.get('bed_c')}°C")
        staged = st.get("staged") or {}
        if staged.get("filename") and not staged.get("started_at"):
            lines.append(f"Staged: {staged['filename']}")
        jpg = None
        try:
            r = self.lab.post("/printer/check", timeout=90) or {}
            lines.append(f"Failure check: {float(r.get('score') or 0):.2f} (fails at 0.6 sustained)")
            jpg = self.lab.get("/watch/last.jpg", timeout=15, raw=True)
        except Err as err:
            lines.append(f"(no camera frame: {err})")
        if jpg:
            self.tg.photo(chat, jpg, "\n".join(lines))
        else:
            self.tg.send(chat, "\n".join(lines))

    def lab_files(self):
        return self.lab.get("/files", timeout=15)

    def files(self, chat):
        f = self.lab_files()
        out = ["Sliced (newest first):"]
        out += [f"  {g['name'][:-6]}  ({g['size'] // 1024} KB)" for g in f["gcode"][:25]] or ["  none"]
        out.append("Models:")
        out += [f"  {m['name']}" for m in f["models"][:25]] or ["  none"]
        out.append("\n/print <name> to put one on the printer.")
        self.tg.send(chat, "\n".join(out))

    def stage_name(self, chat, name):
        base = re.sub(r"\.(gcode|stl|3mf|obj|step|stp|scad)$", "", name.strip(), flags=re.I)
        f = self.lab_files()
        gcode = {g["name"][:-6].lower(): g["name"] for g in f["gcode"]}
        models = {m["name"].lower(): m["name"] for m in f["models"]}
        model_by_base = {}
        for m in f["models"]:
            model_by_base.setdefault(m["name"].rsplit(".", 1)[0].lower(), m["name"])
        key = base.lower()
        if key in gcode:
            self.tg.send(chat, f"Putting {gcode[key]} on the printer...")
            r = self.lab.post("/printer/stage", {"gcode": gcode[key]}, timeout=600) or {}
            details = r.get("printer_details") or {}
            est = duration(details["estimated_time_s"] / 60) if details.get("estimated_time_s") else None
            self.ready(chat, r.get("filename", gcode[key]), est, None)
        elif name.lower() in models or key in model_by_base:
            model = models.get(name.lower()) or model_by_base[key]
            self.tg.send(chat, f"Slicing {model} (a few minutes)...")
            r = self.lab.post("/slice", {"name": safe_name(base), "model": model, "stage": True},
                              timeout=1800) or {}
            s = r.get("sliced") or {}
            self.ready(chat, (r.get("staged") or {}).get("filename"), s.get("estimated_time"), s.get("filament_g"))
        else:
            near = [n for n in list(gcode.values()) + list(models.values()) if key in n.lower()][:8]
            self.tg.send(chat, f"Print Lab has nothing called {name}."
                         + ("\nClose matches:\n  " + "\n  ".join(near) if near else "\nSee /files."))

    def stage_link(self, chat, url):
        self.tg.send(chat, "Looking at that link...")
        file_url = find_model_link(url)
        if not file_url:
            self.tg.send(chat, "I couldn't find a model file behind that link. Send the direct download "
                               "link to the STL/3MF (on Printables or Thingiverse: right-click the file's "
                               "download button and copy the link).")
            return
        stem = os.path.basename(urllib.parse.urlsplit(file_url).path).rsplit(".", 1)[0]
        name = safe_name(urllib.parse.unquote(stem))
        self.tg.send(chat, f"Downloading and slicing {name} (a few minutes)...")
        r = self.lab.post("/slice", {"name": name, "url": file_url, "stage": True}, timeout=1800) or {}
        s = r.get("sliced") or {}
        self.ready(chat, (r.get("staged") or {}).get("filename"), s.get("estimated_time"), s.get("filament_g"))

    def ready(self, chat, filename, est, grams):
        if not filename:
            self.tg.send(chat, "It sliced, but nothing got staged on the printer.")
            return
        bits = [f"{filename} is on the printer."]
        if est:
            bits.append(f"About {est}" + (f", {grams} g of filament." if grams else "."))
        bits.append("Bed clear? Reply yes to start, no to leave it.")
        self.ask(chat, "start", filename, "\n".join(bits))

    # ------------------------------------------------------------- loop
    def run(self, mtime):
        me = self.tg.api("getMe")
        log(f"running as @{me.get('username')} for {len(self.chats)} chat(s)")
        offset = None
        while True:
            if CONFIG.stat().st_mtime != mtime:
                log("config changed; restarting")
                return
            try:
                updates = self.tg.api("getUpdates", {"timeout": 50, "offset": offset,
                                                     "allowed_updates": ["message"]}, timeout=65)
            except Err as err:
                log("getUpdates:", err)
                time.sleep(15)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                if "message" in u:
                    try:
                        self.handle(u["message"])
                    except Err as err:
                        log("reply failed:", err)


def main():
    while not CONFIG.is_file():
        time.sleep(30)
    mtime = CONFIG.stat().st_mtime
    try:
        cfg = json.loads(CONFIG.read_text())
        bot = Bot(cfg)
    except (ValueError, KeyError, OSError) as err:
        log("bad config:", err)
        while CONFIG.stat().st_mtime == mtime:
            time.sleep(30)
        sys.exit(0)
    bot.run(mtime)


if __name__ == "__main__":
    main()
