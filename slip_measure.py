"""
slip_measure.py  -  slip lanes (free right turns) from NRN FORMOFWAY = 9   (v3)
------------------------------------------------------------------------------
Rule (from the NRN coding):
  FORMOFWAY = 9  ->  right-turn slip lane link
  METERS         ->  its length, used directly for FREETURNENTRY and FREETURNEXIT

For every CUBE arm:
  * the right turn goes from arm A to the arm D on its right (A bearing - 90 deg)
  * the FORMOFWAY 9 links lying in the corner between D and A, close to the
    junction box, are that arm's slip lane (connected 9-links are joined)
  * FREETURNENTRY = FREETURNEXIT = sum of METERS of those links
  * right lanes   = LANES of the slip link
  * no FORMOFWAY 9 link in the corner -> right_free = false (turn through junction)
EXITLANES = NRN LANES of the outbound link leaving the junction box on each arm.

Input (data folder):
  NRN_Network.geojson      (needs FNODE, TNODE, ONEWAY, LANES, FORMOFWAY, METERS)
  NRN_JunctionBox.geojson  (SITE_ID)
If FORMOFWAY / METERS are not in the GeoJSON they are joined from
  NRN_2026_Q1_Network.dbf.csv  (by FNODE + TNODE).
"""
import csv
import json
import math
import pickle
from pathlib import Path

from shapely import wkt as _wkt
from shapely.geometry import LineString, Point, shape
from shapely.ops import transform

R_EARTH = 6371000.0
SEARCH_M = 300
SLIP_FOW = 9
MAX_GAP_TO_BOX_M = 60      # slip link must come this close to the junction box
MAX_MID_DIST_M = 150       # and its middle must be within this distance of the centre
CACHE_NAME = "nrn_geometry_v3.pkl"


def _prop(props, *names):
    low = {k.lower(): v for k, v in props.items()}
    for n in names:
        if n.lower() in low:
            return low[n.lower()]
    return None


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _bearing(x, y):
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def _adiff(a, b):
    d = abs(a - b) % 360
    return min(d, 360 - d)


def _head(p0, p1):
    return _bearing(p1[0] - p0[0], p1[1] - p0[1])


def _key(a, b):
    try:
        return (str(int(float(a))), str(int(float(b))))
    except (TypeError, ValueError):
        return None


class Local:
    def __init__(self, lat, lon):
        self.lat0, self.lon0 = lat, lon
        self.kx = math.cos(math.radians(lat)) * math.pi / 180 * R_EARTH
        self.ky = math.pi / 180 * R_EARTH

    def xy(self, lon, lat):
        return ((lon - self.lon0) * self.kx, (lat - self.lat0) * self.ky)

    def ll(self, x, y):
        return (self.lat0 + y / self.ky, self.lon0 + x / self.kx)


# ---------------------------------------------------------------- data
class SlipData:
    def __init__(self, data_dir, cache_dir, utm_to_latlon=None):
        self.data_dir, self.cache_dir = Path(data_dir), Path(cache_dir)
        self.utm = utm_to_latlon
        self.net_file = self.data_dir / "NRN_Network.geojson"
        self.box_file = self.data_dir / "NRN_JunctionBox.geojson"
        self.csv_file = self.data_dir / "NRN_2026_Q1_Network.dbf.csv"
        self.links, self.grid, self.boxes, self.status = [], {}, {}, []
        self._load()

    @property
    def ready(self):
        return bool(self.links)

    def _ll(self, c):
        x, y = c[0], c[1]
        if abs(x) > 180 or abs(y) > 90:
            if self.utm is None:
                raise ValueError("GeoJSON is not in lon/lat (EPSG:4326)")
            la, lo = self.utm(x, y)
            return (lo, la)
        return (x, y)

    def _csv_attrs(self):
        """(FNODE,TNODE) -> (FORMOFWAY, METERS) from the NRN CSV"""
        out = {}
        if not self.csv_file.exists():
            return out
        print("Joining FORMOFWAY / METERS from NRN CSV (first time only) ...")
        with open(self.csv_file, newline="", encoding="latin-1") as f:
            rdr = csv.DictReader(f)
            up = {c.upper(): c for c in rdr.fieldnames}
            need = ["FNODE", "TNODE", "FORMOFWAY", "METERS"]
            if not all(n in up for n in need):
                return out
            for r in rdr:
                k = _key(r[up["FNODE"]], r[up["TNODE"]])
                if k and k not in out:
                    out[k] = (_num(r[up["FORMOFWAY"]]), _num(r[up["METERS"]]))
        return out

    def _load(self):
        self.cache_dir.mkdir(exist_ok=True)
        pk = self.cache_dir / CACHE_NAME
        srcs = [p for p in (self.net_file, self.box_file) if p.exists()]
        if pk.exists() and all(pk.stat().st_mtime > p.stat().st_mtime for p in srcs):
            self.links, self.grid, self.boxes, self.status = pickle.loads(pk.read_bytes())
            return
        if not self.net_file.exists():
            self.status.append(f"{self.net_file.name} not found in data folder - slip lanes cannot be read.")
        else:
            print("Reading NRN network geometry (first time only) ...")
            gj = json.loads(self.net_file.read_text(encoding="utf-8", errors="ignore"))
            feats = gj.get("features", [])
            has_fow = bool(feats) and _prop(feats[0].get("properties") or {}, "FORMOFWAY") is not None \
                or any(_prop(f.get("properties") or {}, "FORMOFWAY") is not None for f in feats[:1000])
            extra = {} if has_fow else self._csv_attrs()
            if not has_fow and not extra:
                self.status.append("FORMOFWAY / METERS not in NRN_Network.geojson and NRN CSV not found - "
                                   "re-export the GeoJSON with FORMOFWAY and METERS fields.")
            for f in feats:
                g, p = f.get("geometry"), f.get("properties") or {}
                if not g:
                    continue
                parts = [g["coordinates"]] if g["type"] == "LineString" else \
                    g["coordinates"] if g["type"] == "MultiLineString" else []
                k = _key(_prop(p, "FNODE", "F_NODE"), _prop(p, "TNODE", "T_NODE"))
                if not k:
                    continue
                coords = [self._ll(c) for part in parts for c in part]
                if len(coords) < 2:
                    continue
                if has_fow:
                    fow, met = _num(_prop(p, "FORMOFWAY")), _num(_prop(p, "METERS"))
                else:
                    fow, met = extra.get(k, (None, None))
                rec = {"f": k[0], "t": k[1], "ow": str(_prop(p, "ONEWAY") or "").strip().upper(),
                       "lanes": _num(_prop(p, "LANES")), "fow": fow, "m": met, "c": coords}
                i = len(self.links)
                self.links.append(rec)
                for cell in {(int(c[0] / 0.005), int(c[1] / 0.005)) for c in coords}:
                    self.grid.setdefault(cell, []).append(i)
            n9 = sum(1 for l in self.links if l["fow"] == SLIP_FOW)
            self.status.append(f"NRN network geometry: {len(self.links)} links, {n9} FORMOFWAY 9 (right-turn) links.")
        if not self.box_file.exists():
            self.status.append(f"{self.box_file.name} not found - junction box unknown.")
        else:
            gj = json.loads(self.box_file.read_text(encoding="utf-8", errors="ignore"))
            for f in gj.get("features", []):
                p = f.get("properties") or {}
                sid = _prop(p, "SITE_ID")
                if not sid or not f.get("geometry"):
                    continue
                key = str(sid).replace(" ", "").replace("-", "").upper()
                key = key if key.startswith("IP") or not key[0].isdigit() else "IP" + key
                geom = transform(lambda x, y, z=None: self._ll((x, y)), shape(f["geometry"]))
                self.boxes[key] = geom.wkt
            self.status.append(f"NRN junction boxes: {len(self.boxes)}.")
        pk.write_bytes(pickle.dumps((self.links, self.grid, self.boxes, self.status)))

    def near(self, lat, lon, radius_m=SEARCH_M):
        d = radius_m / 111000 + 0.005
        seen = set()
        for gx in range(int((lon - d) / 0.005), int((lon + d) / 0.005) + 1):
            for gy in range(int((lat - d) / 0.005), int((lat + d) / 0.005) + 1):
                seen.update(self.grid.get((gx, gy), []))
        return [self.links[i] for i in seen]


# ---------------------------------------------------------------- measure
def _groups(links):
    """join FORMOFWAY 9 links that share a node"""
    parent = list(range(len(links)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]; i = parent[i]
        return i
    at = {}
    for i, l in enumerate(links):
        for n in (l["f"], l["t"]):
            if n in at:
                parent[find(i)] = find(at[n])
            else:
                at[n] = i
    g = {}
    for i in range(len(links)):
        g.setdefault(find(i), []).append(links[i])
    return list(g.values())


def _ordered_pts(group):
    """polyline of a connected group (walk from an end node)"""
    deg = {}
    for l in group:
        for n in (l["f"], l["t"]):
            deg[n] = deg.get(n, 0) + 1
    start = next((n for n, d in deg.items() if d == 1), group[0]["f"])
    pts, used, node = [], set(), start
    while len(used) < len(group):
        nxt = next((i for i, l in enumerate(group) if i not in used and node in (l["f"], l["t"])), None)
        if nxt is None:
            break
        l = group[nxt]; used.add(nxt)
        p = l["xy"] if l["f"] == node else l["xy"][::-1]
        pts += p if not pts else p[1:]
        node = l["t"] if l["f"] == node else l["f"]
    if len(used) < len(group):                       # not a simple chain - plain join
        pts = [p for l in group for p in l["xy"]]
    return pts


def measure_junction(data, ip, centre_latlon, cube_arms):
    out = {"ip": ip, "arms": {}, "notes": [] if data.ready else list(data.status)}
    if not data.ready:
        return out
    box_wkt = data.boxes.get(ip)
    if not box_wkt:
        out["notes"].append(f"{ip}: no NRN junction box polygon - slip lanes not read.")
        return out
    box_ll = _wkt.loads(box_wkt)
    lat, lon = box_ll.centroid.y, box_ll.centroid.x
    loc = Local(lat, lon)
    box = transform(lambda x, y, z=None: loc.xy(x, y), box_ll)

    near = []
    for l in data.near(lat, lon, SEARCH_M):
        xy = [loc.xy(*c) for c in l["c"]]
        if LineString(xy).distance(Point(0, 0)) <= SEARCH_M:
            near.append(dict(l, xy=xy))

    arms = []
    for a in cube_arms:
        ex = loc.xy(a["exit_latlon"][1], a["exit_latlon"][0])
        en = loc.xy(a["entry_latlon"][1], a["entry_latlon"][0])
        arms.append({"dir": a["dir"], "b": _bearing((ex[0] + en[0]) / 2, (ex[1] + en[1]) / 2)})

    # ---- FORMOFWAY 9 slip groups belonging to this junction
    nine = [l for l in near if l["fow"] == SLIP_FOW
            and LineString(l["xy"]).distance(box) <= MAX_GAP_TO_BOX_M]
    if not any(l["fow"] is not None for l in near):
        out["notes"].append("FORMOFWAY not available for NRN links - slip lanes not read.")
    groups = []
    for g in _groups(nine):
        pts = _ordered_pts(g)
        ln = LineString(pts)
        mid = ln.interpolate(0.5, normalized=True)
        if math.hypot(mid.x, mid.y) > MAX_MID_DIST_M or ln.intersects(box.buffer(-1)):
            continue
        groups.append({"links": g, "pts": pts, "mid_b": _bearing(mid.x, mid.y),
                       "dist": ln.distance(box)})

    used = set()
    for A in arms:
        res = {"right_free": None, "free_turn_entry_m": None, "free_turn_exit_m": None,
               "exit_lanes": None, "right_lanes": None, "slip_path": None, "notes": []}
        out["arms"][A["dir"]] = res
        tgt = (A["b"] - 90) % 360
        D = min(arms, key=lambda x: _adiff(x["b"], tgt))
        if D is A or _adiff(D["b"], tgt) > 55:
            res["right_free"] = False
            res["notes"].append("No arm to the right - no right turn.")
            continue
        res["right_to"] = D["dir"]
        corner = (A["b"] - D["b"]) % 360
        mine = [i for i, g in enumerate(groups) if 0 < (g["mid_b"] - D["b"]) % 360 < corner]
        if not mine:
            res["right_free"] = False
            res["notes"].append("No FORMOFWAY 9 link in this corner - right turn coded through the junction.")
            continue
        if len(mine) > 1:
            res["notes"].append(f"{len(mine)} separate FORMOFWAY 9 slips in this corner - nearest used, check.")
        gi = min(mine, key=lambda i: groups[i]["dist"])
        used.add(gi)
        g = groups[gi]
        meters = [l["m"] for l in g["links"]]
        res["right_free"] = True
        res["slip_links"] = [{"FNODE": l["f"], "TNODE": l["t"], "METERS": l["m"], "LANES": l["lanes"]}
                             for l in g["links"]]
        if any(m is None for m in meters):
            res["notes"].append("FORMOFWAY 9 link without METERS - length not set.")
        else:
            total = round(sum(meters), 2)
            res["free_turn_entry_m"] = total
            res["free_turn_exit_m"] = total
            res["slip_length_m"] = total
        lanes = [l["lanes"] for l in g["links"] if l["lanes"]]
        res["right_lanes"] = int(min(lanes)) if lanes else None
        pts = g["pts"]
        if _adiff(_bearing(*pts[0]), A["b"]) > _adiff(_bearing(*pts[-1]), A["b"]):
            pts = pts[::-1]                          # start on the approach side
        res["slip_path"] = [loc.ll(x, y) for x, y in pts]
        res["diverge_latlon"], res["merge_latlon"] = loc.ll(*pts[0]), loc.ll(*pts[-1])

    unused = [g for i, g in enumerate(groups) if i not in used]
    if unused:
        out["notes"].append(f"{len(unused)} FORMOFWAY 9 link group(s) near the junction not matched to an arm.")

    # ---- exit lanes: outbound link crossing the box edge along each arm
    edge_zone = box.boundary.buffer(5)
    for A in arms:
        best = None
        for l in near:
            if l["ow"] == "N" or not l["lanes"] or l["fow"] == SLIP_FOW:
                continue
            for pts, ok in ((l["xy"], l["ow"] in ("FT", "", "B", "BOTH")),
                            (l["xy"][::-1], l["ow"] in ("TF", "", "B", "BOTH"))):
                if not ok:
                    continue
                p0, p1 = pts[0], pts[-1]
                if Point(p1).distance(box) < 2 or not LineString(pts).intersects(edge_zone):
                    continue
                if _adiff(_bearing(*p1), A["b"]) > 25 or _adiff(_head(p0, p1), A["b"]) > 35:
                    continue
                d0 = Point(p0).distance(box.boundary)
                if best is None or d0 < best[0]:
                    best = (d0, l["lanes"])
        if best:
            out["arms"][A["dir"]]["exit_lanes"] = best[1]
        else:
            out["arms"][A["dir"]]["notes"].append("Exit link not found in NRN - CUBE value kept.")
    return out
