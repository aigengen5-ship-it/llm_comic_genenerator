# -*- coding: utf-8 -*-
"""리포트에 들어갈 세부 수치 (방향·위치·기능별)."""
import json, os, collections, statistics as st
HERE = os.path.dirname(os.path.abspath(__file__))
pages = json.load(open(os.path.join(HERE, "pages.json"), encoding="utf-8"))
N = len(pages)
C = lambda p: [c for t in p["tiers"] for c in t["cells"]]

print("A. 첫 행이 전폭(1칸) 페이지:", sum(1 for p in pages if len(p["tiers"][0]["cells"]) == 1), f"({100*sum(1 for p in pages if len(p['tiers'][0]['cells'])==1)/N:.0f}%)")
print("B. 마지막 행이 전폭 페이지:", sum(1 for p in pages if len(p["tiers"][-1]["cells"]) == 1), f"({100*sum(1 for p in pages if len(p['tiers'][-1]['cells'])==1)/N:.0f}%)")
print("C. 전폭 행 보유 페이지:", sum(1 for p in pages if any(len(t['cells'])==1 for t in p['tiers'])), f"({100*sum(1 for p in pages if any(len(t['cells'])==1 for t in p['tiers']))/N:.0f}%)")
big = collections.Counter()
for p in pages:
    if len(p["tiers"]) == 3:
        i = max(range(3), key=lambda k: p["tiers"][k]["h"])
        big[{0: "top", 1: "mid", 2: "bottom"}[i]] += 1
print("D. 3단에서 가장 높은 행:", big.most_common())
side = collections.Counter(); side2 = collections.Counter()
for p in pages:
    n = len(p["tiers"])
    for i, t in enumerate(p["tiers"]):
        if len(t["cells"]) == 2:
            posn = "top" if i == 0 else ("bottom" if i == n - 1 else "mid")
            a, b = t["cells"][0]["w"], t["cells"][1]["w"]
            if abs(a - b) >= 0.08:
                side[("left" if a > b else "right") + ":" + posn] += 1
                side2["left" if a > b else "right"] += 1
print("E. 2칸 행에서 큰 컷 위치(좌/우, 전체):", side2.most_common(), "· 행 위치별:", side.most_common())
sk = [abs(t["cells"][0]["w"] - t["cells"][1]["w"]) for p in pages for t in p["tiers"] if len(t["cells"]) == 2]
print("F. 2칸 행 폭 차이: med=%.2f p75=%.2f (0.08 이상 차이나는 행 %d%%)" % (st.median(sk), sorted(sk)[int(len(sk)*.75)], 100 * sum(1 for x in sk if x >= 0.08) / len(sk)))
as3 = [c["w"] / (1.0 * c["w"]) for p in pages for t in p["tiers"] for c in t["cells"]]
# 컷 종횡(컷 폭/페이지폭 ÷ 행높이)
ar = []
for p in pages:
    for t in p["tiers"]:
        for c in t["cells"]:
            if t["h"] > 0: ar.append((c["w"] / t["h"]))
ar.sort()
print("G. 컷 종횡폭(컷폭÷행높이, 페이지 기준): p10=%.2f med=%.2f p90=%.2f / 세로형(<0.5) %d%% 가로형(>1.4) %d%%" %
      (ar[int(len(ar)*.1)], ar[len(ar)//2], ar[int(len(ar)*.9)],
       100 * sum(1 for x in ar if x < 0.5) / len(ar), 100 * sum(1 for x in ar if x > 1.4) / len(ar)))
# 컷 면적 대비 화각
print("\nH. 화각별 컷 면적(페이지 대비) 중앙값")
for s in ("scenery", "full_body", "upper_body", "bust", "closeup", "object", "crowd"):
    a = [c["w"] * t["h"] for p in pages for t in p["tiers"] for c in t["cells"] if c["shot"] == s]
    if a: print(f"   {s:>10} n={len(a):>4} 면적 중앙값={st.median(a):.3f} (≈페이지 {100*st.median(a):.0f}%)")
print("\nI. 기능별 평균 컷수·행수·대사 비율")
for r in ("establishing", "dialogue", "action", "reaction", "climax", "transition", "title"):
    G = [p for p in pages if p["role"] == r]
    if not G: continue
    nc = [len(C(p)) for p in G]; nt = [len(p["tiers"]) for p in G]
    dl = sum(1 for p in G for c in C(p) if c["text"] == "dialog") / max(1, sum(len(C(p)) for p in G))
    print(f"   {r:>12} n={len(G):>3} 컷 {st.mean(nc):.1f} 행 {st.mean(nt):.1f} 대사컷 {100*dl:.0f}%")
print("\nJ. 컷 수 구간별 기능")
bb = collections.defaultdict(collections.Counter)
for p in pages:
    k = "1~2" if len(C(p)) <= 2 else ("3~4" if len(C(p)) <= 4 else ("5~6" if len(C(p)) <= 6 else "7+"))
    bb[k][p["role"]] += 1
for k in ("1~2", "3~4", "5~6", "7+"):
    tot = sum(bb[k].values())
    print(f"   {k:>4}컷 n={tot:>3}", [(a, f"{100*b/tot:.0f}%") for a, b in bb[k].most_common(4)])
print("\nK. 인물 수별 화각")
cc = collections.defaultdict(collections.Counter)
for p in pages:
    for c in C(p): cc[min(2, c["chars"])][c["shot"]] += 1
for k in (0, 1, 2):
    tot = sum(cc[k].values())
    print(f"   인물{k}명 n={tot:>4}", [(a, f"{100*b/tot:.0f}%") for a, b in cc[k].most_common(5)])
print("\nL. 배경 처리별 페이지 기능")
bgc = collections.defaultdict(collections.Counter)
for p in pages:
    for c in C(p): bgc[c["bg"]][p["role"]] += 1
for k, v in sorted(bgc.items(), key=lambda kv: -sum(kv[1].values())):
    tv = sum(v.values())
    print(f"   {k:>9} n={tv}", [(a, f"{100*b/tv:.0f}%") for a, b in v.most_common(3)])
