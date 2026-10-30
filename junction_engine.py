"""
junction_engine.py  -  Junction Workbench: CUBE junction-file engine
-------------------------------------------------------------------
Reads the CUBE / NRN / signal-plan inputs, validates the mapping for one
IP junction, and writes the CUBE Voyager junction TXT in the exact
layout of the supplied template (IP58B_Node112580_Junction_AM.txt).

Rule used everywhere:  NEVER assume a value.  If something cannot be
read from the input files or from the junction config JSON, it is
reported as an ERROR (blocks the TXT) or a WARNING (TXT still produced,
but you must check it).

Signal groups: sheets may label groups with letters (A, B, ...) or as
G1, G2, ... (e.g. MBZ workbook). G1, G2, ... are read as A, B, ... and a
CHECK warning is added so the mapping is confirmed against the drawing.
"""
import csv
import json
import math
import re
from collections import Counter, deque
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"              # put the input files here
CACHE = BASE / "cache"            # small, fast copies built on first run
CONFIGS = BASE / "junction_configs"

FILES = {
    "nodes": DATA / "NodeShapeFile.dbf.csv",
    "links": DATA / "ShapeFile.dbf.csv",
    "jbox": DATA / "NRN_2026_Q1_JunctionBox.dbf.csv",
    "signals": DATA / "Signal Phasing plan.xlsx",
}

LANE_FIELD = "LANE_2020"           # field used in the template's CHECK line
PERIODS = {"AM": "CP_AM", "OP": "CP_OP", "PM": "CP_PM"}
UTM_ZONE = 40                       # Abu Dhabi, WGS84 / UTM 40N
MAX_POSITION_GAP_M = 100            # CUBE node vs NRN JunctionBox tolerance

# Template constants (copied from the template, can be overridden per junction
# in the config under "constants")
TEMPLATE_CONSTANTS = {
    "LANEADJUST": 1,
    "AVELANEWIDTH": 3.60,
    "MINIMUMCAPACITY": 500.00,
    "RANDOMNESS": 0.55,
    "UTURNCONTROL": 4,
}

RULE = ";" + "=" * 69
DIR_NAMES = {"N": "North", "E": "East", "S": "South", "W": "West",
             "NE": "North-East", "SE": "South-East", "SW": "South-West", "NW": "North-West"}


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------
def norm_ip(text):
    """'IP 58B', 'ip-58b', '58B'  ->  'IP58B'"""
    if text is None:
        return ""
    t = re.sub(r"[\s\-_]", "", str(text)).upper()
    if not t:
        return ""
    if t[0].isdigit():
        t = "IP" + t
    return t


def pretty_ip(ip):
    """'IP58B' -> 'IP-58B' (as written in the template header)"""
    m = re.match(r"^IP(.*)$", ip)
    return f"IP-{m.group(1)}" if m else ip


def num(v):
    try:
        if v is None or str(v).strip() == "":
            return None
        return float(v)
    except ValueError:
        return None


def fmt_m(v):
    """50.0 -> '50' , 47.5 -> '47.5' (header text)"""
    return str(int(v)) if float(v).is_integer() else f"{v:g}"


def utm_to_latlon(E, N, zone=UTM_ZONE):
    a = 6378137.0; f = 1 / 298.257223563; k0 = 0.9996
    e2 = f * (2 - f); ep2 = e2 / (1 - e2)
    x = E - 500000.0; M = N / k0
    mu = M / (a * (1 - e2 / 4 - 3 * e2 ** 2 / 64 - 5 * e2 ** 3 / 256))
    e1 = (1 - math.sqrt(1 - e2)) / (1 + math.sqrt(1 - e2))
    p1 = (mu + (3 * e1 / 2 - 27 * e1 ** 3 / 32) * math.sin(2 * mu)
          + (21 * e1 ** 2 / 16 - 55 * e1 ** 4 / 32) * math.sin(4 * mu)
          + (151 * e1 ** 3 / 96) * math.sin(6 * mu)
          + (1097 * e1 ** 4 / 512) * math.sin(8 * mu))
    N1 = a / math.sqrt(1 - e2 * math.sin(p1) ** 2)
    T1 = math.tan(p1) ** 2; C1 = ep2 * math.cos(p1) ** 2
    R1 = a * (1 - e2) / (1 - e2 * math.sin(p1) ** 2) ** 1.5
    D = x / (N1 * k0)
    lat = p1 - (N1 * math.tan(p1) / R1) * (
        D ** 2 / 2 - (5 + 3 * T1 + 10 * C1 - 4 * C1 ** 2 - 9 * ep2) * D ** 4 / 24
        + (61 + 90 * T1 + 298 * C1 + 45 * T1 ** 2 - 252 * ep2 - 3 * C1 ** 2) * D ** 6 / 720)
    lon = (D - (1 + 2 * T1 + C1) * D ** 3 / 6
           + (5 - 2 * C1 + 28 * T1 - 3 * C1 ** 2 + 8 * ep2 + 24 * T1 ** 2) * D ** 5 / 120) / math.cos(p1)
    return math.degrees(lat), (zone - 1) * 6 - 180 + 3 + math.degrees(lon)


def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1; dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def bearing(x0, y0, x1, y1):
    """0 = north, 90 = east (projected coordinates)"""
    return (math.degrees(math.atan2(x1 - x0, y1 - y0)) + 360) % 360


def compass(b, eight=False):
    if eight:
        return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][int(((b + 22.5) % 360) // 45)]
    return ["N", "E", "S", "W"][int(((b + 45) % 360) // 90)]


def ang_diff(a, b):
    d = abs(a - b) % 360
    return min(d, 360 - d)


# ----------------------------------------------------------------------------
# 1. CACHE BUILDING (big CSV / Excel -> small files, runs once)
# ----------------------------------------------------------------------------
def _read_csv_cols(path, cols, keep=None):
    with open(path, newline="", encoding="latin-1") as f:
        rdr = csv.DictReader(f)
        missing = [c for c in cols if c not in rdr.fieldnames]
        if missing:
            raise ValueError(f"{path.name}: missing columns {missing}")
        for row in rdr:
            r = {c: row[c] for c in cols}
            if keep is None or keep(r):
                yield r


def _parse_signal_workbook(path):
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    out, problems = {}, {}
    for ws in wb.worksheets:
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        try:
            parsed = _parse_signal_sheet(ws.title, rows)
        except Exception as exc:          # keep going, report it
            problems[ws.title] = str(exc)
            continue
        if parsed is None:
            continue
        keys = [norm_ip(k) for k in re.split(r"&", ws.title)]
        for k in keys:
            out[k] = parsed
    return out, problems


def _cell(rows, r, c):
    if r < len(rows) and c < len(rows[r]):
        v = rows[r][c]
        return None if v in ("", None) else v
    return None


def _find_row(rows, labels):
    for i, r in enumerate(rows):
        if r and isinstance(r[0], str) and any(r[0].strip().lower().startswith(l) for l in labels):
            return i
    return None


def _parse_signal_sheet(title, rows):
    r_plan = _find_row(rows, ["signal plan"])
    r_hdr = _find_row(rows, ["phase", "signal group"])
    if r_plan is None or r_hdr is None:
        return None                       # region / summary sheet
    r_sipl = _find_row(rows, ["sipl name", "plan name"])        # CHANGE 2: MBZ "Plan Name for reference"
    r_cyc = _find_row(rows, ["cycle time"])

    name, region = None, None
    for r in rows[:8]:
        for v in r:
            if isinstance(v, str) and v.lower().startswith("intersection name"):
                name = v.split(":", 1)[-1].strip()
            if isinstance(v, str) and re.match(r"region\s*-", v, re.I):
                region = v.split("-", 1)[-1].strip()

    # UTC number (e.g. N05411) - most common N0xxxx code in the sheet
    utc = Counter()
    for r in rows:
        for v in r:
            if isinstance(v, str):
                utc.update(re.findall(r"\b(N\d{5})", v))
    utc_no = utc.most_common(1)[0][0] if utc else None

    # signal group rows: letters (A, B, ... / B1-A) or numbered (G1, G2, ...)
    groups = []
    for i in range(r_hdr + 1, len(rows)):
        lab = _cell(rows, i, 0)
        if lab is None or not isinstance(lab, str):
            break
        lab = lab.strip()
        m = re.match(r"^(?:[A-Z]{1,2}\d+-)?([A-Z]{1,2})$", lab)
        gnum = re.match(r"^G\s*(\d{1,2})$", lab, re.I)            # CHANGE 1: G1 -> A, G2 -> B ...
        if m:
            groups.append((i, lab, m.group(1)))
        elif gnum and 1 <= int(gnum.group(1)) <= 26:
            groups.append((i, lab, chr(64 + int(gnum.group(1)))))
        else:
            break

    plans = {}
    for c, v in enumerate(rows[r_plan]):
        if not (isinstance(v, str) and re.match(r"^#\s*\d+$", v.strip())):
            continue
        pid = "#" + re.sub(r"\D", "", v)
        g = {}
        for i, raw, key in groups:
            s, e, gt = _cell(rows, i, c), _cell(rows, i, c + 1), _cell(rows, i, c + 2)
            if s is None and e is None and gt is None:
                continue
            g[key] = {"label": raw, "start": num(s), "end": num(e), "gtime": num(gt)}
        entry = {
            "sipl": _cell(rows, r_sipl, c) if r_sipl is not None else None,
            "cycle": num(_cell(rows, r_cyc, c)) if r_cyc is not None else None,
            "groups": g,
        }
        # duplicated '#n' columns: keep the one that actually has data
        old = plans.get(pid)
        if old is None or (not old["groups"] and g):
            plans[pid] = entry
    return {"sheet": title, "name": name, "region": region, "utc_no": utc_no,
            "group_order": [k for _, _, k in groups], "plans": plans,
            "g_numbered": any(re.match(r"^G\s*\d", raw, re.I) for _, raw, _ in groups)}   # CHANGE 3


def build_cache(log=print):
    CACHE.mkdir(exist_ok=True)
    for k, p in FILES.items():
        if not p.exists():
            raise FileNotFoundError(f"Input file missing: {p}")

    log("Reading NodeShapeFile ...")
    with open(CACHE / "nodes.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["N", "X", "Y", "IP", "CP_AM", "CP_OP", "CP_PM"])
        for r in _read_csv_cols(FILES["nodes"], ["N", "X", "Y", "IP_NO", "CP_AM", "CP_OP", "CP_PM"]):
            w.writerow([r["N"], r["X"], r["Y"], norm_ip(r["IP_NO"]), r["CP_AM"].strip(),
                        r["CP_OP"].strip(), r["CP_PM"].strip()])

    log("Reading CUBE ShapeFile links (large file, please wait) ...")
    with open(CACHE / "links.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["A", "B", "LEN", "LANES"])
        for r in _read_csv_cols(FILES["links"], ["A", "B", "SHAPE_Leng", LANE_FIELD]):
            w.writerow([r["A"], r["B"], r["SHAPE_Leng"], r[LANE_FIELD]])

    log("Reading NRN JunctionBox ...")
    with open(CACHE / "jbox.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["IP", "SITE_ID", "SIGNALIZE", "AREA", "LAT", "LON"])
        for r in _read_csv_cols(FILES["jbox"], ["SITE_ID", "SIGNALIZE", "NAMEENGLIS", "LATTIDUTE", "LONGTUIDE"]):
            w.writerow([norm_ip(r["SITE_ID"]), r["SITE_ID"], r["SIGNALIZE"], r["NAMEENGLIS"],
                        r["LATTIDUTE"], r["LONGTUIDE"]])

    log("Reading Signal Phasing plan.xlsx ...")
    plans, problems = _parse_signal_workbook(FILES["signals"])
    (CACHE / "signal_plans.json").write_text(json.dumps(plans, indent=1, default=str))
    (CACHE / "signal_plan_problems.json").write_text(json.dumps(problems, indent=1))
    log(f"{len(plans)} signal sheets parsed, {len(problems)} sheets with problems.")
    build_picture_cache(plans, log)
    log("Cache ready.")


def build_picture_cache(plans=None, log=print):
    """extract + read the stage picture of every sheet (about 1-2 minutes)"""
    import stage_reader as sr
    if plans is None:
        plans = json.loads((CACHE / "signal_plans.json").read_text())
    log("Extracting stage pictures from Signal Phasing plan.xlsx ...")
    pics = sr.extract_sheet_pictures(FILES["signals"], CACHE / "pictures")
    by_sheet = {}
    for ip, sp in plans.items():
        by_sheet.setdefault(sp["sheet"], []).append(ip)
    reads = {}
    for i, (sheet, png) in enumerate(pics.items(), 1):
        ips = by_sheet.get(sheet) or [norm_ip(k) for k in sheet.split("&")]
        sp = plans.get(ips[0]) if ips else None
        try:
            pic = sr.read_picture(png, sp["group_order"] if sp else None)
            if pic.get("ok"):
                pic["notes"] += sr.fix_stages_with_timing(pic, sp, stage_window)
        except Exception as exc:
            pic = {"ok": False, "notes": [f"Picture could not be read: {exc}"]}
        pic["sheet"], pic["png"] = sheet, Path(png).name
        for ip in ips:
            reads[ip] = pic
        if i % 20 == 0:
            log(f"  read {i}/{len(pics)} pictures")
    (CACHE / "picture_reads.json").write_text(json.dumps(reads, default=float))
    log(f"Stage pictures read: {sum(1 for r in reads.values() if r.get('stages'))} junctions with stages.")


# ----------------------------------------------------------------------------
# 2. DATA STORE (loaded once when the app starts)
# ----------------------------------------------------------------------------
class Store:
    def __init__(self):
        needed = ["nodes.csv", "links.csv", "jbox.csv", "signal_plans.json"]
        if not all((CACHE / n).exists() for n in needed):
            build_cache()
        self.nodes, self.ip_nodes = {}, {}
        with open(CACHE / "nodes.csv") as f:
            for r in csv.DictReader(f):
                n = int(r["N"])
                self.nodes[n] = {"x": float(r["X"]), "y": float(r["Y"]), "ip": r["IP"],
                                 "AM": r["CP_AM"], "OP": r["CP_OP"], "PM": r["CP_PM"]}
                if r["IP"]:
                    self.ip_nodes.setdefault(r["IP"], []).append(n)
        self.out_links, self.in_links = {}, {}
        with open(CACHE / "links.csv") as f:
            for r in csv.DictReader(f):
                a, b = int(r["A"]), int(r["B"])
                rec = {"a": a, "b": b, "lanes": num(r["LANES"]), "len": num(r["LEN"])}
                self.out_links.setdefault(a, []).append(rec)
                self.in_links.setdefault(b, []).append(rec)
        self.jbox = {}
        with open(CACHE / "jbox.csv", encoding="latin-1") as f:
            for r in csv.DictReader(f):
                self.jbox.setdefault(r["IP"], r)
        self.signals = json.loads((CACHE / "signal_plans.json").read_text())
        pr = CACHE / "picture_reads.json"
        if not pr.exists():
            build_picture_cache(self.signals)
        self.pictures = json.loads(pr.read_text())

    def link(self, a, b):
        for l in self.out_links.get(a, []):
            if l["b"] == b:
                return l
        return None


# ----------------------------------------------------------------------------
# 3. AUTO-DETECTION FROM INPUT FILES
# ----------------------------------------------------------------------------
def pick_node(store, ip, cfg, rep):
    nodes = store.ip_nodes.get(ip, [])
    if cfg.get("node"):
        n = int(cfg["node"])
        if n not in store.nodes:
            rep.error(f"Config node {n} not found in NodeShapeFile.")
            return None
        if nodes and n not in nodes:
            rep.warn(f"Config node {n} is not tagged {ip} in NodeShapeFile (tagged: {nodes}).")
        return n
    if not nodes:
        rep.error(f"{ip} not found in NodeShapeFile IP_NO column.")
        return None
    if len(nodes) > 1:
        best = max(nodes, key=lambda n: len(store.out_links.get(n, [])) + len(store.in_links.get(n, [])))
        rep.warn(f"{ip} is tagged on {len(nodes)} CUBE nodes {nodes}; using {best} (most links). "
                 f"Set \"node\" in the config to confirm.")
        return best
    return nodes[0]


def detect_arms(store, node, rep):
    """Pair each exit node with its entry node by direction."""
    J = store.nodes[node]
    exits = [l["b"] for l in store.out_links.get(node, [])]
    entries = [l["a"] for l in store.in_links.get(node, [])]

    def brg(n):
        p = store.nodes.get(n)
        return bearing(J["x"], J["y"], p["x"], p["y"]) if p else None
    arms, used = [], set()
    for ex in exits:
        be = brg(ex)
        cands = [(ang_diff(be, brg(en)), en) for en in entries if en not in used and brg(en) is not None]
        if not cands:
            rep.error(f"Exit node {ex}: no matching entry node found.")
            continue
        d, en = min(cands)
        if d > 45:
            rep.warn(f"Exit node {ex} paired with entry {en} but directions differ by {d:.0f} deg.")
        used.add(en)
        b = (be + brg(en)) / 2 if abs(be - brg(en)) < 180 else ((be + brg(en) + 360) / 2) % 360
        arms.append({"exit": ex, "entry": en, "bearing": round(b, 1)})
    for en in entries:
        if en not in used:
            rep.warn(f"Entry node {en} has no exit partner (one-way arm?). Not coded automatically.")
    # compass labels; switch to 8-point if 4-point labels collide
    labels = [compass(a["bearing"]) for a in arms]
    if len(set(labels)) < len(labels):
        labels = [compass(a["bearing"], eight=True) for a in arms]
        rep.warn("Two arms fall in the same N/E/S/W sector - using 8-point directions; check config.")
    for a, lab in zip(arms, labels):
        a["dir"] = lab
        # lanes from the CUBE ShapeFile
        lx = store.link(node, a["exit"]); ln = store.link(a["entry"], node)
        a["exit_link_lanes"] = lx["lanes"] if lx else None
        a["entry_link_lanes"] = ln["lanes"] if ln else None
        # downstream link of the exit node (beyond the satellite)
        down = [l for l in store.out_links.get(a["exit"], []) if l["b"] != node]
        a["exit_downstream_lanes"] = down[0]["lanes"] if len(down) == 1 else None
        a["neighbour_ip"] = neighbour_ip(store, a["exit"], node)
    return arms


def neighbour_ip(store, start, junction, max_nodes=400):
    """walk downstream from an exit node until another IP-tagged node is met"""
    own = store.nodes[junction]["ip"]
    seen, q = {junction, start}, deque([start])
    while q and len(seen) < max_nodes:
        n = q.popleft()
        ip = store.nodes.get(n, {}).get("ip")
        if ip and ip != own:
            return pretty_ip(ip)
        for l in store.out_links.get(n, []):
            if l["b"] not in seen:
                seen.add(l["b"]); q.append(l["b"])
    return None


def order_arms(arms, first_dir):
    """Template order: start at APPROACH1, then anticlockwise (W -> S -> E -> N)."""
    if not arms:
        return arms
    start = next((a for a in arms if a["dir"] == first_dir), arms[0])
    return sorted(arms, key=lambda a: (start["bearing"] - a["bearing"]) % 360)


# ----------------------------------------------------------------------------
# 4. SIGNAL TIMING
# ----------------------------------------------------------------------------

# TIMING_GUARD_V1: joint, non-overlapping stage timing validation.
def _joint_stage_timing(plan, stages, arms, rep):
    """Reconstruct only uniquely matchable timing states; never scale greens."""
    if not plan or not stages:
        return stages, {}
    groups, cycle = plan['groups'], plan['cycle']
    if not isinstance(cycle, (int, float)) or not math.isfinite(cycle) or cycle <= 0 or cycle != int(cycle):
        rep.error('Timing guard: positive whole-second cycle required; no rounding applied.')
        return stages, {}
    cycle = int(cycle)
    universe = set(g for mem in stages.values() for g in mem)
    for arm in arms.values():
        for mv in ('left', 'through', 'right'):
            if mv == 'right' and arm.get('right_free'):
                continue
            g = arm.get(mv + '_group')
            if g:
                universe.add(str(g).upper())
    secs = {}
    for g in sorted(universe):
        v = groups.get(g)
        if not v:
            rep.error(f'Timing guard: movement/stage group {g} missing from selected plan.')
            continue
        a, b = v.get('start'), v.get('end')
        if any(not isinstance(x, (int, float)) or not math.isfinite(x) or x != int(x) or x < 0 or x > cycle for x in (a, b)):
            rep.error(f'Timing guard: {g} has missing, fractional or out-of-cycle endpoints; no rounding applied.')
            continue
        duration = (b - a) % cycle
        gt = v.get('gtime')
        if duration == 0 or (gt is not None and gt != duration):
            rep.error(f'Timing guard: {g} has ambiguous or inconsistent duration; check source timing cells.')
            continue
        secs[g] = green_seconds(v, cycle)
    if len(secs) != len(universe):
        return stages, {}
    states = {}
    for t in range(cycle):
        active = frozenset(g for g in universe if t in secs[g])
        if active:
            states.setdefault(active, set()).add(t)
    assigned = {s: frozenset(mem) for s, mem in stages.items()}
    missing = universe - set().union(*assigned.values())
    # A configured singleton can include a second, longer paired stage.
    overlap = any(set.intersection(*(secs[g] for g in assigned[a])) &
                  set.intersection(*(secs[g] for g in assigned[b]))
                  for a in assigned for b in assigned if a < b)
    if missing or overlap or any(mem not in states for mem in assigned.values()):
        # Maximal simultaneous vehicle states are candidates, not assumed stages.
        candidates = [v for v in states if not any(v < w for w in states)]
        options = {s: [v for v in candidates if mem <= v] for s, mem in assigned.items()}
        solutions = []
        def visit(todo, chosen, used):
            if len(solutions) >= 2:
                return
            if not todo:
                solutions.append(dict(chosen)); return
            s = min(todo, key=lambda k: len([v for v in options[k] if v not in used]))
            for v in options[s]:
                if v not in used:
                    chosen[s] = v
                    visit([k for k in todo if k != s], chosen, used | {v})
                    chosen.pop(s, None)
        if len(candidates) == len(assigned):
            visit(list(assigned), {}, set())
        if len(solutions) != 1:
            rep.error('Timing guard: stage letters/timings do not give a unique stage mapping. Export blocked; cycle and greens unchanged.')
            return stages, {}
        recovered = solutions[0]
        if set().union(*recovered.values()) != universe:
            rep.error('Timing guard: recovered stages omit movement groups. Export blocked.')
            return stages, {}
        rep.warn('Stage membership reconstructed uniquely from selected-plan timing states, not confirmed from drawing: ' +
                 '; '.join(f'{s}=' + '/'.join(sorted(recovered[s])) for s in sorted(recovered)) +
                 '. Review against stage drawing before model use.')
        assigned = recovered
    if len(set(assigned.values())) != len(assigned):
        rep.error('Timing guard: duplicate stage memberships are ambiguous. Export blocked.')
        return stages, {}
    info = {}
    for s, mem in sorted(assigned.items()):
        common = states.get(mem, set())
        starts = sorted(t for t in common if (t - 1) % cycle not in common)
        if not common or len(starts) != 1:
            rep.error(f'Timing guard: stage {s} has zero or multiple separate exact green intervals. Export blocked.')
            continue
        info[s] = {'green': len(common), 'start': starts[0], 'runs': 1}
    if len(info) != len(assigned):
        return stages, {}
    order = sorted(info, key=lambda s: info[s]['start'])
    gaps = [int((info[order[(i+1) % len(order)]]['start'] -
                 info[s]['start'] - info[s]['green']) % cycle)
            for i, s in enumerate(order)]
    total = sum(v['green'] for v in info.values())
    if total >= cycle or total + sum(gaps) != cycle:
        rep.error('Timing guard: stage greens and cyclic gaps do not reconcile to the source cycle. Export blocked.')
        return stages, {}
    rep.note(f'Timing guard: {total} s non-overlapping stage green + {sum(gaps)} s between-stage time = {cycle} s. Between-stage time may include continuing groups, not only all-red.')
    return {s: sorted(mem) for s, mem in assigned.items()}, info

def green_seconds(g, cycle):
    s, e = int(g["start"]), int(g["end"])
    return {(s + k) % cycle for k in range((e - s) % int(cycle))}


def stage_window(groups, members, cycle):
    """seconds in which ALL signal groups of the stage are green"""
    common = None
    for m in members:
        secs = green_seconds(groups[m], cycle)
        common = secs if common is None else common & secs
    if not common:
        return None
    # start = first second whose previous second is not in the set
    starts = [t for t in common if (t - 1) % cycle not in common]
    return {"green": len(common), "start": min(starts) if starts else 0, "runs": len(starts)}


# ----------------------------------------------------------------------------
# 5. REPORT
# ----------------------------------------------------------------------------
class Report:
    def __init__(self):
        self.errors, self.warnings, self.info = [], [], []

    def error(self, m): self.errors.append(m)
    def warn(self, m): self.warnings.append(m)
    def note(self, m): self.info.append(m)

    def as_dict(self):
        return {"ok": not self.errors, "errors": self.errors, "warnings": self.warnings, "info": self.info}


def load_config(ip):
    p = CONFIGS / f"{ip}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text())


def save_config(ip, cfg):
    CONFIGS.mkdir(exist_ok=True)
    (CONFIGS / f"{ip}.json").write_text(json.dumps(cfg, indent=2))


# ----------------------------------------------------------------------------
# 6. ANALYSE (validation for one junction + period)
# ----------------------------------------------------------------------------
def analyse(store, ip_text, period):
    ip = norm_ip(ip_text)
    period = period.upper()
    rep = Report()
    cfg = load_config(ip) or {}
    res = {"ip": ip, "period": period, "has_config": bool(cfg)}
    if period not in PERIODS:
        rep.error(f"Period must be one of {list(PERIODS)}.")
        res["report"] = rep.as_dict(); return res, None

    # --- node -------------------------------------------------------------
    node = pick_node(store, ip, cfg, rep)
    res["node"] = node
    if node is None:
        res["report"] = rep.as_dict(); return res, None
    N = store.nodes[node]
    lat, lon = utm_to_latlon(N["x"], N["y"])
    res["node_xy"] = [N["x"], N["y"]]; res["node_latlon"] = [round(lat, 7), round(lon, 7)]

    # --- NRN junction box position check ----------------------------------
    jb = store.jbox.get(ip)
    if jb and num(jb["LAT"]) is not None:
        d = haversine_m(lat, lon, float(jb["LAT"]), float(jb["LON"]))
        res["jbox"] = {"site_id": jb["SITE_ID"], "signalised": jb["SIGNALIZE"], "area": jb["AREA"],
                       "latlon": [float(jb["LAT"]), float(jb["LON"])], "gap_m": round(d, 1)}
        (rep.warn if d > MAX_POSITION_GAP_M else rep.note)(
            f"CUBE node {node} is {d:.0f} m from NRN JunctionBox {jb['SITE_ID']}.")
        if jb["SIGNALIZE"].strip().upper() != "Y":
            rep.warn(f"NRN JunctionBox says {jb['SITE_ID']} is not signalised (SIGNALIZE={jb['SIGNALIZE']}).")
    else:
        rep.warn(f"{ip} not found in NRN JunctionBox - position not cross-checked.")

    # --- signal plan --------------------------------------------------------
    plan_id = (cfg.get("plan_override") or {}).get(period) or N[period]
    res["plan_id"] = plan_id
    sheet = store.signals.get(ip)
    plan = None
    if not plan_id:
        rep.error(f"NodeShapeFile {PERIODS[period]} is empty for node {node}.")
    elif not re.match(r"^#\d+$", plan_id):
        rep.error(f"{PERIODS[period]} = '{plan_id}' is not a fixed-time plan number (#n). "
                  f"Add \"plan_override\": {{\"{period}\": \"#n\"}} to the config if a fixed plan applies.")
    if sheet is None:
        rep.error(f"No sheet for {ip} in Signal Phasing plan.xlsx.")
    else:
        res["sheet"] = {"name": sheet["sheet"], "intersection": sheet["name"], "region": sheet["region"],
                        "utc_no": sheet["utc_no"], "plans": list(sheet["plans"].keys())}
        if sheet.get("g_numbered"):                                   # CHANGE 3: check warning
            rep.warn("Signal groups G1, G2 ... on this sheet read as A, B ... - check they match "
                     "the letters on the stage drawing.")
        if plan_id and plan_id in sheet["plans"]:
            plan = sheet["plans"][plan_id]
            if not plan["cycle"]:
                rep.error(f"Plan {plan_id} on sheet {sheet['sheet']} has no cycle time."); plan = None
            elif not plan["groups"]:
                rep.error(f"Plan {plan_id} on sheet {sheet['sheet']} has no signal-group times."); plan = None
            else:
                res["plan"] = plan
                for k, g in plan["groups"].items():
                    if None in (g["start"], g["end"]):
                        rep.error(f"Signal group {k}: start/end missing in plan {plan_id}.")
                    elif g["gtime"] is not None and (g["end"] - g["start"]) % plan["cycle"] != g["gtime"]:
                        rep.warn(f"Signal group {k}: End-Start = {(g['end'] - g['start']) % plan['cycle']:.0f} s "
                                 f"but G.Time = {g['gtime']:.0f} s in plan {plan_id}.")
        elif plan_id and re.match(r"^#\d+$", plan_id):
            rep.error(f"Plan {plan_id} not found on sheet {sheet['sheet']}.")

    # --- arms ---------------------------------------------------------------
    arms = detect_arms(store, node, rep)
    res["arms_detected"] = arms
    return res, (rep, cfg, node, sheet, plan, arms)


def suggested_config(store, ip_text):
    """Config filled automatically from:
       CUBE network (nodes, arms, link lanes), stage picture (stages, letters, lanes,
       slip-lane hint).  Values that are still unknown stay null."""
    import stage_reader as sr
    res, ctx = analyse(store, ip_text, "AM")
    if ctx is None:
        return res, None
    rep, cfg, node, sheet, plan, arms = ctx
    ip = res["ip"]
    skel = {
        "ip": ip,
        "node": node,
        "confirmed": False,
        "drawing": None,
        "intersection_name": None,
        "approach1": "W" if any(a["dir"] == "W" for a in arms) else (arms[0]["dir"] if arms else None),
        "stages": {},
        "lanes_source": "Layout folder - pending",
        "exit_lanes_source": f"CUBE ShapeFile {LANE_FIELD}",
        "arms": {},
        "_signal_groups_on_sheet": sheet["group_order"] if sheet else None,
    }
    pic = store.pictures.get(ip)
    pic_cfg, notes = {"arms": {}}, []
    if pic and pic.get("ok"):
        pic_cfg, notes = sr.suggest_from_picture(pic, arms)
        skel["stages"] = pic_cfg.get("stages") or {}
        drawn = {g for a in pic_cfg["arms"].values() for g in (a["left_group"], a["through_group"]) if g}
        staged = {g for v in skel["stages"].values() for g in v}
        if drawn - staged:
            notes.append(f"Letters on the drawing but in no stage: {sorted(drawn - staged)} - check reading.")
        if staged - drawn:
            notes.append(f"Letters in stages but not on any arm: {sorted(staged - drawn)} - check reading.")
    elif pic:
        notes = pic.get("notes", [])
    else:
        notes = ["No stage picture found for this junction - fill stages and arms manually."]
    for a in arms:
        p = pic_cfg["arms"].get(a["dir"], {})
        free = p.get("right_free")
        skel["arms"][a["dir"]] = {
            "exit_node": a["exit"], "entry_node": a["entry"],
            "street": None, "to_from": a["neighbour_ip"],
            "left_group": p.get("left_group"), "through_group": p.get("through_group"),
            "right_group": p.get("right_group"),
            # STRICT SOURCE RULE: lane counts are populated by layout_source.py only.
            "left_lanes": None, "through_lanes": None,
            "right_lanes": None,
            "right_free": free,
            "free_turn_entry_m": None if free else None,
            "free_turn_exit_m": None if free else None,
            "exit_lanes": a["exit_link_lanes"],
            "_cube_entry_link_lanes": a["entry_link_lanes"],
            "_picture_total_lanes": None,
            "_picture_letters": p.get("_picture_letters"),
        }
    skel["_picture_notes"] = notes
    res["picture_notes"] = notes
    return res, skel


# ----------------------------------------------------------------------------
# 7. BUILD THE TXT
# ----------------------------------------------------------------------------
def build_junction(store, ip_text, period):
    res, ctx = analyse(store, ip_text, period)
    if ctx is None:
        return res, None
    rep, cfg, node, sheet, plan, arms = ctx
    ip = res["ip"]

    if not cfg:
        rep.error(f"No config file junction_configs/{ip}.json. Download the suggested config, "
                  f"fill the null values from the drawing / stage picture, and save it.")
        res["report"] = rep.as_dict(); return res, None

    # ---------- config checks ----------
    def need(obj, key, where):
        if obj.get(key) in (None, "", []):
            rep.error(f"Config {where}: '{key}' is missing.")
            return False
        return True

    for k in ("drawing", "intersection_name"):
        if not cfg.get(k):
            rep.warn(f"Config '{k}' is empty - written as NOT GIVEN in the TXT header.")
    if cfg.get("confirmed") is False:
        rep.warn("Config was auto-read from the stage picture / CUBE and is not yet confirmed "
                 "(set \"confirmed\": true after checking).")
    stages = {}
    for k, v in (cfg.get("stages") or {}).items():
        if not v:
            rep.error(f"Config stage {k}: signal groups missing.")
            continue
        if not str(k).isdigit():
            rep.error(f"Config stage '{k}' must be a number (UTC stage)."); continue
        stages[int(k)] = [str(x).upper() for x in v]
    if not stages:
        rep.error("Config 'stages' is empty.")
    if any(s >= 10 for s in stages):
        rep.warn("Stage numbers >= 10: PHASES = 'xy' concatenation becomes ambiguous.")

    stages, checked_stage_info = _joint_stage_timing(
        plan, stages, cfg.get("arms") or {}, rep)
    cfg_arms = cfg.get("arms") or {}
    det = {a["dir"]: a for a in arms}
    for d in cfg_arms:
        if d not in det:
            rep.error(f"Config arm '{d}' not found in CUBE network (detected: {list(det)}).")
    for d in det:
        if d not in cfg_arms:
            rep.error(f"CUBE arm '{d}' ({det[d]['exit']},{det[d]['entry']}) is not in the config.")

    groups = plan["groups"] if plan else {}
    cycle = plan["cycle"] if plan else None
    used_groups = set()

    arm_rows = []
    for a in order_arms(arms, cfg.get("approach1") or (arms[0]["dir"] if arms else None)):
        c = cfg_arms.get(a["dir"])
        if c is None:
            continue
        where = f"arm {a['dir']}"
        for key, val in (("exit_node", a["exit"]), ("entry_node", a["entry"])):
            if c.get(key) is not None and int(c[key]) != val:
                rep.error(f"Config {where}: {key} {c[key]} but CUBE network gives {val}.")
        if not c.get("street"):
            rep.warn(f"Config {where}: 'street' is empty - written as NOT GIVEN (comment line only).")
        if c.get("to_from") and a["neighbour_ip"] and norm_ip(c["to_from"]) != norm_ip(a["neighbour_ip"]):
            rep.note(f"{where}: config to_from {c['to_from']} - network walk found {a['neighbour_ip']}.")
        row = {"det": a, "cfg": c, "mv": {}}
        for mv in ("left", "through", "right"):
            g = c.get(f"{mv}_group")
            if g is None:
                rep.note(f"{where}: no {mv} movement coded (group is null).")
                continue
            g = str(g).upper()
            if plan and g not in groups:
                rep.error(f"Config {where}: {mv} group '{g}' not in plan {res.get('plan_id')}.")
            st = sorted([s for s, mem in stages.items() if g in mem])
            if not st:
                rep.error(f"Config {where}: group '{g}' ({mv}) is not in any stage.")
            lanes = c.get(f"{mv}_lanes")
            if lanes is None:
                rep.error(f"Config {where}: '{mv}_lanes' is missing.")
            used_groups.add(g)
            row["mv"][mv] = {"group": g, "stages": st, "lanes": lanes}
        if "right" in row["mv"]:
            if c.get("right_free") is None:
                rep.error(f"Config {where}: 'right_free' (true/false) is missing.")
            elif c["right_free"]:
                for k in ("free_turn_entry_m", "free_turn_exit_m"):
                    v = c.get(k) if c.get(k) is not None else cfg.get(k)
                    if v is None:
                        rep.error(f"Config {where}: free right turn but '{k}' missing - "
                                  f"measure it on the satellite map.")
        # exit lanes: config override first, otherwise CUBE exit link
        if c.get("exit_lanes") is not None:
            row["exit_lanes"] = c["exit_lanes"]
            if a["exit_link_lanes"] is not None and float(c["exit_lanes"]) != a["exit_link_lanes"]:
                rep.warn(f"{where}: config exit_lanes {c['exit_lanes']} vs CUBE exit link "
                         f"{LANE_FIELD} {a['exit_link_lanes']:g}.")
        elif a["exit_link_lanes"] is not None:
            row["exit_lanes"] = a["exit_link_lanes"]
        else:
            row["exit_lanes"] = None
            rep.error(f"{where}: exit lanes not found in ShapeFile and not in config.")
        if (a["exit_downstream_lanes"] is not None and a["exit_link_lanes"] is not None
                and a["exit_downstream_lanes"] != a["exit_link_lanes"]):
            rep.warn(f"{where}: exit link {LANE_FIELD}={a['exit_link_lanes']:g} but downstream link "
                     f"={a['exit_downstream_lanes']:g}.")
        arm_rows.append(row)

    # stage greens: validated jointly before movement-to-stage mapping.
    stage_info = checked_stage_info

    # entry-lane check vs CUBE link
    for r in arm_rows:
        pic = sum((r["mv"][m]["lanes"] or 0) for m in ("left", "through") if m in r["mv"])
        r["picture_lanes"] = pic
        el = r["det"]["entry_link_lanes"]
        if el is not None and pic and el != pic:
            rep.warn(f"Arm {r['det']['dir']}: {LANE_FIELD} of entry link = {el:g}, picture shows {pic}.")

    res["report"] = rep.as_dict()
    if rep.errors:
        return res, None

    txt = render(res, cfg, sheet, plan, arm_rows, stages, stage_info, node)
    res["filename"] = f"{ip}_Node{node}_Junction_{period}.txt"
    return res, txt


def _join_and(items):
    items = list(items)
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def render(res, cfg, sheet, plan, arm_rows, stages, stage_info, node):
    K = dict(TEMPLATE_CONSTANTS, **(cfg.get("constants") or {}))
    cycle = plan["cycle"]
    ip = res["ip"]
    L = []
    L.append(RULE)
    L.append("; JUNCTION FILE - CUBE VOYAGER (FIXED TIME SIGNAL)")
    utc = f"  (UTC No. {sheet['utc_no']})" if sheet.get("utc_no") else ""
    L.append(f"; IP ID         : {pretty_ip(ip)}{utc}  Drawing {cfg.get('drawing') or 'NOT GIVEN'}")
    L.append(f"; Cube NODE     : {node}")
    L.append(f"; Intersection  : {cfg.get('intersection_name') or 'NOT GIVEN'}")
    L.append(f"; Region        : {sheet.get('region') or cfg.get('region') or 'NOT GIVEN'}")
    L.append(f"; Period        : {res['period']}   Signal Plan {res['plan_id']} ({plan['sipl']})   "
             f"Cycle = {cycle:g} s")
    L.append(";" + "-" * 69)

    # approaches
    L.append("; APPROACHES (exit node, entry node)")
    for r in arm_rows:
        a, c = r["det"], r["cfg"]
        sig = " ".join(f"{r['mv'][m]['group']}={m.capitalize()}" for m in ("left", "through") if m in r["mv"])
        tf = c.get("to_from") or a["neighbour_ip"]
        tf = f"(to/from {tf}) " if tf else ""
        L.append(f";   {a['exit']},{a['entry']} : {DIR_NAMES[a['dir']].ljust(5)} - "
                 f"{(c.get('street') or 'NOT GIVEN').ljust(24)}{tf}{sig}".rstrip())

    # phases
    order = sorted(stage_info, key=lambda s: stage_info[s]["start"])
    grp_arm = {}
    for r in arm_rows:
        for m in r["mv"].values():
            grp_arm.setdefault(m["group"], DIR_NAMES[r["det"]["dir"]])
    arm_order = [DIR_NAMES[r["det"]["dir"]] for r in arm_rows]
    L.append("; PHASES (Cube phase = UTC stage; Stage 0 all-red not coded)")
    for s in sorted(stage_info):
        mem = sorted(stages[s])
        owners = sorted({grp_arm.get(g) for g in mem if grp_arm.get(g)}, key=arm_order.index)
        L.append(f";   Phase {s} = Stage {s} : {'/'.join(owners).ljust(11)} {'/'.join(mem)}  "
                 f"green {stage_info[s]['green']} s")
    multi = []
    for g in sorted({g for mem in stages.values() for g in mem}):
        st = [s for s in order if g in stages[s]]
        if len(st) > 1:
            multi.append(f"{g} runs in stages {'+'.join(map(str, st))}" if not multi
                         else f"{g} in stages {'+'.join(map(str, st))}")
    run = f";   Running order {'-'.join(map(str, order))}"
    L.append(run + (" ; " + ", ".join(multi) if multi else ""))
    # intergreens between consecutive stages
    ig = []
    for i, s in enumerate(order):
        nxt = order[(i + 1) % len(order)]
        end = (stage_info[s]["start"] + stage_info[s]["green"]) % cycle
        ig.append(int((stage_info[nxt]["start"] - end) % cycle))
    total_ig = int(cycle - sum(w["green"] for w in stage_info.values()))
    detail = f"({ig[0]} s x {len(ig)})" if len(set(ig)) == 1 else "(" + "+".join(map(str, ig)) + ")"
    ped = [g for g in plan["groups"] if g not in {x for m in stages.values() for x in m}]
    ped_txt = ""
    if ped:
        letters = sorted(ped)
        is_range = (len(letters) > 2 and all(len(x) == 1 for x in letters)
                    and ord(letters[-1]) - ord(letters[0]) == len(letters) - 1)
        shown = f"{letters[0]}-{letters[-1]}" if is_range else ",".join(letters)
        ped_txt = f" ; ped groups {shown} not coded"
    L.append(f";   Between-stage time = {total_ig} s {detail}{ped_txt}")

    # lanes
    pics = {(r["mv"].get("left", {}).get("lanes"), r["mv"].get("through", {}).get("lanes")) for r in arm_rows}
    src = cfg.get("lanes_source", "Layout folder")
    exit_src = cfg.get("exit_lanes_source") or f"CUBE ShapeFile {LANE_FIELD}"
    exit_short = "NRN" if exit_src.upper().startswith("NRN") else "ShapeFile"
    if len(pics) == 1:
        l, t = next(iter(pics))
        L.append(f"; LANES per entry: Left {l or 0} + Through {t or 0} = {(l or 0) + (t or 0)} ({src}) ; "
                 f"Exit from {exit_short}")
    else:
        parts = [f"{DIR_NAMES[r['det']['dir']]} L{r['mv'].get('left', {}).get('lanes') or 0}"
                 f"+T{r['mv'].get('through', {}).get('lanes') or 0}" for r in arm_rows]
        L.append(f"; LANES per entry ({src}): {', '.join(parts)} ; Exit from {exit_short}")
    mism = {}
    for r in arm_rows:
        el = r["det"]["entry_link_lanes"]
        if el is not None and r["picture_lanes"] and el != r["picture_lanes"]:
            mism.setdefault((el, r["picture_lanes"]), []).append(r)
    for (el, pic), rows in mism.items():
        rows = sorted(rows, key=lambda r: DIR_NAMES[r["det"]["dir"]])
        L.append(f";   CHECK: {LANE_FIELD} = {el:g} on "
                 f"{_join_and(DIR_NAMES[r['det']['dir']] + ' (' + str(r['det']['entry']) + ')' for r in rows)} "
                 f"entries, Layout shows {pic}")
    free = [r for r in arm_rows if "right" in r["mv"] and r["cfg"].get("right_free")]
    fe = lambda r, k: r["cfg"].get(k) if r["cfg"].get(k) is not None else cfg.get(k)
    if free:
        who = "" if len(free) == len(arm_rows) else " (" + ", ".join(DIR_NAMES[r["det"]["dir"]] for r in free) + ")"
        L.append(f"; FREE RIGHT TURNS{who}: slip lane, runs in the same phase(s) as its own arm")
        vals = {(fe(r, "free_turn_entry_m"), fe(r, "free_turn_exit_m")) for r in free}
        if len(vals) == 1:
            e, x = next(iter(vals))
            L.append(f";   FREETURNENTRY = {fmt_m(e)} m, FREETURNEXIT = {fmt_m(x)} m")
        else:
            for r in free:
                L.append(f";   {DIR_NAMES[r['det']['dir']]}: FREETURNENTRY = {fmt_m(fe(r, 'free_turn_entry_m'))} m, "
                         f"FREETURNEXIT = {fmt_m(fe(r, 'free_turn_exit_m'))} m")
    else:
        L.append("; FREE RIGHT TURNS: none")
    L.append(f"; EXIT LANES from {exit_src} (LANES of exit link): " +
             ", ".join(f"{DIR_NAMES[r['det']['dir']]} {r['exit_lanes']:g}" for r in arm_rows))
    has_left = [r for r in arm_rows if r["mv"].get("left", {}).get("lanes")]
    if len(has_left) == len(arm_rows):
        L.append("; LEFT TURN: separate exclusive lane on every arm")
    else:
        L.append("; LEFT TURN: separate exclusive lane on " +
                 (", ".join(DIR_NAMES[r["det"]["dir"]] for r in has_left) or "no arm"))
    L.append(RULE)

    # ---------------- body ----------------
    L.append("JUNCTION,")
    L.append(f"  NODE = {node},")
    L.append("  TYPE = FixedTimeSignal,")
    L.append(f"  APPROACH1 = {arm_rows[0]['det']['exit']},")
    L.append(f"  LANEADJUST = {K['LANEADJUST']},")
    L.append(f"  CYCLETIME = {cycle:.2f},")
    for s in sorted(stage_info):
        L.append(f"  PHASE = {s},")
        L.append(f"    ACTUALGREEN = {stage_info[s]['green']:.2f},")
    for i, r in enumerate(arm_rows):
        a = r["det"]
        last = i == len(arm_rows) - 1
        L.append(f"  APPROACH = {a['exit']},{a['entry']},")
        L.append(f"    EXITLANES = {r['exit_lanes']:g},")
        L.append(f"    AVELANEWIDTH = {K['AVELANEWIDTH']:.2f},")
        L.append(f"    MINIMUMCAPACITY = {K['MINIMUMCAPACITY']:.2f},")
        L.append(f"    RANDOMNESS = {K['RANDOMNESS']:.2f},")
        L.append(f"    UTURNCONTROL = {K['UTURNCONTROL']},")
        for mv in ("left", "through", "right"):
            if mv not in r["mv"]:
                continue
            m = r["mv"][mv]
            L.append(f"    MOVEMENT = {mv.capitalize()},")
            L.append(f"      PHASES = {''.join(map(str, m['stages']))},")
            L.append(f"      EXCLUSIVELANES = {m['lanes']},")
            if mv == "right" and r["cfg"].get("right_free"):
                L.append(f"      FREETURNENTRY = {float(fe(r, 'free_turn_entry_m')):.2f},")
                L.append(f"      FREETURNEXIT = {float(fe(r, 'free_turn_exit_m')):.2f},")
        L.append("    MOVEMENT = UTurn" + ("" if last else ","))
    return "\n".join(L) + "\n"


def geo_info(store, ip_text):
    """junction + arm node positions in lat/lon for the satellite map"""
    ip = norm_ip(ip_text)
    rep = Report()
    cfg = load_config(ip) or {}
    node = pick_node(store, ip, cfg, rep)
    if node is None:
        return {"ip": ip, "error": rep.errors}
    N = store.nodes[node]
    lat, lon = utm_to_latlon(N["x"], N["y"])
    arms = detect_arms(store, node, rep)
    out = []
    for a in arms:
        e = store.nodes[a["exit"]]; n = store.nodes[a["entry"]]
        out.append({"dir": a["dir"], "exit": a["exit"], "entry": a["entry"],
                    "exit_latlon": utm_to_latlon(e["x"], e["y"]), "entry_latlon": utm_to_latlon(n["x"], n["y"]),
                    "cube_exit_lanes": a["exit_link_lanes"], "cube_entry_lanes": a["entry_link_lanes"]})
    jb = store.jbox.get(ip)
    return {"ip": ip, "node": node, "node_latlon": [lat, lon], "arms": out,
            "jbox_latlon": [float(jb["LAT"]), float(jb["LON"])] if jb and num(jb["LAT"]) else None}
