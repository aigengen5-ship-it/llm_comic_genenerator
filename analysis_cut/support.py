# -*- coding: utf-8 -*-
"""행-칸 서명별(예: 2-2-1) 실측 중앙값과 화각 빈출 → 템플릿 설계 근거."""
import json, os, collections, statistics as st
HERE = os.path.dirname(os.path.abspath(__file__))
pages = json.load(open(os.path.join(HERE, "pages.json"), encoding="utf-8"))
G = collections.defaultdict(lambda: {"n": 0, "h": [], "w": [], "shot": [], "role": collections.Counter(),
                                     "tach": 0, "ncell": 0, "text": [], "bg": []})
for p in pages:
    T = p["tiers"]; sig = "-".join(str(len(t["cells"])) for t in T)
    g = G[sig]; g["n"] += 1; g["role"][p["role"]] += 1
    g["h"].append([t["h"] for t in T])
    g["w"].append([[c["w"] for c in t["cells"]] for t in T])
    g["shot"].append([c["shot"] for t in T for c in t["cells"]])
    g["text"].append([c["text"] for t in T for c in t["cells"]])
    g["bg"].append([c["bg"] for t in T for c in t["cells"]])
    g["tach"] += sum(1 for t in T for c in t["cells"] if c["tach"])
    g["ncell"] += sum(len(t["cells"]) for t in T)

def med2(rows):
    """같은 모양 리스트들 → 원소별 중앙값(2단 정렬 후 위치별)"""
    return [round(st.median([r[i] for r in rows]), 2) for i in range(len(rows[0]))]

out = []
for sig, g in sorted(G.items(), key=lambda kv: -kv[1]["n"]):
    if g["n"] < 3: continue
    ks = [int(x) for x in sig.split("-")]
    hs = med2([sorted(r, reverse=True) for r in g["h"]])               # 큰 행→작은 행
    # 위치별(위/중간/아래) 행높이·칸폭 중앙값
    hpos = []
    for i in range(len(ks)):
        vals = [r[i] for r in g["h"] if len(r) > i]
        hpos.append(round(st.median(vals), 2) if vals else 0)
    wpos = []
    for i, k in enumerate(ks):
        vals = [sorted(r[i], reverse=True) for r in g["w"] if len(r) > i and len(r[i]) == k]
        wpos.append(med2(vals) if vals else [])
    shotpos = collections.Counter()
    for slist in g["shot"]: shotpos.update(slist)
    textpos = collections.Counter(); bgpos = collections.Counter()
    for l in g["text"]: textpos.update(l)
    for l in g["bg"]: bgpos.update(l)
    out.append({"sig": sig, "n": g["n"], "avg_cell": round(g["ncell"] / g["n"], 2),
                "h_by_pos": hpos, "h_sorted_desc": hs, "w_by_tier": wpos,
                "shot": shotpos.most_common(4), "text": textpos.most_common(3),
                "bg": bgpos.most_common(2), "tach_pct": round(100 * g["tach"] / g["ncell"]),
                "roles": g["role"].most_common(3)})
    print(f"[{sig}] n={g['n']:>3} 평균컷={g['ncell']/g['n']:.1f} 행높이(위→아래)={hpos} "
          f"칸폭(정렬)={wpos} 타치키리={100*g['tach']/g['ncell']:.0f}%")
    print(f"      shot={shotpos.most_common(4)} text={textpos.most_common(3)} bg={bgpos.most_common(2)} role={g['role'].most_common(3)}")
json.dump(out, open(os.path.join(HERE, "support.json"), "w"), ensure_ascii=False, indent=1)
