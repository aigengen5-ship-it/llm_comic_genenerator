#!/usr/bin/env python3
"""comic_gen.py — plot_gen 결과(에피소드 가이드 + 캐릭터 시트)로 에피소드별 만화를 만든다.

[2026-09-08 신규] 실행 시점은 **본문 생성(full_episode_gen) 전**. 따라서 원고는 쓰지 않고
plot_gen이 만들어 놓은 다음 두 가지만 입력으로 쓴다.
  1) 캐릭터 시트  : config.episode_protagonist_sheets / partner / sub (진행형 문자열 시트)
                   + init_anima_tags가 progress/character_sheet_epNN_*.json에서 갱신한 태그
  2) 에피소드 가이드: config.ep_corruption_guides_map[ep] = {"protagonist":[...], "partner":[...], "sub":[...]}
                   + config.special_writing_req[ep] ($ 행동 키워드)

흐름 (1에피소드):
  init_anima_tags(ep)                    → LLM(gemma) 1회로 EP별 태그 세트 생성
                                           (구 rp_visual_tags 결정론 풀 + 태그 리뷰 LLM 폐지, 2026-09-07)
    ↓
  본문 길이 → 컷 수 → 페이지/슬롯 확정     → comic_input.target_panels / plan_pages_layout
    ↓                                        (예전의 6~10컷·4페이지 고정 폐지, 2026-09-08)
  컷 스크립트 — 본문 장면(창)마다 LLM 1회  → [face:action 자동], 서사 순서 = 본문 순서
    ↓                                        직전 컷 캡션·복장을 다음 장면으로 넘긴다
  컷별: _build_simple_prompt_header + _build_tag_block → 플랫 태그 프롬프트
    ↓ comfyui_run_anima(res=5: 1024x1344 = 1:1.3125, seed = 회차 기준 고정 + 컷 오프셋)
  ./image/*.png 회수
    ↓
  comic_page_merge.compose_pages(cut.yaml 페이지 구성) → comic/bookNNN/episode_NN_pageXX.png
  (컷 수만큼 페이지가 늘어난다 — 페이지 수 상한은 config.comic_max_pages)

seed 정책(요구사항): 회차 base_seed는 **컷 스크립트 해시**에서 파생 → 같은 가이드를 다시 돌리면
같은 컷 이미지가 나온다. 컷별 seed = base_seed + 컷 번호.

실행: ./run_main.sh -comic        (기본 disable — config.comic_enable)
"""
import glob
import json
import os
import random
import re
import sys
import time
import zlib

import config
import anima_gen
import comic_input as CI
import comic_page_merge as CPM
from openAPI_control import call_openai_for_plot, get_openai_client_anima, call_openai_for_text, \
    release_llm_for_gpu

# ------------------------------------------------------------------ 상수
PANEL_RES = 5                     # anima_gen.resol[5] = 1024x1344 = 1:1.3125 (요구 종횡비 1:1.3)
WIDE_RES = 9                      # anima_gen.resol[9] = 1366x1024 (와이드 스플래시)
MAX_WIDE_PANELS = 3               # wide 컷 하한값 (실제 상한 = max(이 값, 컷 수의 WIDE_RATIO))
WIDE_RATIO = 0.12                 # 긴 회차일수록 wide를 늘린다 (컷 수에 비례)
MIN_PANELS = 6                    # 회차 컷 하한
MAX_PANELS = 0                    # 컷 상한 (0=무제한) — 컷 수는 본문 길이가 결정한다
PANELS_PER_PAGE = 5
FACE_RATIO_MIN, FACE_RATIO_MAX = 0.40, 0.60     # face:action 5:5 목표, 허용 대역
CAPTION_MAX_LEN = 40                            # 설명(지문) 1줄 최대 길이 (완전한 문장 기준)
SUMMARY_CAPTION_MAX_LEN = 150                   # [2026-09-09] 서두 요약·에필로그 큰 지문(컷 70%를 채운다)
DIALOG_MAX_LEN = 24                             # 풍선 1개 최대 글자 (길면 두 번째 풍선으로 나눔)
DIALOG_LINES = 2                                # 컷당 풍선 최대 개수 (0~2 — 2026-09-09부터 '정확히 2줄'이 아님)
SFX_MAX_LEN = 10                                # 의성어/의태어 최대 길이
WIDE_ENABLE = True          # False면 wide 컷을 전부 portrait로 강등 (런너 --no-wide 스위치)

# [2026-09-07] 시선/구도 정책 (사용자 지시): portrait 컷은 반드시 둘 중 하나
#   front → 인물이 정면(카메라/독자 정면)
#   right → 인물이 화면 왼쪽에서 오른쪽을 보며 moving
#   [2026-09-09] 화면 문법 통일 이후 facing은 텍스트 위치가 아니라 **풍선 꼬리/배치 쪽**을 정한다.
# [2026-09-08] 어구 다듬기: "is looking to the right"은 주어 없는 파편이라 주어만 보강했다.
#   `subject on left` / `negative space` 는 잘못된 어구라고 판단해 없앴다(사용자 지시).
#   시선 어구는 어디까지나 그림의 구도 이야기다(화면 텍스트는 comic_page_merge가 컷 안에서 배치한다).
RIGHT_FACING_TAGS = "She is facing to the right, she is looking to the right, "
FRONT_FACING_TAGS = "she is looking at viewer, "

# ------------------------------------------------------------------ cut.yaml 페이지 레이아웃
# [2026-09-07 A안] data/cut.yaml = 페이지 템플릿 DB(situation: 기/승/전/결 태그 + tier별 행 사양).
#   tier의 `shares`는 행 내부 좌→우 폭 비율(합 1.0, LTR), `center: true`는 중앙 배치.
#   tier의 `height`(선택)는 페이지 높이 중 그 행이 차지할 비율(합 1.0) — 예: climax_impact 2단 4:6.
#   페이지 수만큼 기승전결 가이드에 대응되는 템플릿을 회차 해시 시드로 결정론 선택하고,
#   컷 스크립트 LLM은 **그 슬롯 순서대로** 컷만 채운다. 레이아웃은 comic_page_merge의 row_spec로 간다.
CUT_YAML_FILE = os.path.join(os.path.dirname(__file__), "data", "cut.yaml")
MAX_PAGES_AUTO = 24               # auto 모드 페이지 상한 (≈8컷/페이지 → 최대 ~190컷)
SLOTS_PER_PAGE_MAX = 8            # cut.yaml 템플릿 최대 슬롯(페이지당) — 상한 계산용
_SITUATIONS = ("기", "승", "전", "결")
_CUT_TMPL_CACHE = None


def _num(v):
    """yaml 값 → float(아니면 None)"""
    try:
        return float(v)
    except Exception:
        return None


def load_cut_templates():
    """data/cut.yaml → {id: {id,name,situations,tiers:[{tier,description,shares,center}]}} (캐시)"""
    global _CUT_TMPL_CACHE
    if _CUT_TMPL_CACHE is not None:
        return _CUT_TMPL_CACHE
    out = {}
    try:
        import yaml
        with open(CUT_YAML_FILE, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        for t in data.get("manga_panel_templates") or []:
            tiers = []
            for td in t.get("tier_details") or []:
                try:
                    shares = [float(s) for s in (td.get("shares") or []) if float(s) > 0]
                except Exception:
                    continue
                if not shares:
                    continue
                tiers.append({"tier": int(td.get("tier") or len(tiers) + 1),
                              "description": str(td.get("description") or "").strip(),
                              "shares": shares, "center": bool(td.get("center")),
                              # ★화면 문법 역할("summary"=서두 요약 배경컷 / "epilogue"=반투명 에필로그)
                              "role": str(td.get("role") or ("summary" if td.get("summary") else "")).strip(),
                              # 행 높이 비율(미지정/0 이하 → 자연 높이 사용)
                              "height": _h if isinstance(_h := _num(td.get("height")), float) and _h > 0 else 0.0})
            if tiers and t.get("id"):
                out[str(t["id"])] = {"id": str(t["id"]), "name": str(t.get("name") or ""),
                                     "situations": [str(s) for s in (t.get("situations") or [])],
                                     "epilogue": bool(t.get("epilogue")),      # ★에필로그 전용 페이지
                                     "tiers": tiers}
    except Exception as e:
        _clog(f"cut.yaml 로드 실패(자동 레이아웃으로 계속): {e}")
        out = {}
    _CUT_TMPL_CACHE = out
    return out


def plan_pages(ep_num_1based: int, pages: int = 2):
    """페이지별 템플릿(회차 시드 결정론) → [{page,situation,template_id,template_name,slots:[...]}]

    슬롯 = {page,tier,share,center,wide,desc}. 컷 수는 템플릿 합계로 확정(페이지당 2~8컷).
    cut.yaml 없으면/ pages<=0 → None (레거시 자동 문법).
    [2026-09-08] 1~4페이지(기승전결 1:1) 고정이 풀렸다 — 상한은 config.comic_max_pages(기본
    MAX_PAGES_AUTO)뿐이고, 페이지 수는 plan_pages_layout()이 본문 길이에서 역산한 컷 수로 정한다.
    기승전결 대응은 페이지 수 N에서 비례 대응: 첫 페이지=기, 마지막 페이지=결 보장,
    그 사이를 pi*3/(N-1) 비율로 승→전 배분(옛식 pi*4//N은 2~3페이지에서 결이 아예 안 떘났다).
    """
    tmpls = load_cut_templates()
    if not tmpls or not pages or int(pages) <= 0:
        return None
    total0 = max(1, int(getattr(config, "total_episodes", 1) or 1))
    prologue_pages = 1 if (int(ep_num_1based) <= 1
                           and bool(getattr(config, "comic_prologue_cut", True))) else 0
    cap = max(1, int(getattr(config, "comic_max_pages", MAX_PAGES_AUTO) or MAX_PAGES_AUTO))
    pages = max(1, min(cap, int(pages)))                 # 기승전결 비례 대응, 1~cap 페이지
    # [2026-09-09] variation이 시드에 섞인다 — 같은 회차でも 페이지/템플릿 구성이 값마다 달라진다
    rng = random.Random(f"cut:{ep_num_1based}:{pages}:{int(getattr(config, 'comic_variation', 0) or 0)}")

    def _slots_of(t):
        """템플릿 → 납작한 슬롯 목록 (행의 첫 슬롯이 화면에서 가장 위/가장 왼쪽)"""
        single = len(t["tiers"]) == 1
        sl = []
        for tier in t["tiers"]:
            for share in tier["shares"]:
                full_row = len(tier["shares"]) == 1 and share >= 0.99 and not tier.get("center")
                sl.append({"page": 0, "tier": tier["tier"], "share": round(float(share), 3),
                           "center": bool(tier.get("center")),
                           # 행 높이 비율(cut.yaml tier.height) — 0이면 행 자연 높이
                           "h": round(float(tier.get("height") or 0.0), 3),
                           # ★서두 요약/에필로그 같은 화면 문법 역할(cut.yaml tier.role)
                           "role": str(tier.get("role") or ""),
                           # 전폭+다단 페이지 → 가로 스플래시(1366x1024), 단티어 페이지 → 세로 풀페이지
                           "wide": bool(full_row and not single and WIDE_ENABLE),
                           "desc": tier["description"]})
        return sl

    total_eps = max(1, int(getattr(config, "total_episodes", 1) or 1))
    first_ep, last_ep = int(ep_num_1based) <= 1, int(ep_num_1based) >= total_eps
    plans = []
    # [2026-09-09] 템플릿이 14종 → 34종으로 늘었다(테스트용으로 주신 20종 병합). 그래서 추첨을
    #   '페이지마다 독립 랜덤'에서 **'회차 안 재사용 추첨'**으로 바꿨다 — 용도(기승전결 기능)가
    #   같은 페이지라도 다른 컷 구성이 나온다. 풀이 비면(_long 페이지) 직전 페이지 것만 피해 재사용한다.
    all_pool = sorted((t for t in tmpls.values() if not t.get("epilogue")), key=lambda t: t["id"])
    used_tmpl, prev_id = set(), ""

    def _full_row_first(t):
        """첫 행이 전폭 1컷인가 — 회차의 첫 페이지와 여운 페이지는 이걸 우대한다(사용자: 첫 컷이 좁다)"""
        ts = t.get("tiers") or []
        return bool(ts) and len(ts[0]["shares"]) == 1 and ts[0]["shares"][0] >= 0.9

    def _pick_template(sit, prefer_wide=False):
        nonlocal prev_id
        pool = [t for t in all_pool if sit in t["situations"]] or all_pool
        cand = [t for t in pool if t["id"] not in used_tmpl]
        if not cand:                                  # 풀을 다 썼다 → 직전 페이지 것만 빼고 재사용
            cand = [t for t in pool if t["id"] != prev_id] or pool
        if prefer_wide:                               # 도입부·여운은 화면을 벌려 놓는 것부터
            wide = [t for t in cand if _full_row_first(t)]
            cand = wide or cand
        t = rng.choice(cand)
        used_tmpl.add(t["id"])
        prev_id = t["id"]
        return t

    for pi in range(pages):
        sit = _SITUATIONS[0] if pages == 1 else _SITUATIONS[min(3, int(round(pi * 3 / (pages - 1))))]
        t = _pick_template(sit, prefer_wide=(pi == 0))     # 첫 페이지(도입)는 전폭 컷 우대
        slots = _slots_of(t)
        # [2026-09-09] 사용자 지시 (수정): ★큰 지문(요약)은 **회차의 가장 첫 컷 하나だけ**.
        #   페이지마다(=기승전결마다) 붙이던 예전 규칙은 70% 박스가 화면을 뒤덮어 폐지했다.
        if pi == 0 and slots and bool(getattr(config, "comic_summary_cuts", True)):
            slots[0]["role"] = slots[0].get("role") or "summary"
        for si, s in enumerate(slots):
            s["page"] = pi + 1 + prologue_pages
        plans.append({"page": pi + 1 + prologue_pages, "situation": sit, "template_id": t["id"],
                      "template_name": t["name"], "single_tier": len(t["tiers"]) == 1, "slots": slots})
    # [2026-09-09] ★프롤로그: 회차집의 **첫 회차**에만 맨 앞 1컷(배경만 + 큰 지문)을 둔다.
    #   10회 기준 ★ = 프롤로그 1 + 회차 앞 10 + 마지막 회차 에필로그 1 = 12개(사용자 지정 계산)
    if prologue_pages:
        plans.insert(0, {"page": 1, "situation": _SITUATIONS[0], "template_id": "prologue_opening",
                         "template_name": "프롤로그 (배경만 + 큰 지문)", "single_tier": True,
                         "prologue": True,
                         "slots": [{"page": 1, "tier": 1, "share": 1.0, "center": False,
                                    "h": 0.0, "role": "prologue", "wide": False,
                                    "desc": "전폭 1컷 - 회차집의 첫 장면. 인물 없이 배경만 + 큰 도입 지문"}]})
    # [2026-09-09] ★에필로그: 결 페이지 뒤에 '반투명 이벤트신 + 큰 지문' 페이지를 한 장 더 둔다
    #   (페이지 상한 comic_max_pages를 넘겨서는 안 된다 — 상한까지 채워졌으면 붙이지 않는다)
    #   ★에필로그는 회차집의 **마지막 회차 끝**에만 붙인다(회차마다 붙이면 ★이 남발된다).
    if (bool(getattr(config, "comic_epilogue", True))
            and int(ep_num_1based) >= max(1, int(getattr(config, "total_episodes", 1) or 1))
            and len(plans) < cap):
        _eppool = [t for t in tmpls.values() if t.get("epilogue")]
        # [2026-09-09] 여운 페이지도 1고정(이벤트 신)이 아니라 '빈 풍경 1칸' 같은 대안과 돌린다
        ep = rng.choice(_eppool) if _eppool else None
        if ep:
            slots = _slots_of(ep)
            for s in slots:
                s["page"] = len(plans) + 1
                s["role"] = s.get("role") or "epilogue"
            plans.append({"page": len(plans) + 1, "situation": _SITUATIONS[3], "template_id": ep["id"],
                          "template_name": ep["name"], "single_tier": len(ep["tiers"]) == 1,
                          "epilogue": True, "slots": slots})
    return plans


def spec_slots(page_plans):
    """page_plans → 납작한 슬롯 목록 (컷 수 = len)"""
    return [s for pl in (page_plans or []) for s in pl.get("slots") or []]


def _avg_slots_per_page() -> float:
    """cut.yaml 템플릿 평균 슬롯 수(페이지당) — 목표 컷 수를 페이지 수로 어림잡는 데 쓴다."""
    tmpls = load_cut_templates()
    per = [sum(len(td["shares"]) for td in t["tiers"]) for t in (tmpls.values() or [])]
    return (sum(per) / len(per)) if per else 5.0


def plan_pages_layout(ep_num_1based: int, target_panels: int, pages: int = 0):
    """컷 수(본문 길이에서 역산)에 맞는 페이지 계획 → (page_plans, 페이지 수)

    pages>0 : 그 페이지 수 고정(사용자 --pages 지시).
    pages<=0: 어림 페이지 수(목표 컷 수 ÷ 템플릿 평균 슬롯) 부근 ±2페이지를 훑어
              **목표에 가장 가까운 구성**을 고른다. 모자란 쪽에 1.6배 패널티를 두는 이유는
              '본문 전체 반영'이 목표이므로 컷이 부족한 것(장면 압축)보다 넘치는 것(세분화)을
              선호하기 때문이다. 예전의 1~4페이지 고정은 여기서 풀려나온 제약이다.
    """
    if pages and int(pages) > 0:
        pl = plan_pages(ep_num_1based, int(pages))
        return pl, (len(pl) if pl else 0)
    target = max(1, int(target_panels))
    cap = max(1, int(getattr(config, "comic_max_pages", MAX_PAGES_AUTO) or MAX_PAGES_AUTO))
    est = max(1, int(round(target / max(1.0, _avg_slots_per_page()))))
    lo, hi = max(1, est - 2), min(cap, est + 2)
    best, best_key = None, None
    for n in range(lo, hi + 1):
        pl = plan_pages(ep_num_1based, n)
        if not pl:
            continue
        e = len(spec_slots(pl))
        diff = abs(e - target)
        key = (diff * (1.6 if e < target else 1.0), diff, n)
        if best_key is None or key < best_key:
            best, best_key, best_n = pl, key, n
    if best is None:
        pl = plan_pages(ep_num_1based, min(cap, max(1, est)))
        return pl, (len(pl) if pl else 0)
    return best, best_n


def _layout_block(page_plans, slot_range=None):
    """컷 스크립트 프롬프트에 넣을 페이지/티어/슬롯 설명

    slot_range=(start, end): 이 장면이 채울 슬롯만 보인다(전체 70컷을 매 장면마다 다 보내면
    num_ctx를 레이아웃 설명으로 태워버린다). 번호는 **회차 전체 기준**으로 남겨 순서를 안 잃는다.
    """
    slots_all = spec_slots(page_plans)
    E = len(slots_all)
    g0, g1 = slot_range if slot_range else (0, E)
    if slot_range:
        lines = [f"[페이지 레이아웃(cut.yaml) — 회차 전체 {E}컷 중 이 장면은 {g1 - g0}컷을 "
                 f"전체 {g0 + 1}~{g1}번째 슬롯과 1:1로 채운다]"]
    else:
        lines = [f"[페이지 레이아웃(cut.yaml) — 총 {E}컷, 출력 순서가 아래 슬롯과 1:1 대응]"]
    k = 0
    for pl in page_plans:
        shown = []
        for s in pl["slots"]:
            k += 1
            if g0 < k <= g1:
                shown.append((k, s))
        if not shown:
            continue
        lines.append(f" 페이지 {pl['page']}: {pl['template_name']} ({pl['template_id']}) "
                     f"[{pl['situation']}]")
        for gn, s in shown:
            w = int(round(s["share"] * 100))
            kind = ("가로 전폭(1366x1024)" if s["wide"] else
                    ("세로 풀페이지" if s["share"] >= 0.99 else
                     (f"세로 슬림({w}% 폭)" if s["share"] <= 0.4 else f"세로({w}% 폭)")))
            cen = " 중앙 정렬," if s.get("center") else ""
            hnt = f" (행 높이 {int(round(s['h'] * 100))}%)" if s.get("h") else ""
            star = {
                "summary": (" ★회차 도입 요약: 인물 없이 배경만 + 큰 지문 1개(풍선 없음). "
                            "이 회차 뭘 하는 회차인지 상황 설명 — 본문을 옮겨 적지 말고 미리 보듯이 쓴다"),
                "prologue": (" ★회차집 프롤로그: 인물 없이 배경만 + 큰 도입 지문 1개(풍선 없음). "
                             "작품 전체의 문을 여는 한 문장(사건 설명 금지, 분위기/상황만)"),
                "epilogue": (" ★에필로그: 이벤트 신을 반투명하게 + 큰 여운 지문 1개(풍선·대사 없음). "
                             "**본문 마지막 장면을 그대로 옮겨 적지 않는다** — 그 '다음'(시간 경과, "
                             "일상 복귀, 서로를 의식하는 거리)을 쓴다")}.get(
                    str(s.get("role") or ""), "")
            lines.append(f"  - 슬롯{gn} p{pl['page']}t{s['tier']}: {kind}{cen}{hnt} — "
                         f"{s['desc'][:70]}{star}")
    # [2026-09-09] 템플릿이 34종으로 늘었다 — 그중엔 우산·음식·벽치기처럼 소품이 구체적인 것이 있다.
    #   본문에 그 소품이 없으면 컷이 딴 이야기가 되므로 '지킬 것은 비율·순서'라고 못 박는다.
    lines.append("슬롯 설명의 소품·장소는 **예시**입니다. 반드시 지킬 것은 분할 비율·컷 크기·순서뿐 — "
                 "본문에 없는 물건이나 장소(우산, 음식, 벽, 거리 등)는 같은 크기의 다른 행동으로 바꾸세요.")
    lines.append("컷은 이 순서 그대로. 슬롯마다 page/tier 필드를 JSON에 넣을 필요는 없다(순서로 매칭).")
    return "\n".join(lines)

CAMERA_VOCAB = {"front_view", "side_view", "back_view", "close_up", "pov"}
POSITION_VOCAB = {"He is standing.", "He is sitting.", "He is walking.", "He is lying down.",
                  "He is lying on top of her.", "He is behind her.", "NONE"}
# [2026-09-09] 컷의 climax 슬롯 — 기본(청년향)은 전부 비운다.
#   --allow-explicit(local)에서 사는 어휘만 허용한다 (이 목록 밖은 버린다).
CLIMAX_VOCAB_EXPLICIT = {"", "creampie", "cum", "cum_in_mouth", "cum_on_face", "cum_on_belly",
                         "cum_on_breasts", "cum_on_booty", "cumflush", "orgasm", "squirt"}


def _norm_climax(value) -> str:
    """컷의 climax 슬롯 정규화 — 청년향은 항상 "", explicit 모드만 어휘를 허용한다.

    허용 목록은 내장 + local_settings.yaml의 climax_vocab(로컬 증보)입니다.
    """
    v = str(value or "").strip().lower()
    if not anima_gen.explicit_allowed():
        return ""
    allowed = set(CLIMAX_VOCAB_EXPLICIT) | {str(x).strip().lower()
                                            for x in (getattr(config, "climax_vocab_local", []) or [])}
    return v if v in allowed else ""

# [2026-09-07] portrait = "만화 컷처럼 정확한 정면 초상, 얼굴 전체"(사용자 지시):
#   (face only)(extreme close-up)는 얼굴 일부만 크게 잘라내고 각도가 어긋나는 원인 → 제거.
FACE_CLOSEUP_TAGS = "(portrait:1.8), (close-up:1.4), "
# pose에 섞인 삐뚤어진 각도 표현은 face 컷에서 결정론으로 제거한다
_FACE_ANGLE_STRIP_RE = re.compile(
    r"\b(tilted head|head tilt(?:ed)?|looking away|looking up|looking down|looking (?:to the )?(?:left|right)|looking sideways|profile(?: view)?|over (?:his|her|the) shoulder|turning (?:his|her) head|from behind|from above|from below)\b[,. ]*",
    re.I)

# 표정/얼굴 정황이 명백한 pose 문구 → face 컷으로 전환 가능한 후보
_FACE_HINTS = re.compile(r"face|smile|smirk|grin|cry|tear|blush|eyes|gaze|expression|lips|tongue|"
                         r"mouth|cheek|chin|eyebrow|sweat drop|ahegao|panting|wink|stare", re.I)

_LOG_DIR = "log"
_LOG_FILE = os.path.join(_LOG_DIR, "comic_gen.log")


def _clog(msg: str):
    """log/comic_gen.log + 콘솔 출력 (anima_gen.log와 섞이지 않게 분리)"""
    try:
        os.makedirs(_LOG_DIR, exist_ok=True)
        with open(_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass
    try:
        print(f"[COMIC] {msg}")
    except Exception:
        pass


def comic_enabled() -> bool:
    return bool(getattr(config, "comic_enable", False))


# ------------------------------------------------------------------ 입력 수집
def _sheet_texts(ep_idx: int):
    """캐릭터 시트 문자열 3종 (plot_gen이 채운 진행형 시트)"""
    def _get(lst, i):
        try:
            if lst and 0 <= i < len(lst):
                return str(lst[i] or "")
        except Exception:
            pass
        return ""
    proto = _get(getattr(config, "episode_protagonist_sheets", []), ep_idx)
    partner = _get(getattr(config, "episode_partner_sheets", []), ep_idx)
    sub = _get(getattr(config, "episode_sub_sheets", []), ep_idx)
    if not proto:
        proto = "\n".join([f"이름: {getattr(config,'name','')}", f"성별: {getattr(config,'sex','')}",
                           f"머리색: {getattr(config,'hair_color','')}",
                           f"헤어스타일: {getattr(config,'hair_style','')}",
                           f"눈 색깔: {getattr(config,'eye_color','')}",
                           f"피부 색깔: {getattr(config,'skin_color','')}",
                           f"몸매: {getattr(config,'body_shape','')}",
                           f"복장: {getattr(config,'clothes','')}"])
    return proto, partner, sub


def _episode_text(ep_idx: int) -> str:
    """이 회차 본문 원문 (comic_input이 config.episode_content에 넣은 것).

    [2026-09-08] 예전의 컷 스크립트는 이 원문을 **한 번도 읽지 않았다** — LLM이 축약한
    기승전결 가이드 4문장만 보고 컷을 짰기 때문에 본문 9천 자 중 대부분이 장면으로 안 내려왔다.
    """
    try:
        return anima_gen.get_episode_content(ep_idx) or ""
    except Exception:
        return ""


def _beat_context(captions: list, limit: int = 2) -> list:
    """직전 컷 요약(캡션/복장) — 장면을 나눠도 복장·시간·장소가 이어지게 넘기는 컨텍스트"""
    return [str(c).strip() for c in (captions or []) if str(c).strip()][-limit:]


def _slice_guides(guide_lines, beat_i: int, n_beats: int) -> list:
    """전체 기승전결 가이드 중 이 장면 토막 (본문이 길어 가이드가 창 수만큼 늘어난 회차용)

    [2026-09-08] [protagonist] 줄만 비례로 자른다. [partner]/[sub] 줄은 회차 전체 서술이라
    장면으로 나눌 수 없다 — 옛날처럼 혼합 리스트를 그대로 균등 분할하면 장면1 슬라이스가
    기승전결의 3/4를 먹고 뒷장면은 partner/sub만 들게 됐다(장면-가이드 어긇남).
    회차 전체 줄은 마지막 장면 하나에 붙인다.
    """
    g = [str(x).strip() for x in (guide_lines or []) if str(x).strip()]
    n = max(1, int(n_beats))
    if n <= 1 or len(g) < 2 * n:
        return g
    prot = [x for x in g if x.startswith("[protagonist]")]
    rest = [x for x in g if not x.startswith("[protagonist]")]
    if len(prot) < 2 * n:                     # 주인공 줄이 쪼갤 만큼 안 되면 전체를 다 보여준다
        return g
    lo = int(round(len(prot) * (int(beat_i) - 1) / n))
    hi = max(lo + 1, int(round(len(prot) * int(beat_i) / n)))
    out = prot[lo:hi]
    return out + rest if int(beat_i) == n else out


def _guides_for(ep_num_1based: int):
    """그 회차의 기승전결 가이드 문장 + $ 행동 키워드"""
    m = getattr(config, "ep_corruption_guides_map", {}) or {}
    entry = m.get(ep_num_1based) or m.get(str(ep_num_1based)) or {}
    lines = []
    for who in ("protagonist", "partner", "sub"):
        for g in entry.get(who, []) or []:
            g = str(g).strip()
            if g:
                lines.append(f"[{who}] {g}")
    dollar = []
    try:
        dollar = list((getattr(config, "special_writing_req", {}) or {}).get(ep_num_1based, []) or [])
    except Exception:
        dollar = []
    return lines, dollar


def _current_book_no() -> int:
    """merged/bookNNN 최신 번호 = 지금 작업 중인 권. comic/bookNNN도 같은 번호를 쓴다."""
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "merged")
    nums = []
    for d in glob.glob(os.path.join(base, "book*")):
        m = re.match(r"book(\d+)$", os.path.basename(d))
        if m:
            nums.append(int(m.group(1)))
    override = getattr(config, "comic_book_num", 0) or 0
    return int(override) if int(override) > 0 else (max(nums) if nums else 1)


def comic_out_dir() -> str:
    root = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(root, "comic", f"book{_current_book_no():03d}")


# ------------------------------------------------------------------ 컷 스크립트
def build_panel_script_prompt(ep_num_1based: int, total_eps: int, proto: str, partner: str,
                              sub: str, guide_lines, dollar_actions, page_plans=None,
                              episode_text: str = "", panels_expected: int = 0,
                              prev_tail=None, beat_label: str = "", slot_range=None) -> str:
    """컷 스크립트 생성용 LLM 프롬프트.

    [2026-09-08] 세 가지가 새로 들어간다 —
      episode_text : **에피소드 본문 원문**(예전엔 가이드 4문장만 보였다). 장면을 쪼개면 이 장면 분량만.
      panels_expected : 이 호출이 낼 컷 수(본문 길이에서 역산). 미지정이면 레이아웃 슬롯 수.
      prev_tail/beat_label : 장면 분할 호출에서 직전 컷·장면 번호(연속성)를 넘긴다.
    """
    # [2026-09-09] 수다장이 모드(--chatty): 모든 컷에 지문을 요구한다 — 서술할 사건이 없는 컷은
    #   그녀의 행동·표정 묘사를 시킨다(화면 아래가 조용한 컷이 없어진다).
    chatty_rule = ("   [수다장이 모드] **모든 컷**에 caption_ko를 쓴다. 서술할 사건의 진전이 없는 컷이라도"
                   " 그녀의 **행동\u00b7표정을 한 문장**으로 묘사한다 (예: '그녀는 정면을 바라본다. 볼이 붉어졌다.')"
                   " 지문은 비어 있지 않게 쓴다."
                   if bool(getattr(config, "comic_chatty", False)) else "")
    guides = "\n".join(f"  - {g}" for g in guide_lines) if guide_lines else "  (가이드 없음)"
    acts = ", ".join(dollar_actions) if dollar_actions else "(없음)"
    sub_block = f"\n[서브 캐릭터 시트]\n{sub}\n" if (getattr(config, 'chr_num3', 0) == 1 and sub) else ""
    name1 = str(getattr(config, "name", "") or "주인공").strip() or "주인공"
    name2 = str(getattr(config, "name2", "") or "상대").strip() or "상대"
    slots = spec_slots(page_plans)
    count = int(panels_expected or 0) or (len(slots) if slots else 0)
    body = str(episode_text or "").strip()
    body_block = ("\n[에피소드 본문 — 이 장면을 컷으로 옮긴다]\n" + body + "\n") if (body and not beat_label) \
        else (f"\n[에피소드 본문 — {beat_label}]\n" + body + "\n" if body else "")
    prev_block = ("\n[직전 컷까지의 진행(복장·장소·시간을 이어서)\n"
                  + "\n".join(f"  - {str(x).strip()}" for x in (prev_tail or []) if str(x).strip())
                  + "\n]") if prev_tail else ""
    if slots:
        count_txt = f"**정확히 {count}컷**"
        layout_block = "\n" + _layout_block(page_plans, slot_range=slot_range) + "\n"
        rule1 = ('1. type은 "face"와 "action" 두 종류만. 슬롯이 **세로 슬림**이면 face(표정 클로즈업)를 우선, '
                 '전폭/가로 슬롯은 action을 쓴다. 비율은 강제하지 않는다.\n'
                 '   - face = 표정/감정 클로즈업 컷 (camera는 close_up 또는 pov)\n'
                 '   - action = 자세·행동·현장 묘사 컷')
        rule10 = ('10. wide는 레이아웃의 [가로 전폭] 슬롯만 true (그 외 모든 슬롯 false). face 컷은 항상 false.')
        rule2 = '2. 컷 순서는 페이지 순서 → 각 페이지 안에서 슬롯 순서 그대로. 내용 흐름은 가이드(기승전결)를 따른다.'
    else:
        count_txt = f"**정확히 {count}컷**" if panels_expected else "**6~10컷**"
        layout_block = ""
        rule1 = ('1. type은 "face"와 "action" 두 종류만. **face:action = 5:5** (6컷→3:3, 8컷→4:4, 10컷→5:5).\n'
                 '   - face = 표정/감정 클로즈업 컷 (camera는 close_up 또는 pov)\n'
                 '   - action = 자세·행동·현장 묘사 컷')
        rule10 = ('10. wide는 그 컷을 가로 넓은 프레임(1366x1024)으로 뽑을지 여부. **주인공 혼자가 나오는 장면 전환/배경 강조 컷만 true** (2인 풀샷 금지),\n'
                  f'    회차당 최대 {MAX_WIDE_PANELS}개, face 컷은 반드시 false. 나머지 컷은 false.')
        rule2 = '2. 컷 순서는 가이드의 **서사 흐름을 그대로** 따른다 (도입→위기→클라이맥스→마무리). face/action을 억지로 교대시키지 말 것.'
    # 본문이 보였을 때만 발동하는 규칙: 본문이 최우선 근거이고, 장면 분할 호출이면 자기 장면에 한정한다.
    rule15 = ("\n15. **본문이 최우선 근거다.** [에피소드 본문]에 나온 사건·장소·소품·의상·행동을 컷에"
              " 그대로 옮기고, 본문에 없는 인물·사건·장소는 새로 만들지 않는다."
              + (" 이 회차는 본문을 여러 장면으로 나눠 장면별로 스토리보드를 맡긴다 — **이 장면 본문 밖의"
                 " 내용을 앞뒤에서 끌어오지 말라**(연속성은 위 [직전 컷까지의 진행]만 이어간다)."
                 if beat_label else "") + "\n") if body else ""
    # [2026-09-09] 수위 정책 — 기본은 청년향(성기·climax 안 그림), --allow-explicit(local)에서만 성인을 연다.
    if anima_gen.explicit_allowed():
        policy_line = ("(explicit 정책: 노출·성기 묘사·climax 허용 /\n"
                       " 화면에는 기본 주인공 중심, 상대방은 POV 손이나 프레임 밖 — 두 사람 풀샷 금지)")
        pose_policy = ("   pose에 성기·climax 단어(pussy, penis, vulva, erection, cum, blowjob 등)를 써도 됩니다.\n"
                       "   nipples/topless/cameltoe 노출은 기본, penetration·climax도 화면에 그대로 보인다.")
        rule6_climax = ('6. climax는 climax 이벤트 태그("creampie" | "cum" | "cum_on_face" | "orgasm" 등) — '
                        '그 이벤트가 없는 컷은 빈 문자열 "".')
        genre = "성인(explicit) 만화"
    else:
        policy_line = ("(청년향 정책: 노출은 topless·visible nipples·cameltoe까지 허용 / 성기·climax 묘사 없음 /\n"
                       " 화면에는 기본 주인공 혼자, 상대방은 POV 손이나 프레임 밖 — 두 사람 풀샷 금지)")
        pose_policy = ("   단 pose에 **성기 단어(vagina/pussy/vulva/labia/penis/glans/clitoris) 금지** — 최종 프롬프트에서 제거된다.\n"
                       "   nipples/topless/cameltoe 노출은 허용. 강한 성행위도 화면에는 스킨십/포옹/밀착으로 보인다.")
        rule6_climax = '6. climax는 항상 빈 문자열 "" (청년향: climax 이벤트 없음).'
        genre = "청년향 만화"
    return f"""당신은 **{genre}**의 스토리보드 작가입니다. 아래 회차 정보만으로 {count_txt} 스토리보드를 만듭니다.
{policy_line}

[에피소드] EP{ep_num_1based} / 총 {total_eps}화
[화자 이름] 주인공 = {name1} / 상대방 = {name2}   (대사 말머리에 이 이름을 그대로 쓴다)
[주인공 시트]
{proto}
[상대방 시트]
{partner}
{sub_block}{body_block}{prev_block}
[이 회차 가이드 (기승전결 순서 그대로)]
{guides}
[반드시 1회 이상 등장할 행동 키워드($)]
{acts}
{layout_block}
출력 형식 (JSON 배열 외 텍스트 금지, ```로 감싸도 됨):
[
  {{"no": 1, "type": "face", "caption_ko": "설명(지문) — 없으면 \"\" (주어+서술 완전 문장)",
   "lines": [{{"kind": "speech", "who": "{name1}", "text": "대사 또는 신음(최장 {DIALOG_MAX_LEN}자)",
              "emo": "anger|surprise|sweat|heart|gloom|sparkle|question 중 하나 (없으면 \"\")"}},
             {{"kind": "thought", "who": "{name2}", "text": "속마음(최장 {DIALOG_MAX_LEN}자)", "emo": ""}}],
   "sfx": "의성어/의태어(없으면 \"\", 최장 {SFX_MAX_LEN}자)",
   "wide": false, "facing": "front", "clothes": "police uniform",
   "pose": "She is ... English pose sentence.", "camera": "close_up", "position": "NONE", "climax": ""}},
  ...
]

필수 규칙:
{rule1}
{rule2}
3. pose는 반드시 영어 한 문장(두 문장 가능): "She ..." 또는 "She is ..."로 시작, 주인공은 여성(She), 상대방은 him/her 대명사 사용.
   danbooru 태그를 문장 안에 섞어 쓸 수 있다 (예: ", her bikini bottom pulled aside, cameltoe, trembling").
{pose_policy}
4. camera 어휘는 정확히 다음 5개 중 하나: front_view | side_view | back_view | close_up | pov
5. position은 다음 7개 중 하나 (상대방 상태): He is standing. | He is sitting. | He is walking. |
   He is lying down. | He is lying on top of her. | He is behind her. | NONE
{rule6_climax}
7. $ 행동 키워드는 최소 1개 컷의 pose에 실제 동작으로 등장시킨다.
8. **컷의 화면 텍스트는 다음 3가지 중 하나를 고른다** (이게 이 repo의 만화 문법이다):
   (1) **설명만**  : caption_ko만 채운다. lines는 []로 둔다. — 상황 전개·시간 경과·배경 컷
   (2) **대사/속마음만** : lines만 채운다(1~2개). caption_ko는 ""로 둔다. — 인물 클로즈업·대화
   (3) **설명+대사** : 둘 다 채운다 = **큰 이벤트 컷** (회당 2~4컷만, 남발 금지)
   clothes는 **회차 의상을 그대로** 영문 태그로 쓴다. 본문에서 옷이 실제로 바뀌는 장면(갈아입기·옷을 벗는다·젖는다)이
   없을 한 'tattered / ripped / dirty' 같은 **옷이 망가지거나 사라진다는 어구**를 먼저 제안하지 않는다
   — 근거 없이 넣으면 그 컷이 옷 없이 그려진다. 회차 의상 태그를 복사해 넣는 편이 안전하다.
   caption_ko는 지문(해설)만 쓰고 대사 금지, 최장 {CAPTION_MAX_LEN}자(길어도 된다 — 잘리지 않는다).
{chatty_rule}
   **반드시 완전한 서사 문장**(주어 + 서술어, '~한다/~었다/~고 있다' 종결).
   명사 나열·관형형 토막('네온사인이 빛나는 골목' 같은) 금지 — 소리 내어 읽으면 한 문장이어야 한다.
9. lines는 **최대 {DIALOG_LINES}개의 풍선**: kind=speech(입으로 하는 말 → 말풍선) | thought(속마음·혼잣말 → 속마음 풍선).
   한 풍선은 최장 {DIALOG_MAX_LEN}자 — 그보다 긴 말은 **두 개의 풍선으로 나눔**(한 풍선 한 마디).
   화자 이름은 who에 쓰고 text에는 넣지 않는다(화면에 이름이 안 찍히고 풍선 꼬리만 화자를 가리킨다).
   신음소리·파상음·잘려 나가는 말 적극 사용 (text 예: "아… 응… ♡", "좋아, 다 나오잖아.").
   **대사가 없는 컷은 lines를 []로 비워도 된다**(그때는 설명이 있어야 한다).
   emo는 그 대사의 감정도(풍선 곁에 작은 표시를 그린다): anger(분노) | surprise(놀람) | sweat(식은땀) |
     heart(두근) | gloom(가라앉음) | sparkle(반짝) | question(의문) | ""(없음) — 애매하면 ""로 둔다.
   ※ who에 정확한 화자 이름을 적어야 풍선 자리가 잡힌다(주인공=왼쪽, 상대방=오른쪽).
   ※ 옛 형식 `"dialog": ["{name1}: 대사", "{name2}: 대사"]`이나 `"(속마음)"` 문자열 출력도 그대로 받는다(자동 변환).
{rule10}
11. 얼굴 클로즈업 컷(face)은 표정을 언어로 분명히 적는다 (예: "Her eyes are wet, lips parted.").
12. facing(모든 컷 필수)은 딱 두 값 중 하나 — 컷의 구도 규칙이다:
    "front" = 인물 몸/얼굴이 정면(독자/카메라를 정면으로)
    "right" = 인물이 화면 왼쪽에 서서 왼쪽→오른쪽으로 향함/봄
    ※ 풍선 자리·꼬리는 기본적으로 **화자(lines[].who)**가 정하고, facing은 화자를 모를 때만 보조로 쓰인다
    wide=true 컷은 무조건 "right". **face 컷은 무조건 "front"** — 얼굴 전체가 보이는 정면 초상화만
    허용(측면/후면/POV/고개꺾기 금지. 위반 시 보정 단계에서 front+close_up로 강제 정규화).
    pose 문장에도 이를 반영해 쓴다 (예: front → "She faces the viewer, ...", right → "She is on the
    left of the frame, looking to the right, ...").
13. **1인 화면 원칙**: 컷마다 화면 인물은 기본 주인공 혼자(1girl 또는 1boy). 상대방은 다음으로만 등장:
    (a) camera=pov 컷 — 상대방의 손/팔/어깨만 화면 가장자리에 보임
    (b) 프레임 밖 — 목소리(대사)만
    (c) 손·소매· 셔츠 끝 같은 부분 클로즈업
    (d) 여러 인물이 화면에 같이 보이는 컷(군중/행인/친구 등)은 예외 허용 — 그런 컷만 `multi: true`를 붙인다
    (multi 컷은 다중 캐릭터 가이드, POV 컷은 POV 가이드로 본문을 짜고, portrait(face)은 기존 방식 유지).
    두 사람이 몸통째로 같이 나오는 풀샷은 금지. A/B 대화 컷은 1인 1컷으로 교대한다.
14. **clothes (모든 컷 필수) — 컷별 복장**: 이 컷에서 주인공이 입고 있는 것만 소문자 영문 태그(최대 6개).
    직전 컷에서 연속돼야 하고, caption/pose에 벗고·입고·갈아입는 동작이 나오는 컷에서만 복장을 바꾼다.
    옷이 안 바뀌면 앞 컷과 똑같이 쓰거나 ""(비우면 앞 컷 것이 자동 승계). 노출 정책 동일
    (topless·visible nipples·cameltoe 허용, 성기/속옷 벗은 상태 묘사 금지 — 속옷은 lingerie 등으로 짧게).
{rule15}"""


_PANEL_KEYS = ("caption_ko", "position", "clothes", "camera", "climax", "dialog", "facing",
               "center", "multi", "pose", "type", "tier", "wide", "page", "no",
               "lines", "sfx")     # [2026-09-09] 화면 문법(풍선/의성어) 필드 추가


def _json_repair(s: str) -> str:
    """json.loads 실패 후 1차 완화 — 26B Q4(ollama)에서 실측된 손상 패턴들.

      - `im: "front",`      → 따옴표 빠진 키
      - `}, ]`              → trailing comma
      - `     but "facing":`  → 키 앞에 섞여 들어온 잡단어
      - `"тряtype": "face",`  → 키에 섞인 비ASCII 잡음 → 아는 키 접미어로 복구
    """
    s = re.sub(r",\s*([}\]])", r"\1", s)
    s = re.sub(r'(?m)^(\s*)([A-Za-z_][A-Za-z0-9_\- ]*)\s*:', r'\1"\2":', s)     # 따옴표 빠진 키
    s = re.sub(r'(?m)^(\s*)[A-Za-z가-힣]+\s+(?=")', r"\1", s)                     # 키 앞 잡단어("but \"facing\":")

    def _fix_key(m):
        raw = m.group(2).strip()
        for k in sorted(_PANEL_KEYS, key=len, reverse=True):                   # 긴 키 우선("caption_ko" vs "no")
            if raw.lower().endswith(k):
                return f'{m.group(1)}"{k}":'
        return m.group(0)

    s = re.sub(r'(?m)^(\s*)(?![ "])[^\s"]*"\s*:.*$', "", s)   # 키 자체가 깨진 줄(`быć": "NONE",`)은 통째로 버린다
    return re.sub(r'(?m)^(\s*)"([^"]*)"\s*:', _fix_key, s)                     # "trяtype": → "type":


def _iter_json_objects(text: str):
    """배열 텍스트에서 최상위 {...} 블록을 중괄호 균형으로 하나씩 뽑는다(문자열 안 중괄호는 오차 허용).
    yields (start, end, block) — 마지막 블록 뒤 나머지(잘린 꼬리) 위치를 알기 위해 인덱스를 함께 준다."""
    depth, start = 0, -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
            if depth == 0 and start >= 0:
                yield start, i + 1, text[start:i + 1]
                start = -1


def _salvage_truncated_block(s: str):
    """출력 토큰에 잘려 {…이 닫히지 않은 블록 구제 (num_ctx/max_tokens 잘림은 26B Q4에서 다반사).

    여기까지 쓴 필드는 전부 유효하고 마지막 문자열/괄호만 열려 있다 — 따옴표·괄호를 닫아
    침묵 컷이 될 컷 하나를 살린다. 마지막 컷이 잘리는 게 반복되면 페이지 질감이 확 죽는다.
    """
    i = s.find("{")
    if i < 0:
        return None
    s = re.sub(r"\\+$", "", s[i:].strip())          # 말단의 미완 이스케이프(`...\\`) 제거
    stack, in_str, esc = [], False, False
    for ch in s:
        if esc:
            esc = False
            continue
        if in_str and ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
        elif not in_str:
            if ch in "{[":
                stack.append(ch)
            elif ch in "}]":
                if stack and ((ch == "}") == (stack[-1] == "{")):
                    stack.pop()
    closes = "".join("}" if c == "{" else "]" for c in reversed(stack))
    bases = [s + '"'] if in_str else [s]              # 문자열 중간에서 잘렸으면 일단 닫는다
    if not in_str:
        j = s.rfind('"')                              # 또는 "key":까지 열고 만 경우 그 필드를 버린다
        if j > 0:
            bases.append(s[:j + 1])
    for base in bases:
        # 값 없이 "key":까지만 열린 꼬리는 그 필드만 버린다(선행 쉼표까지 함께)
        b = re.sub(r',\s*"[^"]*"\s*:\s*$', "", base)
        b = re.sub(r'^\s*"[^"]*"\s*:\s*$', "{", b)
        b = re.sub(r",\s*$", "", b.rstrip())
        cand = b + closes
        for attempt in (cand, _json_repair(cand)):
            try:
                v = json.loads(attempt)
            except Exception:
                continue
            if isinstance(v, dict) and len(v) >= 2:
                return v
    return None


def _extract_json_array(text: str):
    """LLM 응답에서 JSON 배열만 관대하게 추출"""
    if not text:
        return []
    t = text.strip()
    t = re.sub(r"^```[a-zA-Z]*", "", t).strip()
    t = re.sub(r"```$", "", t).strip()
    try:
        v = json.loads(t)
        return v if isinstance(v, list) else []
    except Exception:
        pass
    i = t.find("[")
    if i >= 0:
        j = t.rfind("]")
        # [2026-09-08] 출력 잘림이면 배열 닫대가 없다 — 그 경우 끝까지 본다. 닫힌 대괄호에서
        # 자르면 그 이후로 실제 응답된 컷들이 조용히 버려진다. (주의: rfind(']')는 dialog
        # 안의 ']'에 걸린다 — 텍스트가 ']'로 끝나지 않는 한 자르지 말고 끝까지 봐야 한다.)
        seg = t[i:j + 1] if (j > i and t.endswith("]")) else t[i:]
        for cand in (seg, _json_repair(seg)):
            try:
                v = json.loads(cand)
                if isinstance(v, list):
                    return v
            except Exception:
                continue
        # [2026-09-08] 통째 파싱이 Still 실패하면 컷 단위로 쪼갠다 — 26B Q4는 컷 하나에서
        #   키가 깨지는데(`"быć": "NONE",`) 그 때문에 전체(8컷)를 버리면 안 된다.
        #   못 읽은 컷만 버리고 나머지는 슬롯 패딩으로 채운다.
        objs, bad, last_end = [], 0, 0
        for _st, en, blk in _iter_json_objects(seg):
            last_end = en
            got = None
            for cand in (blk, _json_repair(blk)):
                try:
                    got = json.loads(cand)
                    break
                except Exception:
                    continue
            if isinstance(got, dict) and got:
                objs.append(got)
            else:
                bad += 1
        # 잘려서 닫히지 않은 마지막 컷 — 쓴 필드만이라도 구제한다(침묵 컷보다 낫다)
        tail = seg[last_end:]
        if "{" in tail:
            sv = _salvage_truncated_block(tail)
            if sv:
                objs.append(sv)
                _clog(f"JSON 잘림 → 마지막 컷 구제({len(sv)}필드 복원)")
        if bad:
            _clog(f"JSON {bad}블록 파싱 실패 → 컷 {len(objs)}개만 사용(나머지는 슬롯 패딩)")
        return objs
    return []


def _norm_type(v) -> str:
    s = str(v or "").strip().lower()
    if s.startswith("f") or "얼굴" in s or "표정" in s or "close" in s:
        return "face"
    return "action"


def _norm_bool(v) -> bool:
    s = str(v or "").strip().lower()
    return s in ("true", "1", "yes", "y", "o", "on")


def _norm_dialog(v) -> list:
    """[하위호환] dialog 필드 → 대사 문자열 리스트. 화면 문법은 _norm_lines가 새로 잡고
    이 함수는 그 결과를 "화자: 말" 문자열로 되돌려 준 것(옛 스크립트·메타용)."""
    return [f"{b['who']}: {b['text']}".strip(": ").strip() if b.get("who") else b["text"]
            for b in _norm_lines(v)]


_WHO_PREFIX_RE = re.compile(r"^\s*([가-힣A-Za-z]{1,8})\s*[:：]\s*(.+)$", re.S)
_THOUGHT_MARK_RE = re.compile(
    r"^\s*[\(\u300c\uff08]?\s*(?:속마음|혼잣말|마음속)\s*[:：]?\s*(.*?)\s*[\)\u300d\uff09]?\s*$", re.S)


# [2026-09-09] 감정 이모티콘(분노/놀람/땀/하트/음영/반짝/물음) — 대사에서 냄새가 나면 풍선 곁에 그린다.
#   LLM이 lines[].emo 로 직접 주면 그것을 우선하고, 없으면 아래 표로 추정한다.
_EMOTIF_HINTS = (("anger", ("짜증", "젠장", "으으윽", "분노", "이런", "칫")),
                 ("surprise", ("어?", "설마", "뭐?", "헉", "앗", "설마야")),
                 ("sweat", ("땀", "어떡", "식은땀", "icolo")),
                 ("heart", ("하트", "두근", "사랑스", "귀여", "좋아한다")),
                 ("gloom", ("쓸쓸", "외로", "시무룩", "풀죽")),
                 ("sparkle", ("반짝", "눈부", "예뻐", "멋있")))
_EMOTIF_RE = re.compile(r"^(anger|surprise|sweat|heart|gloom|sparkle|question)$", re.I)


# [2026-09-09] 화면 감정(이모티콘 7종) → 렌더 표정 태그. 화면에 보이는 감정과 그림의 표정이
#   같은 말이어야 한다. 이게 없으면 회차 태그셋이 고른 **한 표정**이 모든 컷에 붙는다(아헤가오화).
_EMO_FACE_TAGS = {
    "anger":    "angry, furrowed brow, angry shout",
    "surprise": "surprised, wide eyes, open mouth",
    "sweat":    "uneasy sweat, sweat drop, wavy mouth",
    "heart":    "lovey, blushing, soft smile",
    "gloom":    "sad, downcast eyes, wavy mouth",
    "sparkle":  "happy, excited, sparkling eyes, open mouth",
    "question": "confused, tilted head, open mouth",
}


def _panel_face_emotion(panel) -> str:
    """이 컷의 표정 key — 주인공 풍선의 감정이 우선, 없으면 화면 텍스트에서 추정한다."""
    lines = (panel or {}).get("lines") or []
    me = str(getattr(config, "name", "") or "").strip()
    for b in lines:
        e = str((b or {}).get("emo") or "")
        if not _EMOTIF_RE.fullmatch(e or ""):
            continue
        who = str((b or {}).get("who") or "")
        if not me or who == me:
            return e
    for b in lines:                                     # 화자 불문(1인 화면이 기본) — 남아 있는 감정 사용
        e = str((b or {}).get("emo") or "")
        if _EMOTIF_RE.fullmatch(e or ""):
            return e
    txt = " ".join([str((b or {}).get("text") or "") for b in lines]
                   + [str((panel or {}).get("caption_ko") or "")])
    return _emo_guess(txt) if txt.strip() else ""


def _emo_guess(text: str) -> str:
    """대사 텍스트 → 감정 키(모르면 ""). 표시가 애매하면 그냥 안 그린다."""
    t = str(text or "")
    for kind, cues in _EMOTIF_HINTS:
        if any(c in t for c in cues if c):
            return kind
    tail = t.rstrip()
    if tail.endswith("?") or tail.endswith("？"):
        return "question"
    if tail.endswith("!!") or tail.endswith("!?") or tail.endswith("?!"):
        return "surprise"
    return ""


def _speaker_of(who: str) -> str:
    """화자 표기 → "me"(주인공) | "other"(상대방) — 풍선 자리와 꼬리 방향을 이 값으로 정한다.

    사용자가 정한 화면 규칙: 주인공 = 왼쪽 위(→왼쪽 아래), 상대방 = 오른쪽 위(→오른쪽 아래).
    비어 있으면 주인공 시점(이 만화의 기본), 두 사람 이름 어디에도 안 걸리면 제3자 = other.
    """
    w = re.sub(r"\s+", "", str(who or "")).lower()
    if not w:
        return "me"
    n1 = re.sub(r"\s+", "", str(getattr(config, "name", "") or "")).lower()
    n2 = re.sub(r"\s+", "", str(getattr(config, "name2", "") or "")).lower()
    if n1 and (w == n1 or w in n1 or n1 in w):
        return "me"
    if n2 and (w == n2 or w in n2 or n2 in w):
        return "other"
    if w in ("나", "필자", "주인공", "pov", "me"):
        return "me"
    return "other"


def _norm_lines(v) -> list:
    """[2026-09-09] 컷 화면 텍스트(대사/속마음) → [{"kind":"speech|thought","who":str,"text":str}]

    받는 것: {"kind","who","text"} dict / "유즈키: 대사" 문자열 / "(속마음)" 문자열 / 그 조합 리스트.
    규칙: 풍선은 최대 DIALOG_LINES(2)개, 한 풍선 DIALOG_MAX_LEN자(넘으면 …), 화자 이름은 text에서
    떼어 who로 옮긴다(화면에 이름이 안 찍힌다), (…)·'속마음:' 표시는 thought로 본다.
    """
    if v is None:
        return []
    if isinstance(v, str):
        items = re.split(r"\n+", v)
    elif isinstance(v, dict):
        items = [v] if (v.get("text") or v.get("line") or v.get("kind")) else list(v.values())
    elif isinstance(v, (list, tuple)):
        items = list(v)
    else:
        items = [str(v)]
    out = []
    for it in items:
        who, kind, s, emo = "", "speech", "", ""
        if isinstance(it, dict):
            kind = str(it.get("kind") or it.get("type") or "speech").strip().lower()
            who = str(it.get("who") or it.get("speaker") or "").strip()
            s = str(it.get("text") or it.get("line") or it.get("dialog") or "").strip()
            emo = str(it.get("emo") or it.get("emotion") or "").strip().lower()
        else:
            s = str(it or "").strip().replace("\n", " ").strip('"').strip()
        if not s:
            continue
        if s.startswith("(") and s.endswith(")") and len(s) > 2:
            kind, s = "thought", s[1:-1].strip()          # (…) 전부 감싸면 속마음
        else:
            m = _THOUGHT_MARK_RE.match(s)                  # "속마음: …", "(속마음) …"
            if m and ("속마음" in s or "혼잣말" in s or "마음속" in s):
                kind, s = "thought", (m.group(1) or s).strip()
        if not who:
            m2 = _WHO_PREFIX_RE.match(s)
            if m2:
                who, s = m2.group(1).strip(), m2.group(2).strip()
        # [2026-09-09] 긴 대사를 '…'로 버리지 않는다 — 두 개의 풍선으로 나눠 담는다(화면이 말을 다 한다)
        if len(s) > DIALOG_MAX_LEN and len(out) < DIALOG_LINES:
            head, rest = _split_dialog(s)
            if head and rest:
                if not _EMOTIF_RE.fullmatch(emo or ""):
                    emo = _emo_guess(s)
                out.append({"kind": "thought" if kind.startswith("t") else "speech",
                            "who": who, "text": head, "emo": emo if _EMOTIF_RE.fullmatch(emo) else ""})
                s = rest
                emo = ""
        if s:
            if not _EMOTIF_RE.fullmatch(emo or ""):
                emo = _emo_guess(s)                       # LLM이 안 주면 대사에서 추정
            out.append({"kind": "thought" if kind.startswith("t") else "speech",
                        "who": who, "text": s, "emo": emo if _EMOTIF_RE.fullmatch(emo) else ""})
        if len(out) >= DIALOG_LINES:
            break
    return out


def _split_dialog(s: str):
    """긴 대사를 풍선 두 개로 나눈다 — 끊는 곳은 문장부호·조사 앞, 없으면 길어서 자르되 '…'는 붙이지 않는다."""
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    half = max(6, len(s) // 2)
    best = None
    for m in re.finditer(r"[.!?。…](?:\s|$)|(?<=[가-힣])[,·](?=\s)|\s(?=[가-힣](?:는|는|이|가|을|를|도|만|의|에|로))", s[:len(s)]):
        if m.start() <= 0:
            continue
        if best is None or abs(m.start() - half) < abs(best - half):
            best = m.end()
    if best and 4 <= best < len(s) - 1:
        return s[:best].strip(), s[best:].strip()
    cut = s.rfind(" ", 0, half + 4)                  # 띄어쓰기가 그 근처에 없으면 마지막 공백
    if cut <= 4:
        cut = half
    return s[:cut].strip(), s[cut:].strip()


def panel_text_payload(panel) -> dict:
    """컷 1개 → comic_page_merge 화면 문법 페이로드 (설명 박스 + 풍선 ≤2 + 의성어)."""
    p = panel or {}
    role = str(p.get("text_role") or "")
    emo_on = bool(getattr(config, "comic_emo_marks", True))
    return {"narration": str(p.get("caption_ko") or "").strip(),
            "narr_large": bool(p.get("narr_large")) or role in ("summary", "epilogue"),
            "balloons": [{"kind": b["kind"], "text": b["text"],
                          "speaker": _speaker_of(b.get("who") or ""),
                          "emo": (b.get("emo") or "") if emo_on else "",
                          "side": ("left" if str(p.get("facing") or "").lower() == "left" else
                                   "right" if str(p.get("facing") or "").lower() == "right" else None)}
                         for b in (p.get("lines") or [])],
            "sfx": str(p.get("sfx") or "").strip(),
            "fade": float(p.get("fade") or 0.0)}


def panel_text_blocks(panel) -> list:
    """[하위호환] 컷 화면 텍스트를 평문 블록 리스트로 → [설명, 대사…].
    화면 문법의 원본은 `panel_text_payload`다(설명 박스 + 풍선 + 의성어)."""
    blocks = []
    cap = str((panel or {}).get("caption_ko") or "").strip()
    if cap:
        blocks.append(cap)
    blocks += [d for d in ((panel or {}).get("dialog") or []) if str(d).strip()]
    return blocks


def _apply_text_role(p: dict, role: str, notes: list) -> dict:
    """[2026-09-09] ★ 화면 문법이 강제되는 슬롯(cut.yaml tier `role`)을 컷에 박는다.

      summary  : 각 기승전결의 **가장 앞 컷** — 인물 없이 배경만(타치키리) + 상황 요약 큰 지문
                 (지문이 컷 면적의 ~70%, 풍선 없음)
      epilogue : 결의 마지막 이벤트신 — 컷을 **반투명**으로처리하고 에필로그 큰 지문
    """
    role = str(role or "")
    if role == "prologue":                     # ★프롤로그 = 회차집 첫 회차만 나오는 도입 요약 컷
        if not bool(getattr(config, "comic_prologue_cut", True)):
            return p
        role = "summary"
        p["prologue"] = True                   # 화면에서는 요약과 같지만 라벨/진단에서 구분한다
    if role == "summary" and not bool(getattr(config, "comic_summary_cuts", True)):
        return p
    if role == "epilogue" and not bool(getattr(config, "comic_epilogue", True)):
        return p
    if role not in ("summary", "epilogue"):
        return p
    p["text_role"] = role
    p["narr_large"] = True                      # 지문이 컷의 ~70%
    p["type"], p["camera"] = "action", "front_view"
    p["lines"], p["dialog"], p["sfx"] = [], [], ""      # ★슬롯은 설명 중심
    if role == "summary":
        p["bg_only"] = True                     # 캐릭터 없이 배경만
        p["pose"] = ("Wide establishing shot of the place, nobody in the frame, "
                     "quiet atmosphere, cinematic light.")
    else:
        p["fade"] = float(CPM.FADE_ALPHA)       # 이벤트신을 반투명하게
    notes.append(f"컷 {p['no']}: ★{role} 슬롯 → 큰 지문"
                 + ("+배경만" if role == "summary" else "+반투명"))
    return p


def _default_text_roles(panels: list, notes: list, ep_num_1based: int = 1):
    """cut.yaml 밖(레거시 자동 레이아웃)에서도 ★규칙은 같은 형태로 적용한다.

    [2026-09-09] 사용자 지정 계산대로 ★는 여기만: 회차의 **첫 컷** 하나 + (마지막 회차라면)
    끝 **에필로그** 하나. 그 외 컷은 평범한 설명/풍선 컷으로 남는다.
    """
    if not panels:
        return
    if bool(getattr(config, "comic_summary_cuts", True)):
        _apply_text_role(panels[0], "summary", notes)
    total_eps = max(1, int(getattr(config, "total_episodes", 1) or 1))
    if (len(panels) > 1 and bool(getattr(config, "comic_epilogue", True))
            and int(ep_num_1based) >= total_eps):
        _apply_text_role(panels[-1], "epilogue", notes)


def _repair_panels(raw_list, dollar_actions=None, page_plans=None, max_panels: int = MAX_PANELS,
                   ep_num_1based: int = 1):
    """LLM 컷 목록 → 검증/수정된 컷 리스트. (panels, notes) 반환

    page_plans(cut.yaml) 모드: 컷 수 = 슬롯 수로 확정(부족하면 폴백 컷 패딩/초과 절단),
    슬롯의 wide/page/tier/share 강제. face 비율·wide 상한 보정은 템플릿이 대신하므로 스킵.
    max_panels: 레거시(레이아웃 off) 모드의 컷 상한. **0이면 무제한**(본문 길이가 컷 수를 정한다).
    """
    notes = []
    slots = spec_slots(page_plans)
    spec = bool(slots)
    panels = []
    for i, it in enumerate(raw_list or [], start=1):
        if not isinstance(it, dict):
            continue
        pose = str(it.get("pose") or it.get("text") or "").strip()
        if not pose:
            continue
        if "#" in pose:                                  # LLM이 슬롯을 붙여 쓰면 버리고 ours로 재조립
            pose = pose.split("#", 1)[0].strip()
        cap = str(it.get("caption_ko") or it.get("caption") or "").strip().replace("\n", " ")
        cap = _clamp_caption(cap, 0)                    # 자르지 않는다(★지문도 원문 그대로)
        lns = _norm_lines(it.get("lines") or it.get("dialog") or it.get("lines_ko")
                          or it.get("speech") or it.get("balloons"))
        sfx = str(it.get("sfx") or it.get("oto") or "").strip().replace("\n", " ")
        if len(sfx) > SFX_MAX_LEN:
            sfx = sfx[:SFX_MAX_LEN]
        # [2026-09-07] 컷별 복장: 소문자 영문 태그 ≤6, 성기 태그는 여기서 바로 제거
        cl = str(it.get("clothes") or it.get("outfit") or "").strip().lower().replace("\n", " ")
        cl, _ = anima_gen.strip_anatomy_tags(cl)
        # 스트립이 남기는 홀로 남은 수식어("visible pussy"→"visible")는 옷 태그로 무의미 → 버린다
        _dangling = {"visible", "visible", "and", "with", "her", "his", "the", "a", "an", "of",
                     "none", "no clothes", "-"}   # "none": LLM의 빈 값 표기 → 공백 취급(직전 복장 승계/회차 폴백)
        cl = ", ".join([t for t in (x.strip().strip(".,") for x in cl.split(","))
                        if t and t not in _dangling][:6])
        wide = _norm_bool(it.get("wide") or it.get("wide_frame") or it.get("aspect"))
        cam = str(it.get("camera") or "").strip()
        pos = str(it.get("position") or "NONE").strip()
        clim = str(it.get("climax") or "").strip()
        p = {"no": i, "type": _norm_type(it.get("type")), "caption_ko": cap,
             "lines": lns,                                                   # [2026-09-09] 풍선 ≤2
             "dialog": [f"{b['who']}: {b['text']}" if b["who"] else b["text"] for b in lns],
             "sfx": sfx, "text_role": "", "narr_large": False,
             "bg_only": False, "fade": 0.0,                                  # ★요약/에필로그 슬롯용
             "wide": wide, "pose": pose, "camera": cam, "position": pos, "climax": clim,
             "facing": str(it.get("facing") or "").strip().lower(), "clothes": cl,
             "multi": _norm_bool(it.get("multi") or it.get("two_person"))}
        panels.append(p)

    if not panels:
        return [], ["LLM 응답에 유효한 컷이 없음"]

    if spec:
        # 컷 수 = 템플릿 슬롯 수로 확정. 부족 패딩/초과 절단 (MAX_PANELS 대신 슬롯 수 기준)
        E = len(slots)
        if len(panels) > E:
            notes.append(f"컷 {len(panels)}개 → 템플릿 {E}컷 초과분 절단")
            panels = panels[:E]
        while len(panels) < E:
            s = slots[len(panels)]
            panels.append({"no": len(panels) + 1, "type": "action",
                           "caption_ko": "", "dialog": [], "lines": [], "sfx": "",
                           "text_role": "", "narr_large": False, "bg_only": False, "fade": 0.0,
                           "wide": s["wide"],
                           "pose": "She stands quietly, calm expression.",
                           "camera": "front_view", "position": "NONE", "climax": "",
                           "facing": "right" if s["wide"] else "front"})
            notes.append(f"템플릿 슬롯 {len(panels)}개 미응답 → 폴백 컷 채움")
    # 개수 클램프 (레거시 모드만; spec은 이미 슬롯 수로 절단됨). max_panels=0 → 무제한
    elif max_panels and len(panels) > max_panels:
        notes.append(f"컷 {len(panels)}개 → {max_panels}개으로 절단")
        panels = panels[:max_panels]
    if not spec:
        _default_text_roles(panels, notes, ep_num_1based)          # 레거시 모드도 ★규칙은 적용한다

    # [2026-09-07] 컷별 복장 연속성: 빈 clothes는 직전 컷 것을 승계(탈의 후 원복 금지).
    # 첫 컷이 비어 있으면 회차 기본 의상(config 태그) 폴백 그대로.
    _last_clothes = ""
    for p in panels:
        if p.get("clothes"):
            _last_clothes = p["clothes"]
        elif _last_clothes:
            p["clothes"] = _last_clothes

    # [2026-09-07] portrait 정책: face 컷은 정면 풀페이스 초상화 강제 —
    #   facing=front 고정, 측면/후면/POV 카메라는 close_up로 교체(LLU가 어기더라도 결정론 보정).
    for p in panels:
        if str(p.get("type")) == "face":
            p["facing"] = "front"
            if str(p.get("camera")) in ("side_view", "back_view", "pov"):
                p["camera"] = "close_up"

    # 어휘 정규화
    for p in panels:
        if p["camera"] not in CAMERA_VOCAB:
            p["camera"] = "close_up" if p["type"] == "face" else "front_view"
            notes.append(f"컷 {p['no']}: camera 정규화 → {p['camera']}")
        if p["type"] == "face" and p["camera"] not in ("close_up", "pov"):
            p["camera"] = "close_up"
        if p["position"] not in POSITION_VOCAB:
            p["position"] = "NONE"
        p["climax"] = _norm_climax(p["climax"])

    # 컷 텍스트/wide/시선(facing) 보정 [2026-09-07]
    if spec:
        # 슬롯 메타 강제: wide/page/tier/share + 전폭 가로 슬롯은 action (face 클로즈업은 세로 전용)
        for i, p in enumerate(panels):
            s = slots[i]
            p["page"], p["tier"], p["share"] = s["page"], s["tier"], s["share"]
            p["center"] = s["center"]
            p["h"] = s.get("h") or 0.0
            _apply_text_role(p, s.get("role") or "", notes)
            if bool(p["wide"]) != bool(s["wide"]):
                p["wide"] = bool(s["wide"])
                notes.append(f"컷 {p['no']}: 슬롯 규격 → wide={bool(s['wide'])}")
            if s["wide"] and p["type"] == "face":
                p["type"] = "action"
                notes.append(f"컷 {p['no']}: 전폭 가로 슬롯 → type action")
            if (not s["wide"]) and s["share"] <= 0.35 and p["type"] == "action":
                p["camera"] = "close_up" if p["camera"] not in ("close_up", "pov") else p["camera"]
    if not WIDE_ENABLE:
        for p in panels:
            p["wide"] = False
        notes.append("wide OFF(--no-wide) → 전 컷 portrait")
    for p in panels:
        if len(p.get("lines") or []) > DIALOG_LINES:
            notes.append(f"컷 {p['no']}: 풍선 {len(p['lines'])}개 → {DIALOG_LINES}개로 제한")
            p["lines"] = p["lines"][:DIALOG_LINES]
            p["dialog"] = [f"{b['who']}: {b['text']}" if b["who"] else b["text"]
                           for b in p["lines"]]
        if not (p.get("caption_ko") or p.get("lines") or p.get("sfx")):
            notes.append(f"컷 {p['no']}: 화면 텍스트 없음(설명·풍선·의성어 전부 비음)")
        # [2026-09-09] 설명문을的长度로 자르지 않는다 — '알아서 …'가 화면의 글자를 죽였다.
        #   길면 박스가 자라고, 그래도 안 들어가면 렌더가 폰트를 줄인다(comic_page_merge._draw_caption_box).
        #   CAPTION_MAX_LEN은 이제 **LLM에게 쓰는 권장 길이**일 뿐이다.
        if p["type"] == "face" and p.get("wide"):
            p["wide"] = False
            notes.append(f"컷 {p['no']}: face 컷 wide → false")
        # facing 정규화: front | right 뿐. wide는 right 강제. 미지정 → front.
        f = str(p.get("facing") or "")
        if p["wide"]:
            p["facing"] = "right"
        elif f.startswith("r") or f in ("오른쪽", "왼쪽에서 오른쪽"):
            p["facing"] = "right"
        else:
            p["facing"] = "front"
        # facing이 허락하지 않은 카메라는 구도에 맞게 고친다 (policy: front 또는 left→right)
        if p["facing"] == "right" and p["type"] == "action" and p["camera"] in ("front_view", "back_view"):
            p["camera"] = "side_view"
            notes.append(f"컷 {p['no']}: facing=right → camera side_view")
        if p["facing"] == "front" and p["camera"] in ("side_view", "back_view"):
            p["camera"] = "front_view"
            notes.append(f"컷 {p['no']}: facing=front → camera front_view")
    if not spec:
        # wide 상한은 컷 수에 비례 (긴 회차에서 3고정은 단조로운 페이지를 만든다)
        wide_cap = max(MAX_WIDE_PANELS, int(round(len(panels) * WIDE_RATIO)))
        wides = [p for p in panels if p.get("wide")]
        if len(wides) > wide_cap:
            for p in wides[wide_cap:]:
                p["wide"] = False
            notes.append(f"wide {len(wides)}개 → {wide_cap}개로 제한")

        # face:action 비율 보정 (face가 목표보다 부족하면 표정 정황이 강한 action 컷을 face로 승격)
        def face_count():
            return sum(1 for x in panels if x["type"] == "face")

        target_lo = max(1, int(round(len(panels) * FACE_RATIO_MIN)))
        guard = 0
        while face_count() < target_lo and guard < 40:
            guard += 1
            cand = None
            for p in panels:
                if p["type"] == "action" and _FACE_HINTS.search(p["pose"] or p["caption_ko"]):
                    cand = p
                    break
            if cand is None:
                break
            cand["type"] = "face"
            cand["camera"] = "close_up" if cand["camera"] not in ("close_up", "pov") else cand["camera"]
            notes.append(f"컷 {cand['no']}: action → face 승격 (표정 정황)")
        # face 과잉이면 후반 face부터 action으로 되돌린다 (클라이맥스 컷은 face 유지)
        guard = 0
        while face_count() > int(round(len(panels) * FACE_RATIO_MAX)) and guard < 40:
            guard += 1
            demoted = None
            for p in reversed(panels[:-1]):                 # 마지막 컷은 감정 클로즈업 유지
                if p["type"] == "face":
                    demoted = p
                    break
            if demoted is None:
                break
            demoted["type"] = "action"
            if demoted["camera"] == "close_up":
                demoted["camera"] = "front_view"
            notes.append(f"컷 {demoted['no']}: face → action 강등 (비율 상한)")

    # $ 키워드 1회 이상 등장 확인 (없으면 중반 컷에 키워드 동작 주석 주입)
    if dollar_actions:
        joined = " ".join(p["pose"].lower() for p in panels)

        def _kw_present(a):
            # 원문(한국어)이 pose에 들어있는 경우는 드물다 → 정적 사전으로 영문화한 것도 찾아본다
            k = str(a).strip().lower()
            return k in joined or _apply_static_ko_map(str(a)).strip().lower() in joined

        missing = [a for a in dollar_actions if not _kw_present(a)]
        if len(missing) == len(dollar_actions) and dollar_actions:
            mid = panels[len(panels) // 2]
            kw = str(dollar_actions[0]).strip()
            kw_en = _apply_static_ko_map(kw).strip()
            mid["pose"] = f"She is doing {kw_en}. " + mid["pose"]
            notes.append(f"$키워드 미반영 → 컷 {mid['no']}에 '{kw}'"
                         + (f"(영문 태그 '{kw_en}')" if kw_en != kw else "") + " 주입")

    for i, p in enumerate(panels, start=1):
        p["no"] = i
    if not spec and len(panels) < MIN_PANELS:
        notes.append(f"컷 {len(panels)}개 (최소 {MIN_PANELS}개 미달 — 그대로 진행)")
    return panels, notes


_CLAUSE_END_RE = re.compile(r".*(?:[.!?。…]|(?<=[가-힣])\s(?=[가-힣])|,|·)", re.S)


def _clamp_caption(text: str, limit: int = SUMMARY_CAPTION_MAX_LEN) -> str:
    """지문을 잘 때 **절/문장 경계**에서 자른다. limit<=0이면 안 자른다(2026-09-09 기본값)."""
    if int(limit or 0) <= 0:
        return re.sub(r"\s+", " ", str(text or "")).strip()
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(s) <= limit:
        return s
    head = s[:max(8, limit - 1)]        # '…' 한 자리를 미리 비운다(총 길이가 limit을 넘지 않게)
    m = None
    for m in _CLAUSE_END_RE.finditer(head):
        pass
    cut = (m.group(0).strip() if m else head.strip()).rstrip(" ,·;:")
    if len(cut) < 24:                                  # 경계가 너무 일찍 나오면 그냥 길이로 자른다
        cut = head.rstrip()
    return cut + "…"


def _first_sentence(text: str, limit: int = SUMMARY_CAPTION_MAX_LEN) -> str:
    """본문(또는 장면 조각)에서 첫 문장을 뽑는다 — ★요약/에필로그 지문이 비었을 때의 폴백.

    창작을 더하지 않고 **본문 문장을 그대로 쓴다**(이 repo의 강령: 본문이 대본이다).
    """
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    if not s:
        return ""
    # 가이드/메타 형식("장소: … 상황: …")은 지문이 아니라 표 목록이다 → 라벨을 버리고 서술만 남긴다
    if re.match(r"^(장소|상황|시간|복장|인물|배경)\s*[:：]", s):
        parts = [p.strip() for p in re.split(r"(?<=\s)(?=(?:장소|상황|시간|복장|인물|배경)\s*[:：])", s) if p.strip()]
        s = max(parts, key=len)
        s = re.sub(r"^(장소|상황|시간|복장|인물|배경)\s*[:：]\s*", "", s).strip()
    m = None
    for pat in (r"[^.!?\n]\S*?(?:\.(?!\w)|\.{3}|!|\?)", r"[^\n]\S*?(?:다\.|요\.|라\.|해\.|게\.)"):
        m = re.search(pat, s)
        if m:
            break
    out = (m.group(0) if m else s).strip()
    if len(out) < 12:                                  # 지나치게 짧은 조각은 앞 문단을 쓴다
        out = s
    return out if limit <= 0 else _clamp_caption(out, limit)   # 기본은 '첫 문장을 그대로'(중간 토막 금지)


def _beat_of(i: int, quotas) -> int:
    """컷 번호 → 그 컷이 속한 장면 인덱스 (★폴백과 수다장이 폴백이 같은 대응을 쓴다)."""
    acc = 0
    for k, n in enumerate([int(x) for x in (quotas or [])]):
        if i < acc + n:
            return k
        acc += n
    return 0


def _split_sentences(text: str) -> list:
    """본문을 문장 단위로 나눈다 (한 장면을 두 컷이 같은 문장으로 반복하지 않게 소비한다)."""
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    return [p.strip() for p in re.split(r"(?<=[.!?])\s+", s) if p.strip()] if s else []


def _fill_chatty_narration(panels, beats, quotas, notes):
    """[2026-09-09] 수다장이 모드(--chatty): 지문 없는 컷의 하단 설명을 **LLM에게 작문을 시킨다**.

    재료로 그 컷의 pose·감정·화면 대사와 장면 본문을 주고, 한 컷에 한 문장을 JSON 배열로 받는다.
    LLM이 없거나 응답이 깨진 컷만 장면 본문의 남은 문장 → 짧은 기본 문장 순으로 조용히 메꾼다.
    """
    if not panels:
        return
    need = [(i, p) for i, p in enumerate(panels)
            if not str(p.get("caption_ko") or "").strip() and not p.get("bg_only")]
    if not need:
        return
    lines, ctx = [], {}
    for n, (i, p) in enumerate(need):
        bi = _beat_of(i, quotas)
        src = _split_sentences(beats[bi] if beats and bi < len(beats) else "")
        ctx[i] = src
        bal = " / ".join(f"{b.get('who', '')}: {b.get('text', '')}" for b in (p.get("lines") or [])[:2])
        lines.append(f"컷 {n + 1}(#{p.get('no')}): 행동={str(p.get('pose') or '')[:150]} | "
                     f"감정={_panel_face_emotion(p) or '없음'} | 화면 대사={bal or '없음'} | "
                     f"장면 본문={src[0][:160] if src else '없음'}")
    prompt = ("만화 컷 아래 붙일 **설명(지문)**을 만들어 주세요. 한국어 한 문장, 25~55자, 대사 금지, "
              "그 컷의 상황 또는 그녀의 행동\u00b7표정을 담습니다. 서술할 사건이 없는 컷은 행동\u00b7표정을 짧게 묘사합니다.\n"
              "출력은 JSON 배열 하나 — 입력 컷 순서대로 문자열 정확히 "
              + str(len(need)) + "개 (해설\u00b7코드블록 금지).\n\n" + "\n".join(lines) + "\n")
    got = []
    try:
        raw, _ = call_openai_for_text(prompt, messages=None, log_fn=_clog, temperature=0.7)
        arr = _extract_json_array(raw or "")
        got = [str(x).strip() for x in (arr if isinstance(arr, list) else []) if str(x).strip()]
    except Exception as e:
        _clog(f"EP 수다장이 설명 생성 실패: {e}")
    used = {}
    for n, (i, p) in enumerate(need):
        cap = got[n].strip('"\'') if n < len(got) else ""
        how = "LLM 작문"
        if not cap:
            src = ctx.get(i) or []
            bi = _beat_of(i, quotas)
            k = used.get(bi, 0)
            cap = src[k] if k < len(src) else ""
            if cap:
                used[bi] = k + 1
                how = f"장면 본문 {k + 1}번째 문장"
            else:
                cap = "그녀는 그 자리에 서 있다."
                how = "기본 문장(LLM 응답 없음)"
        p["caption_ko"] = re.sub(r"\s+", " ", cap).strip()
        notes.append(f"컷 {p['no']}: 수다장이 설명({how})")


def _fill_star_narration(panels, beats, quotas, notes):
    """[2026-09-09] ★요약/에필로그 컷의 지문이 비면 그 컷이 속한 **장면 본문의 첫 문장**으로 채운다.

    LLM이 ★ 슬롯 규칙을 어기는 실측 사례(EP1 컷9)가 있었다 — 화면이 아무 글자 없는 컷이 된다.
    """
    if not panels or not beats:
        return
    q = [int(x) for x in (quotas or [])]
    for i, p in enumerate(panels):
        if str(p.get("text_role") or "") not in ("summary", "epilogue"):
            continue
        if str(p.get("caption_ko") or "").strip():
            continue
        acc, bi = 0, 0
        for k, n in enumerate(q):
            if i < acc + n:
                bi = k
                break
            acc += n
        fb = _first_sentence(beats[bi] if bi < len(beats) else "")
        if fb:
            p["caption_ko"] = fb
            p["narr_large"] = True
            notes.append(f"컷 {p['no']}: ★{p['text_role']} 지문 미작성 → 본문 첫 문장으로 채움")


def request_panel_script(ep_num_1based: int, total_eps: int, client=None, retry: int = 1,
                         pages: int = None, episode_text: str = None,
                         chars_per_panel: int = None, max_panels: int = None,
                         beat_chars: int = None) -> dict:
    """본문 전체 → 컷 스크립트. 반환 {"panels","notes","raw","page_plans","beats","target_panels"}

    [2026-09-08] 방향이 뒤집혔다: 예전은 **레이아웃이 컷 수를 먼저 확정**하고 LLM을 그 枠에 맞췄고
    (그래서 본문 9천 자도 24컷에서 잘렸다) 지금은 **본문 길이가 컷 수를 정하고 레이아웃이 맞춘다**.
      ① 본문 길이 → 목표 컷 수 (CI.target_panels, chars_per_panel 기준)
      ② 목표 컷 수 → 페이지/슬롯 계획 (plan_pages_layout — 1~4페이지 고정 폐지)
      ③ 본문을 장면(창)으로 나누고 슬롯을 장면 길이에 비례 배분 (CI.allocate)
      ④ 장면마다 LLM 1회(직전 컷 캡션·복장을 넘긴다) → 이어서 전역 보정 1회
    장면을 쪼개는 이유는 취향이 아니라 물리 제약이다: num_ctx(기본 8192)를 넘기면 ollama가
    프롬프트 **앞부분(규칙 블록)을 조용히 버린다** + 26B Q4는 컷 30개짜리 JSON에서 키를 깨뜨린다.

    pages: None → config.comic_pages (0=본문 길이 자동 / N>0=N페이지 고정 / -1=cut.yaml OFF)
    """
    ep_idx = max(0, ep_num_1based - 1)
    proto, partner, sub = _sheet_texts(ep_idx)
    guides, dollar = _guides_for(ep_num_1based)
    body = str(episode_text if episode_text is not None else _episode_text(ep_idx)).strip()
    if pages is None:
        try:
            pages = int(getattr(config, "comic_pages", 0) or 0)
        except Exception:
            pages = 0
    pages = int(pages or 0)
    cpp = int(chars_per_panel or getattr(config, "comic_chars_per_panel", CI.CHARS_PER_PANEL))
    maxp = int(max_panels if max_panels is not None else (getattr(config, "comic_max_panels", 0) or 0))
    bchars = int(beat_chars or getattr(config, "comic_beat_chars", CI.BEAT_MAX_CHARS))

    # ① 본문 → 컷 수, ② 컷 수 → 페이지/슬롯 (pages<0 이면 cut.yaml 없이 자동 레이아웃)
    target = CI.target_panels(body, cpp, MIN_PANELS, maxp)
    _v0 = int(getattr(config, "comic_variation", 0) or 0)
    if _v0:                                  # 변동이 켜져 있으면 컷 예산을 -1/+1 흔든다(하한 MIN_PANELS 아래로 안 내려간다)
        target = max(MIN_PANELS, target + (CI._vary(f"budget|{ep_num_1based}", _v0, 3) - 1))
    page_plans, n_pages = (plan_pages_layout(ep_num_1based, target, pages)
                           if pages >= 0 else (None, 0))
    slots = spec_slots(page_plans)
    n_cut = len(slots) if slots else target

    # ③ 본문 → 장면(창). 장면 1개 = LLM 1호출 = 최대 PANELS_PER_BEAT_MAX컷
    # [2026-09-08] 자수가 적은 회차는 글자수 균등 분할이 기승전결을 가로질렀다 — LLM이 본문에서
    # 그대로 베껴 온 막 앵커(기/승/전/결 각 첫 문장)가 있으면 **막을 장면 골격**으로 삼고
    # 막당 최소 2컷을 하한으로 컷 예산을 올린다. 앵커가 어긋나면([]) 글자수 분할로 돌아간다.
    _segs_map = getattr(config, "ep_beat_segments", {}) or {}
    segs = _segs_map.get(ep_num_1based) or _segs_map.get(str(ep_num_1based)) or []
    acts = CI.split_by_segments(body, segs) if segs else []
    beat_acts = None
    unit_w = None                                    # 사건(액션) 기준 배분이 켜지면 유닛별 컷 수가 들어온다
    n_beats = max(1, -(-n_cut // CI.PANELS_PER_BEAT_MAX))
    if 2 <= len(acts) <= 8:
        if n_cut < 2 * len(acts):                  # 막당 2컷을 담을 컷 수부터 확보 (레이아웃 재계획)
            target = max(target, 2 * len(acts))
            page_plans, n_pages = (plan_pages_layout(ep_num_1based, target, pages)
                                   if pages >= 0 else (None, 0))
            slots = spec_slots(page_plans)
            n_cut = len(slots) if slots else target
        # [2026-09-08] 16:59 재현: 레이아웃은 '목표에 가장 가까운 구성' 선택이라 1페이지 5컷에
        #   만족해 기(起)막이 1컷이 됐다. 자동 모드에서는 페이지 수를 강제로 늘려 막당 2컷을 지킨다.
        if 0 <= pages and n_cut < 2 * len(acts):
            need_pages = -(-(2 * len(acts)) // max(1, int(round(_avg_slots_per_page()))))
            if need_pages > max(1, n_pages):
                page_plans, n_pages = plan_pages_layout(ep_num_1based, max(target, 2 * len(acts)), need_pages)
                slots = spec_slots(page_plans)
                n_cut = len(slots) if slots else target
        # [2026-09-09] 컷 배분의 저울을 '글자 수'에서 '사건(액션)'으로 옮긴다.
        #   실측: 본문 2666자 → 목표 6컷 → 레이아웃 8컷 → 4막×최소2컷이 8을 다 써서 [2,2,2,2].
        #   기가 2컷인 이유는 본문이 짧아서가 아니라 저울이 글자 수였기 때문이다.
        #   이제 LLM이 나눈 사건 유닛(강한 사건 2컷)이 예산과 배분을 모두 결정하고,
        #   글자 수 배분은 유닛이 없을 때의 폴백으로 남는다.
        _umap = getattr(config, "ep_action_units", {}) or {}
        units = _umap.get(ep_num_1based) or _umap.get(str(ep_num_1based)) or []
        unit_w = None
        if bool(getattr(config, "comic_action_cuts", True)) and units:
            ub, ua, uw = CI.split_acts_by_units(acts, units, strong_weight=int(
                getattr(config, "comic_cut_strong_weight", 2) or 2))
            if len(ub) >= 2:
                beats, beat_acts = ub, ua
                target = CI.target_panels_from_weights(uw, min_panels=MIN_PANELS, max_panels=maxp,
                                                       acts=len(acts))
                if target != n_cut:                              # 사건이 요구하는 컷 수로 레이아웃 재계획
                    page_plans, n_pages = (plan_pages_layout(ep_num_1based, target, pages)
                                           if pages >= 0 else (page_plans, n_pages))
                    slots = spec_slots(page_plans)
                    n_cut = len(slots) if slots else target
                unit_w = uw
        if unit_w is None and n_cut >= len(acts):
            beats, beat_acts = list(acts), list(range(len(acts)))
            while len(beats) < max(n_beats, len(acts)) and len(beats) < n_cut:  # JSON 보호선(컷6) 맞출까지만 긴 막 추가 분할
                i = max(range(len(beats)), key=lambda k: len(beats[k]))
                parts = CI.split_beats(beats[i], n_beats=2, max_chars=bchars)
                if len(parts) < 2:
                    break
                beats[i:i + 1], beat_acts[i:i + 1] = parts, [beat_acts[i], beat_acts[i]]
        elif unit_w is None:
            beats = (CI.split_beats(body, n_beats=n_beats, max_chars=bchars) or [""]) if body else [""]
    else:
        beats = (CI.split_beats(body, n_beats=n_beats, max_chars=bchars) or [""]) if body else [""]
    _var = int(getattr(config, "comic_variation", 0) or 0)
    quotas = CI.allocate(n_cut, (unit_w if unit_w else [max(1, len(b)) for b in beats]),
                         minimum=(2 if (beat_acts is not None and unit_w is None and n_cut >= 2 * len(beats))
                                  else (1 if n_cut >= len(beats) else 0)),
                         variation=_var)
    if unit_w:
        _clog(f"EP{ep_num_1based} 사건 {len(unit_w)}개(강한 사건 {sum(1 for w in unit_w if w > 1)}개 = 컷 2) "
              f"→ 컷 예산 {sum(unit_w)} / 레이아웃 {n_cut}컷 — 배분 저울은 '글자 수'가 아니라 '일어난 사건'")
    _clog(f"EP{ep_num_1based} 본문 {len(body)}자 → 목표 {target}컷 / 레이아웃 {n_cut}컷"
          + (f" ({n_pages}페이지 {[pl['template_id'] for pl in page_plans]})" if page_plans else " (자동 레이아웃)")
          + (f" → 장면 {len(beats)}개 {quotas} (기승전결 막 분할: "
             + "".join(_SITUATIONS[a] for a in sorted(set(beat_acts)) if a < len(_SITUATIONS)) + ")")
          if beat_acts is not None else f" → 장면 {len(beats)}개 {quotas}")

    # 막 분할이면 가이드도 막 단위로: [protagonist] 줄을 막 번호에 비례 배분하고,
    # 회차 전체 줄([partner]/[sub])은 마지막 막에 붙인다.
    beat_guides = None
    if beat_acts is not None and guides:
        _prot = [g for g in guides if g.startswith("[protagonist]")]
        _rest = [g for g in guides if not g.startswith("[protagonist]")]
        A = max(beat_acts) + 1
        if _prot:
            beat_guides = []
            for ai in beat_acts:
                lo2 = int(len(_prot) * ai / A)
                hi2 = max(lo2 + 1, int(len(_prot) * (ai + 1) / A + 0.5))
                beat_guides.append(_prot[lo2:hi2])
            beat_guides[-1] = beat_guides[-1] + _rest

    # ④ 장면별 LLM 호출 (실패/미달 시 장면당 retry회)
    raw_all, notes, tail, last_raw = [], [], [], ""
    for bi, beat in enumerate(beats, 1):
        quota = int(quotas[bi - 1]) if bi - 1 < len(quotas) else 0
        if quota <= 0:
            continue
        s0 = sum(quotas[:bi - 1])
        _ai = beat_acts[bi - 1] if beat_acts is not None else -1
        prompt = build_panel_script_prompt(
            ep_num_1based, total_eps, proto, partner, sub,
            beat_guides[bi - 1] if beat_guides else _slice_guides(guides, bi, len(beats)),
            dollar, page_plans=page_plans,
            episode_text=beat, panels_expected=quota, prev_tail=_beat_context(tail),
            beat_label=(((_SITUATIONS[_ai] if 0 <= _ai < len(_SITUATIONS) else str(_ai + 1)) + f"장면 {bi}/{len(beats)}"
                         if _ai >= 0 else (f"장면 {bi}/{len(beats)}" if len(beats) > 1 else ""))),
            slot_range=((s0, s0 + quota) if slots else None))
        got = []
        for attempt in range(1, max(1, retry) + 1):
            try:
                # 텍스트(상황 묘사·대사)는 gemma 전용 엔드포인트(text_*). 구조 필드도 같은 응답에서 나온다.
                raw, _ = call_openai_for_text(prompt, messages=None, log_fn=_clog,
                                              reasoning_effort="low", enable_thinking=False)
            except Exception as e:
                _clog(f"EP{ep_num_1based} 장면{bi}/{len(beats)} API 예외({attempt}): {e}")
                continue
            last_raw = raw or ""
            got = [x for x in _extract_json_array(last_raw) if isinstance(x, dict)
                   and (x.get("pose") or x.get("text"))]
            if len(got) >= max(1, int(quota * 0.6)):
                break
            _clog(f"EP{ep_num_1based} 장면{bi} 컷 {len(got)}/{quota}(미달) → {attempt}회차 재시도")
        raw_got = len(got)
        if len(got) > quota:
            notes.append(f"장면{bi}: {quota}컷 초과 응답 → 절단")
            got = got[:quota]
        if len(got) < quota:      # 장면 **안에서** 채운다 (맨 뒤로 밀면 페이지가 전체로 어긋난다)
            notes.append(f"장면{bi}: 미응답 {quota - len(got)}컷 → 침묵 컷 채움")
            while len(got) < quota:
                got.append({"type": "action",
                            "pose": "She stands in the same place, breathing quietly.",
                            "camera": "front_view", "position": "NONE", "climax": "",
                            "caption_ko": "", "dialog": [], "lines": [], "sfx": "",
                            "wide": False})
        raw_all.extend(got)
        for g in got:
            cap = str(g.get("caption_ko") or g.get("caption") or "").strip()
            cl = str(g.get("clothes") or "").strip()
            tail.append((cap + (f" / 복장: {cl}" if cl else "")).strip())
        # 패딩 전 실제 응답 수를 남긴다 — '5/5'만 찍히면 침묵 컷이 묻혀 본문 미반영을 못 본다
        _clog(f"EP{ep_num_1based} 장면{bi}/{len(beats)} ({len(beat)}자) → 컷 {raw_got}/{quota}"
              + (f" (미응답 {quota - raw_got}컷 → 침묵 컷)" if raw_got < quota else ""))

    # ⑤ 전역 보정 (슬롯 메타/page·tier 부여, 어휘·복장·시선 정규화)
    panels, notes2 = _repair_panels(raw_all, dollar, page_plans=page_plans, max_panels=maxp,
                                    ep_num_1based=ep_num_1based)
    notes = notes + notes2
    # [2026-09-09] ★요약/에필로그 컷의 지문이 비면 그 장면 본문의 첫 문장으로 채운다
    _fill_star_narration(panels, beats, quotas, notes)
    if bool(getattr(config, "comic_chatty", False)):
        _fill_chatty_narration(panels, beats, quotas, notes)
    if n_cut < target:
        _cap = int(getattr(config, "comic_max_pages", MAX_PAGES_AUTO) or MAX_PAGES_AUTO)
        notes.append(f"본문 {len(body)}자 → 목표 {target}컷, 레이아웃 {n_cut}컷"
                     + (f" — 페이지 상한 {max(1, _cap)}에 닿아 본문 일부가 압축됐다(--max-pages 상향 권장)"
                        if n_pages >= max(1, _cap) else f" — 레이아웃이 {n_pages}페이지 {n_cut}컷으로 목표를 담았다"))
    _clog(f"EP{ep_num_1based} 컷 스크립트 완성: {len(panels)}컷 "
          f"(face {sum(1 for p in panels if p['type']=='face')} / action {sum(1 for p in panels if p['type']=='action')})"
          + (f" — cut.yaml {n_pages}페이지 {len(beats)}장면 LLM {len(beats)}회" if page_plans
             else f" — 자동 레이아웃 {len(beats)}장면 LLM {len(beats)}회"))
    for n in notes:
        _clog(f"  - {n}")
    return {"panels": panels, "notes": notes, "raw": last_raw, "page_plans": page_plans,
            "beats": len(beats), "target_panels": target}


# ------------------------------------------------------------------ 컷 → 프롬프트
# [2026-09-07] comic 경로는 LLM 프롬프트 생성 단계를 타지 않으니(태그를 직접 납작하게 조립),
# 원본 태그에 남은 아래 결함들이 그대로 이미지로 갔다. 전부 이 파일 안에서 결정론적으로 정제한다.
#   ① 한국어 잔존(예: "캐주얼", "hyper-voluptuous (임신 징후로…)")  → EP당 LLM 1회 glossary로 번역
#   ② 동일 태그/문장 반복([AAA FACE]와 [AAA EXPRESSION]이 face_tag를 겹게 실음)
#   ③ 비정석 뷰 토큰(front_view/close_up…)                       → comic은 항상 정석으로 보낸다
#   ④ 카운터 중복(artist 트리거의 "1girl, solo," + 헤더의 ",1girl,solo.") → 하나만 남긴다
_KO_RE = re.compile(r"[가-힣]")
_KO_RUN_RE = re.compile(r"[가-힣]+(?:\s+[가-힣]+)*")   # 한글 연달은 조각(문장 안에서)만 골라 제거
_KO_PAREN_RE = re.compile(r"\([^()]*[가-힣][^()]*\)")
_KO_NOTE_RE = re.compile(r"\s*\(\s*Korean\s*-[\s\S]{0,40}?English\s*\)", re.I)

# [2026-09-07] 정적 사전: 컷 스크립트 LLM이 영문 pose 문장 안에 한국어 어구를 섞어 넣는다
# (실측: 영문 문장 안에 한글 체위명이 파편으로 남는다). 통째로 파기하면 정보가 사라지므로 치환이 먼저다.
# LLM gloss는 이 사전이 못 잡는 나머지(의상/소품 묘사 등)를 EP당 1회 번역한다. 키는 긴 순서로 적용한다.
# [2026-09-07] 청년향 개정: 강한 성행위는 화면에 그리지 않는다 — 스킨십/로맨스로 격하하거나 버린다.
#   격하 예: 체위류 → 안김/포옹류, 강한 구강·손 행동 → 키스/애무.
#   버려지는 어휘는 이후 한글 조각 파기에서 자연히 사라진다(어차피 화면에 안 그린다).
#   해부학: 성기는 파기(노출 금지), **유두는 새 정책(노출 허용)에 따라 복권**.
# [2026-09-09] (A) 공개/로컬 분리: 공개 repo에는 **중립 어휘만** 기본으로 싣는다.
#   본문에 실제로 나오는 민감한 체위·성행위어 키는 local_settings.yaml의 `ko_map_safe`에 둔다
#   (.gitignore 대상). 파일이 없으면(공개 클론) 그 조각은 EP당 1회 LLM gloss가 번역하거나
#   한글 파기 단계에서 사라진다 — 파이프라인은 그대로 돈다(연약한 폴백).
_KO_STATIC_EN = {
    "딥키스": "french kiss", "키스": "kiss", "뽀뽀": "kissing cheek", "이마키": "kissing forehead",
    "애무": "caressing", "페팅": "petting", "패팅": "petting", "터치": "caressing", "만짐": "caressing",
    "손잡음": "holding hands", "포옹": "hugging", "밀착": "close contact", "스킨십": "intimacy",
    "몸비비": "hugging", "포즈": "hugging pose", "안마": "massage", "무릎베개": "lap pillow",
    "유두": "nipples", "가슴": "breasts",
    "임신": "pregnant", "알몸": "naked", "나체": "naked", "속옷": "lingerie", "하의실종": "missing bottom",
    "목욕": "bath", "샤워": "shower", "수면": "sleeping", "잠": "sleep",
}


def _ko_map_safe() -> dict:
    """local_settings.yaml의 `ko_map_safe` → 청년향 강등 사전 증분 (없으면 {}).

    공개 클론은 비어 있어도 정상 동작한다(한글 파편은 LLM gloss 또는 파기로 처리).
    """
    return dict(getattr(config, "ko_map_safe", {}) or {})


# [2026-09-09] --allow-explicit(local)에서 위 기본 사전 위에 얹히는 **증보 사전**.
#   내용이 민감한 어휘라 기본값은 비어 있고, 로컬은 local_settings.yaml의 ko_map로 채웁니다
#   (.gitignore 대상). 파일이 없으면 explicit 모드에서도 강한 성행위어는 청년향 사전(격하/파기)을 탑니다.
def _ko_map_explicit() -> dict:
    """local_settings.yaml의 ko_map → {한글: 영문태그} (없으면 {})."""
    return dict(getattr(config, "ko_map_explicit", {}) or {})


def _apply_static_ko_map(text: str) -> str:
    table = dict(_KO_STATIC_EN)
    table.update(_ko_map_safe())                  # [2026-09-09] local: 청년향 강등 사전 증분
    if anima_gen.explicit_allowed():              # [2026-09-09] local explicit: explicit 어휘를 얹는다
        table.update(_ko_map_explicit())
    for ko in sorted(table, key=len, reverse=True):
        if ko in text:
            text = text.replace(ko, table[ko])
    return text


def _ko_fragments(ep_idx: int) -> list:
    """이 회차 태그 소스에서 한글 조각만 뽑는다 (glossary 생성용, EP당 1회)

    조각 단위: 한글 괄호 노트(균형 잡힌 ()) → 우선, 나머지 쉼표 단위.
    """
    srcs = [getattr(config, "clothes", ""), getattr(config, "outfit2", ""), getattr(config, "location", ""),
            getattr(config, "acc", ""), getattr(config, "face_style", ""), getattr(config, "body_shape", "")]
    for attr in ("bodystyle_tag", "body_tag", "exposure_tag", "p_exposure_tag", "makeup_tag",
                 "marks_tag", "background_tag", "face_tag"):
        arr = getattr(config, attr, None)
        try:
            if arr and 0 <= ep_idx < len(arr):
                srcs.append(str(arr[ep_idx] or ""))
        except Exception:
            pass
    frags, seen = [], set()
    for s in srcs:
        s = str(s or "")
        if not _KO_RE.search(s):
            continue
        s = _KO_NOTE_RE.sub("", s)
        s = _apply_static_ko_map(s)          # 사전으로 해결되는 것은 LLM 문의 대상에서 제외
        if not _KO_RE.search(s):
            continue
        for m in _KO_PAREN_RE.findall(s):
            if m not in seen:
                seen.add(m)
                frags.append(m)
        rest = _KO_PAREN_RE.sub(lambda m: " ", s)
        for part in rest.split(","):
            p = part.strip()
            if p and _KO_RE.search(p) and len(p) <= 120 and p not in seen:
                seen.add(p)
                frags.append(p)
    return frags[:40]


def request_ko_glossary(frags: list) -> dict:
    """한글 조각 → Anima(danbooru) 영문 태그 번역, LLM 1회. 실패하면 {} (나중에 파기 처리)"""
    if not frags:
        return {}
    listing = "\n".join(f"{i}. {f}" for i, f in enumerate(frags, 1))
    prompt = (
        "당신은 이미지 생성 AI(danbooru 태그) 번역가입니다. 아래 한국어 조각을 Anima가 아는 영문 태그로만 번역하세요.\n"
        "규칙: 오직 소문자 영문 태그(단어 1~6개, 쉼표 없이). 설명문 금지. 원문이 괄호 안 노트면 괄호를 살려 "
        "'(english tag)'로 출력. 번역 불가한 고유명사는 english로 음역.\n"
        "출력 형식: 각 줄 '번호. english tag' 만.\n\n" + listing
    )
    try:
        # temperature=0 / repeat_penalty=1.0: gloss가 흔들리면 같은 seed라도 이미지가 달라진다.
        # (컷 재현성은 seed만으로 성립하지 않는다 — 프롬프트도 동일해야 한다)
        raw, _ = call_openai_for_plot(prompt, messages=None, log_fn=_clog, temperature=0.0,
                                      repeat_penalty=1.0, reasoning_effort="low", enable_thinking=False)
    except Exception as e:
        _clog(f"glossary API 예외: {e}")
        return {}
    gloss = {}
    for line in (raw or "").splitlines():
        m = re.match(r"^\s*(\d+)\s*[.)\-]\s*(.+)$", line.strip())
        if not m:
            continue
        idx = int(m.group(1)) - 1
        val = m.group(2).strip().strip('"').strip("'")
        if 0 <= idx < len(frags) and val and not _KO_RE.search(val):
            gloss[frags[idx]] = val
    _clog(f"한글 glossary {len(gloss)}/{len(frags)}건 확보: "
          + ", ".join(f"{k}→{v}" for k, v in list(gloss.items())[:3]))
    return gloss


def sanitize_english(text: str, gloss: dict = None) -> tuple:
    """플랫 태그 문자열 정제 → (클린 문자열, 제거 조각 목록)

    1) "(Korean - translate to English)" 주의문구 삭제
    2) 정적 사전(체위/성행위어) 치환 — 컷 스크립트가 영문 문장에 섞는 한국어는 여기에서 잡는다
    3) gloss(EP당 1회 LLM 번역)로 한글 조각 치환
    4) 그래도 남은 한글: 짧은 태그 조각은 버리고, 긴 문장은 한글만 걷어내고 영어 줄기를 살린다
    """
    dropped = []
    out = _apply_static_ko_map(_KO_NOTE_RE.sub("", text or ""))
    for ko, en in (gloss or {}).items():
        for k in (ko, _apply_static_ko_map(ko)):      # 정적 사전을 거친 형태와 동일하게 맞춰 치환
            if k in out:
                out = out.replace(k, en)
    kept = []
    for part in out.split(","):
        p = part.strip()
        if not p:
            continue
        if _KO_RE.search(p):
            # 한글 조각만 걷어내고 영어 줄기는 살린다 (치환 후 남은 미번역 한국어 때문).
            # 순수 한글 태그(예: '치마')는 줄기가 남지 않아 자연히 파기된다.
            stem = re.sub(r"\s+", " ", _KO_RUN_RE.sub(" ", p)).strip()
            stem = re.sub(r"^[(\[{]+|[])\}]+|[.,;:]+$", "", stem).strip(" ,.")
            if len(re.sub(r"[^a-z]", "", stem.lower())) >= 3 and not _KO_RE.search(stem):
                kept.append(stem)
            else:
                dropped.append(p[:40])
            continue
        kept.append(p)
    return ", ".join(kept), dropped


# [2026-09-09] 프롬프트 정제 로그 — 컷마다 찍으니 한 회차에 1,500줄이 넘는 노이즈였다(실측).
#   일은 필요한다(같은 태그가 두 번 들어가면 가중치가 흔들리고 토큰도 새므로) → 세기만 하고 회차 끝에 한 줄로 낸다.
_PROMPT_SAN = {"dup": 0, "dup_cuts": 0, "anat": set(), "hangul": set()}


def _prompt_san_reset():
    for k in _PROMPT_SAN:
        _PROMPT_SAN[k] = 0 if isinstance(_PROMPT_SAN[k], int) else set()


def _prompt_san_summary(ep=None) -> str:
    """정제 통계를 한 줄로(아무 일도 없으면 빈 문자열) — 로그에 남길 값도 이걸로 돌려준다."""
    dup, cuts = int(_PROMPT_SAN["dup"]), int(_PROMPT_SAN["dup_cuts"])
    anat, hangul = sorted(_PROMPT_SAN["anat"]), sorted(_PROMPT_SAN["hangul"])
    if not (dup or anat or hangul):
        return ""
    parts = []
    if dup:
        parts.append(f"중복 태그 {dup}개(컷 {cuts}개)")
    if anat:
        parts.append(f"해부학 태그 {len(anat)}종")
    if hangul:
        parts.append(f"한글 파기 {len(hangul)}종 {hangul[:3]}")
    return (f"EP{ep} 프롬프트 정제: " if ep is not None else "프롬프트 정제: ") + " · ".join(parts)


def _prompt_san_flush(ep=None) -> str:
    s = _prompt_san_summary(ep)
    if s:
        _clog(s)
    _prompt_san_reset()
    return s


def dedupe_flat(text: str) -> tuple:
    """플랫 태그 문자열에서 중복 제거 → (클린 문자열, 제거 수)

    [AAA FACE]와 [AAA EXPRESSION]이 config.face_tag를 같이 실어서 긴 표정 문장이 두 번 들어온다(실측).
    쉼표 단위 태그도 대소문자/공백 정규화 후 첫 출현만 남긴다(naked/nude, cameltoe 중복 등).
    """
    seen, kept, removed = set(), [], 0
    for part in (text or "").split(","):
        p = part.strip()
        if not p:
            continue
        key = re.sub(r"\s+", " ", p.lower())
        key = re.sub(r"\((.*?):\s*\d+(?:\.\d+)?\)", r"\1", key)   # '(tag:1.6)' → 'tag'
        if key in seen:
            removed += 1
            continue
        seen.add(key)
        kept.append(p)
    return ", ".join(kept), removed


def strip_duplicate_counters(header: str) -> str:
    """카운터 중복 제거: artist 트리거 앞의 '1girl, solo,'를 지우고 헤더가 붙이는 카운터 하나만 남긴다.

    (order/anima_gen_flow.md §15.1 Patch A가 build_prompt_header에서 한 일을 comic 헤더에서도 한다.
     face 컷은 '1girl, solo', action 컷은 '1girl,1 boy' — 2인 구도에 solo가 남는 것도 막는다.)
    """
    if not header:
        return header
    head, sep, tail = header.partition("\n")            # '\n' 이후가 "A detailed anime illustration…" 문장
    tail = sep + tail
    trig = re.sub(r"\b(1girl|1boy|2girls|2boys|1girl, solo|1boy, solo)\b,?\s*(solo,)?\s*", "", head, count=1)
    trig = re.sub(r",\s*,", ", ", trig).strip()
    if "solo" in tail.lower() and re.search(r"\b(1boy|two|both)\b", tail, re.I):   # 2인 구도에서 solo 제거
        tail = re.sub(r"\bsolo\b\s*,?\s*", "", tail)
    return trig + tail


def flatten_tag_block(block: str, is_face: bool) -> str:
    """[ACTION]/[CAMERA]/[AAA FACE] … 블록을 ComfyUI용 평문 태그 문자열로 납작하게 전개.
    LLM 지시용 문장("tags in [AAA ...] lines apply ONLY to…")은 버린다."""
    keep = []
    for line in (block or "").splitlines():
        s = line.strip()
        if not s or not s.startswith("["):
            if s:
                keep.append(s)
            continue
        label, _, rest = s.partition("]")
        rest = rest.strip()
        if not rest or " apply ONLY to" in rest or rest.lower().startswith("("):
            continue
        tag = label[1:].strip().upper()
        if tag in ("AAA", "BBB"):                     # 라벨 전용 라인
            continue
        if tag == "CAMERA":
            continue                                  # 앵글은 헤더 [ANGLE]에서 처리
        keep.append(rest)
    out = ", ".join(keep)
    return out


def panel_raw_line(panel) -> str:
    """컷 → actions.yaml과 동일한 4슬롯 원문 (파서 재사용용)

   Aspect 슬롯은 wide 컷이면 "wide"(1366x1024), 아니면 "tall"(1024x1344).
    """
    aspect = "wide" if panel.get("wide") else "tall"
    parts = [panel["pose"].strip(), f"#{panel['camera']}", f"#{aspect}", f"#{panel['position']}"]
    if panel.get("climax"):
        parts.append(f"#{panel['climax']}")
    return " ".join(parts)


_PROMPT_GUIDE_CACHE = {}


def _prompt_guide(kind: str) -> str:
    """옛 anima_gen의 이미지 프롬프트 가이드(data_comfyui/prompt_pov.md|prompt_multi.md) 로드(캐시)."""
    if kind not in _PROMPT_GUIDE_CACHE:
        path = {"pov": os.path.join(os.path.dirname(__file__), "data_comfyui", "prompt_pov.md"),
                "multi": os.path.join(os.path.dirname(__file__), "data_comfyui", "prompt_multi.md"),
                }.get(kind, "")
        try:
            with open(path, encoding="utf-8") as f:
                _PROMPT_GUIDE_CACHE[kind] = f.read()
        except OSError:
            _PROMPT_GUIDE_CACHE[kind] = ""
    return _PROMPT_GUIDE_CACHE[kind]


def _llm_compose_panel_prompt(ep_idx: int, tag_block: str, angle: str, kind: str,
                              safety_tag: str):
    """[2026-09-07] POV/multi 컷 본문은 예전 anima_gen의 프롬프트 가이드로 LLM이 직접 짠다.

    가이드: pov → data_comfyui/prompt_pov.md(5단 구성+BREAK+OBSERVER 계약),
            multi → data_comfyui/prompt_multi.md(Subject별 Global/and-결합 태그 템플릿).
    portrait(face)은 호출되지 않는다(간단한 기존 방식 유지 — 사용자 지시).
    LLM 실패/출력 빈약 → None = 결정적 태그 본문 폴백(파이프라인 절대 안 깨짐).
    """
    guide = _prompt_guide(kind)
    if not guide:
        return None
    name_a = str(getattr(config, "name", "") or "the girl")
    name_b = str(getattr(config, "name2", "") or "the man")
    task = (
        "Follow the guide in the system message exactly and write ONE final image-generation "
        "prompt for the scene below.\n"
        f"- Replace (PROTAGONIST)/AAA/subject name with: {name_a}\n"
        f"- Replace (PARTNER)/observer/BBB with: {name_b}\n"
        f"- Camera angle tags to place verbatim (unweighted) in the composition part: {angle}\n"
        "- Youth policy: exposure up to nipples/cameltoe is allowed; genitals must NEVER appear.\n"
        + ("- MANDATORY: copy the character identity tags from [AAA TRIGGER]/[BBB TRIGGER] verbatim "
           "into the finished prompt (they select the character — dropping them is an error).\n"
           if "TRIGGER]" in tag_block else "")
        + "- No Korean anywhere. Put ONLY the finished prompt between two lines that each say "
        "##PROMPT## (marker included). No commentary.\n\n"
        f"## Source tags (strict character separation: [AAA ...]=protagonist, [BBB ...]=partner)\n"
        f"{tag_block}\n"
    )
    try:
        raw, _ = call_openai_for_text(task, system_prompt=guide, log_fn=_clog, temperature=0.6)
    except Exception as e:
        _clog(f"EP{ep_idx + 1} 가이드({kind}) LLM 실패 → 결정적 본문 폴백: {e}")
        return None
    txt = str(raw or "")
    if "##PROMPT##" in txt:
        parts = [p.strip() for p in txt.split("##PROMPT##") if p.strip()]
        txt = max(parts, key=len) if parts else ""
    body = txt.strip()
    if len(body) < 60:
        _clog(f"EP{ep_idx + 1} 가이드({kind}) 출력 빈약({len(body)}자) → 결정적 본문 폴백")
        return None
    return body


_JUNK_PHRASES = ("not visible in the frame",)   # 상대방 없는 컷에서 도는 잔해(태그로 들어가 화면을 흐린다)
_DANGLING_VERB = r"(looking|facing|standing|sitting|lying|turning|reaching)"
_SUBJECT_TAIL = re.compile(r"(?i)\b(she|he|they|it|girl|boy|woman|man|person)\s*$")


def _fix_dangling_subject(s: str) -> str:
    """주어 없는 "is looking to the right" 류 절에 she를 보강한다. 앞에 이미 주어가 있으면 그대로 둔다."""
    def rep(m):
        pre = s[:m.start()].rstrip(", ")
        if _SUBJECT_TAIL.search(pre):
            return m.group(0)
        return f"she is {m.group(1)}"
    return re.sub(rf"(?<![A-Za-z])is {_DANGLING_VERB}\b", rep, s)


def _tidy_prompt(text: str) -> str:
    """[2026-09-08] 최종 프롬프트 자연어 정리 (log/tag_out.txt 실측 사례 기준).

      - "center.She has"        → 문장부호 뒤 공백
      - "with him., He is"      → ".," → ","
      - "right..subject"        → 중복 마침표·이중 공백
      - 라인머리 ",1girl,solo." → 선행 쉼표 제거, ",1girl" → ", 1girl"
      - "is looking to the right" → 주어 보강(주어가 이미 있으면 생략)
      - "not visible in the frame" 잔해 제거
      - 같은 절 반복 제거("He is standing." 두 번 등)
    """
    if not text:
        return text
    out = []
    for line in str(text).split("\n"):
        s = line
        for junk in _JUNK_PHRASES:
            s = re.sub(rf"(?i),?\s*{re.escape(junk)}\s*(?=,|$)", "", s)
        s = re.sub(r"\.{2,}", ".", s)                        # "right..subject"
        s = re.sub(r"\s*\.\s*,", ",", s)                    # "him., He" → "him, He"
        s = re.sub(r",\s*\.", ",", s)                        # "him, ." → "him,"
        s = re.sub(r"([^\d\s])\.([A-Za-z])", r"\1. \2", s)    # "center.She" (2.6 같은 수치는 보호)
        s = re.sub(r",(?=[A-Za-z(\[])", ", ", s)               # ",1girl" → ", 1girl"
        s = _fix_dangling_subject(s)                           # 주어 없는 "is looking …"
        s = re.sub(r"[ \t]{2,}", " ", s)                       # 이중 공백
        # "…and a boy He is standing" → 대명사로 시작하는 문장 앞에 마침표가 없다 → 문장 분리
        s = re.sub(r"([a-z])(?= (?:She|He|They|It) (?:is|was|are|were|has|had)\b)", r"\1.", s)
        s = re.sub(r"\.{2,}", ".", s)
        s = re.sub(r",\s*,", ",", s)                            # 연속 쉼표
        s = re.sub(r"[\s,]+$", "", s)                           # 행 끝 쉼표(다음 조각과 붙어 ".,를 만든다)
        s = re.sub(r"^[,\.\s]+", "", s)                         # 라인머리 ",1girl,solo."
        out.append(s.strip())
    # 같은 절 반복 제거 + 마침표로 끝난 조각은 쉼표 없이 문장으로 잇는다("center., She" 방지)
    kept = []
    for line in out:
        seen, joined = set(), ""
        for pt in re.split(r"(?<=\.\s)|, ", line):
            pt = pt.strip()
            if not pt:
                continue
            key = pt.rstrip(".").strip().lower()
            if key in seen:
                continue
            seen.add(key)
            if not joined:
                joined = pt
            elif joined.endswith("."):
                joined += " " + pt.lstrip(", ")
            else:
                joined += ", " + pt.strip(", ")
        kept.append(joined)
    return "\n".join(kept).strip()


BG_ONLY_TAGS = ("detailed background, scenery, landscape, no humans, no people, empty scene, "
                "cinematic light, wide shot")     # ★서두 요약 컷(인물 없이 배경만) 화면 문법용


_BG_STRIP_RE = re.compile(r"\b(1girl|1boy|2girls|2boys|3girls|girls|boys|solo|multiple girls|no humans)\b,?\s*",
                          re.I)   # 배경만 컷에서는 인물 카운터를 헤더에서 지운다


def _build_bg_only_prompt(ep_idx: int, panel, safety_tag: str) -> str:
    """[2026-09-09] ★서두 요약 컷 = 캐릭터 없이 **배경만**(타치키리).

    헤더의 1girl/solo 를 빼야 사람이 안 나온다(artist_anima = '1girl, solo, ' + 트리거).
    LoRA 트리거는 스타일을 위해 남긴다.
    """
    raw = panel_raw_line(panel)
    pose_text, camera_view, aspect_ratio, _, _ = anima_gen._parse_action_entry(raw)
    artist = _BG_STRIP_RE.sub("", str(anima_gen.artist_anima or "")).strip().strip(",").strip()
    head = ((artist + ", " if artist else "")
            + "score_9, score_8, masterpiece, best quality, very aesthetic, "
            f"amazing quality, newest, highres, absurdres, colorful, detailed, detailed background, "
            f"scenery, empty scene, no humans, no people, {safety_tag}, ")
    angle = anima_gen.CAMERA_TAG_CANONICAL.get(camera_view, camera_view) or "front_view"
    body = f"{pose_text}, {BG_ONLY_TAGS}"
    body, _dropped = dedupe_flat(body)
    return _tidy_prompt(f"{head}{angle}\n{body}")


def build_panel_prompt(ep_idx: int, panel, safety_tag: str, gloss: dict = None, angle_preset=None,
                       tidy: bool = True) -> str:
    """컷 1개 → 최종 ComfyUI 프롬프트 (헤더 + [ANGLE] + 납작태그 본문)

    [2026-09-07] gloss: EP당 1회 만든 한글→영문 glossary(없으면 한글 조각 파기).
    angle_preset: -angle_llm ON일 때 action 컷에 지정된 angle.txt 프리셋 (face 컷은 close-up 유지).
    """
    if panel.get("bg_only"):                      # ★서두 요약 컷 — 인물 없이 배경만
        return _build_bg_only_prompt(ep_idx, panel, safety_tag)
    raw = panel_raw_line(panel)
    pose_text, camera_view, aspect_ratio, position_sentence, climax_tag = anima_gen._parse_action_entry(raw)
    is_face = panel["type"] == "face"
    if is_face:
        # [2026-09-07] portrait 정면 정책: "tilted head / looking away" 류 각도 어구를 pose에서 제거
        pose_text = _FACE_ANGLE_STRIP_RE.sub("", pose_text).strip().rstrip(",")
    # [2026-09-07] 1인 화면 원칙: POV가 아닌 컷은 상대방 위치 문장을 프롬프트에 넣지 않는다
    #   ("He is standing." 같은 문장이 1boy을 프레임 안으로 불러온다). POV 컷만 손/팔이 보인다.
    if camera_view != "pov":
        position_sentence = "NONE"

    # [2026-09-07] **1인 화면 = 헤더부터 솔로**: 예전은 face 외 컷에 is_side=True를 넣어
    #   헤더가 ",2girl. two girls... looking at each other"로 고정됐다(side_view는 '구도'일 뿐인데
    #   '2명'으로 해석). 청년향은 주인공 혼자가 기본 — 헤더는 always "1girl/1boy, solo",
    #   상대방은 POV 컷의 OBSERVER(손/팔)로만 들어온다.
    is_pov = camera_view == "pov"
    # [2026-09-09] 컷의 감정을 표정 태그로 넘긴다 — 예전은 ""(빈 값)를 줘서 회차 고정 표정에 100% 밀렸다
    emo_key = _panel_face_emotion(panel)
    step_expression = _EMO_FACE_TAGS.get(emo_key, "")
    if emo_key:
        pose_text = re.sub(r",?\s*\((?:ahegao|heart-shaped pupils|rolling eyes)\)(?::[\d.]+\)?)?", "", pose_text)
    tag_block = anima_gen._build_tag_block(ep_idx, pose_text, camera_view, aspect_ratio,
                                           position_sentence, step_expression, is_side=False,
                                           climax_tag=climax_tag,
                                           clothes_override=str(panel.get("clothes") or ""),
                                           partner_block=is_pov, observer_block=is_pov)
    header = anima_gen._build_simple_prompt_header(config.sex, safety_tag, is_side=False,
                                                   position_sentence="", pose_text=pose_text)
    header = strip_duplicate_counters(header)
    # [2026-09-07] comic은 LLM이 태그를 고치는 단계가 없으니 항상 단부루 정석 뷰 토큰으로 보낸다.
    # (-camera_canon OFF라도 정석 사용. -camera_canon 스위치는 anima_gen_simple A/B 전용으로 남긴다.)
    canon = anima_gen.CAMERA_TAG_CANONICAL
    angle = canon.get(camera_view, camera_view) or "pov"
    if angle_preset and not is_face:
        angle = anima_gen._resolve_angle_tags(angle_preset) or angle
        _clog(f"EP{ep_idx+1} 컷{panel['no']} angle 프리셋 → {angle}")
    if is_face:
        # 정석 close-up에는 방향이 없다 — 정면(from_front)을 명시해 얼굴 전체 정면 구도를 강제한다
        angle = f"{angle}, from_front, {FACE_CLOSEUP_TAGS.rstrip(', ')}\n"
    else:
        angle = f"{angle}\n"
    header = header.replace("[ANGLE]", angle)
    # [2026-09-07] 컷 종류별 본문 전략(사용자 지시):
    #   portrait(face) = 기존 결정론 방식 유지 / POV = prompt_pov.md 가이드 / multi = prompt_multi.md 가이드
    guide_kind = "" if is_face else ("pov" if camera_view == "pov" else
                                     ("multi" if panel.get("multi") else ""))
    composed = _llm_compose_panel_prompt(ep_idx, tag_block, angle.strip(), guide_kind,
                                         safety_tag) if guide_kind else None
    if composed:
        body = composed   # 가이드 산출물이 구도/시선을 이미 소유 → facing 태그 강제 주입 생략
    else:
        body = flatten_tag_block(tag_block, is_face)
        # 시선/구도 정책 강제:
        #   right(오른쪽文本) = 인물 왼쪽 배치 + 왼쪽→오른쪽 시선 / front(아래文本) = 정면
        facing = str(panel.get("facing") or ("right" if panel.get("wide") else "front")).lower()
        if panel.get("wide") or facing.startswith("r"):
            body = RIGHT_FACING_TAGS + body
        else:
            body = FRONT_FACING_TAGS + body
    body, removed = dedupe_flat(body)
    body, dropped = sanitize_english(body, gloss)
    # [2026-09-07] 해부학 정책(갱신): 성기 계열(vagina/penis 등)만 최종 필터로 제거.
    # nipples/cameltoe 노출은 허용. LLM pose 문장과 init_anima_tags(LLM) 태그 양쪽을 여기서 거른다.
    body, anatomy_removed = anima_gen.strip_anatomy_tags(body)
    _PROMPT_SAN["dup"] += int(removed or 0)
    _PROMPT_SAN["dup_cuts"] += 1 if removed else 0
    if anatomy_removed:
        _PROMPT_SAN["anat"].add(str(anatomy_removed))
    if dropped:
        _PROMPT_SAN["hangul"].update(dropped)
    # [2026-09-08] 시트 #…# 캐릭터 공식 태그: 결정론 본문이든 LLM 재작성이든 정제를 통과하며 빠질 수 있어
    #   마지막에 보장 주입한다(상대방 태그는 상대방이 프레임에 실제로 있는 POV 컷에서만 — [BBB] 분리 유지).
    body, ct_added = anima_gen.ensure_char_tags(body, include_partner=is_pov)
    if ct_added:
        _clog(f"EP{ep_idx+1} 컷{panel['no']} #캐릭터 태그 보장 주입: {', '.join(ct_added)}")
    # [2026-09-08] 헤더末尾 마침표와 본문 첫 단어('center.subject')가 붙는 것 → 공백으로 잇는다
    joined = f"{header.rstrip()} {' ' if header.rstrip().endswith('.') else ', '}{body}"
    return _tidy_prompt(joined) if tidy else joined


# ------------------------------------------------------------------ 렌더
def _panel_slug(pose: str, limit: int = 18) -> str:
    """파일명용 slug. 기본 18자 상한:

    SaveImage의 filename_prefix는 comfyui_run_anima가 `prefix[:50]`으로 자른다.
    'episode_N_'(11) + 'comic_eN_pNN_'(14) + slug + '_anima_'(7) ≤ 50 을 유지해야
    잘리지 않은 온전한 파일명이 된다(22자면 53자에서 3자 잘림 → 매치 실패의 방아쇠).
    """
    s = re.sub(r"[^a-zA-Z0-9_]", "_", pose.split(".")[0])
    s = re.sub(r"_+", "_", s).strip("_").lower()
    return s[:limit]


def _base_seed(ep_num_1based: int, panels) -> int:
    """컷 스크립트 내용에서 파생되는 결정적 seed (재실행 시 같은 그림)"""
    key = json.dumps([{"t": p["type"], "p": p["pose"], "c": p["camera"],
                       "x": p.get("climax", "")} for p in panels],
                     ensure_ascii=False, sort_keys=True)
    return zlib.crc32(f"ep{ep_num_1based}|{key}".encode("utf-8")) % (10 ** 15)


def _res_for_aspect(aspect):
    """[2026-09-07] yaml 슬롯의 목표 화면비 → anima_gen.resol 표에서 가장 가까운 해상도 인덱스.

    예: 슬롯 0.33(초슬림) → 가장 세로 긴 해상도(1024x1366 등)를 골라 뽑은 뒤 합성에서 좌우 자르기.
    없으면 None(→ wide/tall 기본 라우팅)."""
    try:
        a = float(aspect)
    except Exception:
        return None
    if a <= 0:
        return None
    best, best_d = None, 1e9
    for i, r in enumerate(anima_gen.resol):
        if not isinstance(r, (list, tuple)) or len(r) != 2:
            continue
        d = abs(r[0] / r[1] / a - a / (r[0] / r[1]))
        if d < best_d:
            best, best_d = i, d
    return best


def _apply_slot_aspects(panels, page_specs):
    """page_specs 레이아웃 기하학을 미리 계산해 컷마다 slot_aspect(너비/높이) 주입.
    render_panel이 '세로로 길게 뽑고 자르기' 위한 최소 오차 해상도를 고를 수 있다."""
    if not page_specs:
        return
    off = 0
    for ps in page_specs:
        k = int(ps.get("size") or 0)
        grp = panels[off:off + k]
        wide_f = [bool(p.get("wide")) for p in grp]
        face_f = [str(p.get("type")) == "face" for p in grp]
        zone_f = [("right" if (p.get("wide") or str(p.get("facing")) == "right") else "bottom")
                  for p in grp]
        try:
            _, geom = CPM._plan_rows(k, [[] for _ in grp], wide_f, face_f, zone_f,
                                     unit_w=CPM.DEFAULT_CELL_W, cols=2, gutter=CPM.DEFAULT_GUTTER,
                                     pad=CPM.DEFAULT_FRAME_PAD, border=4,
                                     font_size=CPM.DEFAULT_FONT_SIZE, row_spec=ps["rows"],
                                     page_size=(CPM.DEFAULT_PAGE_W, CPM.DEFAULT_PAGE_H),
                                     label_h=int(round(CPM.DEFAULT_FONT_SIZE * 1.6)))
            for r in geom:
                for c in r["cells"]:
                    # 실제 페이지에서 컷은 행 높이(row_h)로 자려진다 → 목표 화면비는 w/row_h
                    if 0 <= off + c["idx"] < len(panels) and r["h"] > 0:
                        panels[off + c["idx"]]["slot_aspect"] = round(c["w"] / r["h"], 3)
        except Exception as e:
            _clog(f"슬롯 화면비 계산 실패(기본 해상도 사용): {e}")
        off += k


def render_panel(ep_idx: int, panel, seed: int, safety_tag: str, json_value: dict,
                 wait_seconds: int = 120, gloss: dict = None, angle_preset=None,
                 prompt: str = None):
    """컷 1개 렌더 → 생성 PNG 경로 (실패 시 None)

    prompt: 미리 조립해 둔 프롬프트. 주면 그대로 쓴다(재조립 안 함) — pov/multi 가이드는
            LLM 호출이라 렌더 루프 안에서 돌리면 모델이 VRAM에 다시 올라온다.
    """
    full_prompt = prompt if prompt else build_panel_prompt(ep_idx, panel, safety_tag,
                                                           gloss=gloss, angle_preset=angle_preset)
    old_nametag = getattr(anima_gen, "anima_nametag", "")
    old_epnum = getattr(config, "episode_num", 0)
    anima_gen.anima_nametag = f"comic_e{ep_idx+1}_p{panel['no']:02d}_{_panel_slug(panel['pose'])}"
    config.episode_num = ep_idx
    prefix = ""
    # [2026-09-07] 슬롯 화면비 우선: yaml 컷 비율에 가장 가까운 해상도로 뽑은 뒤 합성에서 자른다.
    res = _res_for_aspect(panel.get("slot_aspect"))
    if res is None:
        res = WIDE_RES if panel.get("wide") else PANEL_RES        # wide 컷 = 1366x1024
    t_queue = time.time() - 2        # 이 시각 이후에 만들어진 파일만 '이번 컷의 결과'로 인정
    try:
        prefix = anima_gen.comfyui_run_anima(json_value, ep_idx, full_prompt, res,
                                             seed=seed, queue_count=1) or ""
        anima_gen._wait_and_copy_image(prefix, json_value, min_mtime=t_queue, wait_seconds=wait_seconds)
    except Exception as e:
        _clog(f"EP{ep_idx+1} 컷{panel['no']} 렌더 실패: {e}")
        return None
    finally:
        anima_gen.anima_nametag = old_nametag
        config.episode_num = old_epnum

    keys = [prefix] + ([prefix[:50]] if len(prefix) > 50 else [])   # SaveImage 50자 절단 대응
    hits = []
    for k in keys:
        hits = sorted(glob.glob(os.path.join("image", f"{k}*.png")))
        if hits:
            break
    if not hits:
        for _outd in anima_gen._comfyui_output_dirs(json_value):
            for k in keys:
                hits = sorted(glob.glob(os.path.join(_outd, f"{k}*.png")))
                if hits:
                    break
            if hits:
                break
    fresh = [h for h in hits if os.path.getmtime(h) >= t_queue]
    if fresh:
        hits = fresh
    _clog(f"EP{ep_idx+1} 컷{panel['no']} ({panel['type']}{'/wide' if panel.get('wide') else ''}) "
          f"res={res} seed={seed} → {os.path.basename(hits[-1]) if hits else '실패'}")
    return hits[-1] if hits else None


# ------------------------------------------------------------------ 에피소드 실행
def _pick_panel_angle(panel) -> dict:
    """-angle_llm ON + action 컷 → angle.txt 프리셋 1개

    comic에는 LLM "앵글 선택" 단계가 없으니(컷 스크립트는 카메라 5어휘만 받음) 후보 중 랜덤으로 고른다.
    comic 출력은 res5=1024x1344(portrait)이므로 aspect는 'tall' 고정, face 컷은 클로즈업 유지가 정상이라
    action 컷에만 적용한다.
    """
    if not getattr(config, "angle_llm_cli", False):
        return None
    if (panel.get("type") or "") == "face":
        return None
    cands = anima_gen._pick_angle_candidates(True, panel.get("camera", "front_view"),
                                             "wide" if panel.get("wide") else "tall")
    # [2026-09-07] 결정론: 같은 컷(동일 seed)은 같은 앵글 — rand.choice는 재현성을 깬다
    return anima_gen._det_choice(cands, f"angle|{panel.get('no')}|{panel.get('pose')}") if cands else None


def build_page_specs(panels):
    """컷 리스트의 page/tier/share 메타 → compose_pages용 page_specs (없으면 None=자동 레이아웃)"""
    if not panels or not all("page" in p for p in panels):
        return None
    by_page = {}
    for i, p in enumerate(panels):
        by_page.setdefault(int(p["page"]), []).append((i, p))
    specs = []
    for pg in sorted(by_page):
        grp = by_page[pg]
        off0 = grp[0][0]
        rows, cur = [], None
        for i, p in grp:
            cell = {"idx": i - off0, "share": float(p.get("share") or 0.5)}
            if cur is not None and cur["tier"] == p.get("tier"):
                cur["cells"].append(cell)
            else:
                cur = {"tier": p.get("tier"), "center": bool(p.get("center")), "cells": [cell],
                       "h_share": float(p.get("h") or 0.0)}
                rows.append(cur)
        specs.append({"size": len(grp), "rows": rows})
    return specs


def _apply_font_roles_from_config():
    """config의 용도별 폰트 경로(--font-narration 등)를 comic_page_merge에 전달한다."""
    for role, key in (("narration", "comic_font_narration"), ("dialog", "comic_font_dialog"),
                      ("thought", "comic_font_thought"), ("sfx", "comic_font_sfx")):
        CPM.set_font_role(role, str(getattr(config, key, "") or ""))


def comic_gen_episode(ep_idx: int, client=None, json_value=None, do_render: bool = True,
                      script: dict = None, files: list = None, pages: int = None,
                      episode_text: str = None, chars_per_panel: int = None,
                      max_panels: int = None, beat_chars: int = None) -> dict:
    """1에피소드 만화 생성. 반환: {"ep","panels","seeds","files","pages","notes","page_plans"}

    do_render=False → 이미지 생성 생략 (스크립트/레이아웃 검증용)
    files=[경로,...] → 렌더를 건너뛰고 이 이미지들로 페이지를 합성 (테스트·재조립용)
    pages → cut.yaml 페이지 수 (None → config.comic_pages: 0=본문 길이 자동 / N>0=고정 / -1=cut.yaml OFF)
    episode_text → 본문 원문 (미지정이면 config.episode_content에서 읽는다)
    """
    json_value = json_value or config.get_json_value()
    ep_num_1 = ep_idx + 1
    _prompt_san_reset()      # 정제 통계는 회차 단위로 모은다
    total = max(1, int(getattr(config, "total_episodes", 1) or 1))

    if script is None:
        script = request_panel_script(ep_num_1, total, client=client, pages=pages,
                                      episode_text=episode_text, chars_per_panel=chars_per_panel,
                                      max_panels=max_panels, beat_chars=beat_chars)
    panels, notes = script.get("panels", []), list(script.get("notes", []))
    if not panels:
        _clog(f"EP{ep_num_1} 컷 스크립트 없음 → 건너뜀")
        return {"ep": ep_num_1, "panels": [], "files": [], "pages": [], "notes": notes + ["컷 없음"]}

    base = _base_seed(ep_num_1, panels)
    seeds = [base + p["no"] for p in panels]

    safety_tag = ""
    try:
        safety_tag = config.review_safety[ep_idx] or ""
    except Exception:
        safety_tag = ""

    rendered = files is None
    if files is None:
        files = []
    if rendered and do_render:
        init = anima_gen.init_anima_tags(ep_idx, client, json_value)
        if isinstance(init, dict) and init.get("status") not in ("ok",):
            _clog(f"EP{ep_num_1} init_anima_tags {init.get('status')} → 렌더 건너뜀")
            return {"ep": ep_num_1, "panels": panels, "files": [], "pages": [],
                    "notes": notes + [f"init_anima_tags:{init.get('status')}"]}
        try:
            safety_tag = config.review_safety[ep_idx] or safety_tag
        except Exception:
            pass
        # [2026-09-07] EP당 1회: 한글 태그→영문 glossary (init_anima_tags가 태그를 채운 뒤 조회)
        gloss = request_ko_glossary(_ko_fragments(ep_idx))
        # [2026-09-08] 프롬프트 조립을 렌더 루프 **밖**에서 끝낸다. pov/multi 가이드는 LLM 호출인데
        #   루프 안에서 부르면 17GB 모델이 렌더 도중에 다시 올라와 ComfyUI VRAM/RAM을 잡아먹는다
        #   (16GB VRAM + 32GB RAM 실측: 컷 전량이 NaN → 검정 이미지). 조립→언로드→렌더 순서로 고정.
        prompts = []
        for p in panels:
            try:
                prompts.append(build_panel_prompt(ep_idx, p, safety_tag, gloss=gloss,
                                                 angle_preset=_pick_panel_angle(p)))
            except Exception as e:
                _clog(f"EP{ep_num_1} 컷{p.get('no')} 프롬프트 조립 실패(렌더 단계에서 재시도): {e}")
                prompts.append(None)
        # [2026-09-07] 이 시점까지 LLM 업무(추출은 런너에서 이미 끝남/태그/스크립트/용어집) 전부 종료.
        #   ComfyUI가 VRAM를 써야 하므로 LLM을 무조건 정지/언로드한다(전용서버=프로세스 종료).
        try:
            release_llm_for_gpu(json_value, log_fn=_clog)
        except Exception as e:
            _clog(f"EP{ep_num_1} LLM 메모리 반납 실패(무시, 렌더 계속): {e}")
        for p, sd, pr in zip(panels, seeds, prompts):
            f = render_panel(ep_idx, p, sd, safety_tag, json_value, gloss=gloss,
                             angle_preset=_pick_panel_angle(p), prompt=pr)
            if f:
                files.append(f)
        if not files:
            _clog(f"EP{ep_num_1} 생성된 컷 이미지 0장 (ComfyUI 확인 필요)")
            return {"ep": ep_num_1, "panels": panels, "files": [], "pages": [],
                    "notes": notes + ["이미지 0장"]}
    elif rendered and not do_render:
        _clog(f"EP{ep_num_1} 렌더 생략(do_render=False) → 컷 {len(panels)}개 스크립트만 확보")

    _prompt_san_flush(ep_num_1)      # [2026-09-09] 컷마다 찍던 정제 로그를 회차 끝 한 줄로 모은다
    out_dir = comic_out_dir()
    os.makedirs(out_dir, exist_ok=True)
    # 화면 텍스트 = 설명(하단 왼쪽 박스) + 풍선(말풍선/속마음 ≤2) + 의성어 (comic_page_merge가 그린다)
    texts = [panel_text_payload(p) for p in panels][:len(files)]
    # [2026-09-07] 레이아웃 v2 입력: wide 플래그 + face 여부(혼합 행 축소 페어링) + 텍스트 존
    wide_flags = [bool(p.get("wide")) for p in panels][:len(files)]
    face_flags = [str(p.get("type")) == "face" for p in panels][:len(files)]
    panel_zones = [("right" if (p.get("wide") or str(p.get("facing")) == "right") else "bottom")
                   for p in panels][:len(files)]
    # [2026-09-07 A안] cut.yaml 모드: 컷에 page/tier/share 메타가 있으면 그 구성으로 페이지를 짠다.
    page_plans = script.get("page_plans") or []
    page_specs = build_page_specs(panels)
    _apply_slot_aspects(panels, page_specs)   # 렌더 전: 슬롯 화면비 → 해상도 선택에 사용
    _apply_font_roles_from_config()          # ★화면 문법 용도별 폰트 지정 반영
    # [2026-09-09] 얼굴 중심 크롭 스위치(기본 켬) — cv2/모델이 없으면 추정치(위에서 8%)로 동작한다
    CPM.FACE_CROP_ENABLE = bool(getattr(config, "comic_face_crop", True))
    if CPM.FACE_CROP_ENABLE and getattr(config, "comic_face_model", ""):
        CPM.FACE_MODEL_FILE = str(config.comic_face_model)
    pages = CPM.compose_pages(files, texts, out_dir, f"episode_{ep_num_1:02d}",
                              panels_per_page=int(getattr(config, "comic_panels_per_page", PANELS_PER_PAGE)),
                              page_label_prefix=f"EP{ep_num_1:02d}",
                              panel_wide=wide_flags, panel_face=face_flags,
                              panel_zone=panel_zones, page_specs=page_specs,
                              panel_facing=[p.get("facing") for p in panels],
                              # 한글 폰트는 OS별로 다르다 — 없을 때만 별도 지정(--font / config.comic_font)
                              font_path=(getattr(config, "comic_font", "") or None))
    meta = {"ep": ep_num_1, "base_seed": base, "seeds": seeds, "panels": panels,
            "files": files, "pages": pages, "notes": notes,
            "beats": script.get("beats"), "target_panels": script.get("target_panels"),
            "page_plans": page_plans or None}
    try:
        with open(os.path.join(out_dir, f"episode_{ep_num_1:02d}_comic.json"), "w",
                  encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    except Exception as e:
        _clog(f"EP{ep_num_1} comic.json 저장 실패: {e}")
    _clog(f"EP{ep_num_1} 만화 완성: 컷 {len(files)}/{len(panels)}장 → 페이지 {len(pages)}장 → {out_dir}")
    return meta


def comic_gen_all(ep_num: int = 0, callback=None, do_render: bool = True) -> str:
    """에피소드 단위 반복. ep_num=0 → 전체, >0 → 단일 회차. (llm_novel_gui_func에서 호출)"""
    total = max(1, int(getattr(config, "total_episodes", 1) or 1))
    targets = list(range(total)) if int(ep_num) == 0 else [int(ep_num) - 1]
    client = None
    try:
        client = get_openai_client_anima(log_fn=_clog)
    except Exception as e:
        _clog(f"anima 클라이언트 생성 실패(테스트/오프라인이면 정상): {e}")
    done = []
    for idx in targets:
        if idx < 0 or idx >= total:
            continue
        if callback:
            try:
                callback("comic", f"만화 생성 중 (EP{idx+1})...", f"만화 EP{idx+1} 생성 중...")
            except Exception:
                pass
        try:
            meta = comic_gen_episode(idx, client=client, do_render=do_render)
            done.append(f"EP{meta['ep']}: 컷 {len(meta.get('files', []))}장 / 페이지 {len(meta.get('pages', []))}장")
        except Exception as e:
            _clog(f"EP{idx+1} 만화 생성 예외: {e}")
            done.append(f"EP{idx+1}: 실패 ({e})")
    summary = "만화 생성 결과\n" + "\n".join(f"  - {d}" for d in done)
    _clog(f"comic_gen_all 완료 ({len(targets)}회차)")
    return summary
