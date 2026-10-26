"""Read model meshes, lay copies out on the bed, and write the plate as one STL.

Used for multi-piece plates instead of the slicer's own arrange (which crashes in some
PrusaSlicer builds). Pieces keep their orientation (turned about Z only when that's what makes them fit); each is dropped onto the bed and
placed by a simple shelf packer on its footprint, with a gap between pieces.
Supports STL (binary/ASCII), 3MF and OBJ. numpy only.
"""
from __future__ import annotations

import re
import struct
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

BED = (250.0, 250.0)
GAP = 6.0
MARGIN = 10.0          # kept clear around the layout for the skirt line
SOLO_MARGIN = 4.0      # a plate of one big piece may come this close to the edge


class MeshError(Exception):
    """fit_scale: the biggest scale (%) at which every piece fits the bed on a plate of its own;
    separate: the pieces fit one at a time but not together."""

    def __init__(self, msg, fit_scale=None, separate=False, full_bed=False):
        super().__init__(msg)
        self.fit_scale = fit_scale
        self.separate = separate
        self.full_bed = full_bed      # every piece fits alone if the whole bed (edge to edge) is used


# --------------------------------------------------------------------------- reading
def _stl(data: bytes) -> np.ndarray:
    if len(data) >= 84:
        n = struct.unpack_from("<I", data, 80)[0]
        if 84 + 50 * n == len(data):
            rec = np.frombuffer(data, dtype=np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")]),
                                count=n, offset=84)
            return rec["v"].astype(np.float64)
    text = data.decode(errors="ignore")
    v = re.findall(r"vertex\s+(\S+)\s+(\S+)\s+(\S+)", text)
    if not v or len(v) % 3:
        raise MeshError("not a readable STL")
    return np.asarray(v, dtype=np.float64).reshape(-1, 3, 3)


def _obj(data: bytes) -> np.ndarray:
    verts, tris = [], []
    for line in data.decode(errors="ignore").splitlines():
        p = line.split()
        if not p:
            continue
        if p[0] == "v" and len(p) >= 4:
            verts.append([float(x) for x in p[1:4]])
        elif p[0] == "f" and len(p) >= 4:
            idx = [int(x.split("/")[0]) for x in p[1:]]
            idx = [i - 1 if i > 0 else len(verts) + i for i in idx]
            tris += [(idx[0], idx[k], idx[k + 1]) for k in range(1, len(idx) - 1)]
    if not tris:
        raise MeshError("not a readable OBJ")
    return np.asarray(verts)[np.asarray(tris)]


def _transform(attr: str | None) -> np.ndarray:
    m = np.eye(4)
    if attr:
        v = [float(x) for x in attr.split()]
        if len(v) == 12:  # 3MF: m00 m01 m02 m10 m11 m12 m20 m21 m22 m30 m31 m32 (row vectors)
            m[:3, :3] = np.asarray(v[:9]).reshape(3, 3)
            m[3, :3] = v[9:]
    return m


def _apply(tris: np.ndarray, m: np.ndarray) -> np.ndarray:
    flat = tris.reshape(-1, 3)
    return (np.c_[flat, np.ones(len(flat))] @ m)[:, :3].reshape(-1, 3, 3)


def _3mf(path: Path) -> np.ndarray:
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as err:
        raise MeshError("not a readable 3MF") from err
    models = [n for n in zf.namelist() if n.lower().endswith(".model")]
    if not models:
        raise MeshError("3MF has no model")
    objects, build, unit = {}, [], 1.0
    scale = {"micron": 0.001, "millimeter": 1.0, "centimeter": 10.0, "inch": 25.4, "foot": 304.8, "meter": 1000.0}
    for name in models:
        root = ET.fromstring(zf.read(name))
        ns = root.tag.split("}")[0] + "}" if root.tag.startswith("{") else ""
        if name.lower().endswith("3dmodel.model") or not build:
            unit = scale.get(root.get("unit", "millimeter"), 1.0)
        for obj in root.iter(f"{ns}object"):
            oid = (name, obj.get("id"))
            mesh = obj.find(f"{ns}mesh")
            entry = {"tris": None, "components": []}
            if mesh is not None:
                verts = np.asarray([[float(v.get("x")), float(v.get("y")), float(v.get("z"))]
                                    for v in mesh.iter(f"{ns}vertex")])
                tri = np.asarray([[int(t.get("v1")), int(t.get("v2")), int(t.get("v3"))]
                                  for t in mesh.iter(f"{ns}triangle")])
                if len(verts) and len(tri):
                    entry["tris"] = verts[tri]
            comps = obj.find(f"{ns}components")
            if comps is not None:
                for c in comps:
                    ref = c.get("{http://schemas.microsoft.com/3dmanufacturing/production/2015/06}path") or name
                    entry["components"].append(((ref.lstrip("/"), c.get("objectid")), _transform(c.get("transform"))))
            objects[oid] = entry
        b = root.find(f"{ns}build")
        if b is not None:
            build += [((name, it.get("objectid")), _transform(it.get("transform"))) for it in b]

    def flatten(oid, m, depth=0):
        e = objects.get(oid)
        if e is None or depth > 8:
            return []
        out = [_apply(e["tris"], m)] if e["tris"] is not None else []
        for ref, cm in e["components"]:
            out += flatten(ref, cm @ m, depth + 1)
        return out

    parts = []
    for oid, m in build or [(k, np.eye(4)) for k in objects]:
        parts += flatten(oid, m)
    if not parts:
        raise MeshError("3MF has no triangles")
    return np.concatenate(parts) * unit


def load(path: Path) -> np.ndarray:
    ext = path.suffix.lower()
    if ext == ".stl":
        tris = _stl(path.read_bytes())
    elif ext == ".obj":
        tris = _obj(path.read_bytes())
    elif ext == ".3mf":
        tris = _3mf(path)
    else:
        raise MeshError(f"can't combine {ext} files on a plate")
    tris = tris[np.isfinite(tris).all(axis=(1, 2))]
    if not len(tris):
        raise MeshError(f"{path.name} has no geometry")
    return tris


# --------------------------------------------------------------------------- layout
def _rot_z(t: np.ndarray, deg: float) -> np.ndarray:
    a = np.radians(deg)
    c, s = np.cos(a), np.sin(a)
    return t @ np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]]).T


def _hull(xy: np.ndarray) -> np.ndarray:
    """The footprint's convex hull points (same box at every angle, far fewer points)."""
    xy = np.unique(np.round(xy, 2), axis=0)
    try:
        from scipy.spatial import ConvexHull
        return xy[ConvexHull(xy).vertices]
    except Exception:          # no scipy, or a degenerate footprint
        return xy


def fit_rotation(t: np.ndarray, room: tuple[float, float], tighten: bool = False) -> tuple[np.ndarray, float]:
    """Turn a piece about Z so its footprint fits room (w, d), if it doesn't already.
    Models are often saved at an angle (diagonal panels), which inflates the box we lay out.
    Picks the fitting angle with the smallest footprint; with tighten, also turns a piece that
    fits when that shrinks its footprint by a fifth or more (packs plates better).
    Returns (piece, degrees turned)."""
    xy = _hull(t.reshape(-1, 3)[:, :2])
    w, d = np.ptp(xy, axis=0)
    fits_now = w <= room[0] and d <= room[1]
    if fits_now and not tighten:
        return t, 0.0
    best = None
    for deg in np.arange(1.0, 180.0, 1.0):
        a = np.radians(deg)
        c, s = np.cos(a), np.sin(a)
        r = xy @ np.array([[c, -s], [s, c]]).T
        rw, rd = np.ptp(r, axis=0)
        fits = rw <= room[0] and rd <= room[1]
        key = (not fits, rw * rd if fits else max(rw - room[0], rd - room[1]))
        if best is None or key < best[0]:
            best = (key, deg)
    if best[0][0]:            # nothing fits; leave it as it was
        return t, 0.0
    if fits_now and best[0][1] > 0.8 * w * d:
        return t, 0.0
    return _rot_z(t, best[1]), float(best[1])


def size(t: np.ndarray) -> tuple[float, float, float]:
    v = t.reshape(-1, 3)
    w, d, h = np.ptp(v, axis=0)
    return float(w), float(d), float(h)


def arrange(pieces: list[np.ndarray], area=None, edge=None) -> list[np.ndarray]:
    """Shelf-pack the pieces' footprints onto the bed, centred as a group. Raises if they don't fit.
    A piece too big for the bed is turned about Z if some angle fits; a plate of one piece may use
    the bed up to SOLO_MARGIN from the edge.
    area: (x0, y0, x1, y1) to use instead, edge to edge with no margin (a plate Robbie asked to
    print full size). edge: that area's (w, d), only to say in the error whether it would fit."""
    usable = (BED[0] - 2 * MARGIN, BED[1] - 2 * MARGIN)
    solo = (BED[0] - 2 * SOLO_MARGIN, BED[1] - 2 * SOLO_MARGIN)
    room = usable if len(pieces) > 1 else solo
    if area is not None:
        usable = solo = room = (area[2] - area[0], area[3] - area[1])
    sized = []
    turned = {}
    for i, t in enumerate(pieces):
        key = id(t)
        if key not in turned:
            turned[key] = fit_rotation(t, room, tighten=len(pieces) > 1)[0]
        t = turned[key]
        lo, hi = t.reshape(-1, 3).min(0), t.reshape(-1, 3).max(0)
        sized.append((i, t - [lo[0], lo[1], lo[2]], hi[0] - lo[0], hi[1] - lo[1]))
    too_big = [(w, d) for _i, _t, w, d in sized if w > room[0] or d > room[1]]
    if too_big:
        # what each piece needs on a plate of its own (turned to its best angle for that)
        alone = [size(fit_rotation(t, solo)[0])[:2] for t in pieces]
        scale = min(min(solo[0] / w, solo[1] / d) for w, d in alone)
        fit = int(scale * 100) if scale < 1 else None
        many = len(pieces) > 1
        w, d = max(too_big, key=lambda s: s[0] * s[1])
        if fit:
            msg = (f"one piece is {w:.0f} x {d:.0f} mm at its best angle, too big for the "
                   f"{BED[0]:.0f} x {BED[1]:.0f} mm bed; it would fit scaled to {fit}%"
                   + (", one piece per plate" if many else ""))
        else:
            msg = (f"one piece is {w:.0f} x {d:.0f} mm, too big to share the bed; "
                   "each piece fits on a plate of its own")
        full = area is None and edge is not None and all(
            max(size(fit_rotation(t, edge)[0])[0] - edge[0], size(fit_rotation(t, edge)[0])[1] - edge[1]) <= 0
            for t in pieces)
        if full:
            msg += (" (or full size" + (", one per plate," if many else "") + " using the bed edge to edge)")
        raise MeshError(msg, fit_scale=fit, separate=many, full_bed=full)
    if len(pieces) == 1:
        usable = room
    sized.sort(key=lambda s: (-s[3], -s[2]))                  # tallest footprint (Y) first
    placed, x, y, row_h, width = {}, 0.0, 0.0, 0.0, 0.0
    for i, t, w, d in sized:
        if x > 0 and x + w > usable[0]:
            x, y, row_h = 0.0, y + row_h + GAP, 0.0
        placed[i] = (t, x, y)
        x += w + GAP
        row_h = max(row_h, d)
        width = max(width, x - GAP)
    depth = y + row_h
    if depth > usable[1]:
        raise MeshError(f"the pieces need about {width:.0f} x {depth:.0f} mm laid out and don't fit "
                        f"together on the {BED[0]:.0f} x {BED[1]:.0f} mm bed; print them on separate plates",
                        separate=True)
    if area is not None:
        ox, oy = (area[0] + area[2] - width) / 2, (area[1] + area[3] - depth) / 2
    else:
        ox, oy = (BED[0] - width) / 2, (BED[1] - depth) / 2
    return [placed[i][0] + [placed[i][1] + ox, placed[i][2] + oy, 0] for i in range(len(pieces))]


def write_stl(path: Path, parts: list[np.ndarray]) -> None:
    tris = np.concatenate(parts).astype(np.float32)
    a, b, c = tris[:, 0], tris[:, 1], tris[:, 2]
    n = np.cross(b - a, c - a)
    n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)
    rec = np.zeros(len(tris), dtype=np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")]))
    rec["n"], rec["v"] = n, tris
    with open(path, "wb") as f:
        f.write(b"Print Lab plate".ljust(80, b"\0") + struct.pack("<I", len(tris)) + rec.tobytes())


def scaled(t: np.ndarray, pct: float) -> np.ndarray:
    return t if pct == 100 else t * (pct / 100.0)


def build_plate(inputs: list[str], out: Path) -> dict:
    """inputs: model paths, one entry per copy. Writes one STL with everything laid out."""
    cache = {}
    pieces = []
    for p in inputs:
        if p not in cache:
            cache[p] = load(Path(p))
        pieces.append(cache[p])
    laid = arrange(pieces)
    write_stl(out, laid)
    allv = np.concatenate(laid).reshape(-1, 3)
    lo, hi = allv.min(0), allv.max(0)
    return {"pieces": len(pieces), "footprint_mm": [round(float(hi[0] - lo[0])), round(float(hi[1] - lo[1]))]}


# --------------------------------------------------------------------------- orientation
def _area_normals(tris: np.ndarray):
    n = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    a = np.linalg.norm(n, axis=1)
    return a / 2, n / np.maximum(a, 1e-12)[:, None]


def _rotation_to_down(d: np.ndarray) -> np.ndarray:
    """Rotation matrix taking unit vector d onto (0, 0, -1)."""
    t = np.array([0.0, 0.0, -1.0])
    d = d / np.linalg.norm(d)
    v, c = np.cross(d, t), float(np.dot(d, t))
    if c > 1 - 1e-9:
        return np.eye(3)
    if c < -1 + 1e-9:
        return np.diag([1.0, -1.0, -1.0])          # 180 degrees about X
    vx = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return np.eye(3) + vx + vx @ vx / (1 + c)


OVERHANG_COS = np.cos(np.radians(45 + 90))     # faces steeper than 45 degrees past vertical, facing down
BRIDGE_SPAN = 12.0                             # mm a horizontal ceiling can bridge without support


def evaluate(tris: np.ndarray, area=None, normals=None) -> dict:
    """Bed contact, unsupported overhang and height for the mesh as it sits (Z up)."""
    if area is None:
        area, normals = _area_normals(tris)
    z = tris[:, :, 2]
    zmin = z.min()
    lowest = z.max(axis=1)
    on_bed = (lowest < zmin + 0.15) & (normals[:, 2] < -0.985)
    down = (normals[:, 2] < OVERHANG_COS) & ~on_bed & (z.min(axis=1) > zmin + 0.3)
    # flat ceilings with a short span bridge fine; only count the ones that are wide
    flat = down & (normals[:, 2] < -0.985)
    span = np.ptp(tris[:, :, :2], axis=1).max(axis=1)
    overhang = float(area[down & ~(flat & (span < BRIDGE_SPAN))].sum())
    return {"contact_mm2": round(float(area[on_bed].sum()), 1), "overhang_mm2": round(overhang, 1),
            "height_mm": round(float(np.ptp(z)), 1), "area_mm2": round(float(area.sum()), 1)}


def orient(tris: np.ndarray) -> tuple[np.ndarray, dict]:
    """Rest the model on its best face: most bed contact, least overhang, then lowest.
    Keeps the designer's orientation unless another is clearly better."""
    area, normals = _area_normals(tris)
    total = float(area.sum()) or 1.0
    # candidate "down" directions: the biggest groups of same-facing triangles, plus the axes
    key = np.round(normals, 2)
    uniq, inv = np.unique(key, axis=0, return_inverse=True)
    sums = np.bincount(inv.ravel(), weights=area)
    dirs = [uniq[i] for i in np.argsort(sums)[::-1][:24] if sums[i] > 0.01 * total]
    dirs += [np.array(v, dtype=float) for v in ((0, 0, -1), (0, 0, 1), (1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0))]

    def cost(e):
        # overhang is what fails; contact is what holds it down; height breaks ties
        return e["overhang_mm2"] - 1.5 * e["contact_mm2"] + 0.002 * total * e["height_mm"]

    original = evaluate(tris, area, normals)
    best = (cost(original) - 0.05 * total, np.eye(3), original)    # the designer's choice gets a head start
    seen = set()
    for d in dirs:
        k = tuple(np.round(d / np.linalg.norm(d), 3))
        if k in seen:
            continue
        seen.add(k)
        r = _rotation_to_down(np.asarray(d, dtype=float))
        rt = tris @ r.T
        e = evaluate(rt, area, normals @ r.T)
        c = cost(e)
        if c < best[0]:
            best = (c, r, e)
    _, r, e = best
    rotated = not np.allclose(r, np.eye(3))
    out = tris @ r.T if rotated else tris
    e = dict(e, rotated=bool(rotated), before=original, needs_supports=_needs_supports(e, total))
    return out, e


def _needs_supports(e: dict, total: float) -> bool:
    return bool(e["overhang_mm2"] > max(40.0, 0.04 * total))


def as_modeled(tris: np.ndarray) -> dict:
    """The model left exactly as it was designed: what it touches, and whether it needs supports."""
    e = evaluate(tris)
    return dict(e, rotated=False, as_modeled=True, needs_supports=_needs_supports(e, e["area_mm2"] or 1.0))
