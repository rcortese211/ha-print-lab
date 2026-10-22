# Print Lab on a Raspberry Pi

Runs the same slice server and print failure check as the Home Assistant app, as a normal
systemd service on its own machine.

One-time setup on Raspberry Pi OS (64-bit, Lite is fine), as a sudo user:

    sudo apt-get install -y --no-install-recommends prusa-slicer openscad ffmpeg \
        python3-numpy python3-venv python3-pip xvfb xauth git
    sudo python3 -m venv --system-site-packages /opt/ai
    sudo /opt/ai/bin/pip install onnxruntime
    sudo git clone https://github.com/rcortese211/ha-print-lab /opt/printlab/repo
    sudo /opt/printlab/repo/pi/install.sh

- Service: `printlab` (port 8099), runs as the `printlab` system user. Data lives in
  `/var/lib/printlab`; settings and the admin token in `/etc/printlab/printlab.env`.
- Updates: `printlab-update.timer` pulls from GitHub daily at 04:30 and reinstalls only when
  the code changed. `POST /admin/update` does the same on demand.
- Maintenance endpoints (`/admin/status`, `/admin/logs`, `/admin/update`, `/admin/restart`,
  `/admin/reboot`) need the `X-Admin-Token` header. The service account may only run those
  actions with sudo; see `pi/sudoers`.
- Printer: Print Lab talks to the printer directly (`/printer/*`, admin token). Set its address
  once with `POST /printer/config {"host": "<printer ip>"}` (HA's `kobra_print.lab_setup` does
  this from the Anycubic integration).
- Telegram bot: `printlab-bot` service (`bot.py`). It idles until `bot.json` exists; settings come
  from `POST /admin/bot` (HA's `kobra_print.lab_setup` with a `bot` section in lab.json).
- External drive (optional): mount it and bind its `printlab` folder over `/var/lib/printlab`,
  both `nofail` so the Pi boots and falls back to the SD card without it. For an exFAT drive:

      LABEL=HA_Expand /mnt/ha_expand exfat nofail,noatime,uid=<printlab uid>,gid=<printlab gid>,dmask=027,fmask=137,x-systemd.device-timeout=20s 0 0
      /mnt/ha_expand/printlab /var/lib/printlab none bind,nofail 0 0
