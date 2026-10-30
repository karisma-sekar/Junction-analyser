r"""
picture_source.py  -  read the UPSCALED stage drawings, and log every image read
--------------------------------------------------------------------------------
Why: the stage reader only reads the picture file it is given. The cache was built
from the small pictures copied out of the workbooks (cache\pictures\<id>__IPxx.png),
and those results (cache\picture_reads.json) stay in use until the cache is rebuilt.
An upscaled copy sitting in a folder is never used unless the reader is pointed at it.

What this file does
  1. Every stage drawing is enlarged (x3, sharp LANCZOS + light sharpening) and saved
     next to the original:   cache\pictures\<id>__IP86__UPSCALED.png
  2. The stage reader reads the UPSCALED file. Only if it gives no stages is the
     original also read, and the better result kept (logged).
  3. Every read is written to   cache\picture_read_log.csv :
        time | IP | original image | upscaled image | image actually read |
        size read | stages read | result | notes
  4. The single-junction report shows the line
        "Stage drawing read from: C:\...\cache\pictures\...__UPSCALED.png"
  5. The app's stage picture (with the green / orange boxes) is drawn on the
     upscaled image, so you see exactly what was read.

Nothing else changes: workbooks, timings, NRN, slip lanes, TXT format.

Use
  a) main.py: replace the line   import picture_upscale   (if you added it) with
                import picture_source
     placed directly ABOVE   import signal_sources
  b) re-read all drawings from the upscaled copies (app stopped, ~15-40 min):
                python picture_source.py
     or only some junctions:
                python picture_source.py IP86 IP401 IP415
"""
import csv
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageFilter

import junction_engine as je
import stage_reader as sr

SCALE = 3                       # enlarge x3 (620 px wide -> 1860 px)
MAX_WIDTH = 2400                # never larger than this
PIC_DIR = je.CACHE / "pictures"
LOG = je.CACHE / "picture_read_log.csv"
TAG = "__UPSCALED"

_original_read = getattr(sr, "_original_read_picture", sr.read_picture)
sr._original_read_picture = _original_read            # protect against double import


# ---------------------------------------------------------------- upscaling
def upscaled_path(original):
    p = Path(original)
    if p.stem.endswith(TAG):
        return p
    return p.with_name(f"{p.stem}{TAG}.png")


def make_upscaled(original):
    src = Path(original)
    dst = upscaled_path(src)
    if dst == src:
        return dst
    if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
        return dst
    im = Image.open(src).convert("RGB")
    f = min(SCALE, MAX_WIDTH / im.size[0]) if im.size[0] else 1
    if f > 1.05:
        im = im.resize((round(im.size[0] * f), round(im.size[1] * f)), Image.LANCZOS)
        im = im.filter(ImageFilter.UnsharpMask(radius=2, percent=60, threshold=3))
    im.save(dst)
    (src.parent / f"annotated_{dst.name}").unlink(missing_ok=True)
    return dst


# ---------------------------------------------------------------- log
def _log(row):
    new = not LOG.exists()
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["Time", "IP / sheet", "Original image", "Upscaled image",
                            "Image actually read", "Size read (px)", "Stages read", "Result", "Notes"])
            w.writerow(row)
    except PermissionError:
        print("  (picture_read_log.csv is open in Excel - log line skipped)")


def _score(pic):
    if not pic or not pic.get("ok"):
        return -1
    st = pic.get("stages") or {}
    arms = sum(len(a.get("letters", [])) for a in pic.get("arms_picture", []))
    return len(st) * 100 + sum(len(v) for v in st.values()) * 10 + arms - len(pic.get("notes", []))


def _size(path):
    try:
        w, h = Image.open(path).size
        return f"{w} x {h}"
    except Exception:
        return "?"


# ---------------------------------------------------------------- reader used by the app
def read_picture(png_path, allowed_letters=None, *args, **kwargs):
    original = Path(png_path).resolve()
    label = original.stem.split("__")[-1].replace(TAG, "")
    try:
        big = make_upscaled(original).resolve()
    except Exception as exc:
        pic = _original_read(str(original), allowed_letters, *args, **kwargs)
        pic.setdefault("notes", []).append(f"Upscaling failed ({exc}) - original image read.")
        pic["read_from"] = str(original)
        _log([datetime.now().isoformat(timespec="seconds"), label, original, "", original,
              _size(original), json.dumps(pic.get("stages") or {}), "ORIGINAL (upscale failed)",
              " | ".join(pic.get("notes", []))])
        return pic

    print(f"  reading stage drawing: {big}")
    pic = _original_read(str(big), allowed_letters, *args, **kwargs)
    used, result = big, "UPSCALED"
    if not (pic.get("ok") and pic.get("stages")):
        small = _original_read(str(original), allowed_letters, *args, **kwargs)
        if _score(small) > _score(pic):
            pic, used, result = small, original, "ORIGINAL (upscaled gave no stages)"
            pic.setdefault("notes", []).append("Upscaled image gave no stages - original image used.")
        else:
            result = "UPSCALED (no stages read)"
    pic["read_from"] = str(used)
    pic["original_image"] = str(original)
    _log([datetime.now().isoformat(timespec="seconds"), label, original, big, used, _size(used),
          json.dumps(pic.get("stages") or {}), result, " | ".join(pic.get("notes", []))])
    return pic


sr.read_picture = read_picture


# ---------------------------------------------------------------- show the source in the app
_orig_suggest = je.suggested_config
_orig_build = je.build_junction


def _source_line(store, ip):
    pic = (getattr(store, "pictures", {}) or {}).get(je.norm_ip(ip)) or {}
    src = pic.get("read_from")
    if src:
        return f"Stage drawing read from: {src}"
    if pic.get("png"):
        return (f"Stage drawing read from: {PIC_DIR / pic['png']}  (OLD read - run "
                f"'python picture_source.py' to re-read from the upscaled image)")
    return "No stage drawing for this junction."


def suggested_config(store, ip_text):
    res, skel = _orig_suggest(store, ip_text)
    if skel is not None:
        line = _source_line(store, ip_text)
        skel["_picture_notes"] = [line] + (skel.get("_picture_notes") or [])
        res["picture_notes"] = skel["_picture_notes"]
        skel["_stage_drawing_file"] = line.split(": ", 1)[-1]
    return res, skel


def build_junction(store, ip_text, period):
    res, txt = _orig_build(store, ip_text, period)
    res.setdefault("report", {}).setdefault("info", []).insert(0, _source_line(store, ip_text))
    return res, txt


je.suggested_config = suggested_config
je.build_junction = build_junction


# ---------------------------------------------------------------- re-read all from upscaled copies
def reread(only=None):
    reads_file = je.CACHE / "picture_reads.json"
    plans_file = je.CACHE / "signal_plans.json"
    if not reads_file.exists() or not plans_file.exists():
        print("No cache found - start the app once first so the pictures are extracted.")
        return
    reads = json.loads(reads_file.read_text(encoding="utf-8"))
    plans = json.loads(plans_file.read_text(encoding="utf-8"))
    only = {je.norm_ip(x) for x in only} if only else None

    by_png = {}
    for ip, pic in reads.items():
        png = pic.get("png")
        if png and (only is None or ip in only):
            by_png.setdefault(Path(png).name.replace(TAG, "").replace("annotated_", ""), []).append(ip)
    if not by_png:
        print("No matching junction pictures found in the cache.")
        return

    reads_file.with_name("picture_reads_before_upscale.json").write_text(
        reads_file.read_text(encoding="utf-8"), encoding="utf-8")
    t0, done, ok = time.time(), 0, 0
    for png, ips in sorted(by_png.items()):
        original = PIC_DIR / png
        if not original.exists():
            print(f"  missing picture file {original} - skipped ({', '.join(ips)})")
            continue
        sp = plans.get(ips[0])
        try:
            pic = sr.read_picture(str(original), sp.get("group_order") if sp else None)
            if pic.get("ok") and sp:
                pic["notes"] = pic.get("notes", []) + sr.fix_stages_with_timing(pic, sp, je.stage_window)
        except Exception as exc:
            pic = {"ok": False, "notes": [f"Picture could not be read: {exc}"],
                   "read_from": str(original)}
        old = reads.get(ips[0], {})
        pic["sheet"] = old.get("sheet")
        pic["png"] = Path(pic.get("read_from", original)).name      # app shows the image that was read
        for k in ("workbook", "source_file"):
            if old.get(k):
                pic[k] = old[k]
        for f in PIC_DIR.glob(f"annotated_{Path(png).stem}*"):
            f.unlink(missing_ok=True)
        for ip in ips:
            reads[ip] = pic
        done += 1
        ok += bool(pic.get("stages"))
        if done % 10 == 0:
            print(f"  {done}/{len(by_png)} drawings re-read ({time.time() - t0:.0f} s)")
    reads_file.write_text(json.dumps(reads, default=float), encoding="utf-8")
    print(f"\nRe-read {done} drawings from upscaled copies: {ok} with stages.")
    print(f"Log of every image path read: {LOG}")
    print(f"Previous results kept in: {reads_file.with_name('picture_reads_before_upscale.json')}")


if __name__ == "__main__":
    reread(sys.argv[1:] or None)
