# -*- coding: utf-8 -*-
"""cut_new.yaml 의 observed(근거 면수) 를 실측 구조와 대조해 계산하고, yaml 에 그 값을 되쓴다."""
import collections, json, os, re

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
Y = os.path.join(ROOT, "data", "cut_new.yaml")
pages = json.load(open(os.path.join(HERE, "pages.json"), encoding="utf-8"))
sig = lambda p: "/".join(str(len(t["cells"])) for t in p["tiers"])

SPEC = {
 "std_3tier_6panel":        lambda p: sig(p) == "2/2/2",
 "std_3tier_5panel_close":  lambda p: sig(p) == "2/2/1",
 "std_3tier_5panel_open":   lambda p: sig(p) == "1/2/2",
 "std_3tier_4panel_close":  lambda p: sig(p) == "2/1/1",
 "std_3tier_5panel_sandwich": lambda p: sig(p) == "2/1/2",
 "asym_2tier_4panel":       lambda p: sig(p) == "2/2",
 "split_2tier_2panel":      lambda p: sig(p) == "1/1",
 "top_split_bottom_full":   lambda p: sig(p) == "2/1",
 "top_full_bottom_split":   lambda p: sig(p) == "1/2",
 "accel_3tier_7panel":      lambda p: sig(p) in ("2/2/3", "2/1/3", "1/2/3"),
 "column3_strip":           lambda p: "3" in sig(p).split("/") and len(p["tiers"]) >= 2,
 "four_tier_action_8":      lambda p: sig(p) == "2/2/2/2",
 "four_tier_banter_6":      lambda p: sig(p) in ("1/2/2/1", "2/1/1/2", "2/2/1/1", "1/1/2/1", "1/2/1/2", "2/1/2/1"),
 "sfx_strip_insert":        lambda p: any(t["h"] <= 0.20 for t in p["tiers"]) and len(p["tiers"]) >= 2,
 "narrow_reaction_pocket":  lambda p: (any(c["w"] <= 0.35 for t in p["tiers"] for c in t["cells"])
                                       and any(c["w"] >= 0.60 for t in p["tiers"] for c in t["cells"])),
 "fullbleed_single_impact": lambda p: sig(p) == "1",
 "black_pressure_center":   lambda p: p["gutter_geom"] == "black" and any(len(t["cells"]) == 1 for t in p["tiers"]),
 "mirror_pair_dialogue":    lambda p: sig(p) == "2/2" and abs(p["tiers"][0]["cells"][0]["w"] - p["tiers"][1]["cells"][1]["w"]) <= 0.08,
 "vertical_full_body_intro": lambda p: False,          # 확장안(이 작품에 세로형 컷 1%)
 "epilogue_aftermath":      lambda p: sig(p) == "1",
}
counts = {k: sum(1 for p in pages if f(p)) for k, f in SPEC.items()}
for k in SPEC: print(f"{k:>26} {counts[k]:>3}면")

src = open(Y, encoding="utf-8").read()
def sub(m):
    tid = m.group(1)
    return f"{tid}\n    observed: {counts.get(tid, 0)}"
# id 줄 바로 아래 observed 를 갱신 (id 와 observed 사이에 다른 키가 없도록 이미 배치됨)
out = re.sub(r"(?m)^  - id: (\w+)\n(?:.*\n)*?^    observed: \d+$",
             lambda m: m.group(0) if f"observed: {counts.get(m.group(1),0)}" in m.group(0)
             else re.sub(r"observed: \d+", f"observed: {counts.get(m.group(1),0)}", m.group(0)), src)
open(Y, "w", encoding="utf-8").write(out)
import yaml
d = yaml.safe_load(open(Y, encoding="utf-8"))
print("yaml observed:", {t["id"]: t.get("observed") for t in d["manga_panel_templates"]})
