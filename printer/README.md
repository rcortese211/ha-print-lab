# Printer-side configuration (Kobra S1 + ACE Pro, Rinkhals)

These files live **on the printer**, not the Pi. Nothing in `pi/install.sh` or the nightly
`printlab-update` deploys them; they are kept here as the source of truth and for recovery.

| File | Lives on the printer at |
|---|---|
| `printer.custom.cfg` | `/userdata/app/gk/printer_data/config/printer.custom.cfg` (Rinkhals merges it into `printer.generated.cfg` at boot) |
| `rinkhals-apps/70-nozzle-purge/` | `/useremain/home/rinkhals/apps/70-nozzle-purge/` (plus an empty `.enabled` file) |

Anycubic's own `/userdata/app/gk/printer.cfg` is never edited.

## ACE unload failure (fixed 2026-10-23)

Symptom: every unload or colour swap stopped with `10011726 filament tracker hardware error`.
The firmware logged `tracker length: 0.000000` after every extrude, so the tracker never counted.
Anycubic had replaced the tracker twice without effect.

Cause: the internal plug behind the rear 6-pin tracker port was not seated. Factory glue on the board
under the connector held it up. The port measured 0 V on every pin with the printer on. One pin was
ground; the supply never arrived. After the plug was cleaned and reseated, swaps work with the tracker
on, and the tracker reports real lengths.

## Swap speed-up (2026-10-24)

A colour swap went from about 4:00 to **1:47**, measured from the nozzle reaching 250 °C (0.8 mm
nozzle, PLA). Purges came out clean and nothing jammed. Each step and its revert is listed in
`REVERT.md`. Timings are in `swap-timings-2026-10-24.log`.

- Purge: volume multiplier 1.0, at most 450 mm³, shorter fan and throw pauses, and a nozzle-dependent
  speed (`sweep_speed`).
- ACE motors: feed 60 mm/s, unwind 40 mm/s (the firmware's `max_filament_unloading_speed`).
- Fixed unwind after the head sensor clears: 500 mm (was 1100). The tracker-guided unwind to the safe
  position still follows it.

`flush_multiplier` and `flush_volume_max` are also stored in `/userdata/app/gk/config/ams_config.cfg`
(the touchscreen/API copy, which takes precedence). They were set to 1.0 and 450 through GoKlipper's
`filament_hub/set_config` on `/tmp/unix_uds1`.

## Nozzle-aware purge app

`70-nozzle-purge` runs at every Rinkhals boot. It reads `extruder.Nozzle_diameter` from GoKlipper and
keeps a managed block in `printer.custom.cfg` with the matching `sweep_speed`
(0.2 → 2, 0.4 → 7, 0.6 → 10, 0.8 → 13 mm/s of filament). GoKlipper reads that value only at
startup, so after a nozzle change one extra power-cycle is needed; the app writes a message to the
screen when it rewrites the block. `rinkhals-apps/install-nozzle-purge.sh` installs it from the Pi.

Not yet verified: a swap inside a real multicolour print. During a print the purge volume may come
from the slicer's flush table instead of the colour calculation.
