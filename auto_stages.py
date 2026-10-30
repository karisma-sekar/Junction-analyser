r"""
auto_stages.py  -  stages from the signal TIMINGS + lanes from one Excel sheet
------------------------------------------------------------------------------
1. STAGES (automatic, no drawing needed)
   For every junction and peak, the app steps through the cycle of that peak's
   signal plan second by second and records which vehicle signal groups are
   green together. Each combination = one stage. Short overlaps in the
   intergreen (one group already red, the next not yet green) are ignored.
     * vehicle groups = groups given to the arms (left / through / right group)
     * stage numbers: if a stage equals a stage already in the config (UTC
       number from the drawing), that number is kept; others are numbered in
       running order
     * if the config stages differ from the timings, a CHECK warning is shown
     * to keep hand-entered stages for a junction, set in its config:
           "stages_source": "manual"

2. SIGNAL GROUPS (one Excel sheet for all junctions)  [STAGE-ONLY: lane columns are ignored]
   data\lane_input.xlsx  - one row per junction arm:
     IP | Arm | Exit node | Entry node | Left group | Through group |
     Left lanes | Through lanes | Right lanes
   Filled cells override the config; empty cells keep the config value.
   Make / refresh the sheet (pre-filled with every junction, its arms and the
   values already known; missing values highlighted yellow):
       python auto_stages.py
   (run in Junction_API with the app stopped; existing entries are kept)

Nothing else changes: timings, plan selection, NRN slip lanes, exit lanes,
GeoJSON and the TXT format. Saved configs are never changed.

Use: in main.py add   import auto_stages   directly below   import signal_sources
"""
import copy
import threading

import junction_engine as je

LANE_SHEET = je.DATA / "lane_input.xlsx"
MAX_INTERGREEN_S = 10      # overlap pieces up to this length inside an intergreen are ignored
MIN_STAGE_S = 3            # shorter pieces are never a stage
_lock = threading.Lock()


# ---------------------------------------------------------------- stages from timings
def _green(g, cycle, t):
    s, e = g.get("start"), g.get("end")
    if s is None or e is None:
        return False
    return (t - s) % cycle < (e - s) % cycle


def stages_from_timings(plan, vehicle):
    """-> list of (start_second, sorted groups, length) in running order, notes"""
    cycle = int(plan["cycle"])
    groups = {k: g for k, g in plan["groups"].items() if k in vehicle}
    notes = []
    missing = sorted(set(vehicle) - set(groups))
    if missing:
        notes.append(f"Arm groups {missing} have no times in this plan.")
    segs = []                                   # [start, frozenset, length]
    for t in range(cycle):
        on = frozenset(k for k, g in groups.items() if _green(g, cycle, t))
        if segs and segs[-1][1] == on:
            segs[-1][2] += 1
        else:
            segs.append([t, on, 1])
    if len(segs) > 1 and segs[0][1] == segs[-1][1]:          # wraps round the cycle
        segs[-1][2] += segs[0][2]
        segs.pop(0)
    out = []
    n = len(segs)
    for i, (start, on, length) in enumerate(segs):
        if not on:
            continue                                         # all-red / intergreen
        prev_on, next_on = segs[i - 1][1], segs[(i + 1) % n][1]
        is_overlap = length <= MAX_INTERGREEN_S and (on < prev_on or on < next_on)
        if is_overlap or length < MIN_STAGE_S:
            notes.append(f"{'/'.join(sorted(on))} green alone for {length} s between stages "
                         f"(intergreen overlap) - not a stage.")
            continue
        out.append((start, sorted(on), length))
    seen, uniq = set(), []                       # same group set twice -> one stage
    for s, on, l in out:
        if tuple(on) not in seen:
            seen.add(tuple(on)); uniq.append((s, on, l))
    return uniq, notes


def number_stages(found, old_stages):
    """keep UTC numbers of stages that match the config, number the rest after them"""
    old = {tuple(sorted(str(x).upper() for x in v)): int(k)
           for k, v in (old_stages or {}).items() if v and str(k).isdigit()}
    result, used = {}, set()
    for _, on, _ in found:
        k = old.get(tuple(on))
        if k is not None and k not in used:
            result[k] = on; used.add(k)
    nxt = 1
    for _, on, _ in found:
        if on in result.values():
            continue
        while nxt in used:
            nxt += 1
        result[nxt] = on; used.add(nxt)
    return {str(k): v for k, v in sorted(result.items())}


# ---------------------------------------------------------------- lane sheet
_lane_cache = {"mtime": None, "rows": {}}


def lane_rows():
    """STAGE-ONLY: {(IP, ARM): {left_group, through_group}} from data\\lane_input.xlsx
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


def apply_lane_sheet(ip, cfg, notes):
    rows = lane_rows()
    for arm, a in (cfg.get("arms") or {}).items():
        rec = rows.get((ip, arm.upper()))
        if not rec:
            continue
        for k, v in rec.items():
            a[k] = v
        if "through_group" in rec and not a.get("right_group"):
            a["right_group"] = rec["through_group"]
        notes.append(f"Arm {arm}: values from lane_input.xlsx used "
                     f"({', '.join(f'{k}={v}' for k, v in rec.items())}).")


# ---------------------------------------------------------------- hook into the app
_orig_build = je.build_junction
_orig_load = je.load_config


def build_junction(store, ip_text, period):
    ip = je.norm_ip(ip_text)
    cfg = _orig_load(ip)
    if cfg is None:
        return _orig_build(store, ip_text, period)
    cfg = copy.deepcopy(cfg)
    cfg.pop("free_turn_entry_m", None)      # STAGE-ONLY: no hidden 50 m slip default
    cfg.pop("free_turn_exit_m", None)
    notes, warns = [], []

    apply_lane_sheet(ip, cfg, notes)

    if str(cfg.get("stages_source", "")).lower() != "manual":
        try:
            res0, ctx = je.analyse(store, ip, period)
            plan = ctx[4] if ctx else None
        except Exception:
            res0, plan = {}, None
        vehicle = {str(a.get(k)).upper() for a in (cfg.get("arms") or {}).values()
                   for k in ("left_group", "through_group", "right_group") if a.get(k)}
        if plan and vehicle:
            found, n2 = stages_from_timings(plan, vehicle)
            notes += n2
            if found:
                new = number_stages(found, cfg.get("stages"))
                old = {k: sorted(str(x).upper() for x in v)
                       for k, v in (cfg.get("stages") or {}).items() if v}
                if old and old != new:
                    warns.append(f"Stages from {period} timings {new} differ from the config / drawing "
                                 f"{old} - timings used.")
                cfg["stages"] = new
                notes.append(f"Stages from plan {res0.get('plan_id')} timings: " +
                             ", ".join(f"{k}={'/'.join(v)}" for k, v in new.items()))
            else:
                warns.append("No stage found in the timings - config stages used.")
        elif not vehicle:
            warns.append("No signal groups on the arms - stages cannot be taken from the timings; "
                         "fill Left / Through group in lane_input.xlsx.")

    with _lock:                                   # engine reads the config through load_config
        je.load_config = lambda x, _ip=ip, _c=cfg: _c if je.norm_ip(x) == _ip else _orig_load(x)
        try:
            res, txt = _orig_build(store, ip_text, period)
        finally:
            je.load_config = _orig_load
    rep = res.setdefault("report", {})
    rep.setdefault("warnings", []).extend(warns)
    rep.setdefault("info", []).extend(notes)
    res["stages_used"] = cfg.get("stages")
    return res, txt


je.build_junction = build_junction
print("Auto stages: stages from signal timings; signal-group letters from data\\lane_input.xlsx (if present). Lanes come from the Layout folder.")


# ---------------------------------------------------------------- make the lane sheet
def make_lane_sheet():
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


if __name__ == "__main__":
    for mod in ("picture_source", "signal_sources"):   # same signal data as the app
        try:
            __import__(mod)
        except ImportError:
            pass
    make_lane_sheet()
