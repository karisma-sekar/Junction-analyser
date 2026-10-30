r"""
stage_images.py  -  UTC stages from the clear stage images
----------------------------------------------------------
Folder:  Junction_API\data\stage_images\<any area folder>\IP401.jpg  (jpg / jpeg / png / bmp / tif)

ONLY the UTC plan stages (the numbered stage boxes 1, 2, 3 ...) are taken from the image.
Everything else is unchanged: arm letters, lanes, slip lanes (FORMOFWAY 9), timings,
NRN / GeoJSON / CUBE logic and the TXT format.

Matching: file name -> Junction ID  ("IP401", "IP 401", "IP-401", "IP401_layout" -> IP401)
Reading:  on demand (single junction or batch), cached in cache\stage_image_reads.json;
          an image is read again only if the file changes.
Rules:    image stages found   -> used instead of the Excel-picture stages (+ note)
          image unreadable     -> Excel-picture stages kept (+ note: check / fill by hand)
          no image for the IP  -> Excel-picture stages kept (+ note)
          stage groups never green together in the signal plan -> note (check)

Use: in main.py add   import stage_images   directly below   import junction_engine as je
"""
import json
import re
from pathlib import Path

import junction_engine as je

IMG_DIR = je.DATA / "stage_images"
READS = je.CACHE / "stage_image_reads.json"
EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"}
_index, _dups, _reads = None, {}, None


def _keys(stem):
    keys = {je.norm_ip(stem)}
    m = re.match(r"^\s*(IP)?\s*[-_ ]?\s*(\d+[A-Za-z]?)", stem, re.I)
    if m:
        keys.add(je.norm_ip(m.group(2)))
    return {k for k in keys if k}


def index():
    """IP -> image path (all area sub-folders)"""
    global _index, _dups
    if _index is not None:
        return _index
    _index, _dups = {}, {}
    if IMG_DIR.exists():
        for p in sorted(IMG_DIR.rglob("*"), key=lambda p: str(p).lower()):
            if p.suffix.lower() not in EXTS or p.name.startswith("~$"):
                continue
            for k in _keys(p.stem):
                if k in _index:
                    _dups.setdefault(k, [str(_index[k].relative_to(IMG_DIR))]).append(str(p.relative_to(IMG_DIR)))
                else:
                    _index[k] = p
    print(f"Stage images: {len(_index)} junction images found in {IMG_DIR}"
          + (f" ({len(_dups)} IPs have more than one image - first used)" if _dups else ""))
    return _index


def _load():
    global _reads
    if _reads is None:
        try:
            _reads = json.loads(READS.read_text(encoding="utf-8"))
        except Exception:
            _reads = {}
    return _reads


def image_stages(store, ip):
    """-> (stages dict or None, image path or None, notes)"""
    import stage_reader as sr
    ip = je.norm_ip(ip)
    p = index().get(ip)
    if p is None:
        return None, None, [f"No stage image for {ip} in data\\stage_images - Excel picture stages kept."]
    notes = []
    if ip in _dups:
        notes.append(f"More than one image for {ip}: {_dups[ip]} - first one used.")
    sig = f"{p}|{p.stat().st_size}|{int(p.stat().st_mtime)}"
    reads = _load()
    cached = reads.get(ip)
    if cached and cached.get("sig") == sig:
        return cached["stages"] or None, p, notes + cached["notes"]
    sheet = store.signals.get(ip)
    try:
        pic = sr.read_picture(str(p), sheet["group_order"] if sheet else None)
        stg = pic.get("stages") or {}
        rn = [n for n in pic.get("notes", []) if n.lower().startswith("stage")]
        if pic.get("ok") and stg and sheet:
            rn += sr.fix_stages_with_timing(pic, sheet, je.stage_window)
            stg = pic.get("stages") or stg
    except Exception as exc:
        stg, rn = {}, [f"Stage image could not be read: {exc}"]
    reads[ip] = {"sig": sig, "stages": stg, "notes": rn}
    je.CACHE.mkdir(exist_ok=True)
    READS.write_text(json.dumps(reads, indent=1), encoding="utf-8")
    return stg or None, p, notes + rn


# ---------------------------------------------------------------- hook (stages only)
_orig_suggest = je.suggested_config


def suggested_config(store, ip_text):
    res, skel = _orig_suggest(store, ip_text)
    if skel is None:
        return res, skel
    stg, p, notes = image_stages(store, skel.get("ip") or ip_text)
    if stg:
        old = skel.get("stages") or {}
        skel["stages"] = stg
        skel["stages_source"] = f"stage image {p.relative_to(IMG_DIR)}"
        msg = [f"UTC stages taken from stage image {p.name}: {stg}"]
        if old and old != stg:
            msg.append(f"(Excel picture had read {old} - replaced by the image.)")
        armg = {a.get(k) for a in (skel.get("arms") or {}).values()
                for k in ("left_group", "through_group") if a.get(k)}
        missing = sorted(armg - {g for v in stg.values() for g in v})
        if missing:
            msg.append(f"Arm groups {missing} are in no image stage - check.")
        notes = msg + notes
    elif p is not None:
        notes = [f"Stage image {p.name} found but no stages could be read - "
                 f"Excel picture stages kept; check / fill 'stages' by hand."] + notes
    skel["_picture_notes"] = notes + (skel.get("_picture_notes") or [])
    res["picture_notes"] = skel["_picture_notes"]
    return res, skel


je.suggested_config = suggested_config
index()
