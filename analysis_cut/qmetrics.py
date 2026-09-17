# -*- coding: utf-8 -*-
"""해상도·화질 지표: 선명도(라플라시안), 흑/백 클러시, 색편차, 블록(압축) 잔존, 정보량."""
import glob, json, os
import numpy as np, cv2
from PIL import Image

DIR = "/run/media/chrisyeo/AIDATA/AI/temp/182856022"
HERE = os.path.dirname(os.path.abspath(__file__))

def one(path):
    im = Image.open(path)
    a = np.asarray(im.convert("RGB")).astype(np.float32)
    g = cv2.cvtColor(a.astype(np.uint8), cv2.COLOR_RGB2GRAY)
    H, W = g.shape
    lap = cv2.Laplacian(g, cv2.CV_32F).var()
    edges = float((cv2.Canny(g, 60, 160) > 0).mean())
    white = float((g >= 250).mean()); black = float((g <= 5).mean())
    hsv = cv2.cvtColor(a.astype(np.uint8), cv2.COLOR_RGB2HSV)
    sat = float(hsv[..., 1].mean())
    ch = [float(a[..., i].std()) for i in range(3)]
    # 8x8 블록 경계 잔존(저품질 JPEG 흔적): 경계 열/행의 평균 절대 차
    g4 = g.astype(np.float32)
    dv = np.abs(np.diff(g4, axis=1)); dh = np.abs(np.diff(g4, axis=0))
    blk = float(np.mean([dv[:, 7::8].mean(), dh[7::8, :].mean()]) / max(1e-6, dv.mean()))
    # 국소 대비(디테일) — 고주파 에너지 비율
    blur = cv2.GaussianBlur(g4, (0, 0), 2.0)
    hp = float(np.abs(g4 - blur).mean())
    # 저해상도 확대 흔적: 2x 다운 → 업 후 오차
    small = cv2.resize(g4, (W // 2, H // 2), interpolation=cv2.INTER_AREA)
    up = cv2.resize(small, (W, H), interpolation=cv2.INTER_CUBIC)
    reconv = float(np.abs(g4 - up).mean())
    return {"file": os.path.basename(path), "w": W, "h": H, "mp": round(W * H / 1e6, 2),
            "aspect": round(W / H, 3), "mode": im.mode, "dpi_meta": im.info.get("dpi"),
            "lap": round(float(lap), 1), "edges": round(edges, 4), "white": round(white, 3),
            "black": round(black, 3), "sat": round(sat, 1), "ch_std": [round(c, 1) for c in ch],
            "block": round(blk, 2), "hf": round(hp, 2), "reconv": round(reconv, 2),
            "bytes": os.path.getsize(path)}

fs = sorted(glob.glob(os.path.join(DIR, "*.png")))
out = [one(f) for f in fs]
json.dump(out, open(os.path.join(HERE, "quality.json"), "w"), ensure_ascii=False, indent=0)

def q(v, ps): return float(np.percentile(v, ps))
arr = lambda k: np.array([x[k] for x in out], float)
print("N", len(out))
for k in ("mp", "aspect", "lap", "edges", "white", "black", "sat", "hf", "reconv", "block", "bytes"):
    v = arr(k)
    print(f"{k:>7}: p5={q(v,5):8.2f} p25={q(v,25):8.2f} med={q(v,50):8.2f} p75={q(v,75):8.2f} p95={q(v,95):8.2f}")
print("chroma(채널표준편차차) mean:", np.mean([max(x["ch_std"]) - min(x["ch_std"]) for x in out]).round(2))
print("흑백(채널차<3) 페이지:", sum(1 for x in out if max(x["ch_std"]) - min(x["ch_std"]) < 3))
