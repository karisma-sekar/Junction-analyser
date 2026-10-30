"""
nrn_lane_source.py - approach, exit and free-turn lanes from NRN
----------------------------------------------------------------
Use with Junction Workbench after the existing slip-lane wrapper is installed.

Reads:
  data/NRN_Network.geojson       geometry + FNODE/TNODE/ONEWAY/LANES/FORMOFWAY/METERS
  data/NRN_JunctionBox.geojson   junction polygons

Writes into the generated config:
  * exit_lanes            = LANES on the NRN outbound approach link
  * right_lanes           = LANES on the FORMOFWAY 9 slip link
  * free_turn_entry/exit  = METERS on the FORMOFWAY 9 slip link (via existing slip_measure)
  * entry_total_lanes     = LANES on the NRN inbound approach link
  * left/through split is calculated ONLY when one part is already known:
       - only one movement exists -> all entry lanes go to that movement
       - left lanes known -> through = total - left
       - through lanes known -> left = total - through
    If both are missing and both movements exist, the split remains missing. NRN contains
    total LANES but not movement allocation; the app will block instead of guessing.

Nothing changes in signal timing, stage coding, GeoJSON, or TXT formatting.
"""
import copy
import math
import threading
from pathlib import Path

from shapely.geometry import LineString, Point

import junction_engine as je
import slip_measure as sm

_lock = threading.Lock()
_data = None
_orig_suggest = je.suggested_config
_orig_build = je.build_junction
_orig_load = je.load_config


def _slip_data():
    global _data
    if _data is None:
        _data = sm.SlipData(je.DATA, je.CACHE, je.utm_to_latlon)
    return _data


def _bearing(x, y):
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def _adiff(a, b):
    d = abs(a - b) % 360
    return min(d, 360 - d)


def _local(lat0, lon0, lon, lat):
    r = 6371000.0
    return ((lon - lon0) * math.cos(math.radians(lat0)) * math.pi / 180 * r,
            (lat - lat0) * math.pi / 180 * r)


def _directions(link):
    """Yield directed coordinate lists according to NRN ONEWAY."""
    ow = str(link.get("ow") or "").strip().upper()
    coords = link.get("c") or []
    if len(coords) < 2 or ow == "N":
        return
    if ow in ("FT", "", "B", "BOTH"):
        yield coords
    if ow in ("TF", "", "B", "BOTH"):
        yield coords[::-1]


def arm_nrn_lanes(store, ip):
    """Return {arm: {entry_total_lanes, exit_lanes, notes}} using NRN geometry."""
    geo = je.geo_info(store, ip)
    if geo.get("error"):
        return {}, list(geo["error"])
    lat0, lon0 = geo["node_latlon"]
    links = _slip_data().near(lat0, lon0, 260)
    result, all_notes = {}, []

    for arm in geo["arms"]:
        d = arm["dir"]
        ex_lat, ex_lon = arm["exit_latlon"]
        en_lat, en_lon = arm["entry_latlon"]
        ex = _local(lat0, lon0, ex_lon, ex_lat)
        en = _local(lat0, lon0, en_lon, en_lat)
        arm_b = _bearing((ex[0] + en[0]) / 2, (ex[1] + en[1]) / 2)
        best_in = best_out = None

        for link in links:
            if link.get("fow") == 9 or not link.get("lanes"):
                continue
            for coords in _directions(link) or []:
                pts = [_local(lat0, lon0, lo, la) for lo, la in coords]
                line = LineString(pts)
                if line.distance(Point(0, 0)) > 90:
                    continue
                p0, p1 = pts[0], pts[-1]
                r0, r1 = math.hypot(*p0), math.hypot(*p1)
                head = _bearing(p1[0] - p0[0], p1[1] - p0[1])

                # inbound: travels toward centre, lies on this arm, heading opposite arm bearing
                if r1 < r0 and _adiff(_bearing(*p0), arm_b) <= 35 \
                        and _adiff(head, (arm_b + 180) % 360) <= 45:
                    score = line.distance(Point(0, 0)) + 0.05 * r1
                    if best_in is None or score < best_in[0]:
                        best_in = (score, int(float(link["lanes"])))

                # outbound: travels away from centre along this arm
                if r1 > r0 and _adiff(_bearing(*p1), arm_b) <= 35 \
                        and _adiff(head, arm_b) <= 45:
                    score = line.distance(Point(0, 0)) + 0.05 * r0
                    if best_out is None or score < best_out[0]:
                        best_out = (score, int(float(link["lanes"])))

        notes = []
        if best_in is None:
            notes.append("NRN inbound approach link not found; existing entry-lane values kept.")
        if best_out is None:
            notes.append("NRN outbound approach link not found; existing exit-lane value kept.")
        result[d] = {"entry_total_lanes": best_in[1] if best_in else None,
                     "exit_lanes": best_out[1] if best_out else None,
                     "notes": notes}
    return result, all_notes


def _split_entry_lanes(arm, total, arm_name, notes):
    """Fill movement lanes without inventing a left/through split."""
    if not total:
        return
    arm["entry_total_lanes"] = total
    lg, tg = arm.get("left_group"), arm.get("through_group")
    ll, tl = arm.get("left_lanes"), arm.get("through_lanes")

    if not lg and tg:
        arm["left_lanes"] = None
        arm["through_lanes"] = total
        notes.append(f"Arm {arm_name}: only Through exists, so all {total} NRN entry lanes assigned to Through.")
    elif lg and not tg:
        arm["left_lanes"] = total
        arm["through_lanes"] = None
        notes.append(f"Arm {arm_name}: only Left exists, so all {total} NRN entry lanes assigned to Left.")
    elif lg and tg:
        if ll is not None and tl is None and 0 <= int(ll) <= total:
            arm["through_lanes"] = total - int(ll)
            notes.append(f"Arm {arm_name}: NRN total {total} - Left {ll} = Through {total-int(ll)}.")
        elif tl is not None and ll is None and 0 <= int(tl) <= total:
            arm["left_lanes"] = total - int(tl)
            notes.append(f"Arm {arm_name}: NRN total {total} - Through {tl} = Left {total-int(tl)}.")
        elif ll is not None and tl is not None and int(ll) + int(tl) != total:
            notes.append(f"Arm {arm_name}: movement lanes {ll}+{tl} do not equal NRN entry LANES {total}; check.")
        elif ll is None and tl is None:
            notes.append(f"Arm {arm_name}: NRN gives {total} total entry lanes but not the Left/Through split. "
                         f"One movement lane count is still required; no value was assumed.")


def apply_nrn(store, ip, cfg):
    lanes, notes = arm_nrn_lanes(store, ip)
    for d, arm in (cfg.get("arms") or {}).items():
        n = lanes.get(d, {})
        _split_entry_lanes(arm, n.get("entry_total_lanes"), d, notes)
        if n.get("exit_lanes") is not None:
            arm["exit_lanes"] = n["exit_lanes"]
        notes += [f"Arm {d}: {x}" for x in n.get("notes", [])]
    if any(v.get("exit_lanes") is not None for v in lanes.values()):
        cfg["exit_lanes_source"] = "NRN_2026_Q1_Network"
    cfg["lanes_source"] = "NRN network (total entry lanes; movement split retained/derived only when known)"
    return notes


def suggested_config(store, ip_text):
    res, cfg = _orig_suggest(store, ip_text)
    if cfg is None:
        return res, cfg
    notes = apply_nrn(store, je.norm_ip(ip_text), cfg)
    cfg.setdefault("_picture_notes", []).extend(notes)
    res["nrn_lane_notes"] = notes
    return res, cfg


def build_junction(store, ip_text, period):
    ip = je.norm_ip(ip_text)
    cfg = _orig_load(ip)
    if cfg is None:
        return _orig_build(store, ip_text, period)
    work = copy.deepcopy(cfg)
    notes = apply_nrn(store, ip, work)
    with _lock:
        je.load_config = lambda x, _ip=ip, _c=work: _c if je.norm_ip(x) == _ip else _orig_load(x)
        try:
            res, txt = _orig_build(store, ip_text, period)
        finally:
            je.load_config = _orig_load
    res.setdefault("report", {}).setdefault("info", []).extend(notes)
    return res, txt


je.suggested_config = suggested_config
je.build_junction = build_junction
print("NRN lane source enabled: approach totals, exit lanes and FORMOFWAY 9 slips come from NRN.")
