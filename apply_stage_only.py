r"""
apply_stage_only.py  -  one-time patch: stage drawings give STAGE LETTERS / PHASES ONLY
------------------------------------------------------------------------------------
Run once, in the Junction_API folder, with the app STOPPED:

        python apply_stage_only.py

It edits stage_reader.py and auto_stages.py in place (backup of each saved as
<name>.py.bak_before_stage_only) and checks that every edit really was found.
picture_source.py has no lane logic, so it is not touched.
Running it a second time does nothing (already patched).
To undo: copy the .bak_before_stage_only files back over the originals.
"""
import ast
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MARK = "STAGE-ONLY"


# ------------------------------------------------------------------ helpers
def replace_function(src, name, new_code):
    """replace the module-level function `name` (whole def ... end) with new_code"""
    tree = ast.parse(src)
    node = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name), None)
    if node is None:
        raise RuntimeError(f"function {name}() not found")
    start = (min(d.lineno for d in node.decorator_list) if node.decorator_list else node.lineno) - 1
    lines = src.split("\n")
    return "\n".join(lines[:start] + new_code.rstrip("\n").split("\n") + lines[node.end_lineno:])


def sub_once(src, pattern, repl, what, flags=0, required=True):
    new, n = re.subn(pattern, lambda m: repl, src, count=1, flags=flags)
    if n != 1 and required:
        raise RuntimeError(f"could not find: {what}")
    return new


# ------------------------------------------------------------------ new code: stage_reader.py
ARM_MOVEMENTS = '''def arm_movements(parm, detectors, centre):
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
'''

SUGGEST_FROM_PICTURE = '''def suggest_from_picture(pic, cube_arms):
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
        a["right_group"] = mv["through_group"]
        a["right_free"] = False                 # slip lane is decided later from NRN FORMOFWAY 9
        a["_picture_letters"] = [l["ch"] for l in parm["letters"]]
        a["_match_error_deg"] = parm["match_error_deg"]
        cfg["arms"][cube["dir"]] = a
        notes += [f"Arm {cube['dir']}: {n}" for n in n2]
    return cfg, notes
'''


def patch_stage_reader(src):
    src = replace_function(src, "arm_movements", ARM_MOVEMENTS)
    src = replace_function(src, "suggest_from_picture", SUGGEST_FROM_PICTURE)
    # stop reading detector boxes (lanes) and zebra islands (slip hint) from the picture
    src = sub_once(
        src,
        r'    dets = \[c for c in components\(m\["detector"\].*?\n    zebras = zebra_corners\(m, area, centre\)[^\n]*\n',
        "    dets, zebras = [], []      # STAGE-ONLY: lanes = Layout folder, slip lanes = NRN FORMOFWAY 9\n",
        "dets = ... / zebras = ... in read_picture()", flags=re.S)
    # docstring (cosmetic)
    src = sub_once(src, r"^[ \t]*\* lanes[ \t]+:.*$",
                   "  * lanes           : NOT read (they come from the Layout folder)  [STAGE-ONLY]",
                   "docstring lanes line", flags=re.M, required=False)
    src = sub_once(src, r"^[ \t]*\* slip lane hint.*$",
                   "  * slip lane hint  : NOT read (slip lanes come from NRN FORMOFWAY 9)",
                   "docstring slip line", flags=re.M, required=False)
    return src


# ------------------------------------------------------------------ new code: auto_stages.py
LANE_ROWS = '''def lane_rows():
    """STAGE-ONLY: {(IP, ARM): {left_group, through_group}} from data\\\\lane_input.xlsx
    (re-read when the file changes).  Only the signal-GROUP letters are used.
    'Left lanes' / 'Through lanes' / 'Right lanes' columns, if still in the sheet, are IGNORED:
    lanes come from the Layout folder."""
    if not LANE_SHEET.exists():
        return {}
    mt = LANE_SHEET.stat().st_mtime
    if _lane_cache["mtime"] == mt:
        return _lane_cache["rows"]
    import openpyxl
    try:
        wb = openpyxl.load_workbook(LANE_SHEET, read_only=True, data_only=True)
    except PermissionError:
        return _lane_cache["rows"]               # open in Excel - use last read
    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    hdr = [str(h or "").strip().lower() for h in rows[0]] if rows else []
    col = {name: hdr.index(name) for name in hdr if name}
    out = {}
    for r in rows[1:]:
        ip = je.norm_ip(r[col["ip"]]) if "ip" in col and r[col["ip"]] else None
        arm = str(r[col["arm"]]).strip().upper() if "arm" in col and r[col["arm"]] else None
        if not ip or not arm:
            continue
        rec = {}
        for key, name in (("left_group", "left group"), ("through_group", "through group")):
            if name in col and r[col[name]] not in (None, ""):
                rec[key] = str(r[col[name]]).strip().upper()
        out[(ip, arm)] = rec
    _lane_cache.update(mtime=mt, rows=out)
    return out
'''

MAKE_LANE_SHEET = '''def make_lane_sheet():
    """STAGE-ONLY: sheet with the signal-group letters per arm (no lane columns)."""
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    print("Loading app data (cache) ...")
    store = je.Store()
    keep = lane_rows()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Groups"
    hdr = ["IP", "Arm", "Exit node", "Entry node", "Left group", "Through group", "Missing"]
    ws.append(hdr)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", start_color="1F4E78")
        c.alignment = Alignment(horizontal="center")
    yellow = PatternFill("solid", start_color="FFF2CC")
    n_rows = n_missing = 0
    for ip in sorted(store.ip_nodes):
        if ip not in store.signals:
            continue
        cfg = _orig_load(ip) or {}
        rep = je.Report()
        node = je.pick_node(store, ip, cfg, rep)
        if node is None:
            continue
        for a in je.detect_arms(store, node, rep):
            c = (cfg.get("arms") or {}).get(a["dir"], {})
            k = keep.get((ip, a["dir"]), {})
            vals = {f: k.get(f, c.get(f)) for f in ("left_group", "through_group")}
            miss = [] if vals["through_group"] not in (None, "") else ["Left/Through group"]
            ws.append([ip, a["dir"], a["exit"], a["entry"], vals["left_group"], vals["through_group"],
                       ", ".join(miss)])
            r = ws.max_row
            for col in (5, 6):
                if ws.cell(row=r, column=col).value in (None, ""):
                    ws.cell(row=r, column=col).fill = yellow
            n_rows += 1
            n_missing += bool(miss)
    for col, w in zip("ABCDEFG", (10, 6, 11, 11, 11, 13, 24)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = ws.dimensions
    if LANE_SHEET.exists():
        LANE_SHEET.replace(LANE_SHEET.with_name("lane_input_backup.xlsx"))
    wb.save(LANE_SHEET)
    print(f"Saved {LANE_SHEET}: {n_rows} arms, {n_missing} with missing groups (yellow cells).")
'''


def patch_auto_stages(src):
    src = replace_function(src, "lane_rows", LANE_ROWS)
    src = replace_function(src, "make_lane_sheet", MAKE_LANE_SHEET)
    # the engine is fed a copy loaded BEFORE main.py's clean-up: drop the old top-level slip default here too
    m = re.search(r"^([ \t]*)cfg = copy\.deepcopy\(cfg\)[^\n]*\n", src, flags=re.M)
    if not m:
        raise RuntimeError("could not find: cfg = copy.deepcopy(cfg) in build_junction()")
    ind = m.group(1)
    add = (f"{ind}cfg.pop(\"free_turn_entry_m\", None)      # STAGE-ONLY: no hidden 50 m slip default\n"
           f"{ind}cfg.pop(\"free_turn_exit_m\", None)\n")
    src = src[:m.end()] + add + src[m.end():]
    src = sub_once(
        src, r'^print\("Auto stages:.*$',
        'print("Auto stages: stages from signal timings; signal-group letters from data\\\\lane_input.xlsx '
        '(if present). Lanes come from the Layout folder.")',
        "print('Auto stages ...') line", flags=re.M, required=False)
    src = sub_once(src, r"^2\. LANES \(one Excel sheet for all junctions\)",
                   "2. SIGNAL GROUPS (one Excel sheet for all junctions)  [STAGE-ONLY: lane columns are ignored]",
                   "docstring heading", flags=re.M, required=False)
    return src


# ------------------------------------------------------------------ run
def run(path, fn):
    p = HERE / path
    if not p.exists():
        print(f"  !! {path} not found next to this script - skipped")
        return False
    src = p.read_text(encoding="utf-8")
    if MARK in src:
        print(f"  {path}: already patched - nothing to do")
        return True
    try:
        new = fn(src)
        ast.parse(new)                                   # must still be valid Python
    except Exception as exc:
        print(f"  !! {path}: NOT changed ({exc})")
        return False
    bak = p.with_name(p.name + ".bak_before_stage_only")
    if not bak.exists():
        bak.write_text(src, encoding="utf-8")
    if MARK not in new:
        new += f"\n# {MARK}: patched by apply_stage_only.py\n"
    p.write_text(new, encoding="utf-8")
    print(f"  {path}: patched (backup: {bak.name})")
    return True


if __name__ == "__main__":
    print("Stage drawings -> letters / stages only")
    ok = run("stage_reader.py", patch_stage_reader)
    ok = run("auto_stages.py", patch_auto_stages) and ok
    print("picture_source.py: no lane logic - not changed")
    print("DONE" if ok else "FINISHED WITH PROBLEMS - see lines marked !!")
    sys.exit(0 if ok else 1)
