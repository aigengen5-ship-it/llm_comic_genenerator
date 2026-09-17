# -*- coding: utf-8 -*-
"""외곽 여백(테두리)·가터 두께·프레임 라인 검출 — VLM 판정을 기하로 독립 검증."""
import glob, json, os
import numpy as np, cv2
from PIL import Image
DIR = "/run/media/chrisyeo/AIDATA/AI/temp/182856022"
HERE = os.path.dirname(os.path.abspath(__file__))

def border_thickness(g, white=240):
    H, W = g.shape
    out = []
    for side, arr in (("top", g), ("bottom", g[::-1]), ("left", g.T), ("right", g.T[::-1])):
        k = 0
        for i in range(arr.shape[0]):
            if (arr[i] > white).mean() < 0.90:
                break
            k += 1
        out.append(k)
    return dict(zip(("top", "bottom", "left", "right"), out))

def lines(g):
    """긴 검은 프레임 라인(컷 테두리) 개수 — 수평/수직 각각"""
    bw = (g < 90).astype(np.uint8)
    H, W = g.shape
    h = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (int(0.30 * W), 1)))
    v = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, int(0.12 * H))))
    def count(mask, axis):
        prof = mask.sum(axis=axis)
        n, on = 0, False
        for p in prof:
            if p > 0 and not on: n, on = n + 1, True
            elif p == 0: on = False
        return n
    return count(h, 1), count(v, 0)

out = []
for f in sorted(glob.glob(os.path.join(DIR, "*.png"))):
    g = cv2.cvtColor(np.asarray(Image.open(f).convert("RGB")), cv2.COLOR_RGB2GRAY)
    H, W = g.shape
    band = 12
    frame = np.concatenate([g[:band].ravel(), g[-band:].ravel(), g[:, :band].ravel(), g[:, -band:].ravel()])
    bt = border_thickness(g)
    hl, vl = lines(g)
    out.append({"file": os.path.basename(f), "frame_white": float((frame > 240).mean()),
                "margin": bt, "h_lines": hl, "v_lines": vl})
json.dump(out, open(os.path.join(HERE, "q2.json"), "w"), ensure_ascii=False, indent=0)
fw = np.array([x["frame_white"] for x in out])
print("외곽 12px 띠가 흰색 비율: p10=%.2f med=%.2f p90=%.2f" % tuple(np.percentile(fw, [10, 50, 90])))
print("외곽이 흰색(<0.10 = 몰아치기/테두리 없음) 페이지:", int((fw < 0.10).sum()), "/ 268")
print("흰 테두리 두께(좌/우) 중앙값:", int(np.median([max(x["margin"]["left"], x["margin"]["right"]) for x in out])),
      "상/하:", int(np.median([max(x["margin"]["top"], x["margin"]["bottom"]) for x in out])))
hl = np.array([x["h_lines"] for x in out]); vl = np.array([x["v_lines"] for x in out])
print("긴 검은 수평선 개수: med=%.0f p90=%.0f max=%d" % (np.median(hl), np.percentile(hl, 90), hl.max()))
print("긴 검은 수직선 개수: med=%.0f p90=%.0f max=%d" % (np.median(vl), np.percentile(vl, 90), vl.max()))
