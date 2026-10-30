"""
layout_source.py  -  lane configuration from the LAYOUT folder   (v2: reads layout PICTURES itself)
---------------------------------------------------------------------------
Rule: lanes (left / through / right / shared / exclusive) come ONLY from the
junction layout files.  Stage drawings are used for stage letters / PHASES only.

Where are the layout files?
    default :  <Junction_API>/data/Layout/   (copy the "Latest PICT Layout" tree here)
    or set  :  set JW_LAYOUT_DIR=D:\\path\\to\\Latest PICT Layout   (before starting the app)

Matching: the IP code is taken from the file name (or, if it is not there, from a
parent folder name):  IP58B.JPG, IP-58B.png, "IP 58B Layout.jpg" -> IP58B.

Reading a layout file:
  * .xlsx / .csv / .json   -> lane table (columns: Arm, Left, Through, Right, optional shared columns)
  * .jpg / .png / .bmp ... -> layout_image.py  (reads the lane ARROWS of the UTC layout picture)
  * anything else (.pdf ..)-> your old layout_lanes.py  read_layout(path, arm_dirs)  if it has one
A lane table (.csv/.xlsx) next to a picture always wins - use it to override a wrongly read picture.

Nothing is assumed: missing layout -> lanes = None -> the config is flagged and the TXT is not
written until the engineer fills the value.
"""
import csv
import json
import os
import re
from pathlib import Path

BASE = Path(__file__).resolve().parent
LAYOUT_DIR = Path(os.environ.get("JW_LAYOUT_DIR") or (BASE / "data" / "Layout"))
LAYOUT_SRC = "Layout folder"
SLIP_SRC = "NRN FORMOFWAY 9"

TABLE_EXT = {".xlsx", ".xlsm", ".csv", ".json"}
PICTURE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".gif"}
OTHER_EXT = {".pdf", ".dwg", ".dxf"}

IP_RE = re.compile(r"IP[\s_\-]*0*(\d+)[\s_\-]?([A-Z]?)(?![A-Z0-9])", re.I)
DIRS = {"N": "N", "NORTH": "N", "NB": "N", "S": "S", "SOUTH": "S", "SB": "S",
        "E": "E", "EAST": "E", "EB": "E", "W": "W", "WEST": "W", "WB": "W",
        "NE": "NE", "SE": "SE", "SW": "SW", "NW": "NW"}

_index = None


# ------------------------------------------------------------------ locate files
def _ip_from_text(text):
    m = IP_RE.search(text or "")
    return f"IP{int(m.group(1))}{m.group(2).upper()}" if m else None


def _norm_ip(ip):
    return _ip_from_text(str(ip)) or str(ip or "").replace(" ", "").replace("-", "").upper()


def _rank(p):
    e = p.suffix.lower()
    # Lane counts are authoritative from the junction Layout IMAGE.
    return (0 if e in PICTURE_EXT else 1 if e in TABLE_EXT else 2, p.name.lower())


def build_index(force=False):
    """IP code -> [layout files]   (whole Layout folder, all sub-folders)"""
    global _index
    if _index is not None and not force:
        return _index
    idx = {}
    if LAYOUT_DIR.exists():
        for p in LAYOUT_DIR.rglob("*"):
            if not p.is_file() or p.suffix.lower() not in TABLE_EXT | PICTURE_EXT | OTHER_EXT:
                continue
            if p.name.startswith("~$"):
                continue
            ip = _ip_from_text(p.stem)
            if not ip:                                  # IP code only in a folder name
                for parent in p.relative_to(LAYOUT_DIR).parents:
                    ip = _ip_from_text(parent.name)
                    if ip:
                        break
            if ip:
                idx.setdefault(ip, []).append(p)
    for v in idx.values():
        v.sort(key=_rank)
    _index = idx
    return idx


def status():
    if not LAYOUT_DIR.exists():
        return [f"Layout folder not found: {LAYOUT_DIR}  (copy the layouts there or set JW_LAYOUT_DIR)"]
    n = sum(len(v) for v in build_index().values())
    return [f"Layout folder: {n} layout file(s) for {len(build_index())} junction(s) in {LAYOUT_DIR}"]


# ------------------------------------------------------------------ read a lane table
def _int(v):
    try:
        if v is None or str(v).strip() == "":
            return None
        return int(float(str(v).strip()))
    except ValueError:
        return None


def _rows_from(path):
    ext = path.suffix.lower()
    if ext in (".xlsx", ".xlsm"):
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        return [list(r) for r in wb.worksheets[0].iter_rows(values_only=True)]
    if ext == ".csv":
        with open(path, newline="", encoding="utf-8-sig", errors="ignore") as f:
            return list(csv.reader(f))
    data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))      # .json
    if isinstance(data, dict):
        data = [dict(arm=k, **v) for k, v in (data.get("arms") or data).items()]
    keys = list(dict.fromkeys(k for r in data for k in r))
    return [keys] + [[r.get(k) for k in keys] for r in data]


def _read_table(path):
    rows = _rows_from(path)
    hdr_i = next((i for i, r in enumerate(rows[:15])
                  if any(re.match(r"\s*(arm|direction|approach)\b", str(c or ""), re.I) for c in r)), None)
    if hdr_i is None:
        return {}, [f"{path.name}: no header row with 'Arm' / 'Direction' column."]
    hdr = [str(c or "").strip().lower() for c in rows[hdr_i]]

    def col(test):
        return next((i for i, h in enumerate(hdr) if h and test(h)), None)

    c_dir = col(lambda h: re.match(r"(arm|direction|approach)", h))
    c_l = col(lambda h: "left" in h and not any(x in h for x in ("through", "thru", "shared", "+")))
    c_t = col(lambda h: any(x in h for x in ("through", "thru")) and not any(x in h for x in ("left", "right", "shared", "+")))
    c_r = col(lambda h: "right" in h and not any(x in h for x in ("through", "thru", "shared", "+", "slip", "free")))
    c_lt = col(lambda h: "left" in h and any(x in h for x in ("through", "thru")))
    c_tr = col(lambda h: "right" in h and any(x in h for x in ("through", "thru")))
    out, notes = {}, []
    for r in rows[hdr_i + 1:]:
        d = DIRS.get(str(r[c_dir] or "").strip().upper()) if c_dir is not None and c_dir < len(r) else None
        if not d:
            continue
        g = lambda c: _int(r[c]) if c is not None and c < len(r) else None
        out[d] = {"left": g(c_l), "through": g(c_t), "right": g(c_r),
                  "left_shared": g(c_lt) or 0, "right_shared": g(c_tr) or 0}
    if not out:
        notes.append(f"{path.name}: no arm rows (N/S/E/W) found.")
    return out, notes


def _read_picture(path, arm_dirs):
    """UTC layout picture -> layout_image.py (lane arrows)"""
    try:
        import layout_image as li
    except Exception as exc:
        return {}, [f"{path.name}: layout_image.py could not be loaded ({exc}) - is opencv-python installed?"]
    try:
        arms, notes = li.read_layout_notes(path, arm_dirs)
    except Exception as exc:
        return {}, [f"{path.name}: picture reader failed ({exc}) - lanes not read."]
    return arms, [f"{path.name}: {n}" if not n.startswith(path.name) else n for n in notes]


def _read_other(path, arm_dirs):
    """PDF / CAD -> your old layout_lanes.py (only if it has read_layout)"""
    try:
        import layout_lanes as ll
    except Exception as exc:
        return {}, [f"{path.name}: layout_lanes.py could not be imported ({exc}) - lanes not read."]
    fn = getattr(ll, "read_layout", None)
    if fn is None:
        return {}, [f"{path.name}: layout_lanes.py has no read_layout(path, arm_dirs) - lanes not read."]
    try:
        res = fn(path, arm_dirs) or {}
    except Exception as exc:
        return {}, [f"{path.name}: layout reader failed ({exc}) - lanes not read."]
    return {DIRS.get(str(k).upper(), k): v for k, v in res.items()}, []


def read_layout(ip, arm_dirs):
    """Read lane counts only from the matching image under data/Layout.

    The search is recursive, so layouts can remain in area/sub-area folders.
    The junction ID is matched from the image filename (or parent folder via the
    existing index). Tables/PDFs are deliberately ignored for lane counts.
    """
    ip = _norm_ip(ip)
    out = {"ip": ip, "file": None, "arms": {}, "notes": []}

    # Rebuild each time so newly copied Layout images are available immediately
    # without restarting the application.
    files = [p for p in (build_index(force=True).get(ip) or [])
             if p.suffix.lower() in PICTURE_EXT]

    if not files:
        out["notes"].append(
            f"No Layout image for {ip} under {LAYOUT_DIR} - lane counts NOT set.")
        return out

    files.sort(key=lambda p: (0 if _ip_from_text(p.stem) == ip else 1,
                              len(p.relative_to(LAYOUT_DIR).parts),
                              p.name.lower()))
    if len(files) > 1:
        out["notes"].append(
            f"{len(files)} Layout images for {ip}; using the first readable exact junction image.")

    for image_path in files:
        arms, notes = _read_picture(image_path, arm_dirs)
        out["notes"] += notes
        if arms:
            out["file"] = str(image_path.relative_to(LAYOUT_DIR))
            out["arms"] = arms
            break

    if not out["arms"]:
        out["file"] = str(files[0].relative_to(LAYOUT_DIR))
    return out


# ------------------------------------------------------------------ write into the config
def apply_layout(cfg, ip):
    """Lane counts per arm come from the Layout folder only.  Anything the stage picture
    guessed for lanes is removed first, so it can never leak into the TXT."""
    arms = cfg.get("arms") or {}
    info = read_layout(ip, list(arms))
    notes = ["LAYOUT: " + n for n in info["notes"]]
    for d, arm in arms.items():
        arm.pop("_picture_total_lanes", None)
        arm.pop("total_lanes", None)
        L = info["arms"].get(d)
        if not L:
            arm["left_lanes"] = arm["through_lanes"] = None
            if not arm.get("right_free"):
                arm["right_lanes"] = None
            if info["arms"]:
                notes.append(f"LAYOUT: arm {d} not found in {info['file']} - lanes NOT set.")
            continue
        arm["left_lanes"] = L.get("left")
        arm["through_lanes"] = None if L.get("through") is None else L["through"] + (L.get("left_shared") or 0) \
            + (L.get("right_shared") or 0)
        arm["left_shared"] = L.get("left_shared") or 0
        arm["right_shared"] = L.get("right_shared") or 0
        if not arm.get("right_free"):                  # with a slip lane right_lanes = NRN slip lanes
            arm["right_lanes"] = L.get("right")
        arm["lanes_source"] = LAYOUT_SRC
        if arm["left_shared"]:
            notes.append(f"LAYOUT: arm {d} has a lane shared Left+Through - rule is 'left on its own lane', check.")
        if arm["right_shared"]:
            notes.append(f"LAYOUT: arm {d} has a lane shared Through+Right - check EXCLUSIVELANES for Right.")
        cube = arm.get("_cube_entry_link_lanes")
        total = sum(v for v in (L.get("left"), L.get("through"), L.get("right"),
                                L.get("left_shared"), L.get("right_shared")) if v)
        if cube and total and not arm.get("right_free") and abs(float(cube) - total) > 0.01:
            notes.append(f"LAYOUT: arm {d} layout lanes = {total} but CUBE entry link LANE = {cube:g} - check.")
        missing = [k for k in ("left", "through") if L.get(k) is None]
        if missing:
            notes.append(f"LAYOUT: arm {d} - {', '.join(missing)} lanes missing in {info['file']}.")
    cfg["lanes_source"] = f"{LAYOUT_SRC} ({info['file']})" if info["arms"] else f"{LAYOUT_SRC} - NOT FOUND"
    cfg["_layout_file"] = info["file"]
    cfg["_picture_notes"] = [n for n in (cfg.get("_picture_notes") or [])] + notes
    cfg["_layout_notes"] = notes
    return cfg


# ------------------------------------------------------------------ checks used before writing a TXT
def config_problems(cfg):
    """Errors that block the TXT.  (Engineer fixes the config, or re-runs Auto-fill.)"""
    errs = []
    if "stage picture" in str(cfg.get("lanes_source", "")).lower():
        errs.append("Lanes in this config were read from the STAGE PICTURE. Lanes must come from the Layout "
                    "folder - run Auto-fill again (or fill the lanes from the layout and change 'lanes_source').")
    for d, arm in (cfg.get("arms") or {}).items():
        if not arm.get("right_free"):
            continue
        e, x = arm.get("free_turn_entry_m"), arm.get("free_turn_exit_m")
        if e is None or x is None:
            errs.append(f"Arm {d}: slip lane (FORMOFWAY 9) but FREETURNENTRY/EXIT length not available in NRN "
                        f"- validate and enter the real length (not 50 m).")
        elif float(e) == 50.0 and float(x) == 50.0 and arm.get("free_turn_source") not in (SLIP_SRC, "manual"):
            errs.append(f"Arm {d}: slip length is exactly 50 m and has no NRN source - looks like the old default. "
                        f"Run Auto-fill again, or set \"free_turn_source\": \"manual\" if you measured 50 m.")
    return errs
