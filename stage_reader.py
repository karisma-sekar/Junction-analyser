"""
stage_reader.py  -  reads the stage picture of every sheet in
"Signal Phasing plan.xlsx" and turns it into junction-config values.

What is read from the picture (all results carry a confidence + notes):
  * stages          : which signal-group letters (blue) are in each stage box
  * arm letters     : which blue letters sit on which approach of the drawing
  * left / through  : the letter nearest the median = Left, the outer one = Through
  * lanes           : NOT read (they come from the Layout folder)  [STAGE-ONLY]
  * slip lane hint  : NOT read (slip lanes come from NRN FORMOFWAY 9)
  * north arrow     : green arrow, used to match picture arms to CUBE arms

Nothing is invented: when the picture cannot be read, the value stays null
and a note explains why. The engineer confirms in the app.
"""
import io
import math
import re
import struct
import zipfile
from pathlib import Path

import numpy as np

S = 1.0          # picture scale factor (set per picture in read_picture)
from PIL import Image

try:
    from scipy import ndimage
except ImportError:
    ndimage = None


# ---------------------------------------------------------------------------
# 1. pull the pictures out of the workbook (PNG / JPEG / EMF)
# ---------------------------------------------------------------------------
def _emf_bitmap(data):
    """EMF files in the workbook hold one embedded bitmap (EMR_STRETCHDIBITS)."""
    i, best = 0, None
    while i + 8 <= len(data):
        t, s = struct.unpack_from("<II", data, i)
        if t == 81:
            ob, cb, obits, cbits = struct.unpack_from("<IIII", data, i + 48)
            bmi = data[i + ob:i + ob + cb]
            bits = data[i + obits:i + obits + cbits]
            hdr = b"BM" + struct.pack("<IHHI", 14 + len(bmi) + len(bits), 0, 0, 14 + len(bmi))
            try:
                im = Image.open(io.BytesIO(hdr + bmi + bits)); im.load()
                if best is None or im.size[0] * im.size[1] > best.size[0] * best.size[1]:
                    best = im
            except Exception:
                pass
        if s == 0:
            break
        i += s
    return best


def extract_sheet_pictures(xlsx_path, out_dir):
    """Largest picture of each sheet -> out_dir/<sheet>.png ; returns {sheet: png path}"""
    out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    z = zipfile.ZipFile(xlsx_path)
    rd = lambda n: z.read(n).decode("utf-8", "ignore")
    names = set(z.namelist())
    wb = rd("xl/workbook.xml")
    rels = {}
    for m in re.finditer(r"<Relationship\b([^>]*)/?>", rd("xl/_rels/workbook.xml.rels")):
        a = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
        rels[a.get("Id")] = a.get("Target")
    result = {}
    for m in re.finditer(r"<sheet\b([^>]*)/?>", wb):
        a = dict(re.findall(r'([\w:]+)="([^"]*)"', m.group(1)))
        name, target = a.get("name"), rels.get(a.get("r:id"), "")
        name = name.replace("&amp;", "&")
        ws = target.split("/")[-1]
        rp = f"xl/worksheets/_rels/{ws}.rels"
        if rp not in names:
            continue
        best = None
        for d in re.findall(r"drawings/(drawing\d+\.xml)", rd(rp)):
            drp = f"xl/drawings/_rels/{d}.rels"
            if drp not in names:
                continue
            for med in re.findall(r"media/([^\"]+)", rd(drp)):
                raw = z.read("xl/media/" + med)
                try:
                    im = _emf_bitmap(raw) if med.lower().endswith(("emf", "wmf")) else Image.open(io.BytesIO(raw))
                except Exception:
                    im = None
                if im is not None and (best is None or im.size[0] * im.size[1] > best.size[0] * best.size[1]):
                    best = im
        if best is not None and best.size[0] * best.size[1] > 200_000:
            fn = out_dir / (re.sub(r"[^\w]+", "_", name) + ".png")
            best.convert("RGB").save(fn)
            result[name] = str(fn)
    return result


# ---------------------------------------------------------------------------
# 2. colour masks (colours used in all the phasing drawings)
# ---------------------------------------------------------------------------
def masks(a):
    R, G, B = a[..., 0].astype(int), a[..., 1].astype(int), a[..., 2].astype(int)
    return {
        "letter_blue": (R < 115) & (G > 80) & (G < 175) & (B > 150) & (B - R > 75) & (B - G > 40),
        "frame_blue": ((R < 120) & (G < 150) & (B > 200) & (B - G > 90))
                      | ((R < 70) & (G > 55) & (G < 135) & (B > 95) & (B - R > 60) & (B - G > 15)),
        "any_blue": ((B > 140) & (B - R > 50) & (B - G > 25)) | ((B > 95) & (B - R > 60) & (B - G > 15) & (R < 90)),
        "green": (G > 120) & (R < 90) & (B < 140) & (G - B > 40),
        "red": (R > 180) & (G < 90) & (B < 90),
        "black": (R < 70) & (G < 70) & (B < 70),
        "detector": (abs(R - 192) < 25) & (abs(G - 208) < 22) & (abs(B - 228) < 22),
    }


def components(mask, min_px=4):
    lab, n = ndimage.label(mask, structure=np.ones((3, 3)))
    out = []
    for i, sl in enumerate(ndimage.find_objects(lab), 1):
        if sl is None:
            continue
        ys, xs = sl
        px = int((lab[sl] == i).sum())
        if px >= min_px:
            out.append({"y0": ys.start, "y1": ys.stop, "x0": xs.start, "x1": xs.stop, "px": px,
                        "cx": (xs.start + xs.stop) / 2, "cy": (ys.start + ys.stop) / 2,
                        "h": ys.stop - ys.start, "w": xs.stop - xs.start})
    return out


_TEMPLATES = {}


def _norm(mask, size=24):
    ys, xs = np.where(mask)
    if len(ys) == 0:
        return None
    crop = mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    h, w = crop.shape
    s = max(h, w)
    sq = np.zeros((s, s), bool)
    sq[(s - h) // 2:(s - h) // 2 + h, (s - w) // 2:(s - w) // 2 + w] = crop
    im = Image.fromarray((sq * 255).astype("uint8")).resize((size, size), Image.BILINEAR)
    v = np.asarray(im, float) / 255.0
    return v, w / h


def _templates():
    if _TEMPLATES:
        return _TEMPLATES
    from PIL import ImageDraw, ImageFont
    fonts = []
    for f in ["arialbd.ttf", "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/arial.ttf",
              "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
              "/usr/share/fonts/liberation/LiberationSans-Regular.ttf",
              "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf"]:
        try:
            fonts.append(ImageFont.truetype(f, 64))
        except Exception:
            pass
    if not fonts:
        fonts = [ImageFont.load_default()]
    for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789":
        for font in fonts:
            img = Image.new("L", (100, 100), 0)
            ImageDraw.Draw(img).text((15, 5), ch, fill=255, font=font)
            n = _norm(np.asarray(img) > 128)
            if n:
                _TEMPLATES.setdefault(ch, []).append(n)
    return _TEMPLATES


_SIZED = {}


def _font_list():
    from PIL import ImageFont
    out = []
    for f in ["C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/arial.ttf",
              "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
              "/usr/share/fonts/liberation/LiberationSans-Regular.ttf"]:
        if Path(f).exists():
            out.append(f)
    return out


def _sized_templates(h):
    """letters rendered so their cap height = h pixels (matches low-res drawings)"""
    if h in _SIZED:
        return _SIZED[h]
    from PIL import ImageDraw, ImageFont
    res = []
    for fp in _font_list():
        for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789":
            size = h * 1.38
            for _ in range(3):
                font = ImageFont.truetype(fp, max(4, int(round(size))))
                img = Image.new("L", (6 * h + 10, 4 * h + 10), 0)
                ImageDraw.Draw(img).text((5, 5), ch, fill=255, font=font)
                v = np.asarray(img) > 100
                ys, xs = np.where(v)
                if len(ys) == 0:
                    break
                th = ys.max() - ys.min() + 1
                if th == h:
                    break
                size *= h / th
            ys, xs = np.where(v)
            if len(ys):
                res.append((ch, v[ys.min():ys.max() + 1, xs.min():xs.max() + 1]))
    _SIZED[h] = res
    return res


ALLOWED = None   # letters allowed for this sheet (signal groups listed in the Excel table)


def classify_glyph(mask_crop, digits=False, rotations=False):
    """compare with letters rendered at the same pixel height -> (char, score 0..1)"""
    if rotations:
        best = classify_glyph(mask_crop, digits)
        for k in (1, 3):
            r = classify_glyph(np.rot90(mask_crop, k), digits)
            if r[1] > best[1] + 0.05:
                best = r
        return best
    r = classify_topk(mask_crop, digits)
    return r[0] if r else (None, 0)


def classify_topk(mask_crop, digits=False, k=4):
    """[(char, score), ...] best first"""
    ys, xs = np.where(mask_crop)
    if len(ys) == 0:
        return []
    g = mask_crop[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    h, w = g.shape
    per = {}
    for ch, t in _sized_templates(h):
        if ch.isdigit() != digits:
            continue
        if not digits and ALLOWED and ch not in ALLOWED:
            continue
        th, tw = t.shape
        if abs(tw - w) > max(3, w * 0.45):
            continue
        W = max(w, tw) + 2
        for dx in range(-1, 2):
            for dy in range(-1, 2):
                A = np.zeros((h + 2, W), bool); Bm = np.zeros((h + 2, W), bool)
                A[1:1 + h, 1:1 + w] = g
                oy, ox = 1 + dy, 1 + dx + (w - tw) // 2
                if ox < 0 or ox + tw > W or oy < 0 or oy + th > h + 2:
                    continue
                Bm[oy:oy + th, ox:ox + tw] = t
                inter = (A & Bm).sum(); uni = (A | Bm).sum()
                sc = inter / uni if uni else 0
                if sc > per.get(ch, -1):
                    per[ch] = float(sc)
    return sorted(per.items(), key=lambda x: -x[1])[:k]


MIN_SCORE = 0.45


def glyphs(mask, lo=None, hi=None):
    lo = lo or 7 * S; hi = hi or 40 * S
    """isolated letter-sized blobs (letters are drawn alone, not in words)"""
    comps = [c for c in components(mask, int(6 * S)) if lo <= c["h"] <= hi and 4 * S <= c["w"] <= hi]
    out = []
    for c in comps:
        near = [d for d in comps if d is not c and abs(d["cy"] - c["cy"]) < c["h"] * 0.6
                and 0 < abs(d["cx"] - c["cx"]) < c["h"] * 0.9]
        if near:              # part of a word (street name etc.)
            continue
        out.append(c)
    return out


def read_letters(mask, region, lo=None, hi=None, rotations=False, skip=None):
    """classify every isolated glyph of `mask` inside region (x0,y0,x1,y1)"""
    x0, y0, x1, y1 = region
    sub = mask[y0:y1, x0:x1]
    res = []
    for g in glyphs(sub, lo, hi):
        if g["px"] / (g["w"] * g["h"]) < 0.18:          # hollow box, not a letter
            continue
        gx, gy = g["cx"] + x0, g["cy"] + y0
        if skip and skip(gx, gy):
            continue
        crop = sub[g["y0"]:g["y1"], g["x0"]:g["x1"]]
        ch, sc = classify_glyph(crop, rotations=rotations)
        alts = [] if rotations else classify_topk(crop)
        if ch and sc >= MIN_SCORE:
            res.append({"ch": ch, "x": gx, "y": gy, "h": g["h"], "score": round(float(sc), 2),
                        "alts": [(c, round(v, 2)) for c, v in alts if v >= MIN_SCORE - 0.1]})
        else:
            res.append({"ch": None, "x": gx, "y": gy, "h": g["h"], "score": round(float(sc), 2)})
    return res


# ---------------------------------------------------------------------------
# 3. stage grid  (boxes 0,1,2,... under "Stream 0")
# ---------------------------------------------------------------------------
def _lines(mask, axis, min_frac):
    prof = mask.sum(axis=axis)
    L = mask.shape[axis]
    idx = np.where(prof > min_frac * L)[0]
    lines, start = [], None
    for k, v in enumerate(idx):
        if start is None:
            start = prev = v
        elif v - prev > 2:
            lines.append((start + prev) // 2); start = v
        prev = v
    if start is not None:
        lines.append((start + prev) // 2)
    return lines


def _boxed(m, g):
    """True if the glyph sits inside a small drawn box (stage number box)"""
    ab = m["any_blue"]; H, W = ab.shape
    pad_in, pad_out = int(2 * S), int(12 * S)
    sides = 0
    x0, y0, x1, y1 = g["x0"], g["y0"], g["x1"], g["y1"]
    strips = [ab[max(0, y0):y1, max(0, x0 - pad_out):max(0, x0 - pad_in)],      # left
              ab[max(0, y0):y1, min(W, x1 + pad_in):min(W, x1 + pad_out)],      # right
              ab[max(0, y0 - pad_out):max(0, y0 - pad_in), max(0, x0):x1],      # top
              ab[min(H, y1 + pad_in):min(H, y1 + pad_out), max(0, x0):x1]]      # bottom
    for st, axis in zip(strips, (0, 0, 1, 1)):
        if st.size and (st.mean(axis=axis) > 0.6).any():
            sides += 1
    return sides >= 3


def find_stage_cells(m):
    """locate the stage boxes by their numbers (digit inside a small box)"""
    H, W = m["letter_blue"].shape
    cand = []
    for g in glyphs(m["letter_blue"] | m["frame_blue"], 6 * S, 40 * S):
        if g["cy"] < H * 0.35 or not _boxed(m, g):
            continue
        crop = (m["letter_blue"] | m["frame_blue"])[g["y0"]:g["y1"], g["x0"]:g["x1"]]
        if g["w"] <= g["h"] * 0.45:
            ch, sc = "1", 1.0
        else:
            ch, sc = classify_glyph(crop, digits=True)
        if ch is not None and sc >= 0.4:
            cand.append(dict(g, n=int(ch)))
    # two-digit numbers (10, 11 ...)
    cand.sort(key=lambda c: (c["cy"], c["cx"]))
    merged = []
    for c in cand:
        if merged and abs(merged[-1]["cy"] - c["cy"]) < 4 * S and 0 <= c["x0"] - merged[-1]["x1"] < 6 * S:
            merged[-1]["n"] = merged[-1]["n"] * 10 + c["n"]; merged[-1]["x1"] = c["x1"]
        else:
            merged.append(c)
    if not merged:
        return []
    # rows
    rows = []
    for c in sorted(merged, key=lambda c: c["cy"]):
        if rows and abs(rows[-1][0]["cy"] - c["cy"]) < 10 * S:
            rows[-1].append(c)
        else:
            rows.append([c])
    widths = [b["x0"] - a["x0"] for r in rows for a, b in zip(sorted(r, key=lambda c: c["x0"]), sorted(r, key=lambda c: c["x0"])[1:])]
    cw = float(np.median(widths)) if widths else W / 4
    heights = [b[0]["y0"] - a[0]["y0"] for a, b in zip(rows, rows[1:])]
    chh = float(np.median(heights)) if heights else cw
    cells = []
    for ri, r in enumerate(rows):
        r.sort(key=lambda c: c["x0"])
        for ci, c in enumerate(r):
            x0 = int(c["x0"] - 14 * S); y0 = int(c["y0"] - 12 * S)
            x1 = int(r[ci + 1]["x0"] - 14 * S) if ci + 1 < len(r) else int(min(W, x0 + cw))
            y1 = int(rows[ri + 1][0]["y0"] - 12 * S) if ri + 1 < len(rows) else int(min(H, y0 + chh + 6 * S))
            cells.append({"box": (x0, y0, x1, y1), "number": c["n"],
                          "number_box": (c["x0"] - 3, c["y0"] - 3, c["x1"] + 3, c["y1"] + 3)})
    return cells


def read_stage_number(m, cell):
    """number in the small box at the top-left of the stage cell -> (number, bbox)"""
    x0, y0, x1, y1 = cell
    w, h = x1 - x0, y1 - y0
    rx1, ry1 = x0 + int(w * 0.25), y0 + int(h * 0.40)
    sub = m["letter_blue"][y0 + 2:ry1, x0 + 2:rx1]
    comps = [c for c in components(sub, 3) if 6 * S <= c["h"] <= 40 * S and 2 <= c["w"] <= 30 * S
             and c["h"] < sub.shape[0] * 0.9 and c["px"] / (c["w"] * c["h"]) >= 0.18]
    if not comps:
        return None, None
    top = min(c["y0"] for c in comps)
    row = sorted([c for c in comps if c["y0"] - top < 6 * S], key=lambda c: c["x0"])
    first = row[0]
    digits = [first] + [c for c in row[1:] if c["x0"] - first["x1"] < 6 * S and abs(c["h"] - first["h"]) < 3 * S]
    txt = ""
    for c in digits:
        ch, sc = classify_glyph(sub[c["y0"]:c["y1"], c["x0"]:c["x1"]], digits=True)
        if c["w"] <= c["h"] * 0.45:
            ch, sc = "1", 1.0                            # a plain bar is "1"
        if ch is None or sc < 0.35:
            return None, None
        txt += ch
    bx0 = x0 + 2 + min(c["x0"] for c in digits); bx1 = x0 + 2 + max(c["x1"] for c in digits)
    by0 = y0 + 2 + min(c["y0"] for c in digits); by1 = y0 + 2 + max(c["y1"] for c in digits)
    return int(txt), (bx0 - 2, by0 - 2, bx1 + 2, by1 + 2)



# ---------------------------------------------------------------------------
# 3b. STAGE LETTERS FROM GREEN ARROWS  (phase letters = blue letters tied to a green movement)
# ---------------------------------------------------------------------------
def _stage_arrows(m, cell):
    """Green movement ARROWS inside one stage box.
    Straight, solid, thin green bars are stop lines / references, not movements, so they are skipped.
    An arrow has a head / bend, so it fills only part of its bounding box."""
    x0, y0, x1, y1 = cell
    w, h = x1 - x0, y1 - y0
    g = m["green"][y0 + 2:y1 - 2, x0 + 2:x1 - 2]
    out = []
    for c in components(g, int(8 * S)):
        if max(c["w"], c["h"]) > 0.9 * max(w, h):
            continue                                              # frame / long line
        sub = g[c["y0"]:c["y1"], c["x0"]:c["x1"]]
        prof = sub.sum(axis=1) if c["h"] >= c["w"] else sub.sum(axis=0)       # width along the long axis
        prof = prof[prof > 0]
        straight = max(c["w"], c["h"]) >= 3 * min(c["w"], c["h"]) and prof.max() <= max(2.0, 1.7 * np.median(prof))
        if straight:
            continue                                              # stop line / reference bar (no arrow head)
        out.append(dict(c, x0=c["x0"] + x0 + 2, x1=c["x1"] + x0 + 2, y0=c["y0"] + y0 + 2, y1=c["y1"] + y0 + 2))
    return out
def _gap(a, b):
    dx = max(a["x0"] - b["x1"], b["x0"] - a["x1"], 0)
    dy = max(a["y0"] - b["y1"], b["y0"] - a["y1"], 0)
    return math.hypot(dx, dy)
def _strip_lines(mask, frac=0.5):
    """remove long straight runs (box borders) so letters touching a border stay separate"""
    out = mask.copy()
    H, W = out.shape
    rows = np.where(out.sum(axis=1) > frac * W)[0]
    cols = np.where(out.sum(axis=0) > frac * H)[0]
    for r in rows:
        out[max(0, r - 1):r + 2, :] = False
    for c in cols:
        out[:, max(0, c - 1):c + 2] = False
    return out
def _split_wide(c, mask):
    """two touching letters in one blob -> split at the weakest column"""
    if c["w"] < 1.5 * c["h"]:
        return [c]
    sub = mask[c["y0"]:c["y1"], c["x0"]:c["x1"]]
    prof = sub.sum(axis=0)
    lo, hi = int(c["w"] * 0.3), int(c["w"] * 0.7)
    if hi <= lo:
        return [c]
    cut = lo + int(np.argmin(prof[lo:hi]))
    parts = []
    for a, b in ((0, cut), (cut + 1, c["w"])):
        p = sub[:, a:b]
        ys, xs = np.where(p)
        if len(ys):
            parts.append({"x0": c["x0"] + a + xs.min(), "x1": c["x0"] + a + xs.max() + 1,
                          "y0": c["y0"] + ys.min(), "y1": c["y0"] + ys.max() + 1,
                          "px": int(p.sum()), "w": int(xs.max() - xs.min() + 1), "h": int(ys.max() - ys.min() + 1)})
    return parts or [c]
def stage_letters_from_arrows(m, cell, num_box):
    """PHASE LETTERS OF ONE STAGE BOX, read from its green arrows.
    1. find every green movement arrow in the box
    2. find every blue letter in the box (red / black text is never a phase letter)
    3. a letter is an active phase only if it sits on a green arrow (nearest-arrow gap <= LIMIT)
    4. every arrow is checked; an arrow with no letter is reported
    Returns (letters, notes, n_arrows) - letters use the same dict format as read_letters()."""
    x0, y0, x1, y1 = cell
    notes = []
    arrows = _stage_arrows(m, cell)
    if not arrows:
        return None, notes, 0
    # The cell box ends ABOVE the drawn stage border (it is cut 12 px above the next row's number box),
    # so letters printed at the bottom of a stage (like E F in IP58B stage 2) are cut in half and read wrongly.
    # Look a little further down so the whole letter is seen.
    Hpic = m["any_blue"].shape[0]
    y1e = min(Hpic, y1 + int(round(9 * S)))
    reg = (slice(y0 + 2, y1e), slice(x0 + 2, x1 - 2))
    loose = _strip_lines(m["any_blue"][reg] & ~m["green"][reg])
    loose_full = np.zeros_like(m["any_blue"])                    # same coordinates as the picture
    loose_full[reg] = loose
    cands = []
    for c in components(loose, int(8 * S)):
        for p in _split_wide(c, loose):
            if not (4.5 * S <= p["h"] <= 40 * S and 1.5 * S <= p["w"] <= 40 * S):
                continue
            if p["px"] / float(p["w"] * p["h"]) < 0.18:                 # hollow box, not a letter
                continue
            p = dict(p, x0=p["x0"] + x0 + 2, x1=p["x1"] + x0 + 2, y0=p["y0"] + y0 + 2, y1=p["y1"] + y0 + 2)
            gx, gy = (p["x0"] + p["x1"]) / 2, (p["y0"] + p["y1"]) / 2
            if num_box and num_box[0] - 3 <= gx <= num_box[2] + 3 and num_box[1] - 3 <= gy <= num_box[3] + 3:
                continue                                                # the stage number itself
            cands.append(p)
    limit = 14 * S
    lets, used = [], set()
    for p in cands:
        near = min(arrows, key=lambda a: _gap(p, a))
        if _gap(p, near) > limit:
            continue                                                    # blue text not on a green movement
        # classify on BOTH masks (anti-aliasing beside the green arrow can eat part of the letter)
        best = (None, 0.0, [])
        for mk in (loose_full, m["letter_blue"]):
            crop = mk[p["y0"]:p["y1"], p["x0"]:p["x1"]]
            if not crop.any():
                continue
            alts = classify_topk(crop)
            if alts and alts[0][1] > best[1]:
                best = (alts[0][0], alts[0][1], alts)
        ch, sc, alts = best
        gx, gy = (p["x0"] + p["x1"]) / 2, (p["y0"] + p["y1"]) / 2
        rec = {"ch": ch if ch and sc >= MIN_SCORE else None, "x": gx, "y": gy, "h": p["h"],
               "score": round(float(sc), 2),
               "alts": [(c2, round(v, 2)) for c2, v in alts if v >= MIN_SCORE - 0.1]}
        lets.append(rec)
        used.add(id(near))
    seen = {}
    for r in lets:
        if r["ch"]:
            seen[r["ch"]] = seen.get(r["ch"], 0) + 1
    dup = sorted(k for k, v in seen.items() if v > 1)
    if dup:
        notes.append(f"letter(s) {dup} read on more than one arrow - confirm (could be E/F, B/D, P/F confusion).")
    for a in arrows:                                                    # every green arrow must have a letter
        if id(a) not in used:
            notes.append(f"green arrow at ({int((a['x0'] + a['x1']) / 2)},{int((a['y0'] + a['y1']) / 2)}) "
                         f"has no phase letter next to it - check.")
    return lets, notes, len(arrows)
# ---------------------------------------------------------------------------
# 4. drawing (approaches, letters, detectors, north arrow, zebra islands)
# ---------------------------------------------------------------------------
def picture_bearing(dx, dy):
    """screen vector -> bearing, 0 = up, clockwise"""
    return (math.degrees(math.atan2(dx, -dy)) + 360) % 360


def ang_diff(a, b):
    d = abs(a - b) % 360
    return min(d, 360 - d)


def north_arrow(m, area):
    x0, y0, x1, y1 = area
    g = m["green"][y0:y1, x0:x1]
    comps = components(g, 10)
    if not comps:
        return None
    arrow = max(comps, key=lambda c: c["px"])
    letters = [c for c in comps if c is not arrow and c["px"] < arrow["px"] * 0.6
               and math.hypot(c["cx"] - arrow["cx"], c["cy"] - arrow["cy"]) < 4 * max(arrow["h"], arrow["w"])]
    if not letters:
        return None
    n = min(letters, key=lambda c: math.hypot(c["cx"] - arrow["cx"], c["cy"] - arrow["cy"]))
    return picture_bearing(n["cx"] - arrow["cx"], n["cy"] - arrow["cy"])


def junction_centre(m, area, ped_letters):
    if len(ped_letters) >= 3:
        xs = np.array([p["x"] for p in ped_letters]); ys = np.array([p["y"] for p in ped_letters])
        mx, my = np.median(xs), np.median(ys)
        dist = np.hypot(xs - mx, ys - my)
        keep = dist <= 1.8 * max(np.median(dist), 1)
        xs, ys = xs[keep], ys[keep]
        return (float((xs.min() + xs.max()) / 2), float((ys.min() + ys.max()) / 2))
    x0, y0, x1, y1 = area
    return ((x0 + x1) / 2, (y0 + y1) / 2)


def zebra_corners(m, area, centre):
    """bearings (picture) of zebra-hatched islands (slip lanes) around the junction"""
    x0, y0, x1, y1 = area
    blk = m["black"][y0:y1, x0:x1]
    comps = [c for c in components(blk, int(8 * S)) if 3 * S <= c["w"] <= 25 * S and 3 * S <= c["h"] <= 25 * S]
    # zebra = >=4 similar small black bars close together
    found = []
    used = set()
    for i, c in enumerate(comps):
        if i in used:
            continue
        cl = [j for j, d in enumerate(comps) if math.hypot(d["cx"] - c["cx"], d["cy"] - c["cy"]) < 25 * S]
        if len(cl) >= 4:
            used.update(cl)
            cx = np.mean([comps[j]["cx"] for j in cl]) + x0
            cy = np.mean([comps[j]["cy"] for j in cl]) + y0
            found.append({"x": cx, "y": cy,
                          "bearing": picture_bearing(cx - centre[0], cy - centre[1]),
                          "dist": math.hypot(cx - centre[0], cy - centre[1])})
    return found


# ---------------------------------------------------------------------------
# 5. main entry: read one picture
# ---------------------------------------------------------------------------
def read_picture(png_path, allowed_letters=None):
    """allowed_letters: signal-group letters of this sheet (improves reading)"""
    global ALLOWED
    ALLOWED = set(allowed_letters) if allowed_letters else None
    notes = []
    if ndimage is None:
        return {"ok": False, "notes": ["scipy not installed (pip install scipy)"]}
    if not _font_list():
        return {"ok": False, "notes": ["Arial font not found (C:/Windows/Fonts/arialbd.ttf) - letters cannot be read"]}
    global S
    im = Image.open(png_path).convert("RGB")
    if im.size[0] < 520:                         # low-resolution pictures: enlarge first
        f = 680 / im.size[0]
        im = im.resize((int(im.size[0] * f), int(im.size[1] * f)), Image.BICUBIC)
    a = np.array(im)
    m = masks(a)
    S = max(0.5, im.size[0] / 680)               # size factor vs a normal 680 px drawing
    H, W = m["black"].shape

    # ---- stage grid ----
    cells = find_stage_cells(m)
    if not cells:
        return {"ok": False, "notes": ["Stage boxes not found in picture."]}
    grid_top = min(c["box"][1] for c in cells) - int(25 * S)
    stages = {}
    cell_log = []
    gx0 = min(c["box"][0] for c in cells); gy0 = min(c["box"][1] for c in cells)
    all_lets = read_letters(m["letter_blue"], (0, max(0, gy0), W, H))
    for cdict in cells:
        cell, no, nb = cdict["box"], cdict["number"], cdict["number_box"]
        x0, y0, x1, y1 = cell
        lets = [l for l in all_lets if x0 <= l["x"] < x1 and y0 <= l["y"] < y1
                and not (nb[0] <= l["x"] <= nb[2] and nb[1] <= l["y"] <= nb[3])]
        # STAGE LETTERS: only blue letters that belong to a green movement arrow count as phases
        arrow_lets, arrow_notes, n_arrows = stage_letters_from_arrows(m, cell, nb) if no != 0 else (None, [], 0)
        if arrow_lets is not None:
            lets = arrow_lets
            notes += [f"Stage {no}: {n}" for n in arrow_notes]
        elif no != 0:
            lets = []
            notes.append(f"Stage {no}: no green movement arrow with a readable phase letter; stage left unread (no fallback OCR used).")
        cell_log.append({"box": [int(v) for v in cell], "number": no, "number_box": nb,
                         "letters": [{k: l.get(k) for k in ("ch", "x", "y", "score", "alts")} for l in lets]})
        bad = [l for l in lets if l["ch"] is None]
        lets = [l for l in lets if l["ch"]]
        if bad:
            notes.append(f"Stage {no}: {len(bad)} blue mark(s) could not be read as a letter.")
        low = [l["ch"] for l in lets if l["score"] < 0.55]
        if low:
            notes.append(f"Stage {no}: low-confidence letters {low} - confirm.")
        if no == 0:
            continue                             # all-red stage
        if lets:
            if str(no) in stages:
                notes.append(f"Stage number {no} read twice - check picture.")
            stages[str(no)] = sorted({l["ch"] for l in lets})

    # ---- drawing ----
    area = (0, 0, W, max(10, grid_top - 5))
    blue = [l for l in read_letters(m["letter_blue"], area, rotations=True) if l["ch"]]
    keep, ALLOWED = ALLOWED, None
    ped = [l for l in read_letters(m["red"], area, rotations=True) if l["ch"]]
    ALLOWED = keep
    centre = junction_centre(m, area, ped)
    narrow = north_arrow(m, area)
    if narrow is None:
        notes.append("North arrow not found - arm matching uses picture up = north.")

    # group blue letters into arms by bearing from centre
    for b in blue:
        b["pb"] = picture_bearing(b["x"] - centre[0], b["y"] - centre[1])
        b["dist"] = math.hypot(b["x"] - centre[0], b["y"] - centre[1])
    blue = [b for b in blue if b["dist"] > 10]
    arms = []
    for b in sorted(blue, key=lambda b: b["pb"]):
        for arm in arms:
            if ang_diff(arm["pb"], b["pb"]) < 40:
                arm["letters"].append(b)
                arm["pb"] = float(np.degrees(np.arctan2(
                    np.mean([np.sin(np.radians(x["pb"])) for x in arm["letters"]]),
                    np.mean([np.cos(np.radians(x["pb"])) for x in arm["letters"]]))) % 360)
                break
        else:
            arms.append({"pb": b["pb"], "letters": [b]})

    # detectors (stop line boxes)
    dets, zebras = [], []      # STAGE-ONLY: lanes = Layout folder, slip lanes = NRN FORMOFWAY 9

    return {"ok": True, "size": [W, H], "cells": cell_log,
            "drawing_letters": [{k: l[k] for k in ("ch", "x", "y", "score")} for l in blue], "centre": centre, "north_arrow_bearing": narrow,
            "stages": stages, "arms_picture": arms, "detectors": dets, "zebras": zebras,
            "ped_letters": sorted({p["ch"] for p in ped}), "notes": notes}


# ---------------------------------------------------------------------------
# 6. match the picture to the CUBE arms and build config values
# ---------------------------------------------------------------------------
def _axis_frame(pb):
    r = math.radians(pb)
    ux, uy = math.sin(r), -math.cos(r)           # outward along arm (screen)
    return ux, uy, -uy, ux                       # along, perpendicular (to the right looking outward)


def match_to_cube(pic, cube_arms):
    """cube_arms: [{'dir','bearing',...}] true bearings.  Returns {dir: picture arm}."""
    import itertools
    rot = pic.get("north_arrow_bearing") or 0.0
    parms = pic["arms_picture"]
    if not parms or not cube_arms:
        return {}, None
    best = None
    for perm in itertools.permutations(range(len(parms)), min(len(parms), len(cube_arms))):
        cost, pairs = 0, []
        for ci, pi in zip(range(len(cube_arms)), perm):
            d = ang_diff((parms[pi]["pb"] - rot) % 360, cube_arms[ci]["bearing"])
            cost += d; pairs.append((ci, pi, d))
        if best is None or cost < best[0]:
            best = (cost, pairs)
    out = {}
    for ci, pi, d in best[1]:
        out[cube_arms[ci]["dir"]] = dict(parms[pi], match_error_deg=round(d, 1))
    return out, round(best[0] / max(1, len(best[1])), 1)


def arm_movements(parm, detectors, centre):
    """STAGE-ONLY: letters of ONE picture arm -> left_group / through_group.   LETTERS ONLY.
    Lane numbers are NOT read from the drawing any more - they come from the Layout folder
    (layout_source.py).  'detectors' is accepted only so old callers keep working; it is ignored.
    Traffic drives on the right, so looking outward along an arm the inbound lanes are on the
    left; the letter nearest the median (largest 'lat') is the Left-turn group."""
    notes = []
    ux, uy, px, py = _axis_frame(parm["pb"])
    lets = []
    for l in parm["letters"]:
        dx, dy = l["x"] - centre[0], l["y"] - centre[1]
        lets.append(dict(l, along=dx * ux + dy * uy, lat=dx * px + dy * py))
    lets.sort(key=lambda l: -l["lat"])                     # median side first
    res = {"left_group": None, "through_group": None, "left_lanes": None, "through_lanes": None}
    if not lets:
        notes.append("No letters on this arm.")
    elif len(lets) == 1:
        res["left_group"] = res["through_group"] = lets[0]["ch"]
        notes.append(f"Only letter {lets[0]['ch']} on this arm: used for Left and Through - confirm.")
    else:
        res["left_group"], res["through_group"] = lets[0]["ch"], lets[1]["ch"]
        if len(lets) > 2:
            notes.append(f"Extra letters on this arm {[l['ch'] for l in lets[2:]]} not coded - check.")
    return res, notes


def suggest_from_picture(pic, cube_arms):
    """STAGE-ONLY: returns (partial config, notes) with STAGES and signal-group LETTERS per arm.
    No lane counts (Layout folder) and no slip-lane hint (NRN FORMOFWAY 9) are taken from the picture."""
    notes = list(pic.get("notes", []))
    cfg = {"stages": pic.get("stages") or {}, "arms": {}}
    matched, err = match_to_cube(pic, cube_arms)
    if err is not None and err > 35:
        notes.append(f"Picture arms vs CUBE arms differ by {err} deg on average - check arm matching.")
    if len(pic["arms_picture"]) != len(cube_arms):
        notes.append(f"Picture shows {len(pic['arms_picture'])} lettered arms, CUBE has {len(cube_arms)}.")
    for cube in cube_arms:
        parm = matched.get(cube["dir"])
        if parm is None:
            notes.append(f"Arm {cube['dir']}: no letters found in picture.")
            continue
        mv, n2 = arm_movements(parm, [], pic["centre"])
        a = dict(mv)
        a.pop("total_lanes", None)
        a["left_lanes"] = None                  # filled later from the Layout folder
        a["through_lanes"] = None
        a["right_lanes"] = None
        a["right_group"] = mv["through_group"]
        a["right_free"] = False                 # slip lane is decided later from NRN FORMOFWAY 9
        a["_picture_letters"] = [l["ch"] for l in parm["letters"]]
        a["_match_error_deg"] = parm["match_error_deg"]
        cfg["arms"][cube["dir"]] = a
        notes += [f"Arm {cube['dir']}: {n}" for n in n2]
    return cfg, notes


def annotate(png_path, pic, out_path):
    """picture with what the reader found drawn on top (shown in the app)"""
    from PIL import ImageDraw
    im = Image.open(png_path).convert("RGB")
    W, H = pic["size"]
    if im.size[0] != W:
        im = im.resize((W, H), Image.LANCZOS)
    d = ImageDraw.Draw(im)
    for c in pic.get("cells", []):
        x0, y0, x1, y1 = c["box"]
        d.rectangle([x0 + 1, y0 + 1, x1 - 1, y1 - 1], outline=(255, 0, 255), width=2)
        d.text((x0 + 30, y0 + 4), f"stage {c['number']}", fill=(255, 0, 255))
        for l in c["letters"]:
            col = (0, 160, 0) if l["ch"] and l["score"] >= 0.55 else (255, 140, 0) if l["ch"] else (255, 0, 0)
            d.rectangle([l["x"] - 8, l["y"] - 9, l["x"] + 8, l["y"] + 9], outline=col, width=2)
            d.text((l["x"] + 10, l["y"] - 6), l["ch"] or "?", fill=col)
    cx, cy = pic["centre"]
    d.ellipse([cx - 5, cy - 5, cx + 5, cy + 5], fill=(255, 0, 255))
    for arm in pic.get("arms_picture", []):
        r = math.radians(arm["pb"])
        d.line([cx, cy, cx + 160 * math.sin(r), cy - 160 * math.cos(r)], fill=(255, 0, 255), width=2)
        for l in arm["letters"]:
            d.rectangle([l["x"] - 9, l["y"] - 10, l["x"] + 9, l["y"] + 10], outline=(0, 160, 0), width=2)
            d.text((l["x"] + 11, l["y"] - 6), l["ch"], fill=(0, 160, 0))
    im.save(out_path)


def fix_stages_with_timing(pic, sheet, stage_window):
    """Every stage's groups must be green at the same time in the signal plan.
    If a stage fails, try the 2nd/3rd best reading of its letters. Returns notes."""
    import itertools
    notes = []
    if not sheet or not pic.get("cells"):
        return notes
    plans = [p for p in sheet["plans"].values() if p.get("cycle") and p.get("groups")]

    def consistent(mem):
        for p in plans:
            g = p["groups"]
            if all(x in g and g[x]["start"] is not None and g[x]["end"] is not None for x in mem):
                try:
                    if stage_window(g, mem, p["cycle"]):
                        return True
                except Exception:
                    pass
        return False

    for c in pic["cells"]:
        no = c["number"]
        if not no or str(no) not in pic["stages"]:
            continue
        lets = [l for l in c["letters"] if l["ch"]]
        mem = sorted({l["ch"] for l in lets})
        if consistent(mem):
            continue
        options = [[(l["ch"], l["score"])] + ([a for a in (l.get("alts") or []) if a[0] != l["ch"]][:2]
                                               if l["score"] < 0.6 else []) for l in lets]
        best = None
        for combo in itertools.product(*options):
            m2 = sorted({c2 for c2, _ in combo})
            changed = sum(1 for (c2, _), l in zip(combo, lets) if c2 != l["ch"])
            if changed == 1 and len(m2) == len(set(mem)) and consistent(m2):
                sc = sum(v for _, v in combo)
                if best is None or sc > best[0]:
                    best = (sc, m2)
        if best:
            notes.append(f"Stage {no}: picture read {mem}, corrected to {best[1]} (only combination that is "
                         f"green together in the signal plan) - confirm.")
            pic["stages"][str(no)] = best[1]
        else:
            notes.append(f"Stage {no}: groups {mem} are never green together in any plan - check picture.")
    return notes
