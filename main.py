"""
main.py  -  Junction Workbench backend (FastAPI)
Run:  python -m uvicorn main:app --reload
Open: http://127.0.0.1:8000/frontend/          (one junction)
      http://127.0.0.1:8000/frontend/batch.html (many junctions from Excel)

CHANGES (29-Oct):
  * Lane configuration now comes from the Layout folder (layout_source.py), NOT from the stage drawing.
  * Stage drawings are used only for stage letters / PHASES.
  * Slip lanes only from NRN FORMOFWAY = 9, lengths from NRN METERS. No 50 m default anywhere;
    missing value = error flag, TXT is not written.
"""
import csv
import io
import json
import re
import zipfile
from datetime import datetime
from pathlib import Path
from fastapi import FastAPI, HTTPException, Body, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import junction_engine as je
import picture_source
import reader_fixes
import signal_sources
import auto_stages
import slip_measure as sm
import layout_source as ls                                   # NEW

app = FastAPI(title="Junction Workbench API")
BASE = Path(__file__).resolve().parent
OUTPUTS = BASE / "outputs"
GEOJSON_FILE = BASE / "Junctions.geojson"
geojson_data = {"features": []}
if GEOJSON_FILE.exists():
    with GEOJSON_FILE.open("r", encoding="utf-8") as f:
        geojson_data = json.load(f)

print("Loading CUBE / NRN / signal-plan inputs (first run builds the cache) ...")
store = je.Store()
slip = sm.SlipData(je.DATA, je.CACHE, je.utm_to_latlon)
for s in slip.status:
    print("  " + s)
for s in ls.status():                                        # NEW
    print("  " + s)
print("Inputs loaded.")


# ------------------------------------------------------------ automatic slip lanes (NRN FORMOFWAY 9 only)
def measure_slips(ip):
    geo = je.geo_info(store, ip)
    if geo.get("error"):
        return {"ip": je.norm_ip(ip), "arms": {}, "notes": geo["error"]}
    return sm.measure_junction(slip, geo["ip"], geo["node_latlon"], geo["arms"])


def apply_slips(cfg, meas):
    """Slip lane only when NRN FORMOFWAY = 9 supports it.
    FREETURNENTRY / FREETURNEXIT = NRN METERS.  Missing value stays None (flagged) - never 50 m."""
    cfg.pop("free_turn_entry_m", None)          # old top-level default (the 50 m) removed
    cfg.pop("free_turn_exit_m", None)
    used_nrn_exit = False
    meas_arms = meas.get("arms") or {}
    for d, arm in (cfg.get("arms") or {}).items():
        m = meas_arms.get(d)
        notes = []
        if m is None:
            arm["right_free"] = False
            arm["free_turn_entry_m"] = arm["free_turn_exit_m"] = None
            arm.pop("free_turn_source", None)
            notes.append("No NRN slip result for this arm - coded as normal right turn, validate.")
        else:
            notes += m.get("notes", [])
            if m.get("right_free") is True:
                e, x = m.get("free_turn_entry_m"), m.get("free_turn_exit_m")
                arm["right_free"] = True
                arm["free_turn_entry_m"], arm["free_turn_exit_m"] = e, x
                arm["free_turn_source"] = ls.SLIP_SRC
                if e is None or x is None:
                    notes.append("FORMOFWAY 9 slip found but NRN METERS missing - validate, enter real length.")
                if m.get("right_lanes"):
                    arm["right_lanes"] = int(m["right_lanes"])
            else:
                arm["right_free"] = False       # no FORMOFWAY 9 (or not confirmable) -> no slip coded
                arm["free_turn_entry_m"] = arm["free_turn_exit_m"] = None
                arm.pop("free_turn_source", None)
                if m.get("right_free") is None:
                    notes.append("NRN could not confirm a slip lane - coded as normal right turn, validate.")
            if m.get("exit_lanes"):
                arm["exit_lanes"] = int(m["exit_lanes"])
                used_nrn_exit = True
        arm["_slip_notes"] = notes
    if used_nrn_exit:
        cfg["exit_lanes_source"] = "NRN_2026_Q1_Network"
    cfg["_slip_notes"] = meas.get("notes", [])
    return cfg


_orig_suggest = je.suggested_config


def suggested_with_slips(st, ip_text):
    res, skel = _orig_suggest(st, ip_text)
    if skel is not None:
        apply_slips(skel, measure_slips(ip_text))
    return res, skel


je.suggested_config = suggested_with_slips

import nrn_lane_source   # noqa: E402  (exit lanes from NRN - unchanged)

# ---- NEW: lanes from the Layout folder. Installed AFTER every other patch, so it has the last word.
_prev_suggest = je.suggested_config


def suggested_with_layout(st, ip_text):
    res, skel = _prev_suggest(st, ip_text)
    if skel is not None:
        ls.apply_layout(skel, skel.get("ip") or je.norm_ip(ip_text))
        if "stage picture" in str(skel.get("lanes_source") or "").lower():
            skel["lanes_source"] = "Layout folder - NOT READ"
    return res, skel


je.suggested_config = suggested_with_layout

# ---- NEW: saved configs - drop the old top-level 50 m default whenever a config is loaded
_orig_load_config = je.load_config


def _apply_layout_preserving_values(cfg, ip):
    if not cfg:
        return cfg
    old_arms = {d: dict(a) for d, a in (cfg.get("arms") or {}).items()}
    old_source = str(cfg.get("lanes_source") or "")
    ls.apply_layout(cfg, je.norm_ip(ip))
    for d, arm in (cfg.get("arms") or {}).items():
        old = old_arms.get(d) or {}
        for field in ("left_lanes", "through_lanes", "right_lanes", "left_shared", "right_shared"):
            if arm.get(field) is None and old.get(field) is not None and "layout" in old_source.lower():
                arm[field] = old[field]
    if "stage picture" in str(cfg.get("lanes_source") or "").lower():
        cfg["lanes_source"] = "Layout folder - NOT READ"
    return cfg


def load_config_clean(ip):
    cfg = _orig_load_config(ip)
    if cfg:
        cfg.pop("free_turn_entry_m", None)
        cfg.pop("free_turn_exit_m", None)
        _apply_layout_preserving_values(cfg, ip)
    return cfg


je.load_config = load_config_clean

# ---- NEW: block the TXT when lanes came from the stage picture or a slip length is missing / assumed
_orig_build = je.build_junction


def build_with_source_checks(st, ip_text, period):
    ip = je.norm_ip(ip_text)
    problems = ls.config_problems(je.load_config(ip) or {})
    try:
        res, txt = _orig_build(st, ip_text, period)
    except Exception as exc:                                  # e.g. float(None) on a missing slip length
        if not problems:
            raise
        res, txt = {"ip": ip, "report": {"errors": [], "warnings": []}}, None
    if problems:
        rep = res.setdefault("report", {})
        rep.setdefault("errors", [])
        rep["errors"] = list(rep["errors"]) + problems
        txt = None
    return res, txt


je.build_junction = build_with_source_checks


def strip_info(cfg):
    cfg = {k: v for k, v in cfg.items() if not k.startswith("_")}
    for arm in (cfg.get("arms") or {}).values():
        for k in [k for k in arm if k.startswith("_")]:
            arm.pop(k)
    return cfg


def keep_manual_values(new, saved):
    """when re-running auto-fill, keep what only the engineer typed"""
    for k in ("drawing", "intersection_name"):
        if saved.get(k):
            new[k] = saved[k]
    for d, arm in (new.get("arms") or {}).items():
        old = (saved.get("arms") or {}).get(d, {})
        if old.get("street"):
            arm["street"] = old["street"]
    return new


# ------------------------------------------------------------ endpoints (single junction)
@app.get("/")
def home():
    return {"message": "Junction API is working", "app": "/frontend/", "batch": "/frontend/batch.html"}


@app.get("/junction/{junction_id}")
def get_junction(junction_id: str):
    for feature in geojson_data.get("features", []):
        properties = feature.get("properties", {})
        if je.norm_ip(properties.get("SITE_ID", "")) == je.norm_ip(junction_id):
            return {"junction_id": junction_id, "properties": properties,
                    "geometry": feature.get("geometry")}
    raise HTTPException(status_code=404, detail=f"Junction {junction_id} not found")


@app.get("/api/junctions")
def list_junctions():
    out = []
    layouts = ls.build_index()
    for ip, nodes in sorted(store.ip_nodes.items()):
        n = store.nodes[nodes[0]]
        out.append({"ip": ip, "nodes": nodes, "AM": n["AM"], "OP": n["OP"], "PM": n["PM"],
                    "signal_sheet": ip in store.signals,
                    "picture_stages": len((store.pictures.get(ip) or {}).get("stages") or {}),
                    "layout_file": ip in layouts,                       # NEW
                    "config": (je.CONFIGS / f"{ip}.json").exists()})
    return out


@app.get("/api/junction/{ip}/check")
def check(ip: str, period: str = "AM"):
    res, _ = je.build_junction(store, ip, period)
    return res


@app.get("/api/junction/{ip}/config")
def get_config(ip: str):
    cfg = je.load_config(je.norm_ip(ip))
    if cfg is None:
        res, skel = je.suggested_config(store, ip)
        if skel is None:
            raise HTTPException(404, detail=res.get("report"))
        return {"source": "suggested (auto-detected, check it)", "config": skel}
    return {"source": "saved", "config": cfg}


@app.get("/api/junction/{ip}/auto")
def auto_config(ip: str):
    res, skel = je.suggested_config(store, ip)
    if skel is None:
        raise HTTPException(404, detail=res.get("report"))
    keep_manual_values(skel, je.load_config(je.norm_ip(ip)) or {})
    return {"source": "auto-read (stage letters + CUBE + NRN + Layout folder) - check", "config": skel,
            "picture_notes": skel.get("_picture_notes", []), "slip_notes": skel.get("_slip_notes", []),
            "layout_notes": skel.get("_layout_notes", [])}


@app.get("/api/junction/{ip}/layout")                           # NEW: what was read from the Layout folder
def layout_info(ip: str):
    geo = je.geo_info(store, ip)
    return ls.read_layout(ip, [a["dir"] for a in geo.get("arms", [])])


@app.get("/api/junction/{ip}/slip")
def slip_lanes(ip: str):
    return measure_slips(ip)


@app.get("/api/junction/{ip}/picture")
def picture(ip: str, annotated: bool = True):
    """Return the stage-plan image for a junction, creating the cache if needed."""
    import stage_reader as sr

    ip = je.norm_ip(ip).upper()
    picture_dir = je.CACHE / "pictures"
    picture_dir.mkdir(parents=True, exist_ok=True)

    def find_cached():
        found = []
        if je.CACHE.exists():
            for f in je.CACHE.rglob("*.png"):
                text = str(f).upper()
                if ip in f.name.upper() and "LAYOUT_CHECK" not in text:
                    found.append(f)
        found.sort(key=lambda f: (
            f.name.upper().startswith("ANNOTATED_"),
            "UPSCALED" not in f.name.upper(),
            -f.stat().st_mtime_ns,
        ))
        return found[0] if found else None

    pic = store.pictures.get(ip) or {}
    png = find_cached()

    # Search can load a saved junction config before the signal-plan picture
    # cache exists. Build/refresh that cache here so the picture endpoint works
    # without requiring the user to press Auto-fill first.
    if png is None:
        try:
            je.ensure_picture_cache(store)
        except Exception:
            pass
        pic = store.pictures.get(ip) or {}
        png = find_cached()

    # If the engine cache builder did not leave a matching file, directly scan
    # the signal-plan workbook(s) using the same stage reader, then reload the
    # store picture metadata. This changes display/cache creation only.
    if png is None:
        try:
            workbooks = list(je.DATA.rglob("*.xlsx")) + list(je.DATA.rglob("*.xlsm"))
            for wb in workbooks:
                try:
                    made = sr.extract_sheet_pictures(wb, picture_dir)
                except Exception:
                    continue
                for sheet_name, image_path in (made or {}).items():
                    if je.norm_ip(str(sheet_name)).upper() == ip:
                        png = Path(image_path)
                        break
                if png is not None and png.exists():
                    break
            try:
                je.ensure_picture_cache(store)
            except Exception:
                pass
            pic = store.pictures.get(ip) or pic
            png = png if png is not None and png.exists() else find_cached()
        except Exception:
            png = find_cached()

    if png is None or not png.exists():
        raise HTTPException(404, f"No stage picture could be extracted for {ip}")

    if not annotated or not pic or not pic.get("ok"):
        return FileResponse(png, media_type="image/png")

    out = picture_dir / f"annotated_{ip}.png"
    try:
        if not out.exists() or out.stat().st_mtime_ns < png.stat().st_mtime_ns:
            sr.annotate(png, pic, out)
        return FileResponse(out, media_type="image/png")
    except Exception:
        return FileResponse(png, media_type="image/png")


@app.get("/api/junction/{ip}/geo")
def geo(ip: str):
    info = je.geo_info(store, ip)
    for feature in geojson_data.get("features", []):
        props = feature.get("properties", {})
        if je.norm_ip(props.get("SITE_ID", "")) == je.norm_ip(ip):
            info["geojson"] = {"properties": props, "geometry": feature.get("geometry")}
            break
    return info


@app.post("/api/junction/{ip}/config")
def post_config(ip: str, cfg: dict = Body(...)):
    ip = je.norm_ip(ip)
    cfg = strip_info(cfg)
    cfg["ip"] = ip
    _apply_layout_preserving_values(cfg, ip)
    je.save_config(ip, cfg)
    res, _ = je.build_junction(store, ip, "AM")
    return {"saved": True, "check_AM": res["report"]}


@app.get("/api/junction/{ip}/txt")
def download_txt(ip: str, period: str = "AM"):
    res, txt = je.build_junction(store, ip, period)
    if txt is None:
        return JSONResponse(status_code=422, content=res)
    return PlainTextResponse(
        txt, headers={"Content-Disposition": f'attachment; filename="{res["filename"]}"'})


def _zip_response(items, zipname):
    buf = io.BytesIO()
    rep = io.StringIO()
    w = csv.writer(rep)
    w.writerow(["IP", "Period", "Status", "Errors", "Warnings"])
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for ip, period in items:
            res, txt = je.build_junction(store, ip, period)
            r = res.get("report", {})
            if txt:
                z.writestr(res["filename"], txt)
            w.writerow([ip, period, "OK" if txt else "NOT GENERATED",
                        " | ".join(r.get("errors", [])), " | ".join(r.get("warnings", []))])
        z.writestr("validation_report.csv", rep.getvalue())
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="{zipname}"'})


@app.get("/api/junction/{ip}/zip")
def download_zip(ip: str):
    ip = je.norm_ip(ip)
    return _zip_response([(ip, p) for p in je.PERIODS], f"{ip}_AM_OP_PM.zip")


@app.get("/api/batch")
def batch(only_configured: bool = True):
    ips = sorted(store.ip_nodes)
    if only_configured:
        ips = [ip for ip in ips if (je.CONFIGS / f"{ip}.json").exists()]
    if not ips:
        raise HTTPException(404, "No junction configs saved yet.")
    return _zip_response([(ip, p) for ip in ips for p in je.PERIODS], "Junction_files_batch.zip")


# ------------------------------------------------------------ batch from Excel list (unchanged)
HEADER_NAMES = {"junctionid", "junction", "junctions", "ip", "ipid", "ipno", "ipcode",
                "siteid", "id", "junctionno", "intersection", "intersectionid"}


def read_junction_ids(data: bytes, filename: str):
    """Junction IDs from .xlsx / .xlsm / .csv / .txt.
    Uses the column headed 'Junction ID' (or IP, IP_NO, SITE_ID ...); otherwise the first column."""
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        rows = [list(r) for r in wb.worksheets[0].iter_rows(values_only=True)]
    elif name.endswith((".csv", ".txt")):
        text = data.decode("utf-8-sig", errors="ignore")
        rows = list(csv.reader(io.StringIO(text)))
    elif name.endswith(".xls"):
        raise ValueError("Old .xls format is not supported - in Excel use File > Save As > .xlsx")
    else:
        raise ValueError("Upload an Excel (.xlsx) or CSV file")
    col, start = 0, 0
    for i, r in enumerate(rows[:10]):
        hit = [j for j, v in enumerate(r) if isinstance(v, str)
               and re.sub(r"[\s_\-.]", "", v).lower() in HEADER_NAMES]
        if hit:
            col, start = hit[0], i + 1
            break
    return split_ids([r[col] for r in rows[start:] if col < len(r)])


def split_ids(values):
    """clean, split 'IP58B, IP57P' cells, remove duplicates (order kept)"""
    out, seen = [], set()
    for v in values:
        if v is None:
            continue
        for part in re.split(r"[,;\n\r\t]+", str(v)):
            part = part.strip()
            if not part:
                continue
            if isinstance(v, float) and v.is_integer():
                part = str(int(v))
            ip = je.norm_ip(part)
            if ip and ip not in seen:
                seen.add(ip); out.append({"input": part, "ip": ip})
    return out


@app.post("/api/batch/read-ids")
async def batch_read_ids(request: Request, filename: str = "list.xlsx"):
    """Upload the Excel file (raw body) -> list of junction IDs with status."""
    data = await request.body()
    try:
        items = read_junction_ids(data, filename)
    except Exception as exc:
        raise HTTPException(400, str(exc))
    return {"count": len(items), "junctions": [_id_status(x) for x in items]}


def _id_status(x):
    ip = x["ip"]
    return dict(x, in_network=ip in store.ip_nodes, signal_sheet=ip in store.signals,
                layout_file=ip in ls.build_index(),                                  # NEW
                config=(je.CONFIGS / f"{ip}.json").exists())


@app.post("/api/batch/run")
def batch_run(body: dict = Body(...)):
    """body: {"ids": ["IP58B", ...], "periods": ["AM","OP","PM"],
              "autofill": true  -> auto-fill junctions that have no saved config,
              "refill": false   -> re-run auto-fill also for junctions with a saved config}
    Writes the TXT files to outputs/Batch_<date_time>/ and a zip of the same."""
    items = split_ids(body.get("ids") or [])
    if not items:
        raise HTTPException(400, "No junction IDs given.")
    periods = [p for p in (body.get("periods") or list(je.PERIODS)) if p in je.PERIODS]
    autofill, refill = bool(body.get("autofill", True)), bool(body.get("refill", False))
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    folder = OUTPUTS / f"Batch_{stamp}"
    folder.mkdir(parents=True, exist_ok=True)
    rows, summary = [], {"junctions": len(items), "files": 0, "junctions_ok": 0,
                         "junctions_failed": 0, "auto_filled": 0}
    results = []
    for x in items:
        ip = x["ip"]
        jr = {"ip": ip, "input": x["input"], "config": "saved", "periods": {}}
        if ip not in store.ip_nodes:
            jr["config"] = "-"
            for p in periods:
                jr["periods"][p] = {"status": "NOT FOUND", "errors": [f"{ip} not in NodeShapeFile IP_NO"]}
                rows.append([x["input"], ip, "", p, "NOT FOUND", "", "", f"{ip} not in NodeShapeFile IP_NO", "", ""])
            summary["junctions_failed"] += 1
            results.append(jr)
            continue
        saved = je.load_config(ip)
        if (saved is None and autofill) or (saved is not None and refill):
            try:
                _, skel = je.suggested_config(store, ip)
                if skel is not None:
                    skel = keep_manual_values(strip_info(skel), saved or {})
                    if saved and saved.get("confirmed"):
                        skel["confirmed"] = False          # changed values -> check again
                    je.save_config(ip, skel)
                    jr["config"] = "auto-filled now"
                    summary["auto_filled"] += 1
            except Exception as exc:
                jr["config"] = f"auto-fill failed: {exc}"
        elif saved is None:
            jr["config"] = "none"
        cfg = je.load_config(ip) or {}
        ok_all = True
        for p in periods:
            try:
                res, txt = je.build_junction(store, ip, p)
            except Exception as exc:
                res, txt = {"report": {"errors": [f"Program error: {exc}"], "warnings": []}}, None
            r = res.get("report", {})
            if txt:
                (folder / res["filename"]).write_text(txt, encoding="utf-8", newline="\n")
                summary["files"] += 1
                status = "OK"
            else:
                status = "NOT GENERATED"; ok_all = False
            jr["periods"][p] = {"status": status, "file": res.get("filename") if txt else None,
                                "plan": res.get("plan_id"), "errors": r.get("errors", []),
                                "warnings": r.get("warnings", [])}
            rows.append([x["input"], ip, res.get("node", ""), p, status, res.get("plan_id", ""),
                         "yes" if cfg.get("confirmed") else "no",
                         " | ".join(r.get("errors", [])), " | ".join(r.get("warnings", [])),
                         res.get("filename", "") if txt else ""])
        summary["junctions_ok" if ok_all else "junctions_failed"] += 1
        results.append(jr)
    with open(folder / "validation_report.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Input ID", "IP", "CUBE node", "Period", "Status", "Signal plan", "Confirmed",
                    "Errors (file blocked)", "Warnings (check)", "File"])
        w.writerows(rows)
    zpath = OUTPUTS / f"Batch_{stamp}.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for fpath in sorted(folder.iterdir()):
            z.write(fpath, fpath.name)
    return {"batch": f"Batch_{stamp}", "folder": str(folder), "zip": f"/api/batch/download/Batch_{stamp}.zip",
            "summary": summary, "results": results}


@app.get("/api/batch/download/{name}")
def batch_download(name: str):
    p = OUTPUTS / Path(name).name
    if not p.exists() or p.suffix != ".zip":
        raise HTTPException(404, "Batch file not found")
    return FileResponse(p, filename=p.name, media_type="application/zip")


@app.get("/api/batch/template")
def batch_template():
    """Excel template: one column 'Junction ID'"""
    import openpyxl
    from openpyxl.styles import Font, PatternFill
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Junctions"
    ws["A1"] = "Junction ID"
    ws["A1"].font = Font(bold=True, color="FFFFFF")
    ws["A1"].fill = PatternFill("solid", fgColor="1769AA")
    for i, v in enumerate(["IP58B", "IP57P", "IP57Q"], start=2):
        ws.cell(row=i, column=1, value=v)
    ws.column_dimensions["A"].width = 18
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": 'attachment; filename="Junction_list_template.xlsx"'})


@app.post("/api/rebuild-cache")
def rebuild_cache():
    global store, slip
    je.build_cache()
    for f in (je.CACHE / "pictures").glob("annotated_*"):
        f.unlink()
    for f in je.CACHE.glob("nrn_geometry*.pkl"):
        f.unlink()
    store = je.Store()
    slip = sm.SlipData(je.DATA, je.CACHE, je.utm_to_latlon)
    ls.build_index(force=True)                                    # NEW: re-scan the Layout folder
    return {"rebuilt": True}


@app.post("/api/rescan-layouts")                                  # NEW: after adding layout files
def rescan_layouts():
    ls.build_index(force=True)
    return {"status": ls.status()}


app.mount("/frontend", StaticFiles(directory=str(BASE / "frontend"), html=True), name="frontend")
