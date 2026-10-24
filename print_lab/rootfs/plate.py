"""Look at a sliced plate: where the extrusions land, and a top-down picture of it.

Pure Python + numpy (no imaging library): the picture is drawn into an array and written
as a PNG with zlib.
"""
import re
import struct
import zlib

import numpy as np

BED = (250.0, 250.0)          # Kobra S1 bed, mm
MOVE_RE = re.compile(rb"^G1\b([^;]*)")
ARG_RE = re.compile(rb"([XYZE])(-?\d*\.?\d+)")


def segments(data: bytes):
    """Extrusion moves after the first layer change, as arrays x0, y0, x1, y1, z."""
    start = data.find(b";LAYER_CHANGE")
    if start < 0:
        start = 0
    x = y = z = 0.0
    out = []
    for line in data[start:].split(b"\n"):
        if not line.startswith(b"G1"):
            continue
        m = MOVE_RE.match(line)
        if not m:
            continue
        nx, ny, e = x, y, None
        for k, v in ARG_RE.findall(m.group(1)):
            v = float(v)
            if k == b"X":
                nx = v
            elif k == b"Y":
                ny = v
            elif k == b"Z":
                z = v
            else:
                e = v
        if e is not None and e > 0 and (nx != x or ny != y):
            out.append((x, y, nx, ny, z))
        x, y = nx, ny
    if not out:
        return None
    a = np.asarray(out, dtype=np.float32)
    return a[:, 0], a[:, 1], a[:, 2], a[:, 3], a[:, 4]


def extents(segs):
    x0, y0, x1, y1, _ = segs
    xs = np.concatenate([x0, x1])
    ys = np.concatenate([y0, y1])
    return float(xs.min()), float(xs.max()), float(ys.min()), float(ys.max())


def fits(segs, margin=0.5, area=None):
    """area: (x0, y0, x1, y1) allowed instead of the nominal bed (full-size plates)."""
    lo_x, hi_x, lo_y, hi_y = extents(segs)
    x0, y0, x1, y1 = area or (0.0, 0.0, BED[0], BED[1])
    return lo_x >= x0 - margin and lo_y >= y0 - margin and hi_x <= x1 + margin and hi_y <= y1 + margin


def _png(rgb: np.ndarray) -> bytes:
    h, w, _ = rgb.shape
    raw = b"".join(b"\x00" + rgb[r].tobytes() for r in range(h))

    def chunk(tag, body):
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def render(segs, px_per_mm: float = 2.4, max_samples: int = 30_000_000) -> bytes:
    """Top-down view of the bed: extrusions in orange, lighter where the print is taller."""
    x0, y0, x1, y1, z = segs
    w, h = int(BED[0] * px_per_mm), int(BED[1] * px_per_mm)
    s = px_per_mm
    length = np.hypot(x1 - x0, y1 - y0) * s
    n = np.ceil(length).astype(np.int64) + 1
    if n.sum() > max_samples:                       # huge prints: keep every k-th segment
        keep = np.arange(len(n)) % int(np.ceil(n.sum() / max_samples)) == 0
        x0, y0, x1, y1, z, n = x0[keep], y0[keep], x1[keep], y1[keep], z[keep], n[keep]
    idx = np.repeat(np.arange(len(n)), n)
    first = np.repeat(np.cumsum(n) - n, n)
    t = (np.arange(len(idx)) - first) / np.maximum(np.repeat(n - 1, n), 1)
    px = np.clip(((x0[idx] + t * (x1[idx] - x0[idx])) * s).astype(np.int64), 0, w - 1)
    py = np.clip(((y0[idx] + t * (y1[idx] - y0[idx])) * s).astype(np.int64), 0, h - 1)
    height = np.full(w * h, -1.0, dtype=np.float32)
    np.maximum.at(height, (h - 1 - py) * w + px, z[idx])
    height = height.reshape(h, w)

    img = np.empty((h, w, 3), dtype=np.uint8)
    img[:] = (38, 38, 42)                            # bed
    img[::int(25 * s)] = (58, 58, 64)                # 25 mm grid
    img[:, ::int(25 * s)] = (58, 58, 64)
    on = height >= 0
    top = max(float(height.max()), 0.01)
    shade = (0.45 + 0.55 * (height / top))[..., None]
    img[on] = (np.array([255, 120, 30], dtype=np.float32) * shade)[on].astype(np.uint8)
    img[0, :] = img[-1, :] = img[:, 0] = img[:, -1] = (120, 120, 130)  # bed edge
    return _png(img)
