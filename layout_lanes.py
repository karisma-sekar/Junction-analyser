"""
layout_lanes.py  -  read LANE COUNTS from the clear stage-layout images
----------------------------------------------------------------------
Images:  Junction_API\\data\\stage_images\\<any sub-folder>\\IP401.jpg  (also .png/.bmp/.tif)
Matched to the junction by file name:  IP401, IP 401, IP-401 -> IP401.

What it reads (UTC layout picture, dark background):
  * red / green group letters next to the approach arrows (A, B, C ...)
  * red / green lane arrows on each approach
  * lanes for a group = number of DIFFERENT lane positions of the arrows
    that belong to that letter (two arrows in the same lane count once)
Panels (menu, logo, "UTC PLAN STAGES", "UTC LIVE STAGES") and the green
vehicle bars are ignored.

What it changes:
  * Auto-fill (single junction + batch): left_lanes / through_lanes are taken
    from the image for the arm's left_group / through_group (stages and the
    left/through letters still come from the Excel workbook, as before).
  * Generate TXT: if the saved config differs from the image, a CHECK warning
    is added (the saved config is NOT changed).
Nothing else (signal timings, NRN slip lanes, exit lanes, TXT format) changes.

Check pictures (what was read):  Junction_API\\cache\\stage_image_check\\<IP>.png
Summary of all reads:            Junction_API\\cache\\stage_image_lanes.csv

Use: in main.py add   import layout_lanes   directly below   import junction_engine as je
"""
import csv
import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy import ndimage

import junction_engine as je

IMG_DIR = je.DATA / "stage_images"
CHECK_DIR = je.CACHE / "stage_image_check"
SUMMARY = je.CACHE / "stage_image_lanes.csv"
EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"}
REF_W = 1367.0                      # width of the sample IP401.jpg - sizes scale from this
FONTS = ["C:/Windows/Fonts/timesbd.ttf", "C:/Windows/Fonts/times.ttf",
         "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/arial.ttf",
         "/usr/share/fonts/liberation/LiberationSerif-Bold.ttf",
         "/usr/share/fonts/liberation/LiberationSerif-Regular.ttf",
         "/usr/share/fonts/liberation/LiberationSans-Bold.ttf"]
MIN_SCORE = 0.42

_index, _reads, _templ = None, {}, {}


# ---------------------------------------------------------------- find the image
def find_images(ip):
    global _index
    if _index is None:
        _index = {}
        if IMG_DIR.exists():
            for p in sorted(IMG_DIR.rglob("*")):
                if p.is_file() and p.suffix.lower() in EXTS:
                    _index.setdefault(je.norm_ip(p.stem), []).append(p)
    return _index.get(je.norm_ip(ip), [])


# ---------------------------------------------------------------- letters
def _fonts():
    return [f for f in FONTS if Path(f).exists()]


def _templates(h):
    if h in _templ:
        return _templ[h]
    res = []
    for fp in _fonts():
        for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            size, v = h * 1.4, None
            for _ in range(4):
                font = ImageFont.truetype(fp, max(4, int(round(size))))
                img = Image.new("L", (6 * h + 10, 4 * h + 10), 0)
                ImageDraw.Draw(img).text((5, 5), ch, fill=255, font=font)
                v = np.asarray(img) > 100
                ys, xs = np.where(v)
                if not len(ys):
                    break
                th = ys.max() - ys.min() + 1
                if th == h:
                    break
                size *= h / th
            ys, xs = np.where(v)
            if len(ys):
                res.append((ch, v[ys.min():ys.max() + 1, xs.min():xs.max() + 1]))
    _templ[h] = res
    return res


def _classify(m, allowed):
    ys, xs = np.where(m)
    if not len(ys):
        return None, 0.0
    g = m[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    h, w = g.shape
    best = {}
    for ch, t in _templates(h):
        if allowed and ch not in allowed:
            continue
        th, tw = t.shape
        if abs(tw - w) > max(3, 0.5 * w):
            continue
        Wd = max(w, tw) + 2
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                A = np.zeros((h + 2, Wd), bool); Bm = np.zeros((h + 2, Wd), bool)
                A[1:1 + h, 1:1 + w] = g
                oy, ox = 1 + dy, 1 + dx + (w - tw) // 2
                if ox < 0 or oy < 0 or ox + tw > Wd or oy + th > h + 2:
                    continue
                Bm[oy:oy + th, ox:ox + tw] = t
                u = (A | Bm).sum()
                sc = (A & Bm).sum() / u if u else 0
                if sc > best.get(ch, 0):
                    best[ch] = float(sc)
    if not best:
        return None, 0.0
    ch = max(best, key=best.get)
    return ch, best[ch]


# ---------------------------------------------------------------- image parts
def _panels(light, W, H):
    """rectangular cream/white frames (menu, logo, stage panels) -> boxes to ignore"""
    lab, _ = ndimage.label(light, np.ones((3, 3)))
    boxes = []
    for i, sl in enumerate(ndimage.find_objects(lab), 1):
        if sl is None:
            continue
        ys, xs = sl
        h, w = ys.stop - ys.start, xs.stop - xs.start
        if w < 0.08 * W or h < 0.06 * H or w * h > 0.5 * W * H:
            continue
        m = lab[sl] == i
        b = max(2, int(0.012 * min(W, H)))
        sides = [m[:b, :].any(0).mean(), m[-b:, :].any(0).mean(),
                 m[:, :b].any(1).mean(), m[:, -b:].any(1).mean()]
        if min(sides) > 0.8:
            boxes.append((xs.start, ys.start, xs.stop, ys.stop))
    return boxes


def _components(mask):
    closed = ndimage.binary_closing(mask, np.ones((3, 3)))
    lab, _ = ndimage.label(closed, np.ones((3, 3)))
    out = []
    for i, sl in enumerate(ndimage.find_objects(lab), 1):
        if sl is None:
            continue
        ys, xs = sl
        sub = lab[sl] == i
        raw = mask[sl] & sub
        h, w = sub.shape
        out.append({"x0": xs.start, "y0": ys.start, "x1": xs.stop, "y1": ys.stop, "w": w, "h": h,
                    "px": int(sub.sum()), "fill": float(sub.sum()) / (w * h),
                    "cx": (xs.start + xs.stop) / 2, "cy": (ys.start + ys.stop) / 2,
                    "mask": sub, "raw": raw})
    return out


# ---------------------------------------------------------------- read one image
def read_image(path, allowed=None):
    """-> {"lanes": {letter: n}, "letters": [...], "arrows": [...], "notes": [...]}"""
    path = Path(path)
    im = Image.open(path).convert("RGB")
    a = np.asarray(im).astype(int)
    H, W = a.shape[:2]
    s = W / REF_W
    R, G, B = a[..., 0], a[..., 1], a[..., 2]
    red = (R > 100) & (R > 1.9 * G) & (R > 1.9 * B)            # incl. anti-aliased edges
    green = (G > 100) & (G > 1.6 * R) & (G > 1.4 * B)
    light = (R > 185) & (G > 175) & (B > 130)
    panels = _panels(light, W, H)

    def in_panel(c):
        return any(x0 <= c["cx"] <= x1 and y0 <= c["cy"] <= y1 for x0, y0, x1, y1 in panels)

    letters, arrows, notes = [], [], []
    allowed = set(allowed) if allowed else None
    for colour, mask in (("red", red), ("green", green)):
        for c in _components(mask):
            if c["px"] < 12 * s * s or in_panel(c):
                continue
            big = max(c["w"], c["h"])
            if c["fill"] > 0.8 and big > 6 * s:              # vehicle bar / bar end
                continue
            if 7 * s <= c["h"] <= 30 * s and c["w"] <= 30 * s and big < 28 * s:
                ch, sc = _classify(c["raw"], allowed)
                if ch and sc >= MIN_SCORE:
                    letters.append(dict(c, ch=ch, score=round(sc, 2), colour=colour))
            elif big >= 28 * s and c["fill"] < 0.6:
                arrows.append(dict(c, colour=colour))

    if not letters:
        notes.append("No group letters read from the image.")
    if not arrows:
        notes.append("No lane arrows found in the image.")

    # orientation + lane position of every arrow
    for ar in arrows:
        if ar["h"] >= 1.4 * ar["w"]:
            ar["ori"] = "v"
        elif ar["w"] >= 1.4 * ar["h"]:
            ar["ori"] = "h"
        else:
            ar["ori"] = None                                  # turning arrow
    straight = [x for x in arrows if x["ori"]]
    for ar in arrows:
        if ar["ori"] is None:
            near = min(straight, key=lambda x: math.hypot(x["cx"] - ar["cx"], x["cy"] - ar["cy"]),
                       default=None)
            ar["ori"] = near["ori"] if near else "v"
        m = ar["mask"]
        if ar["ori"] == "v":
            ar["lane_pos"] = ar["x0"] + int(np.argmax(m.sum(0)))
        else:
            ar["lane_pos"] = ar["y0"] + int(np.argmax(m.sum(1)))

    # each arrow belongs to the nearest letter (distance letter centre -> arrow pixels)
    for ar in arrows:
        ys, xs = np.where(ar["mask"])
        ys, xs = ys + ar["y0"], xs + ar["x0"]
        best = None
        for L in letters:
            d = float(np.min(np.hypot(xs - L["cx"], ys - L["cy"])))
            if best is None or d < best[0]:
                best = (d, L)
        if best and best[0] <= 120 * s:
            ar["letter"] = best[1]["ch"]
        else:
            ar["letter"] = None
    lanes = {}
    tol = 12 * s
    for L in {l["ch"] for l in letters}:
        pos = sorted({(x["ori"], x["lane_pos"]) for x in arrows if x["letter"] == L})
        uniq = []
        for o, p in pos:
            if not any(o == o2 and abs(p - p2) <= tol for o2, p2 in uniq):
                uniq.append((o, p))
        if uniq:
            lanes[L] = len(uniq)
    dup = [ch for ch in {l["ch"] for l in letters} if sum(1 for l in letters if l["ch"] == ch) > 1]
    if dup:
        notes.append(f"Letter(s) {sorted(dup)} found more than once in the image - check.")
    lost = [x for x in arrows if not x["letter"]]
    if lost:
        notes.append(f"{len(lost)} arrow(s) not next to any letter - not counted.")
    return {"file": str(path), "size": [W, H], "lanes": lanes, "letters": letters, "arrows": arrows,
            "panels": panels, "notes": notes}


def save_check_picture(r, out):
    im = Image.open(r["file"]).convert("RGB")
    d = ImageDraw.Draw(im)
    for x0, y0, x1, y1 in r["panels"]:
        d.rectangle([x0, y0, x1, y1], outline=(120, 120, 120), width=2)
    for ar in r["arrows"]:
        col = (0, 255, 255) if ar["letter"] else (255, 0, 255)
        d.rectangle([ar["x0"] - 2, ar["y0"] - 2, ar["x1"] + 2, ar["y1"] + 2], outline=col, width=2)
        d.text((ar["x1"] + 3, ar["y0"]), ar["letter"] or "?", fill=col)
    for L in r["letters"]:
        d.rectangle([L["x0"] - 3, L["y0"] - 3, L["x1"] + 3, L["y1"] + 3], outline=(255, 255, 0), width=2)
        d.text((L["x1"] + 4, L["y0"] - 12), f"{L['ch']}={r['lanes'].get(L['ch'], 0)}", fill=(255, 255, 0))
    out.parent.mkdir(parents=True, exist_ok=True)
    im.save(out)


def lanes_for(ip, allowed=None):
    """cached read for one junction -> dict or None (no image)"""
    ip = je.norm_ip(ip)
    files = find_images(ip)
    if not files:
        return None
    p = files[0]
    key = (ip, str(p), p.stat().st_mtime, tuple(sorted(allowed or [])))
    if key in _reads:
        return _reads[key]
    try:
        r = read_image(p, allowed)
        if len(files) > 1:
            r["notes"].append(f"{len(files)} images named {ip} - used {p.name} "
                              f"({', '.join(str(f.parent.name) for f in files)})")
        save_check_picture(r, CHECK_DIR / f"{ip}.png")
    except Exception as exc:
        r = {"file": str(p), "lanes": {}, "notes": [f"Image could not be read: {exc}"]}
    _reads[key] = r
    _write_summary()
    return r


def _write_summary():
    latest = {}
    for (ip, *_), r in _reads.items():
        latest[ip] = r
    try:
        SUMMARY.parent.mkdir(parents=True, exist_ok=True)
        with open(SUMMARY, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["IP", "Image", "Lanes per group (from image)", "Notes"])
            for ip, r in sorted(latest.items()):
                w.writerow([ip, Path(r["file"]).name,
                            ", ".join(f"{k}={v}" for k, v in sorted(r["lanes"].items())),
                            " | ".join(r["notes"])])
    except PermissionError:
        pass                                          # CSV open in Excel - skip this time


def _allowed(store, ip, cfg=None):
    sheet = store.signals.get(je.norm_ip(ip)) or {}
    letters = set(sheet.get("group_order") or [])
    for arm in ((cfg or {}).get("arms") or {}).values():
        for k in ("left_group", "through_group"):
            if arm.get(k):
                letters.add(str(arm[k]).upper())
    return letters or None


# ---------------------------------------------------------------- hooks into the app
_orig_suggest = je.suggested_config
_orig_build = je.build_junction


def suggested_config(store, ip_text):
    res, skel = _orig_suggest(store, ip_text)
    if skel is None:
        return res, skel
    ip = je.norm_ip(ip_text)
    r = lanes_for(ip, _allowed(store, ip, skel))
    notes = skel.setdefault("_picture_notes", [])
    if r is None:
        notes.append(f"No stage image for {ip} in data\\stage_images - lanes from the Excel picture.")
        return res, skel
    img = Path(r["file"]).name
    notes += [f"Stage image {img}: {n}" for n in r["notes"]]
    used = False
    for d, arm in (skel.get("arms") or {}).items():
        lg, tg = arm.get("left_group"), arm.get("through_group")
        if lg and tg and lg == tg:
            notes.append(f"Arm {d}: one group {lg} for Left and Through - image shows "
                         f"{r['lanes'].get(lg, '?')} lanes in total; split them by hand.")
            continue
        for mv, g in (("left", lg), ("through", tg)):
            if not g:
                continue
            n = r["lanes"].get(str(g).upper())
            if n:
                old = arm.get(f"{mv}_lanes")
                arm[f"{mv}_lanes"] = n
                used = True
                if old is not None and old != n:
                    notes.append(f"Arm {d}: {mv} lanes {old} (Excel picture) -> {n} (stage image {img}).")
            else:
                notes.append(f"Arm {d}: group {g} ({mv}) not found in stage image {img} - "
                             f"Excel picture value kept.")
    if used:
        skel["lanes_source"] = "stage image"
        skel["_stage_image"] = img
    return res, skel


def build_junction(store, ip_text, period):
    res, txt = _orig_build(store, ip_text, period)
    try:
        ip = je.norm_ip(ip_text)
        cfg = je.load_config(ip) or {}
        r = lanes_for(ip, _allowed(store, ip, cfg)) if cfg else None
        if r and r.get("lanes"):
            warn = res.setdefault("report", {}).setdefault("warnings", [])
            for d, arm in (cfg.get("arms") or {}).items():
                for mv in ("left", "through"):
                    g = arm.get(f"{mv}_group")
                    n = r["lanes"].get(str(g).upper()) if g else None
                    if g and n and arm.get(f"{mv}_lanes") not in (None, n) \
                            and arm.get("left_group") != arm.get("through_group"):
                        warn.append(f"Arm {d}: config {mv}_lanes = {arm.get(f'{mv}_lanes')} but stage image "
                                    f"{Path(r['file']).name} shows {n} lanes for group {g}.")
    except Exception:
        pass
    return res, txt


je.suggested_config = suggested_config
je.build_junction = build_junction
find_images("")
print(f"Stage images: {len(_index)} junction images found in {IMG_DIR}")
