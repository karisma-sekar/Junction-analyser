"""
signal_sources.py  -  read ALL area signal-timing workbooks instead of one file
------------------------------------------------------------------------------
Put every area workbook in:   Junction_API\\data\\signal_plans\\
  (Abu Dhabi_IP wise_Greentime_Details_25-01-2024.xlsx, Al Raha_..., Yas Island_..., ...)

What it does
  * reads every .xlsx / .xlsm in that folder (Excel lock files "~$..." are skipped)
  * each file is read ONCE and cached (cache\\signal_files\\). Adding / replacing a file
    later only reads that file again - the others come from the cache
  * the same IP in more than one file -> the file with the LATEST date in its name is used
    (date such as 25-01-2024, 10.02.2021, 2025); all duplicates are listed in
    cache\\signal_duplicates.csv
  * cache\\signal_sources_report.csv shows, per workbook: sheets read, IPs found,
    stage pictures read, sheets with problems
  * if the folder is empty / missing, the old single file data\\Signal Phasing plan.xlsx is used

Use: in main.py add ONE line directly under  "import junction_engine as je":
        import signal_sources
"""
import csv
import hashlib
import json
import re
import shutil
from datetime import date
from pathlib import Path

import junction_engine as je

SIGNAL_DIR = je.DATA / "signal_plans"
FILE_CACHE = je.CACHE / "signal_files"
PIC_DIR = je.CACHE / "pictures"
SOURCES_JSON = je.CACHE / "signal_sources.json"


# ---------------------------------------------------------------- helpers
def signal_files():
    files = []
    if SIGNAL_DIR.exists():
        files = sorted(p for p in SIGNAL_DIR.iterdir()
                       if p.suffix.lower() in (".xlsx", ".xlsm") and not p.name.startswith("~$"))
    if not files:
        single = je.DATA / "Signal Phasing plan.xlsx"
        if single.exists():
            files = [single]
    return files


def file_date(name):
    """date written in the file name (used to pick the newest file for a duplicate IP)"""
    m = re.search(r"(\d{1,2})[-._](\d{1,2})[-._](\d{4})", name)
    if m:
        d, mo, y = map(int, m.groups())
        try:
            return date(y, mo, d)
        except ValueError:
            pass
    m = re.search(r"(20\d{2})", name)
    if m:
        return date(int(m.group(1)), 1, 1)
    return date(1900, 1, 1)


def file_id(p):
    return hashlib.md5(p.name.encode("utf-8")).hexdigest()[:8]


def signature(p):
    st = p.stat()
    return f"{p.name}|{st.st_size}|{int(st.st_mtime)}"


# ---------------------------------------------------------------- one workbook
def read_one_file(p, log=print):
    """parse plans + stage pictures of one workbook -> cached json"""
    import stage_reader as sr
    FILE_CACHE.mkdir(parents=True, exist_ok=True)
    PIC_DIR.mkdir(parents=True, exist_ok=True)
    fid = file_id(p)
    cache_file = FILE_CACHE / f"{fid}.json"
    sig = signature(p)
    if cache_file.exists():
        old = json.loads(cache_file.read_text(encoding="utf-8"))
        if old.get("signature") == sig:
            return old

    log(f"Reading signal workbook: {p.name}  (first time, please wait) ...")
    for f in PIC_DIR.glob(f"{fid}__*"):              # old pictures of this file
        f.unlink()
    for f in PIC_DIR.glob(f"annotated_{fid}__*"):
        f.unlink()
    try:
        plans, problems = je._parse_signal_workbook(p)
    except Exception as exc:
        plans, problems = {}, {"(whole file)": f"could not be read: {exc}"}
    for sp in plans.values():
        sp["source_file"] = p.name

    # stage pictures -> cache\pictures\<fid>__<sheet>.png
    tmp = FILE_CACHE / f"tmp_{fid}"
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        pics = sr.extract_sheet_pictures(p, tmp)
    except Exception as exc:
        pics = {}
        problems["(pictures)"] = f"could not be extracted: {exc}"
    by_sheet = {}
    for ip, sp in plans.items():
        by_sheet.setdefault(sp["sheet"], []).append(ip)
    reads = {}
    for i, (sheet, png) in enumerate(pics.items(), 1):
        flat = PIC_DIR / f"{fid}__{Path(png).name}"
        shutil.move(png, flat)
        ips = by_sheet.get(sheet) or [je.norm_ip(k) for k in sheet.split("&")]
        sp = plans.get(ips[0]) if ips else None
        try:
            pic = sr.read_picture(str(flat), sp["group_order"] if sp else None)
            if pic.get("ok"):
                pic["notes"] += sr.fix_stages_with_timing(pic, sp, je.stage_window)
        except Exception as exc:
            pic = {"ok": False, "notes": [f"Picture could not be read: {exc}"]}
        pic["sheet"], pic["png"], pic["source_file"] = sheet, flat.name, p.name
        for ip in ips:
            reads[ip] = pic
        if i % 25 == 0:
            log(f"   {p.name}: {i}/{len(pics)} stage pictures read")
    shutil.rmtree(tmp, ignore_errors=True)

    out = {"signature": sig, "file": p.name, "date": file_date(p.name).isoformat(),
           "plans": plans, "problems": problems, "pictures": reads,
           "sheets_with_pictures": len(pics)}
    cache_file.write_text(json.dumps(out, default=float), encoding="utf-8")
    log(f"   {p.name}: {len(plans)} IPs, {sum(1 for r in reads.values() if r.get('stages'))} "
        f"with stages read from picture, {len(problems)} sheets with problems")
    return out


# ---------------------------------------------------------------- all workbooks
def sync_signal_plans(force=False, log=print):
    """(re)build cache\\signal_plans.json + picture_reads.json from all workbooks"""
    je.CACHE.mkdir(exist_ok=True)
    files = signal_files()
    if not files:
        raise FileNotFoundError(f"No signal workbooks found in {SIGNAL_DIR} "
                                f"(or data\\Signal Phasing plan.xlsx)")
    sigs = [signature(p) for p in files]
    merged_ok = (je.CACHE / "signal_plans.json").exists() and (je.CACHE / "picture_reads.json").exists()
    if not force and merged_ok and SOURCES_JSON.exists():
        if json.loads(SOURCES_JSON.read_text(encoding="utf-8")).get("signatures") == sigs:
            return False                              # nothing changed

    results = [read_one_file(p, log) for p in files]
    # newest file first -> wins for duplicate IPs
    order = sorted(results, key=lambda r: r["date"], reverse=True)
    plans, pictures, problems, seen_in = {}, {}, {}, {}
    for r in order:
        for ip, sp in r["plans"].items():
            seen_in.setdefault(ip, []).append(r["file"])
            if ip not in plans:
                plans[ip] = sp
                if ip in r["pictures"]:
                    pictures[ip] = r["pictures"][ip]
        for ip, pic in r["pictures"].items():        # picture without a parsed plan
            pictures.setdefault(ip, pic)
        for sheet, msg in r["problems"].items():
            problems[f"{r['file']} :: {sheet}"] = msg

    (je.CACHE / "signal_plans.json").write_text(json.dumps(plans, indent=1, default=str), encoding="utf-8")
    (je.CACHE / "picture_reads.json").write_text(json.dumps(pictures, default=float), encoding="utf-8")
    (je.CACHE / "signal_plan_problems.json").write_text(json.dumps(problems, indent=1), encoding="utf-8")

    with open(je.CACHE / "signal_sources_report.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Workbook", "Date in name", "IPs read", "IPs used (not overridden)",
                    "Stage pictures", "IPs with stages read", "Sheets with problems"])
        for r in results:
            used = sum(1 for ip, sp in plans.items() if sp.get("source_file") == r["file"])
            w.writerow([r["file"], r["date"], len(r["plans"]), used, r["sheets_with_pictures"],
                        sum(1 for p in r["pictures"].values() if p.get("stages")), len(r["problems"])])
    with open(je.CACHE / "signal_duplicates.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["IP", "Used from (newest)", "Also in"])
        for ip, fl in sorted(seen_in.items()):
            if len(fl) > 1:
                w.writerow([ip, fl[0], " | ".join(fl[1:])])

    SOURCES_JSON.write_text(json.dumps({"signatures": sigs}, indent=1), encoding="utf-8")
    dups = sum(1 for fl in seen_in.values() if len(fl) > 1)
    log(f"Signal plans ready: {len(files)} workbooks, {len(plans)} IPs, {dups} IPs found in more than "
        f"one workbook (newest used - see cache\\signal_duplicates.csv).")
    return True


# ---------------------------------------------------------------- replace engine functions
def build_cache(log=print):
    """same as before for nodes / links / junction box, signal plans from ALL workbooks"""
    je.CACHE.mkdir(exist_ok=True)
    for k in ("nodes", "links", "jbox"):
        if not je.FILES[k].exists():
            raise FileNotFoundError(f"Input file missing: {je.FILES[k]}")
    log("Reading NodeShapeFile ...")
    with open(je.CACHE / "nodes.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["N", "X", "Y", "IP", "CP_AM", "CP_OP", "CP_PM"])
        for r in je._read_csv_cols(je.FILES["nodes"], ["N", "X", "Y", "IP_NO", "CP_AM", "CP_OP", "CP_PM"]):
            w.writerow([r["N"], r["X"], r["Y"], je.norm_ip(r["IP_NO"]), r["CP_AM"].strip(),
                        r["CP_OP"].strip(), r["CP_PM"].strip()])
    log("Reading CUBE ShapeFile links (large file, please wait) ...")
    with open(je.CACHE / "links.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["A", "B", "LEN", "LANES"])
        for r in je._read_csv_cols(je.FILES["links"], ["A", "B", "SHAPE_Leng", je.LANE_FIELD]):
            w.writerow([r["A"], r["B"], r["SHAPE_Leng"], r[je.LANE_FIELD]])
    log("Reading NRN JunctionBox ...")
    with open(je.CACHE / "jbox.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["IP", "SITE_ID", "SIGNALIZE", "AREA", "LAT", "LON"])
        for r in je._read_csv_cols(je.FILES["jbox"], ["SITE_ID", "SIGNALIZE", "NAMEENGLIS", "LATTIDUTE", "LONGTUIDE"]):
            w.writerow([je.norm_ip(r["SITE_ID"]), r["SITE_ID"], r["SIGNALIZE"], r["NAMEENGLIS"],
                        r["LATTIDUTE"], r["LONGTUIDE"]])
    sync_signal_plans(force=True, log=log)
    log("Cache ready.")


def build_picture_cache(plans=None, log=print):
    sync_signal_plans(force=True, log=log)


je.build_cache = build_cache
je.build_picture_cache = build_picture_cache

# run at start-up: reads only new / changed workbooks
_base = ["nodes.csv", "links.csv", "jbox.csv"]
if all((je.CACHE / n).exists() for n in _base):
    sync_signal_plans(log=print)
