r"""
reader_fixes.py  -  three fixes for reading letters in the workbook stage drawings
---------------------------------------------------------------------------------
1. Letters side by side (E F, B A, H G) are no longer skipped.
   Before: any letter with a neighbour was treated as part of a word and ignored.
   Now:    a letter is skipped only if it almost touches a neighbour, or has letters
           close on BOTH sides (a word like "Corniche Road"). Pairs like "E F" are kept.
2. Serif font added.  The drawings use Times New Roman; the reader compared only
   with Arial shapes.  Times New Roman (bold + regular) is now tried first, Arial after.
3. Enlarged pictures: letters are compared at their real pixel height, and blurred
   edge pixels are cleaned (small holes filled, single stray pixels removed) so a
   letter does not split into pieces.

Nothing else changes: timings, plans, NRN, slip lanes, TXT format.
Use: in main.py add   import reader_fixes   directly below   import picture_upscale
"""
from pathlib import Path

import numpy as np
from scipy import ndimage

import stage_reader as sr

SERIF_FONTS = ["C:/Windows/Fonts/timesbd.ttf", "C:/Windows/Fonts/times.ttf",
               "/usr/share/fonts/liberation/LiberationSerif-Bold.ttf",
               "/usr/share/fonts/liberation/LiberationSerif-Regular.ttf"]
WORD_GAP = 0.2           # gap / letter height below which two letters are one word (words: ~0.1, "B A": ~0.3)

# ---- fix 2: serif templates first, then the original Arial ones
_orig_fonts = sr._font_list


def _font_list():
    serif = [f for f in SERIF_FONTS if Path(f).exists()]
    return serif + [f for f in _orig_fonts() if f not in serif]


sr._font_list = _font_list
sr._SIZED.clear()


# ---- fix 3: clean each letter before it is compared
_orig_classify = sr.classify_topk


def classify_topk(mask_crop, digits=False, k=4):
    m = np.asarray(mask_crop, bool)
    if m.shape[0] >= 14:                                 # enlarged letters only
        m = ndimage.binary_closing(m, np.ones((2, 2)))   # join split strokes
        m = ndimage.binary_opening(m, np.ones((2, 2))) | (m & ndimage.binary_erosion(m))
        if not m.any():
            m = np.asarray(mask_crop, bool)
    return _orig_classify(m, digits, k)


sr.classify_topk = classify_topk


# ---- fix 1: keep letter pairs, skip only real words
def glyphs(mask, lo=None, hi=None):
    S = sr.S
    lo = lo or 7 * S
    hi = hi or 40 * S
    allc = sr.components(mask, 3)
    comps = [c for c in allc if c["px"] >= int(6 * S) and lo <= c["h"] <= hi and 4 * S <= c["w"] <= hi]
    out = []
    for c in comps:
        near = []
        for d in allc:                    # neighbours incl. small lower-case letters
            if d is c or d["h"] < 0.35 * c["h"] or abs(d["cy"] - c["cy"]) > 0.6 * c["h"]:
                continue
            gap = max(d["x0"] - c["x1"], c["x0"] - d["x1"])
            if gap < 0.7 * c["h"]:
                near.append(gap)
        # word = touching neighbour, or part of a run of 3+ letters ("Road", "Corniche")
        word = any(g < WORD_GAP * c["h"] for g in near) or len(near) >= 2
        if not word:
            out.append(c)
    return out


sr.glyphs = glyphs
print("Stage reader fixes loaded: letter pairs kept, Times New Roman letters, cleaned enlarged letters.")
