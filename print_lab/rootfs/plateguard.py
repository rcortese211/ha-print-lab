"""Plate-aware failure check: knows where the parts should be, flags plastic that isn't.

With the camera calibration (calib.py) and the G-code being printed, each check:

1. projects every bit of plastic printed so far (at its real height, with the bed lowered
   to the current layer) into the camera image: where the parts should appear;
2. adds the zone the print head can occupy around the current layer;
3. lines up a reference frame taken at the first layers with the current frame on the bed
   plane (the bed drops as the print grows);
4. counts changed pixels on the bed outside parts and head zone, keeps only changes that
   are still there at the next check, and turns their size into a score.

It also hands the failure model a tight crop around where the parts are, so small parts
far back on the bed are big enough for it to judge.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

import calib
import plate

HEAD_XY = 35.0          # mm the head body reaches around the nozzle
HEAD_UP = 60.0          # mm of head above the nozzle
DEBRIS_DIFF = 45        # grey levels that count as a change
SCORE_FROM = 150        # px of persistent debris before the score rises ...
SCORE_SPAN = 1000       # ... and px more for a full 1.0


class Plan:
    """What the current layer of the current job should look like from the camera."""

    def __init__(self, p, segs, zc, shape):
        self.p, self.zc = p, zc
        h, w = shape
        x0, y0, x1, y1, z = segs
        done = z <= zc + 1e-3
        # sample printed paths every ~1 mm, at their real height relative to the nozzle plane
        length = np.hypot(x1[done] - x0[done], y1[done] - y0[done])
        n = np.clip(np.ceil(length).astype(int), 1, 400)
        idx = np.repeat(np.arange(len(n)), n)
        t = (np.arange(len(idx)) - np.repeat(np.cumsum(n) - n, n)) / np.repeat(np.maximum(n, 1), n)
        xs = x0[done][idx] + t * (x1[done] - x0[done])[idx]
        ys = y0[done][idx] + t * (y1[done] - y0[done])[idx]
        zs = z[done][idx] - zc
        if len(xs) > 400_000:
            keep = np.linspace(0, len(xs) - 1, 400_000).astype(int)
            xs, ys, zs = xs[keep], ys[keep], zs[keep]
        uv = calib.project(p, np.c_[xs, ys, zs])
        self.expected = _mask_from(uv, shape, grow=7)
        # the parts proper (above the skirt and first layers): what disappears when one is knocked off
        high = (zs + zc) >= 1.5
        self.expected_high = _mask_from(uv[high], shape, grow=1) if high.any() else np.zeros(shape, bool)
        # the head: anywhere around this layer's paths, from the nozzle up
        top = z[done].max() if done.any() else z.min()
        layer = np.abs(z - top) < 1e-3
        lx = np.r_[x0[layer], x1[layer]] if layer.any() else np.r_[x0, x1]
        ly = np.r_[y0[layer], y1[layer]] if layer.any() else np.r_[y0, y1]
        bx = (lx.min() - HEAD_XY, lx.max() + HEAD_XY)
        by = (ly.min() - HEAD_XY, ly.max() + HEAD_XY)
        corners = np.array([[x, y, zz] for x in bx for y in by for zz in (0.0, HEAD_UP)])
        self.head = _hull_mask(calib.project(p, corners), shape)
        # the bed itself, where it is now
        bed = np.array([[0, 0, -zc], [250, 0, -zc], [250, 250, -zc], [0, 250, -zc]], float)
        self.bed = _hull_mask(calib.project(p, bed), shape)
        # crop for the model: around where the parts are, with room for a mess, never tiny
        ys_, xs_ = np.nonzero(self.expected)
        self.crop = None
        if len(xs_):
            cx, cy = (xs_.min() + xs_.max()) / 2, (ys_.min() + ys_.max()) / 2
            half_w = max((xs_.max() - xs_.min()) * 0.8, 120)
            half_h = max((ys_.max() - ys_.min()) * 0.8, 90)
            x_a, x_b = max(cx - half_w, 0), min(cx + half_w, w)
            y_a, y_b = max(cy - half_h, 0), min(cy + half_h, h)
            self.crop = (round(x_a / w, 3), round(y_a / h, 3), round((x_b - x_a) / w, 3), round((y_b - y_a) / h, 3))

    def bed_h(self, zc=None):
        """Homography: bed mm (x, y) -> pixel, with the bed lowered to zc."""
        p, zc = self.p, self.zc if zc is None else zc
        return np.c_[p[:, 0], p[:, 1], p[:, 3] - zc * p[:, 2]]


def _mask_from(uv, shape, grow):
    from scipy import ndimage  # noqa: PLC0415
    h, w = shape
    m = np.zeros(shape, bool)
    ok = np.isfinite(uv).all(1)
    u, v = uv[ok, 0].round().astype(int), uv[ok, 1].round().astype(int)
    inside = (u >= 0) & (u < w) & (v >= 0) & (v < h)
    m[v[inside], u[inside]] = True
    return ndimage.binary_dilation(m, iterations=grow) if grow else m


def _hull_mask(uv, shape):
    """Filled convex hull of points (pixels), clipped to the image."""
    h, w = shape
    uv = uv[np.isfinite(uv).all(1)]
    if len(uv) < 3:
        return np.zeros(shape, bool)
    pts = sorted(map(tuple, uv))

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for pt in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], pt) <= 0:
            lower.pop()
        lower.append(pt)
    for pt in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], pt) <= 0:
            upper.pop()
        upper.append(pt)
    hull = np.asarray(lower[:-1] + upper[:-1])
    yy, xx = np.mgrid[0:h, 0:w]
    inside = np.ones(shape, bool)
    for k in range(len(hull)):
        a, b = hull[k], hull[(k + 1) % len(hull)]
        inside &= (b[0] - a[0]) * (yy - a[1]) - (b[1] - a[1]) * (xx - a[0]) >= 0
    return inside


class PlateGuard:
    def __init__(self, cal_path: Path, gcode_dir: Path):
        self.cal_path, self.gcode_dir = cal_path, gcode_dir
        self.job = None            # (filename, gcode mtime)
        self.segs = None
        self.layer_z = None
        self.ref = None            # (grey frame, zc) from the first layers
        self.prev = None           # last check's debris mask

    def reset(self):
        self.job = self.segs = self.layer_z = self.ref = self.prev = None
        self.part_peak = 0.0

    def plan(self, status: dict | None, shape=(720, 1280)):
        """A Plan for the current layer, or None (no calibration, not printing, unknown file)."""
        p, _cal = calib.load(self.cal_path)
        job = (status or {}).get("job") or {}
        name = str(job.get("filename") or "")
        if p is None or not name.endswith(".gcode") or job.get("state") not in ("printing", "resumed"):
            if not job:
                self.reset()
            return None
        path = self.gcode_dir / Path(name).name
        if not path.is_file():
            return None
        key = (name, path.stat().st_mtime)
        if key != self.job:
            self.reset()
            data = path.read_bytes()
            self.segs = plate.segments(data)
            self.layer_z = [float(line[3:]) for line in data.split(b"\n") if line.startswith(b";Z:")]
            self.job = key
        if self.segs is None or not self.layer_z:
            return None
        layer = int(job.get("curr_layer") or 1)
        zc = self.layer_z[min(max(layer, 1), len(self.layer_z)) - 1]
        plan = Plan(p, self.segs, zc, shape)
        plan.layer = layer
        return plan

    @staticmethod
    def head_mask(rgb: np.ndarray):
        """Where the print head is in this frame: its orange band, plus the body above it."""
        from scipy import ndimage  # noqa: PLC0415
        r, g, b = (rgb[..., i].astype(np.int16) for i in range(3))
        orange = (r > 140) & (r - g > 55) & (r - b > 90) & (g > 35)
        orange = ndimage.binary_opening(orange, np.ones((3, 3)))
        lab, n = ndimage.label(orange)
        mask = np.zeros(orange.shape, bool)
        if not n:
            return mask, None
        sizes = ndimage.sum(orange, lab, np.arange(1, n + 1))
        k = int(np.argmax(sizes)) + 1
        if sizes[k - 1] < 300:
            return mask, None
        ys, xs = np.nonzero(lab == k)
        x0, x1, y0, y1 = xs.min(), xs.max(), ys.min(), ys.max()
        w, h = x1 - x0 + 1, y1 - y0 + 1
        hh, ww = orange.shape
        box = (max(int(x0 - 0.2 * w), 0), max(int(y0 - 3.0 * h), 0),
               min(int(x1 + 0.2 * w), ww - 1), min(int(y1 + 0.6 * h), hh - 1))
        mask[box[1]:box[3] + 1, box[0]:box[2] + 1] = True
        return mask, box

    def analyse(self, frame: Path, plan: Plan) -> dict:
        rgb = calib.rgb(frame)
        g = rgb.astype(np.float32).mean(axis=2)
        if g.shape != plan.expected.shape:
            return {"plate": "frame size differs from the calibration"}
        head, head_box = self.head_mask(rgb)
        if head_box is None:
            head = plan.head          # can't see the head: fall back to everywhere it could be
        if self.ref is None:
            if plan.layer <= 3:
                self.ref = (g, plan.zc, head)
                return {"plate": "reference taken", "plate_score": 0.0, "debris_px": 0}
            return {"plate": "no reference yet (joined mid-print)"}
        from scipy import ndimage  # noqa: PLC0415
        ref_g, ref_zc, ref_head = self.ref
        # line the reference up with the bed as it sits now: pixel -> bed point -> reference pixel
        h_now, h_ref = plan.bed_h(), plan.bed_h(ref_zc)
        to_ref = h_ref @ np.linalg.inv(h_now)
        hh, ww = g.shape
        yy, xx = np.mgrid[0:hh, 0:ww]
        q = np.stack([xx.ravel(), yy.ravel(), np.ones(xx.size)]).T @ to_ref.T
        u, v = q[:, 0] / q[:, 2], q[:, 1] / q[:, 2]
        warped = ndimage.map_coordinates(ref_g, [v, u], order=1, cval=np.nan).reshape(g.shape)
        ref_head_now = ndimage.map_coordinates(ref_head.astype(np.float32), [v, u], order=0,
                                               cval=0).reshape(g.shape) > 0.5
        valid = np.isfinite(warped) & ~head & ~ref_head_now
        look = plan.bed & ~plan.expected & valid
        if look.sum() < 500:
            return {"plate": "too little bed in view", "plate_score": 0.0, "debris_px": 0}
        gain = np.median(g[look]) / max(np.median(warped[look]), 1.0)     # exposure drifts
        diff = np.abs(g - warped * gain)
        changed = ndimage.binary_opening(diff > DEBRIS_DIFF, np.ones((3, 3)))
        # 1) debris: plastic on the bed where no part should be, still there at the next check
        stray = changed & look
        persistent = stray & ndimage.binary_dilation(self.prev, iterations=3) if self.prev is not None \
            else np.zeros_like(stray)
        self.prev = stray
        area = int(persistent.sum())
        debris_score = float(np.clip((area - SCORE_FROM) / SCORE_SPAN, 0, 1))
        # 2) vanished parts: where the parts sit, plastic was seen before and is gone now
        parts = plan.expected_high & valid
        seen = float(changed[parts].mean()) if parts.sum() > 150 else None
        missing_score = 0.0
        if seen is not None:
            self.part_peak = max(getattr(self, "part_peak", 0.0), seen)
            peak = self.part_peak
            if peak >= 0.25 and seen < 0.6 * peak:
                missing_score = float(np.clip((0.6 * peak - seen) / (0.4 * peak), 0, 1))
        score = max(debris_score, missing_score)
        out = {"plate": "checked", "plate_score": round(score, 3), "debris_px": area,
               "parts_seen": None if seen is None else round(seen, 3),
               "parts_peak": round(getattr(self, "part_peak", 0.0), 3), "missing_score": round(missing_score, 3),
               "head": head_box, "layer": plan.layer, "z": plan.zc}
        if area:
            ys, xs = np.nonzero(persistent)
            out["debris_box"] = [round(xs.min() / ww, 3), round(ys.min() / hh, 3),
                                 round(xs.max() / ww, 3), round(ys.max() / hh, 3)]
        return out
