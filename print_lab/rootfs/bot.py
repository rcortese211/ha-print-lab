#!/usr/bin/env python3
"""Print Lab Telegram bot (runs on the Pi next to Print Lab).

A fallback way to check on and drive the printer from Telegram. It only answers the
chat ids in its config, and never starts or stops anything without a "yes".

  /status        printer state, progress, temperatures, failure score, camera frame
  /files         what Print Lab has (sliced G-code and models)
  /print <name>  stage a Print Lab file on the printer (re-slices a model if needed)
  a link         download a model file, slice it and stage it
  a model file   send STL/3MF/OBJ/STEP files or a zip straight to the chat (caption: which/how many)
  archive only   add to a link, file caption or file pick: keep the files on the drive, don't slice
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

import sources

LAB = Path(os.environ.get("PRINTLAB_DIR", "/var/lib/printlab"))
CONFIG = LAB / "bot.json"
LAB_URL = os.environ.get("PRINTLAB_URL", "http://127.0.0.1:8099")
MODEL_EXT = (".stl", ".3mf", ".obj", ".step", ".stp")
TOKEN = os.environ.get("PRINTLAB_ADMIN_TOKEN", "")
CONFIRM_FOR = 15 * 60
STATE = LAB / "bot_state.json"     # queued plates and per-job temperatures, kept across restarts
LINK_RE = re.compile(r"https?://\S+")
# Quality presets for links, as overrides on the tuned profile (pla_08, 0.8 mm nozzle). Its
# flow cap (15 mm3/s) sets the real speed, so these change layers, walls, infill and ironing.
INFILLS = tuple(range(10, 101, 10))
COLOUR_NAMES = {"black": (20, 20, 20), "white": (245, 245, 245), "grey": (128, 128, 128),
                "red": (200, 30, 30), "orange": (240, 120, 20), "yellow": (240, 220, 40),
                "gold": (212, 175, 55), "green": (40, 160, 60), "blue": (40, 80, 200),
                "purple": (130, 50, 170), "pink": (240, 130, 180), "brown": (110, 70, 40),
                "beige": (240, 200, 160), "silver": (190, 190, 195)}


def colour_name(hexcol):
    """#rrggbb -> the nearest plain colour name (for the bot's buttons)."""
    try:
        rgb = tuple(int(str(hexcol).lstrip("#")[k:k + 2], 16) for k in (0, 2, 4))
    except ValueError:
        return "unknown colour"
    return min(COLOUR_NAMES, key=lambda n: sum((a - b) ** 2 for a, b in zip(COLOUR_NAMES[n], rgb)))


def slot_label(s):
    what = " ".join(x for x in (colour_name(s["color"]) if s.get("color") else "", s.get("name") or s.get("material") or "") if x)
    return f"Slot {s['slot']} · {what or 'filament'}"
ARCHIVE_RE = re.compile(r"\b(?:archive(?:\s+only)?|save\s+only|just\s+save(?:\s+it|\s+them)?)\b", re.I)


def archive_flag(text):
    """'archive only' (or 'just save', 'save only') anywhere in a message: keep the files, don't slice.
    Returns (archive?, the text without it)."""
    text = text or ""
    return bool(ARCHIVE_RE.search(text)), " ".join(ARCHIVE_RE.sub(" ", text).split()).strip(" ,;")


INFILL_RE = re.compile(r"\d{2,3}%?")

QUALITY = {
    "fast": ("Fast", {"layer_height": "0.5", "fill_density": "10%", "ironing": "0",
                      "top_solid_layers": "3"}),
    "medium": ("Medium", {}),
    "high": ("High quality", {"layer_height": "0.24", "perimeters": "3", "external_perimeter_speed": "30",
                              "top_solid_layers": "6", "bottom_solid_layers": "5", "fill_density": "20%"}),
}

HELP = (
    "Print Lab bot\n\n"
    "/status - printer state and a camera frame\n"
    "/files - files Print Lab has\n"
    "/frames [n] - camera frames the failure check kept (every 5 min while printing + suspicious ones)\n"
    "/print <name> - put a Print Lab file on the printer\n"
    "archive only - add it to a link, a file's caption or your file pick to just keep the files for later\n"
    "bed 65 - hotter (or cooler) bed for the next thing you send; bed normal to undo\n"
    "first 225 - nozzle temperature for the first layer, same way; first normal to undo\n"
    "Send a link to an STL/3MF/OBJ/STEP file to slice and stage it (fast / medium / high)\n"
    "Several at once: one link per line with a count, e.g. 3x <link> or <link> x5\n"
    "Printables / Thingiverse pages work; say which files: <link> 2 small and 2 large,\n"
    "  or filter by name: all 19x, all except base, 2 of each 19x\n"
    "/print hook x3, clip x5 - same with models Print Lab already has\n"
    "/pause  /resume  /stop\n"
    "Nothing starts or stops until you reply yes."
)


def log(*args):
    print("[bot]", *args, flush=True)


class Err(Exception):
    """data: the error answer's JSON, when there was one (e.g. fit_scale from /slice)."""

    def __init__(self, msg, data=None):
        super().__init__(msg)
        self.data = data or {}


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
        text = err.read()[:4000].decode(errors="ignore")
        j = None
        try:
            j = json.loads(text)
            text = j.get("message") or j.get("error") or j.get("description") or text
        except (ValueError, AttributeError):
            j = None
        raise Err(f"{str(text).strip()[:600] or 'error'} (HTTP {err.code})",
                  j if isinstance(j, dict) else None) from err
    except (urllib.error.URLError, OSError) as err:
        host = urllib.parse.urlsplit(url).hostname
        raise Err(f"can't reach {host} ({getattr(err, 'reason', err)})") from err
    return out if raw else json.loads(out or b"null")


class Telegram:
    def __init__(self, token):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.files = f"https://api.telegram.org/file/bot{token}/"

    def download(self, file_id, limit=20 * 1024 * 1024):
        """A file someone sent the bot (Telegram lets bots fetch up to 20 MB)."""
        info = self.api("getFile", {"file_id": file_id})
        if not info.get("file_path"):
            raise Err("Telegram didn't hand over that file")
        data = http("GET", self.files + info["file_path"], timeout=120, raw=True)
        if len(data) > limit:
            raise Err("that file is over 20 MB")
        return data

    def api(self, method, params=None, timeout=30):
        r = http("POST", self.base + method, params or {}, timeout=timeout)
        if not r.get("ok"):
            raise Err(r.get("description", "Telegram error"))
        return r["result"]

    def buttons(self, chat, text, rows):
        """A message with tap buttons: rows of (label, callback_data)."""
        return self.api("sendMessage", {"chat_id": chat, "text": text, "reply_markup": {
            "inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}})

    def send(self, chat, text):
        for i in range(0, max(len(text), 1), 4000):
            self.api("sendMessage", {"chat_id": chat, "text": text[i:i + 4000],
                                     "disable_web_page_preview": True})

    def photo(self, chat, jpg, caption="", png=False):
        b = uuid.uuid4().hex
        name, ctype = ("plate.png", "image/png") if png else ("printer.jpg", "image/jpeg")
        head = (f'--{b}\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n{chat}\r\n'
                f'--{b}\r\nContent-Disposition: form-data; name="caption"\r\n\r\n{caption[:1000]}\r\n'
                f'--{b}\r\nContent-Disposition: form-data; name="photo"; filename="{name}"\r\n'
                f'Content-Type: {ctype}\r\n\r\n').encode()
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


COUNT_BEFORE = re.compile(r"(\d{1,2})\s*[x×*]\s*$", re.I)
COUNT_AFTER = re.compile(r"^\s*[x×*]\s*(\d{1,2})\b", re.I)
ENTRY_RE = re.compile(r"^(?:(\d{1,2})\s*[x×*]\s*)?(.+?)(?:\s*[x×*]\s*(\d{1,2}))?$", re.I)


def parse_links(text):
    """[(url, count)] from a message; a count goes before or after its link: "3x URL", "URL x3"."""
    items = []
    for m in LINK_RE.finditer(text):
        url = m.group(0).rstrip(".,;)")
        before = text[:m.start()].split("\n")[-1]
        after = text[m.end():].split("\n")[0]
        c = COUNT_AFTER.match(after) or COUNT_BEFORE.search(before)
        items.append((url, max(1, int(c.group(1))) if c else 1))
    return items


def parse_entries(text):
    """[(name, count)] from "/print hook x3, clip x5"."""
    out = []
    for part in re.split(r"[,\n]+", text):
        part = part.strip()
        if not part:
            continue
        m = ENTRY_RE.match(part)
        count = int(m.group(1) or m.group(3) or 1)
        out.append((m.group(2).strip(), max(1, count)))
    return out


def plate_name(labels):
    """A file name for a plate: hook_x3_clip_x5 (or just the model's name for one piece)."""
    if len(labels) == 1 and labels[0][1] == 1:
        return safe_name(labels[0][0])
    return safe_name("_".join(f"{n}_x{c}" if c > 1 else n for n, c in labels))


def describe(labels):
    return ", ".join(f"{n} ×{c}" if c > 1 else n for n, c in labels)


class Bot:
    def __init__(self, cfg):
        self.tg = Telegram(cfg["telegram_token"])
        self.lab = Lab()
        self.chats = {int(c) for c in cfg["chat_ids"]}
        sources.THINGIVERSE_TOKEN = str(cfg.get("thingiverse_token", ""))
        self.pending = {}          # chat -> (action, arg, since)
        self.job = None            # thread doing a slice/stage
        self.lock = threading.Lock()
        self.albums = {}           # media_group_id -> files sent together, gathered for a moment
        self.plates = {}           # chat -> {"jobs": [...], "total": n}: plates still to slice
        self.bed = {}              # chat -> bed temperature for the next job (all its plates)
        self.first = {}            # chat -> nozzle temperature for the first layer, same scope
        self.load_state()

    def load_state(self):
        try:
            st = json.loads(STATE.read_text())
        except (OSError, ValueError):
            return
        for key in ("plates", "bed", "first"):
            getattr(self, key).update({int(c): v for c, v in (st.get(key) or {}).items()})

    def save_state(self):
        try:
            tmp = STATE.with_suffix(".tmp")
            tmp.write_text(json.dumps({"plates": self.plates, "bed": self.bed, "first": self.first}))
            tmp.replace(STATE)
        except OSError as err:
            log("couldn't save bot state:", err)

    # ------------------------------------------------------------- dispatch
    def handle(self, msg):
        chat = msg.get("chat", {}).get("id")
        sender = msg.get("from", {}).get("id")
        if chat not in self.chats or (sender is not None and sender not in self.chats and chat != sender):
            log(f"ignored a message from {sender} in {chat}")
            return
        doc = msg.get("document")
        if doc:
            self.take_document(chat, msg)
            return
        text = (msg.get("text") or msg.get("caption") or "").strip()
        if not text:
            what = next((k for k in ("photo", "video", "voice", "sticker", "audio", "animation") if k in msg),
                        "that")
            self.tg.send(chat, f"I can't do anything with a {what}. Send a model file (STL, 3MF, OBJ, STEP), "
                         "a link to one, or /help.")
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
            elif word == "/frames":
                self.background(chat, self.frames, rest)
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
            elif word in ("bed", "/bed"):
                self.set_bed(chat, rest)
            elif word in ("first", "/first", "firstlayer"):
                self.set_first(chat, rest)
            elif word in ("next", "/next"):
                self.next_plate(chat)
            elif word in QUALITY:
                self.choose_quality(chat, word)
            elif INFILL_RE.fullmatch(word) and self.pending.get(chat, ("",))[0] == "infill":
                self.choose_infill(chat, int(word.rstrip("%")))
            elif LINK_RE.search(text):
                self.background(chat, self.stage_links, (parse_links(text), LINK_RE.sub(" ", text).strip()))
            elif self.pending.get(chat, ("",))[0] == "pick":
                self.background(chat, self.pick_files, text)
            else:
                self.tg.send(chat, "Not sure what you mean. /help lists what I can do.")
        except Err as err:
            self.tg.send(chat, f"That didn't work: {err}")

    def handle_button(self, cq):
        sender = cq.get("from", {}).get("id")
        msg = cq.get("message") or {}
        chat = msg.get("chat", {}).get("id")
        self.tg.api("answerCallbackQuery", {"callback_query_id": cq["id"]})
        if sender not in self.chats or chat not in self.chats:
            log(f"ignored a button press from {sender}")
            return
        data = str(cq.get("data", ""))
        if data.startswith("q:") and data[2:] in QUALITY:
            try:   # drop the buttons so they can't be pressed twice
                self.tg.api("editMessageReplyMarkup", {"chat_id": chat, "message_id": msg.get("message_id"),
                                                       "reply_markup": {"inline_keyboard": []}})
            except Err:
                pass
            self.choose_quality(chat, data[2:])
        elif data in ("c:one", "c:per") or (data.startswith("s:") and data[2:].isdigit()):
            try:
                self.tg.api("editMessageReplyMarkup", {"chat_id": chat, "message_id": msg.get("message_id"),
                                                       "reply_markup": {"inline_keyboard": []}})
            except Err:
                pass
            if data.startswith("c:"):
                self.choose_colour_mode(chat, data[2:])
            else:
                self.choose_slot(chat, int(data[2:]))
        elif data in ("z:scale", "z:split", "z:full", "z:fullsplit"):
            try:
                self.tg.api("editMessageReplyMarkup", {"chat_id": chat, "message_id": msg.get("message_id"),
                                                       "reply_markup": {"inline_keyboard": []}})
            except Err:
                pass
            self.choose_resize(chat, data[2:])
        elif data.startswith("o:") and data[2:].isdigit():
            try:
                self.tg.api("editMessageReplyMarkup", {"chat_id": chat, "message_id": msg.get("message_id"),
                                                       "reply_markup": {"inline_keyboard": []}})
            except Err:
                pass
            self.choose_order(chat, int(data[2:]))
        elif data.startswith("i:") and data[2:].isdigit():
            try:
                self.tg.api("editMessageReplyMarkup", {"chat_id": chat, "message_id": msg.get("message_id"),
                                                       "reply_markup": {"inline_keyboard": []}})
            except Err:
                pass
            self.choose_infill(chat, int(data[2:]))

    def background(self, chat, fn, arg):
        if self.job and self.job.is_alive():
            self.tg.send(chat, "Still busy with the last request; give it a minute.")
            return

        def run():
            try:
                fn(chat, arg)
            except (Err, sources.SourceError) as err:
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
        def temp(now, target):
            return f"{now}°C → {target}°C" if target else f"{now}°C"
        lines.append(f"Nozzle {temp(st.get('nozzle_c'), st.get('nozzle_target_c'))}, "
                     f"bed {temp(st.get('bed_c'), st.get('bed_target_c'))}")
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

    def frames(self, chat, rest):
        """Send the camera frames the failure check kept (newest first)."""
        n = max(1, min(int(rest) if rest.strip().isdigit() else 4, 10))
        kept = self.lab.get("/frames").get("frames") or []
        if not kept:
            self.tg.send(chat, "No kept frames yet.")
            return
        for name in kept[:n]:
            day, fname = name.split("/")
            parts = fname[:-4].split("-")
            when = f"{day} {parts[0][:2]}:{parts[0][2:4]}:{parts[0][4:]}"
            caption = f"{when}  score {parts[1]}" + ("  (camera looked frozen)" if "frozen" in parts else "")
            self.tg.photo(chat, self.lab.get(f"/frames/{name}", raw=True), caption)
        if len(kept) > n:
            self.tg.send(chat, f"{len(kept) - n} older frames kept; /frames 10 for more.")

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

    def stage_name(self, chat, text):
        """/print: one sliced file goes straight on the printer; models (any count) get sliced."""
        entries = parse_entries(text)
        f = self.lab_files()
        gcode = {g["name"][:-6].lower(): g["name"] for g in f["gcode"]}
        models = {m["name"].lower(): m["name"] for m in f["models"]}
        for m in f["models"]:
            models.setdefault(m["name"].rsplit(".", 1)[0].lower(), m["name"])
        if len(entries) == 1 and entries[0][1] == 1:
            key = re.sub(r"\.gcode$", "", entries[0][0], flags=re.I).lower()
            if key in gcode:
                self.tg.send(chat, f"Putting {gcode[key]} on the printer...")
                r = self.lab.post("/printer/stage", {"gcode": gcode[key]}, timeout=600) or {}
                details = r.get("printer_details") or {}
                est = duration(details["estimated_time_s"] / 60) if details.get("estimated_time_s") else None
                self.ready(chat, r.get("filename", gcode[key]), est, None)
                return
        items, labels, missing = [], [], []
        for name, count in entries:
            key = name.lower()
            model = models.get(key) or models.get(re.sub(r"\.(stl|3mf|obj|step|stp|scad)$", "", key))
            if not model:
                missing.append(name + (" (only sliced G-code, which can't share a plate)"
                                       if key.removesuffix(".gcode") in gcode else ""))
                continue
            items.append({"model": model, "count": count})
            labels.append((model.rsplit(".", 1)[0], count))
        if missing:
            near = [n for n in models.values() if any(m.split()[0].lower() in n.lower() for m in missing)][:8]
            self.tg.send(chat, "Print Lab doesn't have: " + "; ".join(missing)
                         + ("\nClose matches:\n  " + "\n  ".join(near) if near else "\nSee /files."))
            return
        self.ask_colours(chat, items, labels)

    def stage_links(self, chat, job):
        links, words = job
        archive, words = archive_flag(words)
        self.tg.send(chat, "Looking at that link..." if len(links) == 1 else f"Looking at {len(links)} links...")
        if len(links) == 1:
            url, count = links[0]
            title, cands = sources.resolve(url)
            return self.choose_candidates(chat, title, cands, words, archive, count)
        picked, problems = [], []
        for n, (url, count) in enumerate(links, 1):
            try:
                _title, cands = sources.resolve(url)
            except sources.SourceError as err:
                problems.append(f"link {n}: {err}")
                continue
            if len(cands) == 1:
                picked.append((cands[0], count))
            else:
                problems.append(f"link {n} has {len(cands)} files - send it on its own and say which ones")
        if problems:
            self.tg.send(chat, "Couldn't do that batch:\n" + "\n".join(problems))
            return
        self.fetch_and_ask(chat, picked, archive)

    def choose_candidates(self, chat, title, cands, words, archive=False, count=1):
        """Files from a link, zip or upload: the words say which and how many; with none, ask
        (or, when archiving, keep them all)."""
        if not words and (len(cands) == 1 or archive):
            return self.fetch_and_ask(chat, [(c, count) for c in cands], archive)
        if not words:
            return self.list_files_to_pick(chat, title, cands, archive=archive)
        picked, problems = sources.select(words, cands)
        if problems or not picked:
            return self.list_files_to_pick(chat, title, cands, problems, archive)
        return self.fetch_and_ask(chat, picked, archive)

    def list_files_to_pick(self, chat, title, cands, problems=None, archive=False):
        self.pending[chat] = ("pick", (title, cands, archive), time.time())
        lines = [f"Hmm: {'; '.join(problems)}.", ""] if problems else []
        lines.append(f"{title} has {len(cands)} files:")
        for i, c in enumerate(cands, 1):
            lines.append(f"  {i}. {c.name}" + (f"  ({max(1, round(c.size / 1024))} KB)" if c.size else ""))
        lines.append(('Which ones should I keep? e.g. "all", "all except base"' if archive else
                      'Which ones, and how many? e.g. "2 small, 2 large", "1 x2, 2 x2", '
                      '"all 19x", "2 of each except base"')
                     + ("" if archive else '. Add "archive only" to just keep them for later.'))
        self.tg.send(chat, "\n".join(lines))

    def pick_files(self, chat, text):
        _action, (title, cands, *rest), _since = self.pending[chat]
        archive, text = archive_flag(text)
        archive = archive or bool(rest and rest[0])
        if archive and not text:
            text = "all"
        picked, problems = sources.select(text, cands)
        if problems or not picked:
            return self.list_files_to_pick(chat, title, cands, problems or ["I didn't catch which files"], archive)
        del self.pending[chat]
        self.fetch_and_ask(chat, picked, archive)

    def fetch_and_ask(self, chat, picked, archive=False):
        """Download the chosen files into Print Lab's models folder, then ask for the quality
        (archive: just keep them there for later)."""
        models = LAB / "models"
        items, labels = [], []
        for cand, count in picked:
            ext = os.path.splitext(cand.name)[1].lower()
            fname = safe_name(cand.stem) + ext
            dest = models / fname
            if not dest.is_file() or (cand.size and dest.stat().st_size != cand.size):
                data = cand.get()
                if not data:
                    raise Err(f"{cand.name} downloaded empty")
                tmp = models / f".{fname}.part"
                tmp.write_bytes(data)
                tmp.replace(dest)
            items.append({"model": fname, "count": count})
            labels.append((cand.stem, count))
        if archive:
            names = list(dict.fromkeys(i["model"] for i in items))
            self.tg.send(chat, f"Saved {len(names)} file" + ("s" if len(names) != 1 else "")
                         + " to the models drive, not sliced:\n  " + "\n  ".join(names)
                         + "\n\nTo print later: /print " + names[0].rsplit(".", 1)[0]
                         + (" (or several: /print a, b x2)" if len(names) > 1 else ""))
            return
        self.ask_colours(chat, items, labels)

    # ------------------------------------------------------------- colours
    def take_document(self, chat, msg):
        """A model file sent straight to the bot. Several sent together (an album) make one plate."""
        doc = msg["document"]
        name = doc.get("file_name") or "model"
        ext = os.path.splitext(name)[1].lower()
        if ext not in MODEL_EXT + (".zip",):
            self.tg.send(chat, f"{name} isn't a model file I can slice (STL, 3MF, OBJ, STEP, or a zip of them).")
            return
        if (doc.get("file_size") or 0) > 20 * 1024 * 1024:
            self.tg.send(chat, f"{name} is over 20 MB, which is as big as Telegram lets me fetch. "
                         "Send a link to it instead.")
            return
        caption = (msg.get("caption") or "").strip()
        entry = (doc["file_id"], name, caption)
        group = msg.get("media_group_id")
        if not group:
            self.background(chat, self.fetch_documents, [entry])
            return
        # an album arrives as one message per file: collect them for a moment, then do them together
        with self.lock:
            batch = self.albums.setdefault(group, [])
            batch.append(entry)
            first = len(batch) == 1
        if first:
            def go():
                with self.lock:
                    entries = self.albums.pop(group, [])
                self.background(chat, self.fetch_documents, entries)
            threading.Timer(3.0, go).start()

    def fetch_documents(self, chat, entries):
        """Files (and zips of files) sent to the chat become candidates, picked like a link's files:
        a caption says which and how many ("all 2x", "all except base"), else I ask when there are several."""
        cands, words = [], []
        self.tg.send(chat, "Got " + (entries[0][1] if len(entries) == 1 else f"{len(entries)} files") + ".")
        for file_id, name, caption in entries:
            data = self.tg.download(file_id)
            if not data:
                raise Err(f"{name} came through empty")
            if name.lower().endswith(".zip"):
                inside = sources._zip_candidates(data)
                if not inside:
                    raise Err(f"{name} has no model files in it (STL, 3MF, OBJ or STEP)")
                cands += inside
            else:
                cands.append(sources.Candidate(name, len(data), lambda d=data: d))
            if caption:
                words.append(caption)
        title = entries[0][1] if len(entries) == 1 else f"those {len(entries)} files"
        archive, text = archive_flag(" ".join(words))
        if len(cands) == 1:
            m = re.search(r"(\d{1,2})\s*[x×*]|[x×*]\s*(\d{1,2})", text)
            count = max(1, min(50, int(m.group(1) or m.group(2)))) if m else 1
            return self.fetch_and_ask(chat, [(cands[0], count)], archive)
        return self.choose_candidates(chat, title, cands, text, archive)

    def ask_colours(self, chat, items, labels):
        """Several different parts: offer one colour per part from what's loaded in the ACE."""
        if len(items) < 2:
            return self.ask_quality(chat, items, labels)
        self.pending[chat] = ("colours", (items, labels), time.time())
        self.tg.buttons(chat, f"{describe(labels)}. One colour, or a colour per part?",
                        [[("One colour", "c:one"), ("Colour per part", "c:per")]])

    def choose_colour_mode(self, chat, mode):
        action, arg, since = self.pending.get(chat, (None, None, 0))
        if action != "colours" or time.time() - since > CONFIRM_FOR:
            self.tg.send(chat, "Send me a link (or /print a model) first.")
            return
        items, labels = arg
        if mode == "one":
            return self.ask_quality(chat, items, labels)
        try:
            slots = [s for s in (self.lab.get("/printer/ace", timeout=20) or {}).get("slots", []) if s.get("loaded")]
        except Err as err:
            self.tg.send(chat, f"Couldn't read the ACE ({err}), so this one prints in one colour.")
            return self.ask_quality(chat, items, labels)
        if not slots:
            self.tg.send(chat, "The ACE reports no filament loaded, so this one prints in one colour.")
            return self.ask_quality(chat, items, labels)
        self.pending[chat] = ("slot", (items, labels, slots, []), time.time())
        self.ask_slot(chat)

    def ask_slot(self, chat):
        _action, (items, labels, slots, picks), _since = self.pending[chat]
        i = len(picks)
        part, count = labels[i]
        self.tg.buttons(chat, f"{part}" + (f" (x{count})" if count > 1 else "")
                        + f": which slot? ({i + 1} of {len(items)})",
                        [[(slot_label(s), f"s:{s['slot']}")] for s in slots])

    def choose_slot(self, chat, slot):
        action, arg, since = self.pending.get(chat, (None, None, 0))
        if action != "slot" or time.time() - since > CONFIRM_FOR:
            self.tg.send(chat, "Send me a link (or /print a model) first.")
            return
        items, labels, slots, picks = arg
        by_slot = {s["slot"]: s for s in slots}
        if slot not in by_slot:
            self.tg.send(chat, f"Slot {slot} has nothing loaded.")
            return
        picks = picks + [slot]
        if len(picks) < len(items):
            self.pending[chat] = ("slot", (items, labels, slots, picks), time.time())
            return self.ask_slot(chat)
        order = list(dict.fromkeys(picks))          # colour 1 = the first slot picked, ...
        filaments = []
        for n in order:
            s = by_slot[n]
            f = {"slot": n, "color": s.get("color") or "#ffffff", "material": s.get("material") or "PLA"}
            if s.get("temp") and 170 <= int(s["temp"]) <= 280:
                f["temp"] = int(s["temp"])
            filaments.append(f)
        items = [{**it, "filament": order.index(p) + 1} for it, p in zip(items, picks)]
        self.tg.send(chat, "Colours:\n" + "\n".join(
            f"  {part}" + (f" x{count}" if count > 1 else "") + f" → {slot_label(by_slot[p])}"
            for (part, count), p in zip(labels, picks)))
        self.ask_quality(chat, items, labels, filaments)

    def ask_quality(self, chat, items, labels, filaments=None):
        self.pending[chat] = ("quality", (items, labels, filaments), time.time())
        what = describe(labels)
        pieces = sum(c for _, c in labels)
        self.tg.buttons(chat, f"{what}" + (f" ({pieces} pieces on one plate)" if pieces > 1 else "")
                        + ". How should I print " + ("them?" if pieces > 1 else "it?"),
                        [[(QUALITY[k][0], f"q:{k}") for k in ("fast", "medium", "high")]])

    def choose_quality(self, chat, key):
        action, arg, since = self.pending.get(chat, (None, None, 0))
        if action != "quality" or time.time() - since > CONFIRM_FOR:
            self.tg.send(chat, "Send me a link (or /print a model) first.")
            return
        # second question: infill, 10-100 % in steps of 10
        self.pending[chat] = ("infill", (arg[0], arg[1], key, arg[2] if len(arg) > 2 else None), time.time())
        self.tg.buttons(chat, f"{QUALITY[key][0]}. How much infill?",
                        [[(f"{n}%", f"i:{n}") for n in INFILLS[:5]],
                         [(f"{n}%", f"i:{n}") for n in INFILLS[5:]]])

    def choose_infill(self, chat, pct):
        action, arg, since = self.pending.get(chat, (None, None, 0))
        if action != "infill" or time.time() - since > CONFIRM_FOR:
            self.tg.send(chat, "Send me a link (or /print a model) first.")
            return
        if pct not in INFILLS:
            self.tg.send(chat, "Infill goes from 10% to 100% in steps of 10.")
            return
        del self.pending[chat]
        self.background(chat, self.slice_plate, (*arg, pct))

    def slice_plate(self, chat, job, plate=None):
        """job: (items, labels, quality key, filaments, infill[, scale %[, full bed]]).
        plate: (n, total) when it's one of several plates."""
        items, labels, key, filaments, infill = job[:5]
        scale = job[5] if len(job) > 5 else 100
        full = bool(job[6]) if len(job) > 6 else False
        label, overrides = QUALITY[key]
        name = plate_name(labels) + (f"_{scale:g}pct" if scale != 100 else "") + ("_full" if full else "")
        self.tg.send(chat, (f"Plate {plate[0]} of {plate[1]}: " if plate else "")
                     + f"Slicing {describe(labels)} ({label}, {infill}% infill"
                     + (f", scaled to {scale:g}%" if scale != 100 else "")
                     + (", full size edge to edge" if full else "") + ", a few minutes)...")
        # PrusaSlicer applies the preset as overrides; OrcaSlicer uses Anycubic's own process.
        # The infill answer goes on top of either.
        body = {"name": name, "stage": True, "quality": key, "quality_overrides": overrides,
                "overrides": {"fill_density": f"{infill}%"}}
        if filaments:
            body["filaments"] = filaments
        if scale != 100:
            body["scale"] = scale
        bed = self.bed.get(chat)
        if bed:
            body["overrides"].update({"bed_temperature": str(bed), "first_layer_bed_temperature": str(bed)})
        first = self.first.get(chat)
        if first:
            body["overrides"]["first_layer_temperature"] = str(first)
        if full:
            body["full_bed"] = True
        if len(items) == 1 and items[0]["count"] == 1 and not filaments:
            body.update({k: v for k, v in items[0].items() if k not in ("count", "filament")})
        else:
            body["models"] = items
        try:
            r = self.lab.post("/slice", body, timeout=1800) or {}
        except Err as err:
            if not plate and (err.data.get("fit_scale") or err.data.get("separate")):
                self.offer_resize(chat, job, err)
                return
            raise
        s = r.get("sliced") or {}
        notes = []
        if s.get("nozzle"):
            lh = (s.get("orca") or {}).get("layer_height")
            notes.append(f"{s['nozzle']} mm nozzle, {label.lower()}" + (f" ({lh} mm layers)" if lh else "")
                         + f", {infill}% infill"
                         + (" - the printer didn't answer, so that's the last nozzle it reported"
                            if s.get("nozzle_from") == "last known" else "") + ".")
        if s.get("colours"):
            notes.append("Colours: " + ", ".join(
                f"slot {c['slot']} {colour_name(c['color'])} {c['material']}"
                + (f" ({c['grams']:g} g)" if c.get("grams") else "") for c in s["colours"])
                + (f"; {s['colour_changes']} colour change" + ("s" if s["colour_changes"] != 1 else "")
                   + " (about 2 min each at the ACE)" if s.get("colour_changes") else "") + ".")
        for model, o in (s.get("orientation") or {}).items():
            if o.get("rotated"):
                notes.append(f"Turned {model.rsplit('.', 1)[0]} onto its best face "
                             f"({o['before']['contact_mm2']:.0f} → {o['contact_mm2']:.0f} mm² on the bed).")
        if s.get("supports"):
            notes.append("Supports are on: some overhangs can't print without them.")
        if s.get("brim"):
            notes.append("Brim added: tall or small-footprint pieces hold on better with one.")
        if scale != 100:
            notes.append(f"Scaled to {scale:g}% (every part the same, so they still fit together).")
        if bed or first:
            notes.append("You asked for: " + ", ".join(
                x for x in (f"bed at {bed} °C" if bed else "", f"first layer at {first} °C" if first else "") if x)
                + ".")
        if full:
            notes.append("Full size, edge to edge: no brim or skirt, and it runs right up to the bed's "
                         "edges, so check the sheet is seated square.")
        left = self.plates.get(chat, {}).get("jobs")
        if not left:
            self.bed.pop(chat, None)       # the job's done: next one is back to normal
            self.first.pop(chat, None)
            self.save_state()
        if plate and left:
            notes.append(f"Plate {plate[0]} of {plate[1]}. When it's off the bed, send next for plate "
                         f"{plate[0] + 1}.")
        elif plate:
            notes.append(f"Last plate ({plate[1]} of {plate[1]}).")
        self.ready(chat, (r.get("staged") or {}).get("filename"), s.get("estimated_time"),
                   s.get("filament_g"), s.get("preview"), describe(labels), notes)

    def set_bed(self, chat, arg):
        arg = arg.strip().lower().rstrip("c°").strip()
        if arg in ("", "normal", "default", "off", "reset"):
            self.bed.pop(chat, None)
            self.save_state()
            self.tg.send(chat, "Bed back to the profile's temperature (55 °C for PLA).")
            return
        try:
            temp = int(float(arg))
        except ValueError:
            self.tg.send(chat, "bed <temperature>, e.g. bed 65 (or bed normal)")
            return
        if not 40 <= temp <= 100:
            self.tg.send(chat, "Bed temperature goes from 40 to 100 °C.")
            return
        self.bed[chat] = temp
        self.save_state()
        self.tg.send(chat, f"Bed at {temp} °C for the next thing you send (every plate of it). "
                     "bed normal to undo.")

    def set_first(self, chat, arg):
        arg = arg.strip().lower().rstrip("c°").strip()
        if arg in ("", "normal", "default", "off", "reset"):
            self.first.pop(chat, None)
            self.save_state()
            self.tg.send(chat, "First layer back to the profile's temperature (215 °C for PLA).")
            return
        try:
            temp = int(float(arg))
        except ValueError:
            self.tg.send(chat, "first <temperature>, e.g. first 225 (or first normal)")
            return
        if not 180 <= temp <= 260:
            self.tg.send(chat, "First-layer nozzle temperature goes from 180 to 260 °C.")
            return
        self.first[chat] = temp
        self.save_state()
        self.tg.send(chat, f"First layer at {temp} °C on the nozzle for the next thing you send "
                     "(every plate of it). first normal to undo.")

    def offer_resize(self, chat, job, err):
        """The plate doesn't fit: offer to scale it, and/or put each piece on its own plate."""
        fit, separate = err.data.get("fit_scale"), err.data.get("separate")
        full = err.data.get("full_bed")
        items, labels = job[0], job[1]
        pieces = sum(c for _, c in labels)
        self.pending[chat] = ("resize", (job, fit, separate, full), time.time())
        msg = str(err).replace("Couldn't lay out the plate: ", "").split(" (HTTP")[0]
        text = f"That won't fit: {msg}."
        if separate and pieces > 1:
            choice = f"Scale everything to {fit}%, one plate per piece" if fit else "One plate per piece"
            text += (f"\n\nI can slice them as {pieces} plates, one after the other"
                     + (f", all scaled to {fit}% so the parts still match" if fit else "") + ".")
            rows = [[(choice, "z:split")]]
            if full:
                text += " Or full size: each one alone fits if I use the bed edge to edge."
                rows.append([("Full size, one plate per piece, edge to edge", "z:fullsplit")])
        else:
            text += f"\n\nI can scale it to {fit}% to fit." if fit else ""
            rows = [[(f"Scale to {fit}%", "z:scale")]] if fit else []
            if full:
                text += " Or print it full size using the bed edge to edge."
                rows.append([("Full size, edge to edge", "z:full")])
        self.tg.buttons(chat, text + " Or send no to leave it.", rows)

    def choose_resize(self, chat, how):
        action, arg, since = self.pending.get(chat, (None, None, 0))
        if action != "resize" or time.time() - since > CONFIRM_FOR:
            self.tg.send(chat, "That offer ran out; send the link again.")
            return
        del self.pending[chat]
        job, fit, _separate, _full = (*arg, False)[:4]
        items, labels, key, filaments, infill = job[:5]
        full = how in ("full", "fullsplit")
        scale = 100 if full else (fit or 100)
        if how in ("scale", "full"):
            self.background(chat, self.slice_plate, (items, labels, key, filaments, infill, scale, full))
            return
        # one plate per copy of each piece, each with just its own colour
        jobs = []
        for item, (lab, count) in zip(items, labels):
            fil = None
            one = {k: v for k, v in item.items() if k not in ("count", "filament")}
            if filaments:
                fil = [filaments[max(1, int(item.get("filament") or 1)) - 1]]
            for _ in range(int(item.get("count") or count or 1)):
                jobs.append(([dict(one, count=1)], [(lab, 1)], key, fil, infill, scale, full))
        self.plates[chat] = {"jobs": jobs, "total": len(jobs), "next": 1}
        self.save_state()
        self.next_plate(chat)

    def next_plate(self, chat):
        """Slice the next plate; when more than one is left, ask which goes next."""
        q = self.plates.get(chat)
        if not q or not q["jobs"]:
            self.plates.pop(chat, None)
            self.save_state()
            self.tg.send(chat, "No more plates waiting.")
            return
        if len(q["jobs"]) == 1:
            self.run_plate(chat, 0)
            return
        self.pending[chat] = ("order", None, time.time())
        names = [describe(j[1]) for j in q["jobs"]]
        seen = {}
        rows = []
        for i, n in enumerate(names):
            seen[n] = seen.get(n, 0) + 1
            label = n + (f" (copy {seen[n]})" if names.count(n) > 1 else "")
            rows.append([(label, f"o:{i}")])
        self.tg.buttons(chat, f"Plate {q['next']} of {q['total']}: which one should I print "
                        + ("first?" if q["next"] == 1 else "next?"), rows)

    def choose_order(self, chat, i):
        action, _arg, _since = self.pending.get(chat, (None, None, 0))
        q = self.plates.get(chat)
        if action != "order" or not q or not 0 <= i < len(q["jobs"]):
            self.tg.send(chat, "That choice ran out; send next to pick again.")
            return
        del self.pending[chat]
        self.run_plate(chat, i)

    def run_plate(self, chat, i):
        q = self.plates[chat]
        job = q["jobs"].pop(i)
        n, total = q["next"], q["total"]
        q["next"] += 1
        if not q["jobs"]:
            self.plates.pop(chat, None)
        self.save_state()

        def run(chat, job):
            try:
                self.slice_plate(chat, job, plate=(n, total))
            except Exception:
                # keep it queued (e.g. the printer was still busy): send next to try again
                q2 = self.plates.setdefault(chat, {"jobs": [], "total": total, "next": n})
                q2["jobs"].insert(0, job)
                q2["next"] = min(q2["next"], n)
                self.save_state()
                raise

        self.background(chat, run, job)

    def ready(self, chat, filename, est, grams, preview=None, what=None, notes=None):
        if not filename:
            self.tg.send(chat, "It sliced, but nothing got staged on the printer.")
            return
        bits = [f"{what or filename} is on the printer."]
        if est:
            bits.append(f"About {est}" + (f", {grams} g of filament." if grams else "."))
        bits += notes or []
        bits.append("Bed clear? Reply yes to start, no to leave it.")
        text = "\n".join(bits)
        self.pending[chat] = ("start", filename, time.time())
        png = None
        if preview:
            try:
                png = self.lab.get(preview, timeout=60, raw=True)
            except Err as err:
                log("no preview:", err)
        if png:
            self.tg.photo(chat, png, text, png=True)
        else:
            self.tg.send(chat, text)

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
                                                     "allowed_updates": ["message", "callback_query"]}, timeout=65)
            except Err as err:
                log("getUpdates:", err)
                time.sleep(15)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                if "callback_query" in u:
                    try:
                        self.handle_button(u["callback_query"])
                    except Err as err:
                        log("button failed:", err)
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
