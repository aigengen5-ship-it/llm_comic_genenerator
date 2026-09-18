# -*- coding: utf-8 -*-
"""
cutsheet.py — 원작 생성기가 만든 **구조화 컷 시트**를 우리 컷 사양으로 옮겨 심는 수신기

입력(원작 저장소가 뱉는 그대로, 한 디렉토리에 모여 있으면 됩니다)
    character_sheet_ep{NN}_{hash}.json   캐릭터 시트(해시 포함)
    episode_{NN}_cuts.json               ★컷 시트(구조화) —=cut sheet正本
    ep{NN}_{hash}.txt                    컷 시트의 텍스트 사본(폴백)
    episode_{NN}_reviewed.md             전체 에피소드 본문(감사·재현용)
    manifest.json                        (있으면) 해시·sha256 대조

왜 이 파일이 생겼는가
    컷 분할(장면 카드·발화 분리·스탠딩·한도 강등)은 원작 쪽 `comic_cut_gen`이 이미 끝내 놓습니다.
    우리는 그 의도를 **다시 정하지 않고** 읽어서 컷 1:1 배분에 씁니다. 그래서 이 수신기는 컷 시트를
    `comic_gen._special_specs()`가 이미 읽는 행 모양({tag,text,line,act,who})으로만 바꿔 줍니다 —
    매핑 로직은 한 곳에 하나만 있습니다.

컷 시트 행(실측 키): cut, act, tag, kind(cut|card), speaker, text, chars, caption, balloon, asset,
                    inherited, split_part
    · kind=card  → 장면 카드(LOCATION/SITUATION/TIME/CLOTHES…). 상태의 **1순위 근거**로 씁니다.
    · inherited  → 안 바뀐 카드. 우리가 다시 컷을 만들면 확립/스탠딩이 중복되므로 버립니다.
    · split_part → 이미 두 컷으로 나뉜 발화의 조각. 여기서 다시 쪼개지 않습니다.
    · kind=cut   → ACTION/ACTION_LARGE/STANDING/NARR/SFX/INSERT/… 는 오늘짜리가 읽는 형태로 강등합니다.

실행 예
    python cutsheet.py --dir inbox/book7_ae7853edbd894547 --ep 1 --target-pages 12
"""

import argparse
import glob
import json
import os
import re

# ── 카드/발화 계약 ────────────────────────────────────────────────────────────────
CARD_TAGS = ("LOCATION", "SITUATION", "TIME", "CLOTHES", "CLOTHES2", "CLOTHES3")
# 카드 행 → 추출 프롬프트의 장면 카드 키 (novel_progress 와 같은 말)
CARD_KEY = {"LOCATION": "장소", "SITUATION": "상황", "TIME": "시간",
            "CLOTHES": "복장", "CLOTHES2": "복장2", "CLOTHES3": "복장3"}
SPEECH_TAGS = ("TALK", "TALK2", "INNER", "INNER2")
KEEP_TAGS = CARD_TAGS + SPEECH_TAGS + ("ACTION", "SFX")

# 오늘짜리 `_special_specs()` 가 이해하는 형태: 장면 카드 + TALK/INNER + ACTION(서술 컷).
# 나머지는 아래 사다리로 내립니다. 형태 강등은 모드 공통이고, 모드는 "무엇을 버리나"만 조절합니다.
KNOWN_TAGS = CARD_TAGS + ("TALK", "INNER", "ACTION")
ALWAYS_DOWN = {"TALK2": "TALK", "INNER2": "INNER",      # 화자 이름을 본문에 붙여 보냅니다(화자 추정기가 이름 언급을 봅니다)
               "CLOTHES3": "CLOTHES2",
               "STANDING": "CLOTHES",                   # 전신 스탠딩 — [CLOTHES] 와 같은 자리(우리가 새로 그려 재사용한다)
               "ACTION_LARGE": "ACTION", "NARR": "ACTION", "SFX": "ACTION",
               "INSERT": "ACTION", "REACTION": "ACTION", "POV": "ACTION", "FLASHBACK": "ACTION"}
DROP_BY_MODE = {"min": {"PAGE_TURN", "STANDING", "STANDING2"},     # 렌더 대상 아니거나 슬롯만 미는 것
                "rec": {"PAGE_TURN", "STANDING2"},
                "full": {"STANDING2"}}
# 형태는 내리되 뜻을 남기는 깃발(나중에 컷 크기·의성어·자산 재사용에 쓴다)
FLAG_BY_TAG = {"ACTION_LARGE": "big", "SFX": "sfx", "STANDING": "standing", "STANDING2": "standing",
               "FLASHBACK": "flashback", "POV": "pov", "INSERT": "insert", "REACTION": "reaction"}
MODES = ("min", "rec", "full")
MODE_RANK = {"min": 0, "rec": 1, "full": 2}

# 가지치기 우선순위(점수가 낮을수록 먼저 버립니다) — 원작 대사·속마음과 장면 전환은 마지막까지 남깁니다
SCORE = {"TALK": 3, "TALK2": 3, "INNER": 3, "INNER2": 3,
         "LOCATION": 2, "SITUATION": 2, "TIME": 2, "CLOTHES": 2, "CLOTHES2": 2, "CLOTHES3": 2,
         "ACTION": 1, "ACTION_LARGE": 1, "NARR": 1, "SFX": 1, "INSERT": 1, "REACTION": 1, "POV": 1}


def _b(v) -> bool:
    """JSON 사본에서 문자열 'False'로 적혀 오는 경우가 있어 진위를 직접 봅니다."""
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "1", "yes")


def _who(sp) -> str:
    return {"protagonist": "protagonist", "partner": "partner", "author": "author"}.get(str(sp or ""), "")


def _line_of(tag: str, text: str, speaker: str, names: dict) -> str:
    """대사/속마음 행을 novel_progress 가 읽을 수 있는 형태로 되살립니다(화자 추정기는 이름 언급을 봅니다)."""
    pname = str((names or {}).get("partner") or "").strip()
    if tag in ("TALK", "TALK2"):
        who = pname if (tag == "TALK2" or str(speaker or "") == "partner") else ""
        return f"{who}: {text}".strip(": ").strip()
    if tag == "INNER":
        return f"(속마음) {text}"
    if tag == "INNER2":
        return f"({pname or '상대방'} 속마음) {text}"
    return text


def from_json(path: str, names: dict = None, mode: str = "rec") -> dict:
    """구조화 컷 시트 → {"items":[행…], "audit":{…}}  (행 = {tag,text,line,act,who,…})"""
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    cuts = raw.get("cuts") or []
    mode = mode if mode in MODES else "rec"
    items, dropped = [], []
    for c in cuts:
        tag = str(c.get("tag") or "").strip().upper()
        text = str(c.get("text") or "").strip()
        if not tag or not text:
            continue
        kind = str(c.get("kind") or "cut")
        act = str(c.get("act") or "")
        row = {"tag": tag, "text": text, "line": _line_of(tag, text, c.get("speaker"), names),
               "act": act, "who": _who(c.get("speaker")),
               "cut": c.get("cut"), "asset": c.get("asset"), "chars": c.get("chars"),
               "balloon": _b(c.get("balloon")), "caption": _b(c.get("caption")),
               "split_part": c.get("split_part"), "card": kind == "card"}
        if kind == "card":
            # 안 바뀐 카드는 확립/스탠딩 컷을 중복시키므로 버립니다 (원작 생성기가 inherited 로 표시합니다)
            if _b(c.get("inherited")):
                dropped.append({"cut": c.get("cut"), "tag": tag, "why": "안 바뀐 장면 카드(중복 확립/스탠딩 방지)"})
                continue
            items.append(row)
            continue
        # 컷 — 오늘짜리가 읽는 형태로 내립니다(형태 강등은 공통, 버리기만 모드가 조절)
        flags = [FLAG_BY_TAG[tag]] if FLAG_BY_TAG.get(tag) else []
        if tag in KNOWN_TAGS:
            tgt = tag
        elif tag in DROP_BY_MODE.get(mode, set()):
            dropped.append({"cut": c.get("cut"), "tag": tag, "why": f"{mode} 모드에서 컷을 만들지 않는 태그"})
            continue
        else:
            tgt = ALWAYS_DOWN.get(tag, "ACTION")
            if tgt != tag:
                dropped.append({"cut": c.get("cut"), "tag": tag, "why": f"오늘짜리 형태로 강등 → {tgt}"})
        row["tag"] = tgt
        row["flags"] = flags
        items.append(row)
    tally = {}
    for r in items:
        tally[r["tag"]] = tally.get(r["tag"], 0) + 1
    return {"items": items,
            "audit": {"source": os.path.basename(path), "sheet_total": raw.get("cut_total") or len(cuts),
                      "after_compat": len(items), "dropped": dropped, "tally": tally,
                      "speech_splits": sum(1 for r in items if r.get("split_part") not in (None, "None", ""))}}


def _spread(rows: list, quota: int) -> list:
    """주어진 순서가 유지된 채 quota개만 고릅니다 — 앞/중간/뒤가 골고루 남도록 균등 간격으로."""
    if quota >= len(rows) or quota <= 0:
        return list(rows) if quota >= len(rows) else []
    if quota == 1:
        return [rows[0]]
    step = (len(rows) - 1) / float(quota - 1)
    out, seen = [], set()
    for k in range(quota):
        idx = int(round(k * step))
        while idx in seen and idx + 1 < len(rows):
            idx += 1
        seen.add(idx)
        out.append(rows[idx])
    return sorted(out, key=lambda r: r[0])


def budget(items: list, target_pages: int = 12, per_page: float = 5.0) -> tuple:
    """페이지 예산으로 컷을 줄입니다 — (살린 행, 버린 사연).

    근거: 우리 페이지 템플릿은 한 면 2~6컷(실측 평균 ~5). 목표 면수를 넘길 때 원작 대사를 잘르는 것보다
    **서술 컷**을 줄이는 편이 안전합니다(대사는 원작 그 자체이고, 말풍선 글자 하한도 대사를 못 줄입니다).
    그래서 범주별 비율로 자릅니다: 장면 전환 카드는 거의 전부, 발화는 대부분, 서술은 남은 자리만큼.
    한 막이 통째로 사라지지 않도록 자리 전체에서 균등 간격으로 고릅니다(막 순서·컷 순서 유지).
    """
    cap = max(4, int(round(max(1, int(target_pages or 12)) * max(2.0, float(per_page or 5.0)))))
    if len(items) <= cap:
        return list(items), []
    card, speech, narr = [], [], []
    for i, r in enumerate(items):
        tag = str(r["tag"])
        (card if SCORE.get(tag, 1) == 2 else speech if SCORE.get(tag, 1) == 3 else narr).append((i, r))
    q_card = min(len(card), max(2, int(cap * 0.15)))
    q_narr = min(len(narr), max(1, int(cap * 0.35)))
    q_speech = max(0, cap - q_card - q_narr)
    keep = _spread(card, q_card) + _spread(speech, q_speech) + _spread(narr, q_narr)
    keep.sort()
    kept = [items[i] for i, _r in keep]
    dropped = [{"cut": r.get("cut"), "tag": r["tag"], "why": f"페이지 예산({cap}컷 ≈ {target_pages}면)"}
               for i, r in enumerate(items) if i not in {k for k, _ in keep}]
    return kept, dropped


def find(dir_path: str, ep_num: int) -> str:
    """회차의 구조화 컷 시트를 찾습니다 (episode_NN_cuts.json, 없으면 *_cuts.json 중 회차 번호로)."""
    if not dir_path or not os.path.isdir(dir_path):
        return ""
    want = os.path.join(dir_path, f"episode_{int(ep_num):02d}_cuts.json")
    if os.path.isfile(want):
        return want
    for p in sorted(glob.glob(os.path.join(dir_path, "*_cuts.json"))):
        m = re.search(r"(\d{1,3})", os.path.basename(p))
        if m and int(m.group(1)) == int(ep_num):
            return p
    return ""


def load(dir_path: str, ep_num: int, names: dict = None, mode: str = "rec",
         target_pages: int = 12, per_page: float = 5.0) -> dict:
    """컷 시트를 읽어 {"items", "audit"} 로 줍니다. 컷 시트가 없으면 None(본문 경로가 그대로 돕니다)."""
    path = find(dir_path, ep_num)
    if not path:
        return None
    got = from_json(path, names=names, mode=mode)
    kept, dropped = budget(got["items"], target_pages=target_pages, per_page=per_page)
    audit = dict(got["audit"])
    audit["dropped"] = list(audit.get("dropped") or []) + list(dropped)
    audit["after_budget"] = len(kept)
    audit["target_pages"] = int(target_pages or 12)
    return {"items": kept, "audit": audit, "path": path}


def cards_for_extract(items: list) -> list:
    """컷 시트의 카드 행 → 추출 프롬프트가 읽는 장면 카드로 묶습니다 (상태카드 만드는 법의 우리 측 반영).

    한 장의 카드는 '붙어 있는 카드 행 여러 개'입니다(LOCATION+SITUATION+TIME+CLOTHES).
    회차 시작 카드는 회차 태그(복장·배경)의 **정답**으로, 막 중간 카드는 그 막 컷의 상태로 쓰입니다
    (act 가 붙은 카드는 for_start=False 로 빠집니다).
    """
    out, cur, cur_act = [], {}, ""
    head = True        # 컷 시트 맨 앞의 카드 묶음 = 회차 시작 카드(장면이 열리자마자의 상태)

    def flush():
        if cur:
            d = dict(cur)
            if cur_act:
                d["act"] = cur_act
            out.append(d)
    for r in items:
        if not r.get("card"):
            head = False
            continue
        key = CARD_KEY.get(str(r["tag"]))
        if not key:
            continue
        act = "" if head else str(r.get("act") or "")     # 맨 앞 카드는 회차 시작 카드, 나머지는 그 막의 상태
        if cur and (act != cur_act):
            flush()
            cur = {}
        cur_act = act
        if key == "복장":
            who = str(r.get("who") or "")
            if who and who != "protagonist":
                cur["복장 주인"] = "상대방" if who == "partner" else "그 외 인물"
        txt = str(r.get("text") or "").strip()
        if txt:
            cur[key] = f"{cur[key]} / {txt}" if cur.get(key) else txt
    flush()
    return out


def write_audit(audit: dict, out_dir: str, ep_num: int) -> str:
    """감사 리포트를 회차 옆에 남깁니다(컷이 왜 줄었는지 원작 쪽에서 볼 수 있게)."""
    try:
        os.makedirs(out_dir, exist_ok=True)
        p = os.path.join(out_dir, f"audit_ep{int(ep_num):02d}.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(audit, f, ensure_ascii=False, indent=1)
        return p
    except OSError:
        return ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="구조화 컷 시트(episode_NN_cuts.json) 수신기")
    ap.add_argument("--dir", required=True, help="컷 시트가 모아진 디렉토리")
    ap.add_argument("--ep", type=int, default=1)
    ap.add_argument("--mode", choices=MODES, default="rec", help="태그 강등 사다리 폭")
    ap.add_argument("--target-pages", type=int, default=12)
    ap.add_argument("--cuts-per-page", dest="cpp", type=float, default=5.0)
    a = ap.parse_args(argv)
    got = load(a.dir, a.ep, mode=a.mode, target_pages=a.target_pages, per_page=a.cpp)
    if not got:
        print(f"컷 시트가 없습니다: {a.dir}/episode_{a.ep:02d}_cuts.json")
        return 1
    au = got["audit"]
    print(f"컷 시트 {au['source']} : 시트 {au['sheet_total']}컷 → 강등 후 {au['after_compat']}컷"
          f" → 예산 {au['after_budget']}컷(목표 {au['target_pages']}면 ≈ {au['after_budget']/a.cpp:.1f}면)")
    print("  태그:", ", ".join(f"{k}×{v}" for k, v in sorted(au["tally"].items())))
    why = {}
    for d in au["dropped"]:
        why[d["why"]] = why.get(d["why"], 0) + 1
    for k, v in sorted(why.items(), key=lambda kv: -kv[1]):
        print(f"  ! {k} — {v}컷")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
