"""Read model meshes, lay copies out on the bed, and write the plate as one STL.

Used for multi-piece plates instead of the slicer's own arrange (which crashes in some
PrusaSlicer builds). Pieces keep their orientation; each is dropped onto the bed and
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


class MeshError(Exception):
    pass


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
def arrange(pieces: list[np.ndarray]) -> list[np.ndarray]:
    """Shelf-pack the pieces' footprints onto the bed, centred as a group. Raises if they don't fit."""
    sized = []
    for i, t in enumerate(pieces):
        lo, hi = t.reshape(-1, 3).min(0), t.reshape(-1, 3).max(0)
        sized.append((i, t - [lo[0], lo[1], lo[2]], hi[0] - lo[0], hi[1] - lo[1]))
    usable = (BED[0] - 2 * MARGIN, BED[1] - 2 * MARGIN)
    sized.sort(key=lambda s: (-s[3], -s[2]))                  # tallest footprint (Y) first
    placed, x, y, row_h, width = {}, 0.0, 0.0, 0.0, 0.0
    for i, t, w, d in sized:
        if w > usable[0] or d > usable[1]:
            raise MeshError(f"one piece is {w:.0f} x {d:.0f} mm, too big for the bed")
        if x > 0 and x + w > usable[0]:
            x, y, row_h = 0.0, y + row_h + GAP, 0.0
        placed[i] = (t, x, y)
        x += w + GAP
        row_h = max(row_h, d)
        width = max(width, x - GAP)
    depth = y + row_h
    if depth > usable[1]:
        raise MeshError(f"the pieces need about {width:.0f} x {depth:.0f} mm laid out; the bed is "
                        f"{BED[0]:.0f} x {BED[1]:.0f}")
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
