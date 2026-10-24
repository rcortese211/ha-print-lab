# ACE swap speed-up: how to revert

Every change goes into /userdata/app/gk/printer_data/config/printer.custom.cfg on the printer (Anycubic's printer.cfg is never edited).
Before each step, a dated copy is saved next to it as printer.custom.cfg.bak-<timestamp>.

To revert everything: copy printer.custom.cfg.pre-tracker-test over printer.custom.cfg (that's the stock, empty override file), then power-cycle.
To undo one step: copy that step's .bak file over printer.custom.cfg, then power-cycle.

## Steps
- 20261023-2017 step A (purge): flush_multiplier 1.5->1.0, max_length_per_extrude 150->300, delay_before_throw 5->2, fan_on_duration 5->3, fan_off_duration 2->1. Backup: printer.custom.cfg.bak-20261023-2017
  - Also in step A: sweep_speed 7->10 (purge extrusion speed in mm/s of filament, about 24 mm3/s; chosen for the 0.8 nozzle, too fast for a 0.4).
  - flush_multiplier is ALSO stored in /userdata/app/gk/config/ams_config.cfg (the touchscreen/API value, which wins). It was set live 1.5->1.0 through GoKlipper's filament_hub/set_config.
    Revert: send {"method":"filament_hub/set_config","params":{"flush_multiplier":1.5}} to /tmp/unix_uds1 on the printer (or use Rinkhals SET_ACE_FLUSH_MULTIPLIER VALUE=1.5).
- Nozzle: the printer stores the fitted nozzle in /userdata/app/gk/config/nozzle.cfg (currently "-", i.e. unset). Set it from the touchscreen.
- The firmware reports the fitted nozzle live as extruder.Nozzle_diameter (0.8 brass, set on the touchscreen).
- 20261023-2027 step B+C (ACE motor speeds): v1_feed_speed 30->60, default_feed_speed (unset)->60, v1_unwind_speed 20->40, default_unwind_speed (unset)->40, unwind_speed_old_ace 15->40. Unwind stays at the firmware's max_filament_unloading_speed of 40. Backup: printer.custom.cfg.bak-20261023-2027
- 20261023-2035 step D (purge): sweep_speed 10->13 (about 31 mm3/s, for the 0.8 nozzle only), flush_volume_max 800->450 (screen swaps don't know the previous colour, so they purge ~690 mm3; this caps them). Backup: printer.custom.cfg.bak-20261023-2035
  - flush_volume_max also set live 800->450 through filament_hub/set_config (stored in ams_config.cfg). Revert: set_config {"flush_volume_max":800}.
- 20261023-2045 step E (unwind): unwind_length_after_triggered 1100->500. Backup: printer.custom.cfg.bak-20261023-2045
- 2026-10-24 00:52 nozzle hook: Rinkhals app /useremain/home/rinkhals/apps/70-nozzle-purge (source in ~/ace-debug/nozzle-purge/). At each boot it reads extruder.Nozzle_diameter and keeps the managed "# BEGIN nozzle-purge" block in printer.custom.cfg (sweep_speed: 0.2->2, 0.4->7, 0.6->10, 0.8->13) in sync, with a .bak-nozzle-<time> backup on change. After a nozzle change it needs one extra power-cycle.
  Earlier sweep_speed lines from steps A/D are commented out. Backup from before the install: printer.custom.cfg.bak-20261023-2052.
  Disable: rm /useremain/home/rinkhals/apps/70-nozzle-purge/.enabled. Remove: delete that folder and the managed block.
