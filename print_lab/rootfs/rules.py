"""Slicing rules of thumb, picked from the plate's geometry and handed to Orca as overrides.

Each rule only fills in settings the request didn't set itself, so an explicit override always
wins, and "rules": false on /slice turns them all off. Sources and reasons are in RULES.md.
"""
import numpy as np

ROUND = 0.92          # footprint circularity (1 = disc, 0.91 = hexagon, 0.79 = square)
FLAT_MM = 10.0        # at or under this height a part is "flat" (mostly shells, little infill)
LARGE_MM = 120.0      # a footprint this wide or more counts as a large first layer


def _hull(xy: np.ndarray) -> np.ndarray:
    """Convex hull, in order (monotone chain: needs no scipy, and the order matters here)."""
    pts = np.unique(np.round(xy, 2), axis=0)
    if len(pts) < 3:
        return pts

    def turn(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    def half(points):
        out = []
        for p in points:
            while len(out) >= 2 and turn(out[-2], out[-1], p) <= 0:
                out.pop()
            out.append(p)
        return out[:-1]
    return np.array(half(pts) + half(pts[::-1]))


def shape(tris: np.ndarray) -> dict:
    """Footprint and height of one piece as it sits on the bed."""
    v = tris.reshape(-1, 3)
    hull = _hull(v[:, :2])
    if len(hull) < 3:
        return {"width_mm": 0.0, "height_mm": float(np.ptp(v[:, 2])), "circularity": 0.0}
    nxt = np.roll(hull, -1, axis=0)
    area = 0.5 * abs(float(np.sum(hull[:, 0] * nxt[:, 1] - nxt[:, 0] * hull[:, 1])))
    perim = float(np.linalg.norm(nxt - hull, axis=1).sum())
    return {"width_mm": round(float(np.ptp(v[:, :2], axis=0).max()), 1),
            "height_mm": round(float(np.ptp(v[:, 2])), 1),
            "circularity": round(4 * np.pi * area / perim ** 2, 3) if perim else 0.0}


def plan(parts: list[np.ndarray], taken: set[str]) -> tuple[dict, list[str]]:
    """Orca overrides for this plate, plus one line per rule that fired.
    taken: the Orca keys the request already set (left alone)."""
    shapes = [shape(p) for p in parts]
    if not shapes:
        return {}, []
    out, notes = {}, []

    def put(why, **settings):
        new = {k: v for k, v in settings.items() if k not in taken and k not in out}
        if new:
            out.update(new)
            notes.append(f"{why}: " + ", ".join(f"{k}={v}" for k, v in new.items()))

    # round parts (discs, rings, spool adapters): concentric lines follow the outline, so no
    # short zig-zags hitting the curved edge, fewer direction changes, and a smoother surface
    if all(s["circularity"] >= ROUND for s in shapes):
        put("round footprint", top_surface_pattern="concentric", bottom_surface_pattern="concentric",
            internal_solid_infill_pattern="concentric")
        # flat ones carry no load through the infill, so concentric there too (fast, no travel)
        if all(s["height_mm"] <= FLAT_MM for s in shapes):
            put("round and flat", sparse_infill_pattern="concentric")
    # a big first layer is where a plate fails: slower and hotter so it sticks edge to edge
    # (225/65 is what part 1 of the 200 mm spool ring printed with; the profile has 215/55)
    if any(s["width_mm"] >= LARGE_MM for s in shapes):
        put("large first layer", initial_layer_speed="30", initial_layer_infill_speed="40",
            nozzle_temperature_initial_layer="225", textured_plate_temp_initial_layer="65")
    return out, notes
