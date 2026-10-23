# Print Lab Pi

You are running on Robbie's Raspberry Pi 4 ("printlab", 192.168.0.22), which hosts Print Lab
for his Anycubic Kobra S1 (192.168.0.112, LAN mode). Robbie is the owner; he swears casually in
chat, which is fine, but keep anything written to files or external places clean.

## What lives here
- Code: `/opt/printlab/repo` (GitHub `rcortese211/ha-print-lab`, public). `pi/` has the install
  script, systemd units and this file. Daily auto-update pulls from GitHub at 04:30
  (`printlab-update.timer`); `sudo /usr/local/sbin/printlab-update --force` reinstalls now.
- Services: `printlab` (slicer, failure detector, printer control; port 8099),
  `printlab-bot` (Telegram @RCPrintLabBot), `printlab-claude` (you).
- Data: `/var/lib/printlab` — a bind mount of the 4 TB exFAT drive `HA_Expand` (`/mnt/ha_expand/printlab`):
  models, gcode, profiles (`pla_08.ini`: the old PrusaSlicer profile, now only for the camera
  calibration print), `watch/history` (kept camera frames), `calibration/` (camera calibration runs),
  `camera_calibration.json`.
- Settings and the admin token: `/etc/printlab/printlab.env` (root:printlab 640). Printer API:
  `http://127.0.0.1:8099/printer/...` with header `X-Admin-Token`.
- Logs: `journalctl -u printlab`, `-u printlab-bot`, `-u printlab-claude`.
- Home Assistant (HA Green) uses Print Lab through its `kobra_print` integration
  (`/config/custom_components/kobra_print`, settings in `/config/kobra_print/lab.json`).

## Slicing
- OrcaSlicer 2.4.2 (`/opt/orcaslicer`) is the default slicer: Anycubic's own Kobra S1 profiles adapted
  to the 0.8 mm nozzle, in `print_lab/rootfs/profiles/orca/` (rebuild with `tools/orca_profiles/build.py`).
  It beat the old PrusaSlicer profile clearly on a 20 mm test cube (Oct 2026). PrusaSlicer is still
  installed; `/slice` takes `slicer: prusa` to use it.
- Rinkhals (custom firmware, Moonraker on :7125, Fluidd on :4409) is installed on the printer. The camera
  is Rinkhals' mjpg-streamer now; the stock FLV feed on :18088 is gone.

## Rules
- Anything physical waits for Robbie's explicit go ("send it"): starting prints, moving the head
  or bed, heating, stopping a print he didn't ask to stop.
- Prefer changes in the repo (commit + push, then update) over hand-editing installed copies, so
  the daily update doesn't undo them. Keep the printer's own firmware/config untouched unless
  Robbie asks; say what you'll change first.
- Never put Robbie's email, tokens or passwords anywhere external.
