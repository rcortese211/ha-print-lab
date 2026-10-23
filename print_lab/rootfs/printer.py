"""Direct LAN control of an Anycubic Kobra (stock firmware, LAN Mode on).

The same protocol the Anycubic Home Assistant integration speaks: GET :18910/info, a
signed POST to the printer's ctrl URL, AES-CBC-decrypt the answer to get the printer's
local MQTT login, then short MQTT sessions over TLS to :9883 for queries and commands,
and a multipart POST to :18910/gcode_upload for files.

Only the printer's own address is ever contacted: the host in every URL the printer
hands back is replaced with the configured one.
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import random
import re
import ssl
import string
import threading
import time
import urllib.parse
import urllib.request
import uuid

BASE = "anycubic/anycubicCloud/v1"
INFO_PORT = 18910


class PrinterError(Exception):
    """A printer step failed; the message is meant for the user."""


# --------------------------------------------------------------------------- handshake
def _sign(token: str, ts: int, nonce: str) -> str:
    first = hashlib.md5(token[:16].encode()).hexdigest()
    return hashlib.md5((first + str(ts) + nonce).encode()).hexdigest()


def _decrypt(info_b64: str, token: str, local_token: str) -> dict:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.padding import PKCS7

    key = token[16:32].encode()
    iv = local_token.encode()[:16].ljust(16, b"\0")
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = dec.update(base64.b64decode(info_b64)) + dec.finalize()
    unpad = PKCS7(128).unpadder()
    return json.loads((unpad.update(padded) + unpad.finalize()).decode())


def _http_json(host: str, port: int, method: str, path: str, timeout: float = 8) -> dict:
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request(method, path)
        resp = conn.getresponse()
        return json.loads(resp.read(256 * 1024))
    except (OSError, http.client.HTTPException, ValueError) as err:
        raise PrinterError(f"The printer at {host} didn't answer ({err})") from err
    finally:
        conn.close()


def handshake(host: str) -> dict:
    info = _http_json(host, INFO_PORT, "GET", "/info")
    if info.get("ctrlType") == "cloud":
        raise PrinterError("The printer is in cloud mode; turn LAN Mode on")
    token, ctrl_url, model_id = info.get("token"), info.get("ctrlInfoUrl"), info.get("modelId")
    if not (isinstance(token, str) and isinstance(ctrl_url, str) and model_id):
        raise PrinterError("The printer didn't answer the LAN handshake")
    ts = int(time.time() * 1000)
    nonce = "".join(random.choices(string.ascii_letters + string.digits, k=6))
    did = "".join(random.choices(string.ascii_uppercase + string.digits, k=32))
    qs = urllib.parse.urlencode({"ts": ts, "nonce": nonce, "sign": _sign(token, ts, nonce), "did": did})
    p = urllib.parse.urlsplit(ctrl_url)
    path = (p.path if p.path.startswith("/") else "/" + p.path) + "?" + (f"{p.query}&{qs}" if p.query else qs)
    ctrl = _http_json(host, p.port or INFO_PORT, "POST", path)
    if ctrl.get("code") != 200 or not isinstance(ctrl.get("data"), dict):
        raise PrinterError(f"The printer refused the LAN handshake ({ctrl.get('message')})")
    try:
        data = _decrypt(ctrl["data"]["info"], token, ctrl["data"]["token"])
    except Exception as err:  # noqa: BLE001
        raise PrinterError(f"Couldn't read the printer's handshake answer ({err})") from err
    m = re.match(r"mqtts?://[^:]+:(\d+)", str(data.get("broker", "")))
    if not m or not all(k in data for k in ("username", "password", "deviceId")):
        raise PrinterError("The printer's handshake answer is missing its login")
    return {"broker_port": int(m.group(1)), "username": data["username"], "password": data["password"],
            "device_id": data["deviceId"], "model_id": str(model_id), "serial": info.get("cn"),
            "model_name": info.get("modelName")}


# --------------------------------------------------------------------------- MQTT session
def _client(client_id: str):
    import paho.mqtt.client as mqtt

    versions = getattr(mqtt, "CallbackAPIVersion", None)
    if versions is None:
        return mqtt.Client(client_id=client_id)
    return mqtt.Client(callback_api_version=versions.VERSION1, client_id=client_id)


class Session:
    """One short-lived MQTT session to the printer's broker. Use as a context manager."""

    def __init__(self, host: str, creds: dict) -> None:
        self.host, self.c = host, creds
        self._msgs: list[tuple[float, str, dict]] = []
        self._cv = threading.Condition()
        self._connected = threading.Event()
        self._refused = False
        self._m = _client(f"pl-{uuid.uuid4().hex[:8]}")
        self._m.username_pw_set(creds["username"], creds["password"])
        self._m.tls_set(cert_reqs=ssl.CERT_NONE)
        self._m.tls_insecure_set(True)
        self._m.on_connect = self._on_connect
        self._m.on_message = self._on_message

    def _on_connect(self, _c, _u, _f, rc, *args) -> None:
        failed = getattr(rc, "is_failure", None)
        if (failed if failed is not None else int(rc) != 0):
            self._refused = True
            self._connected.set()
            return
        self._m.subscribe(f"{BASE}/printer/+/{self.c['model_id']}/{self.c['device_id']}/#")
        self._connected.set()

    def _on_message(self, _c, _u, msg) -> None:
        try:
            body = json.loads(msg.payload)
        except ValueError:
            return
        if isinstance(body, dict):
            with self._cv:
                self._msgs.append((time.time(), msg.topic, body))
                self._cv.notify_all()

    def __enter__(self):
        try:
            self._m.connect(self.host, self.c["broker_port"], keepalive=30)
        except OSError as err:
            raise PrinterError(f"Couldn't connect to the printer ({err}); is it on?") from err
        self._m.loop_start()
        if not self._connected.wait(10) or self._refused:
            self.close()
            raise PrinterError("The printer refused the login" if self._refused
                               else "The printer didn't answer the login (is LAN Mode on?)")
        time.sleep(0.3)
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        try:
            self._m.loop_stop()
            self._m.disconnect()
        except Exception:  # noqa: BLE001
            pass

    def publish(self, kind: str, action: str, data, channel: str = "slicer") -> tuple[str, float]:
        msgid, sent = str(uuid.uuid4()), time.time()
        self._m.publish(f"{BASE}/{channel}/printer/{self.c['model_id']}/{self.c['device_id']}/{kind}",
                        json.dumps({"type": kind, "action": action, "msgid": msgid,
                                    "timestamp": int(sent * 1000), "data": data}, separators=(",", ":")))
        return msgid, sent

    def wait(self, test, timeout: float):
        deadline = time.time() + timeout
        with self._cv:
            while True:
                for item in list(self._msgs):
                    hit = test(*item)
                    if hit is not None:
                        return hit
                left = deadline - time.time()
                if left <= 0:
                    return None
                self._cv.wait(min(left, 1.0))

    def _request_once(self, kind, action, data, timeout, channel):
        msgid, sent = self.publish(kind, action, data, channel)

        def acked():
            return any(t.endswith("/response") and b.get("msgid") == msgid for _ts, t, b in self._msgs)

        def test(ts, topic, body):
            if topic.endswith("/response") and body.get("msgid") == msgid \
                    and body.get("code") not in (0, 200, None):
                raise PrinterError(f"The printer rejected {kind}/{action} (code {body.get('code')})")
            if ts >= sent and topic.endswith(f"/{kind}/report") and body.get("type") == kind \
                    and isinstance(body.get("data"), dict):
                if body.get("msgid") == msgid or acked():
                    return body
            return None

        return self.wait(test, timeout)

    def request(self, kind: str, action: str = "query", data=None, timeout: float = 12) -> dict:
        hit = self._request_once(kind, action, data, timeout, "slicer")
        if hit is None:
            hit = self._request_once(kind, action, data, timeout, "web")
        if hit is None:
            raise PrinterError(f"No answer from the printer to {kind}/{action}")
        if hit.get("code", 200) not in (0, 200):
            raise PrinterError(f"The printer answered {kind}/{action} with code {hit.get('code')}")
        return hit["data"]

    def command(self, kind: str, action: str, data, channel: str = "web", timeout: float = 10) -> dict | None:
        """Send a command and wait for its acknowledgement (None if none came)."""
        msgid, _ = self.publish(kind, action, data, channel)

        def test(ts, topic, body):
            if topic.endswith("/response") and body.get("msgid") == msgid:
                return body
            return None

        ack = self.wait(test, timeout)
        if ack is not None and ack.get("code") not in (0, 200, None):
            raise PrinterError(f"The printer refused {kind}/{action} (code {ack.get('code')})")
        return ack


# --------------------------------------------------------------------------- printer
NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,80}\.gcode")


def _camera_answers(url: str) -> bool:
    """True if url answers with video (FLV) or a JPEG/MJPEG stream within a few seconds."""
    try:
        with urllib.request.urlopen(url, timeout=4) as r:
            kind = (r.headers.get("Content-Type") or "").lower()
            head = r.read(16)
        return r.status == 200 and (head.startswith(b"FLV") or "jpeg" in kind or "multipart" in kind
                                    or head.startswith(b"\xff\xd8") or b"--" in head)
    except Exception:  # noqa: BLE001
        return False


class Printer:
    def __init__(self, host: str) -> None:
        self.host = host
        self._creds: dict | None = None
        self._lock = threading.Lock()       # one session at a time
        self._stream = None                 # (camera url, when it was checked)

    def _session(self) -> Session:
        if self._creds is None:
            self._creds = handshake(self.host)
        return Session(self.host, self._creds)

    def _run(self, fn):
        """Run fn(session); on a login failure, handshake again once and retry."""
        with self._lock:
            for attempt in (1, 2):
                try:
                    with self._session() as s:
                        return fn(s)
                except PrinterError as err:
                    if attempt == 2 or "login" not in str(err) and "connect" not in str(err):
                        raise
                    self._creds = None

    @property
    def stream_url(self) -> str:
        """The camera stream: the stock firmware's FLV feed, or Rinkhals' mjpg-streamer
        (Rinkhals takes the camera over, and the FLV port goes away). Checked once a minute."""
        now = time.time()
        if self._stream and now - self._stream[1] < 60:
            return self._stream[0]
        url = self.camera_urls()[0]
        for cand in self.camera_urls():
            if _camera_answers(cand):
                url = cand
                break
        self._stream = (url, now)
        return url

    def camera_urls(self) -> list[str]:
        return [f"http://{self.host}:18088/flv", f"http://{self.host}:8080/?action=stream",
                f"http://{self.host}/webcam/?action=stream"]

    # ---- reading
    def status(self) -> dict:
        info = self._run(lambda s: s.request("info"))
        temp = info.get("temp") or {}
        proj = info.get("project") or {}
        out = {"state": info.get("state"), "model": info.get("model") or info.get("printerName"),
               "firmware": info.get("version"),
               "nozzle_c": temp.get("curr_nozzle_temp"), "nozzle_target_c": temp.get("target_nozzle_temp"),
               "bed_c": temp.get("curr_hotbed_temp"), "bed_target_c": temp.get("target_hotbed_temp")}
        if proj:
            out["job"] = {k: proj.get(k) for k in ("filename", "state", "progress", "curr_layer",
                                                   "total_layers", "remain_time", "print_time", "pause")}
        return out

    def files(self) -> list[dict]:
        def go(s):
            data = s.request("file", "listLocal", {"page_num": 1, "page_size": 200, "path": "/"})
            return [{k: r.get(k) for k in ("filename", "size", "is_dir", "timestamp") if k in r}
                    for r in data.get("records") or [] if isinstance(r, dict)]
        return self._run(go)

    # ---- writing
    def stage(self, name: str, data: bytes) -> dict:
        if not NAME_RE.fullmatch(name):
            raise PrinterError("File name must be a plain ASCII name ending in .gcode")
        first_layer = data.find(b";LAYER_CHANGE")   # Orca: thumbnails + object list come first
        if b"G9111 " not in data[:first_layer if first_layer > 0 else 2_000_000]:
            raise PrinterError("G-code was not sliced with the Kobra S1 profile (no G9111 start)")
        md5 = hashlib.md5(data).hexdigest()

        def go(s):
            info = s.request("info")
            if info.get("state") != "free" or info.get("project"):
                raise PrinterError("The printer is busy; try again when it's idle")
            self._upload((info.get("urls") or {}).get("fileUploadurl") or "", name, data)
            listed, details = None, {}
            try:
                listed = next((r for r in s.request("file", "listLocal",
                                                    {"page_num": 1, "page_size": 200, "path": "/"})
                               .get("records") or [] if r.get("filename") == name), None)
            except PrinterError:
                pass
            try:
                fd = s.request("file", "fileDetails", {"root": "local", "filename": name,
                                                       "plate_index": 1}).get("file_details") or {}
                details = {k: fd.get(k) for k in ("layer_count", "estimated_time_s", "supplies_usage")
                           if k in fd}
            except PrinterError:
                pass
            if listed is not None and listed.get("size") not in (None, len(data)):
                raise PrinterError("The file on the printer is a different size than the one sent")
            return {"filename": name, "size": len(data), "md5": md5,
                    "confirmed_on_printer": listed is not None, "printer_details": details}

        return self._run(go)

    def _upload(self, url: str, name: str, data: bytes) -> None:
        p = urllib.parse.urlsplit(url)
        if p.path != "/gcode_upload" or not re.fullmatch(r"s=[A-Za-z0-9_-]+", p.query or ""):
            raise PrinterError("The printer's upload address looks wrong; not sending")
        boundary = "------------------------" + uuid.uuid4().hex
        sep = f"--{boundary}\r\n".encode()
        body = (sep + b'Content-Disposition: form-data; name="filename"\r\n\r\n' + name.encode() + b"\r\n"
                + sep + f'Content-Disposition: form-data; name="gcode"; filename="{name}"\r\n'.encode()
                + b"Content-Type: application/octet-stream\r\n\r\n" + data + f"\r\n--{boundary}--\r\n".encode())
        headers = {"User-Agent": "AnycubicSlicerNext/1.3.9.4", "Accept": "*/*",
                   "X-BBL-Client-Name": "AnycubicSlicerNext", "X-BBL-Client-Type": "slicer",
                   "X-BBL-Client-Version": "01.03.09.04", "X-BBL-Device-ID": str(uuid.uuid4()),
                   "X-BBL-Language": "en-US", "X-BBL-OS-Type": "linux", "X-BBL-OS-Version": "Debian",
                   "X-File-Length": str(len(data)),
                   "Content-Type": f"multipart/form-data; boundary={boundary}",
                   "Content-Length": str(len(body)), "Connection": "close"}
        conn = http.client.HTTPConnection(self.host, p.port or INFO_PORT, timeout=300)
        try:
            conn.request("POST", f"{p.path}?{p.query}", body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read(1024 * 1024)
        except OSError as err:
            raise PrinterError(f"Upload failed ({err})") from err
        finally:
            conn.close()
        if not 200 <= resp.status < 300:
            raise PrinterError(f"Upload failed (HTTP {resp.status})")
        try:
            result = json.loads(raw)
        except ValueError as err:
            raise PrinterError("The printer's upload answer was not understood") from err
        if not isinstance(result, dict) or result.get("code") not in (0, 200):
            raise PrinterError("The printer refused the upload")

    def start(self, staged: dict, auto_leveling: bool = True) -> dict:
        def go(s):
            info = s.request("info")
            if info.get("state") != "free" or info.get("project"):
                raise PrinterError("The printer is busy; not starting")
            payload = {
                "taskid": "-1", "url": "", "filename": staged["filename"], "md5": staged["md5"],
                "filepath": None, "filetype": 1, "project_type": 1, "filesize": staged["size"],
                "ams_settings": {"use_ams": False, "ams_box_mapping": []},
                "task_settings": {"auto_leveling": int(auto_leveling), "vibration_compensation": 0,
                                  "flow_calibration": 0, "dry_mode": 0,
                                  "ai_settings": {"status": 0, "count": 0, "type": 0},
                                  "timelapse": {"status": 0, "count": 0, "type": 0},
                                  "drying_settings": {"status": 0, "target_temp": 0, "duration": 0,
                                                      "remain_time": 0},
                                  "model_objects_skip_parts": []}}
            if s.command("print", "start", payload, channel="slicer", timeout=15) is None:
                raise PrinterError("The printer did not acknowledge the start command")
            state = None
            for _ in range(10):
                time.sleep(2)
                try:
                    state = s.request("info", timeout=6).get("state")
                except PrinterError:
                    continue
                if state and state != "free":
                    break
            return {"filename": staged["filename"], "acknowledged": True,
                    "printer_busy": bool(state and state != "free")}
        return self._run(go)

    def control(self, action: str) -> dict:
        if action not in ("stop", "pause", "resume"):
            raise PrinterError(f"Unknown action {action}")
        ack = self._run(lambda s: s.command("print", action, {"taskid": "-1"}))
        return {"action": action, "acknowledged": ack is not None}

    def kick_camera(self) -> None:
        """Start the camera stream the way the official client does: stop, pause, start."""
        def go(s):
            s.command("video", "stopCapture", None, timeout=3)
            time.sleep(1.0)
            s.command("video", "startCapture", None, timeout=5)
        self._run(go)
