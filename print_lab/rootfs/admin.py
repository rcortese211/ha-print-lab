"""Maintenance endpoints for Print Lab when it runs on its own machine (the Pi).

Enabled only when PRINTLAB_ADMIN_TOKEN is set; every call must send it in the
X-Admin-Token header. The actions are a fixed list - there is no way to run an
arbitrary command through here.

  GET  /admin/status            versions, uptime, memory, disk, temperature
  GET  /admin/logs?lines=N&unit=printlab-bot   a service's recent log
  POST /admin/bot               save the Telegram bot's settings (bot.json)
  POST /admin/update            pull the latest code from GitHub; reinstall and restart if it changed
  POST /admin/restart           restart the Print Lab service
  POST /admin/reboot            reboot the machine
"""
import hmac
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

TOKEN = os.environ.get("PRINTLAB_ADMIN_TOKEN", "")
REPO = Path(os.environ.get("PRINTLAB_REPO", "/opt/printlab/repo"))
SERVICE = os.environ.get("PRINTLAB_SERVICE", "printlab")
BOT_SERVICE = "printlab-bot"


def enabled() -> bool:
    return len(TOKEN) >= 24


def authorised(header_value: str | None) -> bool:
    return enabled() and hmac.compare_digest((header_value or "").encode(), TOKEN.encode())


def _run(cmd, timeout=60) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as err:
        return 1, str(err)


def _later(cmd, delay=1.5) -> None:
    """Run cmd shortly after the HTTP answer has gone out (it may kill this process)."""
    def go():
        time.sleep(delay)
        subprocess.run(cmd, capture_output=True)
    threading.Thread(target=go, daemon=True).start()


def status(lab: Path) -> dict:
    def read(path, default=""):
        try:
            return Path(path).read_text().strip()
        except OSError:
            return default

    mem = {}
    for line in read("/proc/meminfo").splitlines():
        k, _, v = line.partition(":")
        if k in ("MemTotal", "MemAvailable"):
            mem[k] = round(int(v.split()[0]) / 1024 / 1024, 2)
    temp = read("/sys/class/thermal/thermal_zone0/temp")
    disk = shutil.disk_usage(lab)
    _, rev = _run(["git", "-c", "safe.directory=*", "-C", str(REPO), "log", "-1", "--format=%h %cs %s"])
    _, throttled = _run(["vcgencmd", "get_throttled"])
    _, active = _run(["systemctl", "is-active", SERVICE])
    _, bot = _run(["systemctl", "is-active", BOT_SERVICE])
    return {
        "host": os.uname().nodename,
        "model": read("/proc/device-tree/model").rstrip("\x00"),
        "code": rev,
        "uptime_h": round(float(read("/proc/uptime", "0").split()[0]) / 3600, 1),
        "load": read("/proc/loadavg").split()[:3],
        "mem_total_gb": mem.get("MemTotal"), "mem_available_gb": mem.get("MemAvailable"),
        "disk_free_gb": round(disk.free / 1e9, 1), "disk_total_gb": round(disk.total / 1e9, 1),
        "cpu_temp_c": round(int(temp) / 1000, 1) if temp.isdigit() else None,
        "throttled": throttled.split("=")[-1] if throttled else None,
        "service": active,
        "bot": bot if (lab / "bot.json").is_file() else f"{bot} (not configured)",
    }


def logs(lines: int, unit: str = SERVICE) -> dict:
    lines = max(10, min(int(lines), 1000))
    if unit not in (SERVICE, BOT_SERVICE):
        unit = SERVICE
    code, out = _run(["journalctl", "-u", unit, "-n", str(lines), "--no-pager", "-o", "short-iso"])
    return {"ok": code == 0, "unit": unit, "log": out}


def set_bot(lab: Path, cfg: dict) -> dict:
    """Save the Telegram bot's settings (bot.py notices the change and restarts itself)."""
    tg = str(cfg.get("telegram_token", ""))
    chats = cfg.get("chat_ids") or []
    if not re.fullmatch(r"\d{5,15}:[A-Za-z0-9_-]{30,60}", tg):
        raise ValueError("telegram_token doesn't look like a BotFather token")
    if not chats or not all(isinstance(c, int) and not isinstance(c, bool) for c in chats):
        raise ValueError("chat_ids must be a non-empty list of numbers")
    tv = str(cfg.get("thingiverse_token", ""))
    if tv and not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", tv):
        raise ValueError("thingiverse_token doesn't look like a Thingiverse app token")
    dest = lab / "bot.json"
    tmp = lab / "bot.json.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"telegram_token": tg, "chat_ids": chats, **({"thingiverse_token": tv} if tv else {})}, f)
    os.replace(tmp, dest)
    return {"ok": True, "chat_ids": len(chats)}


def update() -> dict:
    """Pull the latest code from GitHub and reinstall if it changed (runs as root via sudo)."""
    _later(["sudo", "-n", "systemctl", "start", "--no-block", "printlab-update.service"])
    return {"ok": True, "updating": True,
            "note": "check /admin/status in a minute; 'code' shows the running version"}


def restart() -> dict:
    _later(["sudo", "-n", "systemctl", "restart", SERVICE])
    return {"ok": True, "restarting": True}


def reboot() -> dict:
    _later(["sudo", "-n", "systemctl", "reboot"])
    return {"ok": True, "rebooting": True}
