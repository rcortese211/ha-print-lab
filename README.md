# Print Lab

A Home Assistant app that slices 3D prints on the Green, with all the heavy
lifting on an external USB drive.

* The app image itself is a small Alpine shell (~30 MB on HA's storage).
* On first start it creates `printlab/rootfs.img` (an ext4 disk image) on the
  external drive, installs a minimal Debian into it with PrusaSlicer and
  OpenSCAD, and runs everything from there.
* Models, G-code and profiles live in plain folders on the drive
  (`printlab/models`, `printlab/gcode`, `printlab/profiles`).
* A small HTTP API on the internal Supervisor network (port 8099) takes slice
  jobs. Nothing is exposed outside HA.

## API

* `GET /health` - status and tool versions
* `POST /slice` - JSON `{name, scad | url | model, profile, overrides}`; returns the G-code name and stats
* `GET /gcode/<name>` - the sliced file
* `GET /profiles`, `PUT /profiles/<name>` - list / replace slicer profiles
