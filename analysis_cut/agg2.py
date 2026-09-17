# -*- coding: utf-8 -*-
"""vlm(구조·의미) + fit(실측 치수) + geom(독립 기하 검증) + quality(화질) → pages.json + 통계."""
import json, os, collections, re

HERE = os.path.dirname(os.path.abspath(__file__))
rd = lambda n, cast=lambda x: x: (cast(json.load(open(os.path.join(HERE, n), encoding="utf-8")))
                                  if os.path.exists(os.path.join(HERE, n)) else None)
geom = {x["file"]: x for x in (rd("geom.json") or [])}
fit = {x["file"]: x for x in (rd("fit.json") or [])}
qual = {x["file"]: x for x in (rd("quality.json") or [])}
vlm = {}
p = os.path.join(HERE, "vlm.jsonl")
if os.path.exists(p):
    for ln in open(p, encoding="utf-8"):
        r = json.loads(ln)
        if r.get("raw_ok"):
            vlm[r["file"]] = r["vlm"]

def fmt_row(cells, widths):
    return "| " + " | ".join(str(c).ljust(w) for c, w in zip(cells, widths)) + " |"

pages = []
for f in sorted(vlm):
    V = vlm[f]; F = fit.get(f) or {}; G = geom.get(f) or {}
    tiers = []
    for ti, t in enumerate(V.get("tiers") or []):
        ft = (F.get("tiers") or [{}] * 9)[ti] if F.get("tiers") else {}
        cells = []
        for ci, c in enumerate(t.get("cells") or []):
            fc = (ft.get("cells") or [{}] * 9)[ci] if ft.get("cells") else {}
            cells.append({
                "w": float(fc.get("w", c.get("w") or 0)), "x": float(fc.get("x", 0)),
                "shot": str(c.get("shot") or "?"), "angle": str(c.get("angle") or "?"),
                "chars": int(c.get("chars") or 0), "text": str(c.get("text") or "?"),
                "bg": str(c.get("bg") or "?"), "tach": bool(c.get("tachikiri")),
                "focus": str(c.get("focus") or ""), "ink": fc.get("mean")})
        tiers.append({"h": float(ft.get("h", t.get("h") or 0)), "cells": cells})
    pages.append({"file": f, "no": int(re.search(r"(\d+)\.png$", f).group(1)),
                  "role": str(V.get("page_role") or "?"), "density": str(V.get("density") or "?"),
                  "gutter": str(V.get("gutter") or "?"), "gutter_geom": str(G.get("gutter") or "?"),
                  "notes": str(V.get("notes") or ""),
                  "measured": bool(F.get("measured")), "geom_panel": int(G.get("n_panel") or 0),
                  "tiers": [{"h": round(t["h"], 3),
                             "cells": [{k: c[k] for k in ("w", "x", "shot", "angle", "chars", "text", "bg", "tach", "focus", "ink")}
                                       for c in t["cells"]]} for t in tiers]})
json.dump(pages, open(os.path.join(HERE, "pages.json"), "w"), ensure_ascii=False, indent=0)

N = len(pages)
cells = [c for p in pages for t in p["tiers"] for c in t["cells"]]
print(f"=== {N}페이지 / {len(cells)}컷 ===")
print("[컷/쪽]", sorted(collections.Counter(len([c for t in p['tiers'] for c in t['cells']]) for p in pages).items()))
print("[행/쪽]", sorted(collections.Counter(len(p["tiers"]) for p in pages).items()))
print("[행-칸 서명]", collections.Counter("-".join(str(len(t["cells"])) for t in p["tiers"]) for p in pages).most_common(18))
print("[컷수]", collections.Counter(sum(len(t['cells']) for t in p['tiers']) for p in pages).most_common())
print("[기능]", collections.Counter(p["role"] for p in pages).most_common())
print("[가터 VLM]", collections.Counter(p["gutter"] for p in pages).most_common(),
      "| 기하:", collections.Counter(p["gutter_geom"] for p in pages).most_common())
print("[기하 실측 성공]", sum(1 for p in pages if p["measured"]), "/", N)
print("[VLM vs 자유기하 컷수 일치]",
      sum(1 for p in pages if p["geom_panel"] == sum(len(t["cells"]) for t in p["tiers"])), "/", N)
tot = len(cells)
print(f"[타치키리 컷] {sum(1 for c in cells if c['tach'])}/{tot} = {100*sum(1 for c in cells if c['tach'])/tot:.0f}%")
for k in ("shot", "angle", "text", "bg"):
    cc = collections.Counter(c[k] for c in cells)
    print(f"[{k}]", [(s, n, f"{100*n/tot:.0f}%") for s, n in cc.most_common(8)])
print("[인물수/컷]", sorted(collections.Counter(c["chars"] for c in cells).items()))
print("[전폭컷(행 안 칸 1개·w≥0.9)]", f"{sum(1 for t in [t for p in pages for t in p['tiers']] if len(t['cells'])==1 and t['cells'][0]['w']>=0.9)}/{len([t for p in pages for t in p['tiers']])}행")

# 행 위치·칸 폭별 화각
def bucket(w): return "narrow<0.35" if w < 0.35 else ("mid0.35-0.6" if w < 0.6 else "wide>=0.6")
print("\n[칸 폭별 화각 %]")
wb = collections.defaultdict(collections.Counter)
for c in cells: wb[bucket(c["w"])][c["shot"]] += 1
for k, v in sorted(wb.items()):
    s = sum(v.values())
    print(f"  {k:>13} n={s:>4}", [(x, f"{100*y/s:.0f}%") for x, y in v.most_common(6)])
print("\n[행 위치(위/중간/아래)별 화각 %]")
pos = collections.defaultdict(collections.Counter)
for p in pages:
    n = len(p["tiers"])
    for i, t in enumerate(p["tiers"]):
        k = "top" if i == 0 else ("bottom" if i == n - 1 else "mid")
        for c in t["cells"]: pos[k][c["shot"]] += 1
for k in ("top", "mid", "bottom"):
    s = sum(pos[k].values())
    print(f"  {k:>6} n={s:>4}", [(x, f"{100*y/s:.0f}%") for x, y in pos[k].most_common(6)])
print("\n[행 높이 분포(행 단위)]")
hh = [t["h"] for p in pages for t in p["tiers"]]
import statistics
sh = sorted(hh)
print(f"  n={len(hh)} mean={statistics.mean(hh):.2f} p10={sh[int(len(sh)*.1)]:.2f} p25={sh[int(len(sh)*.25)]:.2f} "
      f"med={sh[len(sh)//2]:.2f} p75={sh[int(len(sh)*.75)]:.2f} p90={sh[int(len(sh)*.9)]:.2f}")
print("[2단 페이지 행 높이비(큰/작은)]")
r2 = [max(t['h'] for t in p['tiers'])/min(t['h'] for t in p['tiers']) for p in pages if len(p['tiers'])==2]
r2.sort(); print(f"  n={len(r2)} med={r2[len(r2)//2]:.2f} p25={r2[len(r2)//4]:.2f} p75={r2[int(len(r2)*.75)]:.2f}")
print("[3단 페이지 행 높이 중앙값]", sorted(sorted(t['h'] for t in p['tiers']) for p in pages if len(p['tiers'])==3))
