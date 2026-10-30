"""
layout_image.py  -  read LANE ARROWS from a UTC-screen layout picture (IP58B.JPG type)   (NEW)
---------------------------------------------------------------------------------------------
What it reads (nothing else):
    every red / green LANE ARROW on the road  ->  one arrow = one approach lane
        straight arrow          -> through lane
        arrow bending left      -> left-turn lane      (left / right = as seen by the DRIVER)
        arrow bending right     -> right-turn lane
        arrow with a branch     -> not guessed: flagged "shared / unclear - check"
    The arm is taken from the direction the arrows travel (picture "up" = North):
        arrows heading South -> arm N,  heading North -> arm S,
        arrows heading West  -> arm E,  heading East  -> arm W

What it ignores: signal-group letters, green/red bars, the UTC PLAN STAGES and UTC LIVE STAGES
panels, logos.  Only arrows that sit in line with a stop-line detector box (the small white
squares on the road) are accepted, so the panels can never add lanes.

Output  (same format layout_source.py expects):
    {"N": {"left": 2, "through": 3, "right": 0, "left_shared": 0, "right_shared": 0}, ...}
plus notes, and a check picture  cache\\layout_check\\<name>.png  (arrows labelled L / T / R,
arm names written on it) so every junction can be checked by eye.
"""
from pathlib import Path

import cv2
import numpy as np

CHECK_DIR = Path(__file__).resolve().parent / "cache" / "layout_check"
TRAVEL_TO_ARM = {(0, 1): "N", (0, -1): "S", (-1, 0): "E", (1, 0): "W"}   # travel vector (image x, y) -> arm
LEFT_OF = lambda t: (t[1], -t[0])                                         # driver's left, image coords (y down)


def _mask(img):
    b, g, r = [img[:, :, i].astype(int) for i in range(3)]
    red = (r > 170) & (g < 100) & (b < 100)
    grn = (g > 170) & (r < 120) & (b < 120)
    m = (red | grn).astype(np.uint8)
    return cv2.dilate(m, np.ones((3, 3), np.uint8), iterations=1)


def _detectors(img):
    """Find the hollow white lane/stop-line detector boxes.

    Layout exports are not all saved at the same pixel size.  The old detector
    required an almost exact 14 x 17 px box, which made some junctions work and
    others fail.  This version uses shape/fill ratios plus broad size limits.
    """
    b, g, r = [img[:, :, i].astype(int) for i in range(3)]
    white = ((r > 165) & (g > 165) & (b > 165)).astype(np.uint8)
    n, _, st, cen = cv2.connectedComponentsWithStats(white, connectivity=8)
    H, W = img.shape[:2]
    out = []
    for i in range(1, n):
        x, y, ww, hh, area = st[i]
        long_, short = max(ww, hh), min(ww, hh)
        if short < 5 or long_ < 7:
            continue
        # Reject large text/panels but allow detector boxes over a wide range of
        # layout resolutions and mild aspect-ratio distortion.
        if long_ > max(60, int(0.04 * max(H, W))):
            continue
        aspect = long_ / max(1.0, short)
        fill = area / max(1.0, ww * hh)
        if 1.0 <= aspect <= 1.8 and 0.18 <= fill <= 0.72:
            out.append((float(cen[i][0]), float(cen[i][1])))
    return out


def _classify(comp):
    """comp = boolean array of one arrow (cropped).  -> dict(travel, kind, offset, length, width)"""
    h, w = comp.shape
    horizontal = w > h
    A = comp if horizontal else comp.T                      # axis along columns
    ys, xs = np.nonzero(A)
    length = A.shape[1]
    cols = A.sum(axis=0).astype(float)
    k = max(2, int(round(length * 0.30)))
    head_first = cols[:k].sum() > cols[-k:].sum()           # head = the end with more pixels
    # axis coordinate along the travel direction (head = +)
    ax = (length - 1 - xs) if head_first else xs
    if head_first:
        ax = (length - 1) - xs
    lat = ys.astype(float)
    shaft = lat[(ax >= 0.35 * length) & (ax <= 0.65 * length)]
    shaft_lat = float(np.median(shaft)) if shaft.size else float(np.median(lat))
    head = lat[ax >= 0.70 * length]
    off_img = float(head.mean() - shaft_lat) if head.size else 0.0      # + = towards larger row/col index
    # travel vector in IMAGE coordinates
    if horizontal:
        t = (-1, 0) if head_first else (1, 0)
        lat_dir = (0, 1)                                    # lat index = image y
    else:
        t = (0, -1) if head_first else (0, 1)
        lat_dir = (1, 0)                                    # lat index = image x (A = comp.T)
    left = LEFT_OF(t)
    off_left = off_img * (lat_dir[0] * left[0] + lat_dir[1] * left[1])  # + = to the driver's left
    width = int(A.shape[0])
    return dict(travel=t, off_left=off_left, length=length, width=width)


def read_layout_notes(path, arm_dirs=None, save_check=True):
    path = Path(path)
    img = cv2.imread(str(path))
    notes = []
    if img is None:
        return {}, [f"{path.name}: picture could not be opened."]
    best = (len(_detectors(img)), 1.0)
    # Try a wider set of scales because Layout screenshots are exported at
    # different resolutions across the junction set.
    for sc in (0.4, 0.5, 2 / 3, 0.75, 0.85, 1.15, 4 / 3, 1.5, 1.75, 2.0, 2.5, 3.0):
        big = cv2.resize(img, None, fx=sc, fy=sc,
                         interpolation=cv2.INTER_CUBIC if sc > 1 else cv2.INTER_AREA)
        cnt = len(_detectors(big))
        if cnt > best[0]:
            best = (cnt, sc)
    if best[1] != 1.0:
        sc = best[1]
        img = cv2.resize(img, None, fx=sc, fy=sc, interpolation=cv2.INTER_CUBIC if sc > 1 else cv2.INTER_AREA)
        notes.append(f"Picture resized x{sc:.2f} to read it.")
    dets = _detectors(img)
    if len(dets) < 4:
        return {}, [f"{path.name}: no stop-line detector boxes found - this does not look like a UTC lane layout "
                    f"(or the picture is too small). Lanes NOT read."]
    m = _mask(img)
    n, lab, st, cen = cv2.connectedComponentsWithStats(m, connectivity=8)
    arrows = []
    scale = best[1]
    min_arrow_area = max(28, int(round(85 * scale * scale)))
    for i in range(1, n):
        x, y, w, h, a = st[i]
        if a < min_arrow_area or max(w, h) / max(1, min(w, h)) < 1.55 or a / (w * h) > 0.68:
            continue                                                        # letters, bars, blobs
        comp = (lab[y:y + h, x:x + w] == i)
        c = _classify(comp)
        cx, cy = x + w / 2.0, y + h / 2.0
        horizontal = w > h
        # must be in line with a detector box (keeps the stage panels out)
        if horizontal:
            ok = any(abs(dy - cy) <= 9 and abs(dx - cx) <= 200 for dx, dy in dets)
        else:
            ok = any(abs(dx - cx) <= 9 and abs(dy - cy) <= 200 for dx, dy in dets)
        if ok:
            arrows.append(dict(c, x=x, y=y, w=w, h=h, cx=cx, cy=cy))
    if not arrows:
        return {}, [f"{path.name}: detector boxes found but no lane arrows - lanes NOT read."]

    # straight-arrow reference width (for spotting branched / shared arrows)
    straight_w = [a["width"] for a in arrows if abs(a["off_left"]) < 2.5]
    ref_w = float(np.median(straight_w)) if straight_w else None
    for a in arrows:
        if abs(a["off_left"]) >= 3.0:
            a["kind"] = "left" if a["off_left"] > 0 else "right"
        elif ref_w and a["width"] > ref_w * 1.45:
            a["kind"] = "shared"
        else:
            a["kind"] = "through"

    # arm + one arrow per lane (same arm, same lane position within 6 px = repeated arrow)
    arms, seen = {}, set()
    for a in sorted(arrows, key=lambda a: (a["cy"], a["cx"])):
        arm = TRAVEL_TO_ARM.get(a["travel"])
        pos = round((a["cx"] if a["travel"][0] == 0 else a["cy"]) / 6)
        key = (arm, pos)
        if key in seen:
            continue
        seen.add(key)
        a["arm"] = arm
        d = arms.setdefault(arm, {"left": 0, "through": 0, "right": 0, "left_shared": 0, "right_shared": 0,
                                  "_shared": 0})
        if a["kind"] == "shared":
            d["_shared"] += 1
        else:
            d[a["kind"]] += 1
    for arm, d in arms.items():
        if d.pop("_shared"):
            notes.append(f"Arm {arm}: an arrow with a branch (shared lane?) was found - NOT counted, "
                         f"check the picture.")
    if arm_dirs:
        want = {str(x).upper() for x in arm_dirs}
        missing = sorted(want - set(arms))
        extra = sorted(set(arms) - want)
        if missing:
            notes.append(f"CUBE arms with no arrows in the picture: {', '.join(missing)} "
                         f"(is picture 'up' = North? check).")
        if extra:
            notes.append(f"Picture has arrows for arms not in CUBE: {', '.join(extra)}.")
    # check picture
    if save_check:
        try:
            CHECK_DIR.mkdir(parents=True, exist_ok=True)
            vis = img.copy()
            lab_txt = {"left": "L", "right": "R", "through": "T", "shared": "?"}
            for a in arrows:
                col = (255, 0, 255)
                cv2.rectangle(vis, (a["x"] - 3, a["y"] - 3), (a["x"] + a["w"] + 3, a["y"] + a["h"] + 3), col, 2)
                cv2.putText(vis, f"{a.get('arm')}-{lab_txt[a['kind']]}", (a["x"] - 4, a["y"] - 7),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, col, 2)
            cv2.imwrite(str(CHECK_DIR / f"{path.stem}_check.png"), vis)
            notes.append(f"Check picture saved: cache\\layout_check\\{path.stem}_check.png")
        except Exception:
            pass
    return arms, notes


def read_layout(path, arm_dirs=None):
    """simple form used by layout_source (arms only)"""
    return read_layout_notes(path, arm_dirs)[0]


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        arms, notes = read_layout_notes(p)
        print(p)
        for k in sorted(arms):
            print("  ", k, arms[k])
        for n_ in notes:
            print("  note:", n_)
