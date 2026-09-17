# -*- coding: utf-8 -*-
"""기하학적 컷 경계 측정 — 흰 띠/검은 띠(가터)를 '얇은 균일 띠'로 보고 찾아 정확한 비율을 재는다."""
import glob, json, os
import numpy as np, cv2
from PIL import Image

DIR = "/run/media/chrisyeo/AIDATA/AI/temp/182856022"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "geom.json")
WHITE, BLACK = 243, 16          # 종이 흰 / 완전 검은 기준
U_MIN, T_MIN, T_MAX = 0.90, 4, 0.075   # 균일도, 가터 최소 두께(px), 최대 두께(쪽 크기 대비)
MIN_TIER, MIN_CELL = 0.055, 0.06       # 행 최소 높이 / 칸 최소 폭(쪽 대비)

def runs(flags, keep):
    out, s = [], None
    for i, v in enumerate(flags):
        if v and s is None: s = i
        elif not v and s is not None: out.append((s, i)); s = None
    if s is not None: out.append((s, len(flags)))
    return [b for b in out if keep(b[1] - b[0])]

def analyze(path):
    im = Image.open(path); W0, H0 = im.size
    g = cv2.cvtColor(np.asarray(im.convert("RGB")), cv2.COLOR_RGB2GRAY)
    H, W = g.shape
    fw = (g > WHITE).mean(axis=1); fb = (g < BLACK).mean(axis=1)
    u = np.maximum(fw, fb)                        # 행 균일도(가터 후보)
    tmax = T_MAX * H
    bands = runs(u >= U_MIN, lambda L: T_MIN <= L <= tmax)
    sep = np.zeros(H, bool)
    for a, b in bands: sep[a:b] = True
    # 행 = 가터가 아닌 연속 구간(작은 조각은 윗행에 병합)
    content = ~sep
    tiers = [t for t in runs(content, lambda L: True)]
    tiers = [t for t in tiers if (t[1]-t[0]) >= MIN_TIER * H] or [(0, H)]
    cells_out = []
    for (ty0, ty1) in tiers:
        seg = g[ty0:ty1, :]
        cf = np.maximum((seg > WHITE).mean(axis=0), (seg < BLACK).mean(axis=0))
        wmax = T_MAX * W
        cb = runs(cf >= U_MIN, lambda L: T_MIN <= L <= wmax)
        csep = np.zeros(W, bool)
        for a, b in cb: csep[a:b] = True
        cs = [c for c in runs(~csep, lambda L: True) if (c[1]-c[0]) >= MIN_CELL * W] or [(0, W)]
        row = []
        for (cx0, cx1) in cs:
            cell = g[ty0:ty1, cx0:cx1]
            row.append({"x": cx0 / W, "w": (cx1-cx0) / W, "xpx": [int(cx0), int(cx1)],
                        "mean": float(cell.mean()), "p05": float(np.percentile(cell, 5)),
                        "white": float((cell > WHITE).mean()),
                        "black": float((cell < BLACK).mean()),
                        "edge": [bool(cx0 <= 4), bool(cx1 >= W-4), bool(ty0 <= 4), bool(ty1 >= H-4)]})
        cells_out.append({"y": ty0 / H, "h": (ty1-ty0) / H, "ypy": [int(ty0), int(ty1)], "cells": row})
    gut = "white" if float((g > WHITE).mean()) > float((g < BLACK).mean()) * 1.6 else \
          ("black" if float((g < BLACK).mean()) > float((g > WHITE).mean()) * 1.6 else "mixed")
    return {"file": os.path.basename(path), "w": W0, "h": H0,
            "white_frac": round(float((g > WHITE).mean()), 3), "black_frac": round(float((g < BLACK).mean()), 3),
            "gutter": gut, "gutter_bands": len(bands),
            "n_tier": len(cells_out), "n_panel": sum(len(t["cells"]) for t in cells_out),
            "tiers": [{"y": round(t["y"], 3), "h": round(t["h"], 3),
                       "cells": [{"x": round(c["x"], 3), "w": round(c["w"], 3), "mean": round(c["mean"], 1),
                                  "white": round(c["white"], 2), "black": round(c["black"], 2),
                                  "edge": int(sum(c["edge"]))} for c in t["cells"]]} for t in cells_out]}

fs = sorted(glob.glob(os.path.join(DIR, "*.png")))
out = [analyze(f) for f in fs]
json.dump(out, open(OUT, "w"), ensure_ascii=False, indent=1)
print("saved", len(out))
