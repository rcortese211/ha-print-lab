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

## Print failure watch

`POST /detect` grabs one frame from the printer camera (or takes a JPEG body) and
scores it with Obico's open-source failure-detection model
([obico-server](https://github.com/TheSpaghettiDetective/obico-server), AGPL-3.0),
run on the CPU with onnxruntime. The model is downloaded once, checksum-verified,
to `printlab/ai/` on the drive. Frames scoring above 0.3 are kept in
`printlab/watch/history/` for tuning. Smoothing and the pause decision live in the
Home Assistant `kobra_print` integration.

### Temperature towers

`POST /slice` applies a few slicing rules from the plate's shape (concentric surfaces for round parts, a slower and hotter first layer for big ones). They never override a setting the request sets, `"rules": false` turns them off, and the response lists the ones that fired in `rules`. See [print_lab/rootfs/RULES.md](print_lab/rootfs/RULES.md).

`POST /slice` also takes `temps`: a list of `[height_mm, nozzle_c]` steps. After slicing, an `M104` is inserted at the first layer at or above each height, so each tier of a temperature tower prints at its own temperature. The response lists the steps it applied in `temps_applied`.

### Calibration steps (`tune`)

`POST /slice` takes `tune`, a set of height-based changes applied after slicing:

- `temps`: `[[height_mm, nozzle_c], ...]` sets the nozzle temperature from each height up (temperature towers).
- `retracts`: `[[height_mm, length_mm], ...]` changes the retraction length from each height up (retraction towers). Needs relative extrusion; every retraction, including the part done during a wipe, is scaled.

The response lists what was applied in `temps_applied` and `retracts_applied`.

### Profile updates

Startup replaces a profile on the drive with the newly shipped one only when the drive copy is an unedited earlier version (its checksum is listed in `profiles/previous.md5`). A profile edited on the drive is left alone.

### Close-up crops for the failure check

A print is small in the camera's wide view, and shrinking the whole frame to the model's input size can hide a failure entirely. The check therefore scores the whole frame plus a few crops of it at the model's full resolution, and the frame's score is the best view's. The crops are set with the `watch_crops` option as `x,y,w,h` fractions of the frame, separated by `;`. The response lists every view's score in `views` and which one won in `view`.
