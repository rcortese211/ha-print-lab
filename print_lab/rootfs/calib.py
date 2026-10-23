"""Camera calibration: where any point of the printer appears in the camera image.

A one-layer print of small squares in three rows, each row spaced unevenly so that its
cross-ratio (a perspective-invariant number) identifies it. After the layer the printer
holds the bed at three heights while the Pi photographs it. Matching the squares in those
photos gives image points for known 3D points (the bed moves; the camera does not), and a
direct linear transform gives the camera's 3x4 projection matrix P:

    pixel ~ P @ [X, Y, z, 1]      X, Y bed mm; z = height above the bed minus the bed's Z

During a print the top layer is at the nozzle, so a point h mm above the bed when the
printer is at layer height Zc is at z = h - Zc.

numpy + scipy.ndimage.
"""
from __future__ import annotations

import itertools
import json
import subprocess
import time
from pathlib import Path

import numpy as np

# --------------------------------------------------------------------------- the marker print
ROWS = {                        # row Y (mm) -> square centres X (mm); uneven so cross-ratios differ
    "A": (60.0, (55.0, 75.0, 125.0, 195.0)),
    "B": (125.0, (55.0, 110.0, 140.0, 195.0)),
    "C": (190.0, (55.0, 125.0, 165.0, 195.0)),
}
SQUARE = 9.0                    # mm
LAYER = 0.3                     # mm (the profile's first layer)
DWELLS = ((1.0, 60), (12.0, 20), (25.0, 40))   # (bed Z mm, seconds held) after the layer
NAME = "camera_calibration"


def cross_ratio(t):
    t1, t2, t3, t4 = t
    return ((t3 - t1) * (t4 - t2)) / ((t3 - t2) * (t4 - t1))


ROW_CR = {k: cross_ratio(xs) for k, (_y, xs) in ROWS.items()}


def scad() -> str:
    cubes = [f"translate([{x - SQUARE / 2},{y - SQUARE / 2},0]) cube([{SQUARE},{SQUARE},{LAYER}]);"
             for y, xs in ROWS.values() for x in xs]
    return "union(){\n" + "\n".join(cubes) + "\n}\n"


def postprocess(gcode: bytes) -> bytes:
    """After the layer (once the head has parked at the back), hold the bed at each height."""
    marker = b"G1 Y270"
    i = gcode.rfind(marker)
    if i < 0:
        raise ValueError("couldn't find the end-of-print park move to add the bed dwells after")
    i = gcode.index(b"\n", i) + 1
    seq = "; camera calibration: hold the bed at known heights\n"
    for z, secs in DWELLS:
        seq += f"G1 Z{z:.1f} F600\nG4 S{secs}\n"
    return gcode[:i] + seq.encode() + gcode[i:]


def expected_bbox():
    xs = [x for _y, row in ROWS.values() for x in row]
    ys = [y for y, _row in ROWS.values()]
    h = SQUARE / 2
    return min(xs) - h, max(xs) + h, min(ys) - h, max(ys) + h


# --------------------------------------------------------------------------- images
def gray(jpg: Path) -> np.ndarray:
    p = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=width,height", "-of", "csv=p=0:s=x", str(jpg)],
                       capture_output=True, text=True, timeout=20)
    w, h = (int(v) for v in p.stdout.strip().split("x")[:2])
    raw = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(jpg), "-f", "rawvideo",
                          "-pix_fmt", "gray", "-"], capture_output=True, timeout=30).stdout
    return np.frombuffer(raw, np.uint8).reshape(h, w).astype(np.float32)


def rgb(jpg: Path) -> np.ndarray:
    g = gray(jpg)
    h, w = g.shape
    raw = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(jpg), "-f", "rawvideo",
                          "-pix_fmt", "rgb24", "-"], capture_output=True, timeout=30).stdout
    return np.frombuffer(raw, np.uint8).reshape(h, w, 3)


def blobs(g: np.ndarray, max_blobs: int = 60) -> np.ndarray:
    """Centroids (x, y) of small bright blobs: white squares on a dark bed."""
    from scipy import ndimage  # noqa: PLC0415
    local = ndimage.uniform_filter(g, 41)
    mask = (g - local > 40) & (g > 110)
    mask = ndimage.binary_opening(mask, np.ones((2, 2)))
    lab, n = ndimage.label(mask)
    if n == 0:
        return np.zeros((0, 2))
    idx = np.arange(1, n + 1)
    area = ndimage.sum(mask, lab, idx)
    cy, cx = np.asarray(ndimage.center_of_mass(mask, lab, idx)).T
    slices = ndimage.find_objects(lab)
    out = []
    for k, sl in enumerate(slices):
        h, w = sl[0].stop - sl[0].start, sl[1].stop - sl[1].start
        # a flat square seen at an angle: wider than tall, bright, solid
        if not 20 <= area[k] <= 12000 or area[k] / (h * w) < 0.45 or not 1.2 <= w / h <= 14 or w > 260:
            continue
        if ndimage.mean(g, lab, k + 1) < 205:
            continue
        # white plastic on the dark bed stands far above its surroundings; glare on the
        # brighter, glittery near half of the bed doesn't
        y0, y1 = max(sl[0].start - 2 * h, 0), min(sl[0].stop + 2 * h, g.shape[0])
        x0, x1 = max(sl[1].start - w, 0), min(sl[1].stop + w, g.shape[1])
        ring = g[y0:y1, x0:x1][~mask[y0:y1, x0:x1]]
        if ring.size and ndimage.mean(g, lab, k + 1) - np.median(ring) < 120:
            continue
        out.append((area[k], cx[k], cy[k]))
    out.sort(reverse=True)
    return np.asarray([(x, y) for _a, x, y in out[:max_blobs]]).reshape(-1, 2)


# --------------------------------------------------------------------------- geometry
def _norm(pts):
    c = pts.mean(0)
    s = np.sqrt(2) / max(np.sqrt(((pts - c) ** 2).sum(1)).mean(), 1e-9)
    t = np.array([[s, 0, -s * c[0]], [0, s, -s * c[1]], [0, 0, 1]])
    return t, (np.c_[pts, np.ones(len(pts))] @ t.T)[:, :2]


def homography(src, dst):
    """Plane-to-image homography (DLT, normalised). src, dst: (n, 2), n >= 4."""
    ts, s = _norm(np.asarray(src, float))
    td, d = _norm(np.asarray(dst, float))
    rows = []
    for (x, y), (u, v) in zip(s, d):
        rows.append([-x, -y, -1, 0, 0, 0, u * x, u * y, u])
        rows.append([0, 0, 0, -x, -y, -1, v * x, v * y, v])
    _, _, vt = np.linalg.svd(np.asarray(rows))
    h = vt[-1].reshape(3, 3)
    h = np.linalg.inv(td) @ h @ ts
    return h / h[2, 2]


def apply_h(h, pts):
    p = np.c_[pts, np.ones(len(pts))] @ h.T
    return p[:, :2] / p[:, 2:3]


def projection(world, image):
    """3x4 camera matrix from >= 6 non-coplanar 3D points and their pixels (DLT)."""
    world, image = np.asarray(world, float), np.asarray(image, float)
    ti, im = _norm(image)
    c = world.mean(0)
    s = np.sqrt(3) / max(np.sqrt(((world - c) ** 2).sum(1)).mean(), 1e-9)
    tw = np.diag([s, s, s, 1.0])
    tw[:3, 3] = -s * c
    w = (np.c_[world, np.ones(len(world))] @ tw.T)
    rows = []
    for (x, y, z, one), (u, v) in zip(w, im):
        rows.append([x, y, z, one, 0, 0, 0, 0, -u * x, -u * y, -u * z, -u * one])
        rows.append([0, 0, 0, 0, x, y, z, one, -v * x, -v * y, -v * z, -v * one])
    _, _, vt = np.linalg.svd(np.asarray(rows))
    p = vt[-1].reshape(3, 4)
    p = np.linalg.inv(ti) @ p @ tw
    return p / np.linalg.norm(p[2, :3])


def project(p, world):
    world = np.asarray(world, float).reshape(-1, 3)
    q = np.c_[world, np.ones(len(world))] @ p.T
    return q[:, :2] / q[:, 2:3]


# --------------------------------------------------------------------------- lens
CENTER = np.array([640.0, 360.0])
LENS_SCALE = 1000.0


def undistort(pts, k1):
    """Observed pixel -> ideal (pinhole) pixel, one-parameter radial model."""
    pts = np.asarray(pts, float).reshape(-1, 2)
    d = pts - CENTER
    r2 = (d ** 2).sum(1, keepdims=True) / LENS_SCALE ** 2
    return CENTER + d * (1 + k1 * r2)


def distort(pts, k1):
    """Ideal pixel -> observed pixel (inverse of undistort, by iteration)."""
    ideal = np.asarray(pts, float).reshape(-1, 2)
    obs = ideal.copy()
    for _ in range(8):
        d = obs - CENTER
        r2 = (d ** 2).sum(1, keepdims=True) / LENS_SCALE ** 2
        obs = CENTER + (ideal - CENTER) / (1 + k1 * r2)
    return obs


class Camera:
    def __init__(self, p, k1=0.0):
        self.p, self.k1 = np.asarray(p, float), float(k1)

    def project(self, world):
        return distort(project(self.p, world), self.k1)

    def bed_h(self, zc):
        """Ideal-pixel homography of the bed plane lowered by zc."""
        p = self.p
        return np.c_[p[:, 0], p[:, 1], p[:, 3] - zc * p[:, 2]]


# --------------------------------------------------------------------------- matching
MODEL = np.array([(x, y) for y, xs in ROWS.values() for x in xs], float)


def _batch_h(src, dst):
    """Homographies for a batch of 4-point correspondences: (n, 4, 2) -> (n, 3, 3)."""
    n = len(src)
    a = np.zeros((n, 8, 9))
    x, y, u, v = src[..., 0], src[..., 1], dst[..., 0], dst[..., 1]
    a[:, 0::2, 0], a[:, 0::2, 1], a[:, 0::2, 2] = -x, -y, -1
    a[:, 0::2, 6], a[:, 0::2, 7], a[:, 0::2, 8] = u * x, u * y, u
    a[:, 1::2, 3], a[:, 1::2, 4], a[:, 1::2, 5] = -x, -y, -1
    a[:, 1::2, 6], a[:, 1::2, 7], a[:, 1::2, 8] = v * x, v * y, v
    _, _, vt = np.linalg.svd(a)
    return vt[:, -1].reshape(n, 3, 3)


def _assign(h, pts, radius):
    """One-to-one marker <-> detection pairs within radius for homography h."""
    with np.errstate(all="ignore"):
        proj = apply_h(h, MODEL)
    d = np.linalg.norm(proj[:, None, :] - pts[None, :, :], axis=-1)
    pairs, used_m, used_d = [], set(), set()
    for flat in np.argsort(d, axis=None):
        m, k = divmod(int(flat), len(pts))
        if d[m, k] > radius:
            break
        if m in used_m or k in used_d:
            continue
        used_m.add(m)
        used_d.add(k)
        pairs.append((m, k))
    return pairs


def match_frame(pts: np.ndarray, samples: int = 150_000, seed: int = 0, prior=None):
    """(bed_xy, image_xy, rms_px) for the markers in one frame, or None (needs 6+).
    Robust search: random 4-marker/4-detection hypotheses, scored by how many distinct
    markers land on a detection; then refined on all matches. With a prior homography
    (from another frame) it only refines."""
    if len(pts) < 6:
        return None
    if prior is not None:
        h = prior
    else:
        rng = np.random.default_rng(seed)
        di = np.argsort(rng.random((samples, len(pts))), axis=1)[:, :4]
        mi = np.argsort(rng.random((samples, len(MODEL))), axis=1)[:, :4]
        hs = _batch_h(MODEL[mi], pts[di])
        with np.errstate(all="ignore"):
            proj = np.einsum("nij,mj->nmi", hs, np.c_[MODEL, np.ones(len(MODEL))])
            proj = proj[..., :2] / proj[..., 2:3]
            d = np.linalg.norm(proj[:, :, None, :] - pts[None, None, :, :], axis=-1)
        near, ok = d.argmin(-1), d.min(-1) < 8
        # distinct detections hit by the 12 markers
        hit = np.zeros((samples, len(pts)), bool)
        rows = np.repeat(np.arange(samples), len(MODEL))
        hit[rows[ok.ravel()], near.ravel()[ok.ravel()]] = True
        score = hit.sum(1)
        h = hs[int(np.argmax(score))]
    for radius in (20, 12, 8):
        pairs = _assign(h, pts, radius)
        if len(pairs) < 6:
            return None
        h = homography(MODEL[[m for m, _ in pairs]], pts[[k for _, k in pairs]])
    bed, img = MODEL[[m for m, _ in pairs]], pts[[k for _, k in pairs]]
    rms = float(np.sqrt(((apply_h(h, bed) - img) ** 2).sum(1).mean()))
    return (bed, img, rms, h) if rms < 10.0 else None


def _same_picture(a, b, tol=3.0):
    if len(a) < 4 or len(b) < 4 or abs(len(a) - len(b)) > 2:
        return False
    d = np.linalg.norm(a[:, None, :] - b[None, :, :], axis=-1).min(1)
    return float(np.median(d)) < tol


def solve(frames: list[tuple[float, Path]]) -> dict:
    """frames: (time, jpg) taken through the calibration run. Returns the calibration."""
    # 1) group consecutive frames that show the same blobs (one bed height each)
    groups = []
    for t, jpg in frames:
        try:
            pts = blobs(gray(jpg))
        except Exception:  # noqa: BLE001
            continue
        if groups and _same_picture(groups[-1]["pts"][-1], pts):
            g = groups[-1]
            g["t1"] = t
            g["pts"].append(pts)
            g["frames"].append(jpg)
        else:
            groups.append({"t0": t, "t1": t, "pts": [pts], "frames": [jpg]})
    groups = [g for g in groups if len(g["frames"]) >= 2 and len(g["pts"][-1]) >= 6]
    # 2) find the markers in each held group (search once, then refine across its frames)
    prior = None
    for g in groups:
        rep = g["pts"][len(g["pts"]) // 2]
        m = match_frame(rep, prior=None) if prior is None else (match_frame(rep, prior=prior) or match_frame(rep))
        if m is None:
            g["match"] = None
            continue
        prior = m[3]
        g["match"] = {tuple(b): i for b, i in zip(m[0], m[1])}
        for pts in g["pts"]:          # average each marker over the group's frames
            mm = match_frame(pts, prior=m[3])
            if mm:
                for b, i in zip(mm[0], mm[1]):
                    g.setdefault("acc", {}).setdefault(tuple(b), []).append(i)
    groups = [g for g in groups if g.get("acc")]
    if not groups:
        raise ValueError("couldn't find the marker squares in any held view")
    for g in groups:
        g["dur"] = g["t1"] - g["t0"] + 4.0
    # 3) the holds are the last groups, in order (Z1 60 s, Z12 20 s, Z25 40 s)
    tail = groups[-5:]
    best = None
    for k in range(min(len(tail), len(DWELLS)), 0, -1):
        for gi in itertools.combinations(range(len(tail)), k):
            for di in itertools.combinations(range(len(DWELLS)), k):
                cost = sum(abs(tail[g]["dur"] - DWELLS[d][1]) / DWELLS[d][1] for g, d in zip(gi, di))
                cost += 0.5 * (len(tail) - 1 - gi[-1])
                if best is None or cost < best[0]:
                    best = (cost, [(DWELLS[d][0], tail[g]) for g, d in zip(gi, di)])
        if best is not None and best[0] < 0.6 * k:
            break
    assigned = best[1]
    world, image = [], []
    for z, g in assigned:
        for (x, y), vs in g["acc"].items():
            world.append((x, y, LAYER - z))
            image.append(np.median(vs, 0))
    world, image = np.asarray(world), np.asarray(image)
    info = {"made": time.strftime("%Y-%m-%d %H:%M:%S"), "points": len(world),
            "heights": sorted(z for z, _g in assigned), "frame": str(assigned[0][1]["frames"][-1]),
            "frame_bed_z": assigned[0][0], "groups": [round(g["dur"]) for g in groups]}
    if len({z for z, _g in assigned}) < 2:
        # one bed height: a flat map of the bed is all that can be had
        g = assigned[0][1]
        bed = np.asarray(list(g["acc"].keys()))
        img = np.asarray([np.median(v, 0) for v in g["acc"].values()])
        h = homography(bed, img)
        err = np.hypot(*(apply_h(h, bed) - img).T)
        return {**info, "H": h.tolist(), "P": None, "k1": 0.0,
                "rms_px": round(float(np.sqrt((err ** 2).mean())), 2), "max_px": round(float(err.max()), 2),
                "note": "bed height didn't change in the camera; flat bed map only"}
    # 4) camera matrix, with the lens bend that fits best
    best = None
    for k1 in np.linspace(-0.6, 0.6, 61):
        ideal = undistort(image, k1)
        p = projection(world, ideal)
        err = np.linalg.norm(distort(project(p, world), k1) - image, axis=1)
        rms = float(np.sqrt((err ** 2).mean()))
        if best is None or rms < best[0]:
            best = (rms, k1, p, err)
    rms, k1, p, err = best
    return {**info, "P": p.tolist(), "k1": round(float(k1), 4), "rms_px": round(rms, 2),
            "max_px": round(float(err.max()), 2)}


def load(path: Path):
    """(Camera or None, calibration dict or None)."""
    try:
        cal = json.loads(path.read_text())
        return (Camera(cal["P"], cal.get("k1", 0.0)) if cal.get("P") else None), cal
    except (OSError, ValueError, KeyError):
        return None, None


def draw_overlay(jpg: Path, cam, out: Path, bed_z: float = DWELLS[0][0]) -> None:
    """The frame with the bed outline and marker centres drawn where the calibration puts them."""
    g = gray(jpg)
    h, w = g.shape
    img = np.repeat(np.clip(g, 0, 255).astype(np.uint8)[..., None], 3, axis=2)

    def dot(u, v, colour, r=3):
        if not (np.isfinite(u) and np.isfinite(v) and 0 <= u < w and 0 <= v < h):
            return
        u, v = int(round(u)), int(round(v))
        img[max(v - r, 0):min(v + r + 1, h), max(u - r, 0):min(u + r + 1, w)] = colour

    edge = np.concatenate([np.c_[np.linspace(0, 250, 200), np.full(200, y)] for y in (0, 250)] +
                          [np.c_[np.full(200, x), np.linspace(0, 250, 200)] for x in (0, 250)])
    for u, v in cam.project(np.c_[edge, np.full(len(edge), -bed_z)]):
        dot(u, v, (0, 200, 255), 1)
    for y, xs in ROWS.values():
        for x in xs:
            u, v = cam.project([(x, y, LAYER - bed_z)])[0]
            dot(u, v, (255, 40, 40), 4)
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
                    "-s", f"{w}x{h}", "-i", "-", "-q:v", "3", str(out)], input=img.tobytes(), check=True)
