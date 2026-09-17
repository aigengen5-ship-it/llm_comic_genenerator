# -*- coding: utf-8 -*-
"""VLM 이 알려준 행/칸 뼈대에 맞춰, 실제 픽투 프로파일에서 정확한 경계 좌표를 찾는다.

행/칸 경계 = '얇은 균일 띠'(흰 가터 또는 검은 가터). 후보 띠(로컬 최대) 중에서
VLM 뼈대가 요구하는 개수만큼 DP 로 골라 최상위 균일도의 조합을 택한다.
候補가 없거나 점수가 낮으면(테두리 없는 타치키리 페이지) VLM 의 눈대중 비율을 쓴다.
"""
import glob, itertools, json, os
import numpy as np, cv2
from PIL import Image

DIR = "/run/media/chrisyeo/AIDATA/AI/temp/182856022"
HERE = os.path.dirname(os.path.abspath(__file__))
WHITE, BLACK = 243, 16

def cands(prof, min_len, min_gap, nmax=28):
    """균일도 프로파일에서 '얇은 균일 띠' 후보 중심점과 그 점수"""
    H = len(prof)
    ok = prof >= 0.80
    out = []
    i = 0
    while i < H:
        if ok[i]:
            j = i
            while j + 1 < H and ok[j + 1]:
                j += 1
            seg = prof[i:j + 1]
            L = j - i + 1
            if L <= max(min_len * 8, 3):                      # 두꺼운 균일 덩어리(배경)는 가터 아님
                k = int(np.argmax(seg)) + i
                out.append((k, float(seg.max()), float(seg.mean() * min(1.0, L / 6.0))))
            i = j + 1
        else:
            i += 1
    out.sort(key=lambda t: -t[2])
    keep = []
    for c in out:
        if all(abs(c[0] - k[0]) >= min_gap for k in keep):
            keep.append(c)
        if len(keep) >= nmax:
            break
    return sorted(keep)

def pick(prof, k, lo, hi, min_len, min_gap):
    """k 개 경계 선택 → (positions, score)"""
    cs = cands(prof, min_len, min_gap)
    if k == 0:
        return [], 1.0
    if len(cs) < k:
        return None, 0.0
    best, arg = -1.0, None
    for combo in itertools.combinations(cs, k):
        ps = [c[0] for c in combo]
        if any(ps[i + 1] - ps[i] < min_gap for i in range(len(ps) - 1)):
            continue
        edges = [0] + ps + [len(prof)]
        if any(edges[i + 1] - edges[i] < lo for i in range(len(edges) - 1)):
            continue
        if max(c[2] for c in combo) < 0.0:
            continue
        s = sum(c[2] for c in combo) / (len(prof) ** 0)
        if s > best:
            best, arg = s, ps
    return (arg, best / k) if arg else (None, 0.0)

def fit(path, vstruct):
    g = cv2.cvtColor(np.asarray(Image.open(path).convert("RGB")), cv2.COLOR_RGB2GRAY)
    H, W = g.shape
    rp = np.maximum((g > WHITE).mean(axis=1), (g < BLACK).mean(axis=1))
    tiers = vstruct.get("tiers") or []
    ks = [len(t.get("cells") or []) for t in tiers]
    ys, sc = pick(rp, len(tiers) - 1, int(0.06 * H), 0, 6, int(0.07 * H))
    use_vlm = ys is None or sc < 0.30
    rows = ([0] + ys + [H]) if ys else [0, H]
    out, scores = [], []
    for ti, t in enumerate(tiers):
        y0, y1 = (rows[ti], rows[ti + 1]) if len(rows) == len(tiers) + 1 else (0, H)
        seg = g[y0:y1, :]
        cp = np.maximum((seg > WHITE).mean(axis=0), (seg < BLACK).mean(axis=0))
        xs, s2 = pick(cp, ks[ti] - 1, int(0.09 * W), 0, 6, int(0.07 * W))
        ok2 = xs is not None and s2 >= 0.30
        cols = ([0] + xs + [W]) if ok2 else [0, W]
        cells = []
        for ci in range(ks[ti]):
            if ok2 and len(cols) == ks[ti] + 1:
                x0, x1 = cols[ci], cols[ci + 1]
            else:
                wv = [float(c.get("w") or 0) for c in (t.get("cells") or [])]
                s0 = sum(wv[:ci]) / max(1e-6, sum(wv)); s1 = sum(wv[:ci + 1]) / max(1e-6, sum(wv))
                x0, x1 = int(s0 * W), int(s1 * W)
            cells.append({"x": x0 / W, "w": (x1 - x0) / W,
                          "mean": float(g[y0:y1, x0:x1].mean())})
        hv = [float(tt.get("h") or 0) for tt in tiers]
        if use_vlm or len(rows) != len(tiers) + 1:
            s0 = sum(hv[:ti]) / max(1e-6, sum(hv)); s1 = sum(hv[:ti + 1]) / max(1e-6, sum(hv))
            y0f, y1f = s0 * H, s1 * H
        else:
            y0f, y1f = y0, y1
        out.append({"y": y0f / H, "h": (y1f - y0f) / H, "cells": cells})
        scores.append(round(float(s2 if ok2 else 0.0), 2))
    return {"tiers": [{"y": round(t["y"], 3), "h": round(t["h"], 3),
                       "cells": [{"x": round(c["x"], 3), "w": round(c["w"], 3), "mean": round(c["mean"], 1)}
                                 for c in t["cells"]]} for t in out],
            "fit_h": round(float(0.0 if use_vlm else sc), 2), "fit_w": scores,
            "measured": bool(not use_vlm)}

def main():
    vlm = {}
    for ln in open(os.path.join(HERE, "vlm.jsonl"), encoding="utf-8"):
        r = json.loads(ln)
        if r.get("raw_ok"):
            vlm[r["file"]] = r["vlm"]
    out = []
    for f in sorted(glob.glob(os.path.join(DIR, "*.png"))):
        n = os.path.basename(f)
        if n not in vlm:
            continue
        try:
            r = fit(f, vlm[n]); r["file"] = n; out.append(r)
        except Exception as e:
            out.append({"file": n, "error": repr(e)})
    json.dump(out, open(os.path.join(HERE, "fit.json"), "w"), ensure_ascii=False, indent=0)
    ok = sum(1 for x in out if x.get("measured"))
    print(f"fit {len(out)}장, 실측 경계 {ok}장")

main()
