#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
comic_cut_gen.py — [별도 실험 파일 / 2026-09-15]
소설 원고(episode_NN_reviewed.md) → '만화'용 컷 스크립트(comic/) 생성기

기존 파이프라인(theme_gen_auto → plot_gen → full_episode_gen → anima_gen)과 **아직 합치지 않았다**.
필요한 조각을 골라 옮길 때까지 이 파일 하나만 돌린다.

입력
  --input <디렉토리> : episode_NN_reviewed.md が ある 디렉토리
                       reviewed 우선, 없으면 episode_NN.md (util/llm_markdown_gen.py 와 같은 규칙)
                       프롤로그 = episode_01_reviewed.md 맨 앞 '# 프롤로그' 구간
                       에필로그 = 마지막 회차의 '# 에필로그' 구간 (독립 파일 prologue.md/epilogue.md 도 인정)

출력 (--out, 기본 ./comic)
  ep{NN}_{hash}.txt          : progress/ 포맷과 같은 컷 스크립트
                               → llm_comic_gen: run_comic.py --special --episode comic --plot-hash {hash}
  prologue_{hash}.txt        : 프롤로그 본문만 (요구사항 3a — 회차 본문에 섞지 않고 분리)
  epilogue_{hash}.txt        : 에필로그 본문만
  episode_{NN}_cuts.json     : 컷 구조화 색인(컷 번호/막/태그/종류/화자/글자수/분리 여부/자산)
  episode_{NN}_assets.json   : 원고 디렉토리에서 찾은 standing/event 이미지와 본문의 이미지 참조
  summary.txt                : 회차별 컷 집계 + 감사 리포트(대사 유실/카드 유실/컷 폭주/분리 건수)
  character_sheet_ep*.json   : 입력에 사본이 있을 때만 복사 (이 프로그램은 새로 만들지 않는다 — 요구사항 3b)

헤더 계약 (요구사항 4 + 2026-09-15 분석)
  상태 카드(컷 종류에 반영만 됨)
    [LOCATION] [SITUATION] [TIME]   장면 카드 → 배경만 있는 확립 컷 1장의 재료
    [CLOTHES]                      주인공 '현재' 복장. **갈아입으면 같은 헤더를 다시 쓴다(번호를 안 붙인다)**
                                   → 전신 스탠딩 컷 1장 + 다음 [CLOTHES]까지 뒤 컷에 승계
    [CLOTHES2] [CLOTHES3]          상대방 / 서브·군중 의상 — 컷을 늘리지 않고 상태만 승계
  컷
    [ACTION]        평범한 행동 컷
    [ACTION_LARGE]  중요한 행동이라 컷을 크게 받는다                     (요구 4a)
    [STANDING]      주인공 standing 이미지를 재사용하는 전신 컷(렌더 생략) (요구 4b)
    [TALK] [INNER]  주인공 대사 / 속마음                                  (llm_comic_gen 대응됨)
    [TALK2] [INNER2] 상대방의 대사 / 속마음                               (요구 4c, 신규)
    [NARR] [SFX]    작가 지문(설명 박스) / 의성어 — 렌더러는 sfx 를 이미 안다
    [INSERT] [REACTION] [POV] [FLASHBACK] [PAGE_TURN] [CLOTHES3] [STANDING2]  (확장, --headers-mode full)

대사 분리(요구사항 3d — "대사가 길면 2컷으로")
  발화가 --max-speech-chars(기본 55)를 넘으면 **문장부호 → 접속어 → 공백** 순으로 한 번 끊어 두 컷.
  근거: 말풍선은 컷 폭의 25%·면적 25%까지만 쓰고 글자는 11px(FONT_FLOOR)까지밖에 안 줄어든다.

실행
  source /home/chrisyeo/AI/.venv/bin/activate
  python3 comic_cut_gen.py --input done/book7                        # main LLM(plot.json ip_main:port_main)
  python3 comic_cut_gen.py --input done/book7 --eps 1                 # 회차 필터
  python3 comic_cut_gen.py --input done/book7 --dry-run               # 프롬프트만 확인
  python3 comic_cut_gen.py --input done/book7 --no-llm                # LLM 없이 따옴표 규칙으로만
  python3 comic_cut_gen.py --input done/book7 --headers-mode full     # 확장 헤더까지 허용
  python3 comic_cut_gen.py --input done/book7 --compat-progress       # 오늘짜리 llm_comic_gen이 읽는 형태로 강등
"""

import os
import re
import sys
import json
import glob
import time
import uuid
import argparse
import datetime

import config
import openAPI_control as OAC

# =====================================================================
# 상수 — 원고 파싱
# =====================================================================

ACTS = ("기", "승", "전", "결")
ACT_RE = re.compile(r"^\s*(기|승|전|결)\s*[:：]\s*$")
HASH_ACT_RE = re.compile(r"^\s*(?:기|승|전|결)\s*[:：]\s*")     # 막 라벨은 우리가 붙이므로 지운다
DIV_H_RE = re.compile(r"(?m)^\s*#{3,12}\s*$")                    # '#####' (reviewed 원고의 막 구분선)
DIV_D_RE = re.compile(r"(?m)^\s*[-=~*_]{3,}\s*$")                # '-----' 를 쓰는 원고용 폴백
FRAME_HEAD_RE = re.compile(r"^\s*#{0,6}\s*(프롤로그|에필로그|prologue|epilogue)\s*$", re.I)
EP_HEAD_RE = re.compile(r"^\s*#{0,6}\s*episode\s*(\d+)\s*$", re.I)
MD_IMG_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
BULLET_RE = re.compile(r"^\s*(?:[-*•]\s+|\d+[.)]\s+)")
DIVIDER_LINE_RE = re.compile(r"^\s*(?:#{1,12}|[-=~*_]{3,}|={3,})\s*$")
TAG_LINE_RE = re.compile(r"^[\[\(（]?\s*([A-Za-z_][A-Za-z_0-9]*)\s*[\]\)）]?\s*[:：]?\s*(.+)$")
FILE_NOISE_RE = re.compile(r"\S*\.(?:png|jpg|jpeg|webp|gif)\b", re.I)   # LLM이 자산 파일명을 지문에 옮겨 적는다

# 원고 감사용 발화 검출 (원고마다 따옴표 규칙이 달라서 '검출'로만 쓴다)
DQUOTE_RE = re.compile(r"[“\"『「]([^“”\"『」]{4,})[”\"』」]")
SQUOTE_RE = re.compile(r"[‘']([^‘’'\n]{6,})[’']")
QUOTED_LINE_RE = re.compile(r"^\s*[“\"『「](.+?)[”\"』」]\s*[.!?…]*\s*$", re.S)
INNER_LINE_RE = re.compile(r"^\s*[‘'](.+?)[’']\s*[.!?…]*\s*$", re.S)

BODY_MARK = "--- 에피소드 내용 ---"
TAIL_SHEET_MARK = "--- 주인공 캐릭터 시트 ---"

# =====================================================================
# 헤더 레지스트리
#   mode    : min = llm_comic_gen(2026-09-15)이 오늘 그대로 읽는 것
#             rec = 권장 추가(병합 때 5줄만 고치면 화면에 드러난다)
#             full= 선택 확장
#   kind    : card(상태 카드) | cut(컷) | flag(컷을 만들지 않는 레이아웃 지시)
#   speaker : protagonist | partner | other | author | -
#   support : llm_comic_gen 쪽 대응 상태 = 병합 시 할 일
# =====================================================================
HEADERS = {
    "LOCATION": dict(mode="min", kind="card", speaker="-",
                     desc="장소 카드. 배경 확립 컷의 재료 — 한 문장(80자 이내)",
                     support="대응됨: [LOCATION]+[SITUATION]+[TIME] 를 합쳐 배경만 있는 확립 컷 1장"),
    "SITUATION": dict(mode="min", kind="card", speaker="-",
                      desc="이 장면의 상황 한 문장",
                      support="대응됨(확립 컷 지문으로 합쳐진다)"),
    "TIME": dict(mode="min", kind="card", speaker="-",
                 desc="시간(아침/낮/황혼/밤/새벽/시각)",
                 support="대응됨(확립 컷 지문으로 합쳐진다)"),
    "CLOTHES": dict(mode="min", kind="card", speaker="protagonist",
                    desc="주인공 현재 복장 한 문장. 갈아입으면 같은 헤더를 다시 쓴다(번호 금지)",
                    support="대응됨: 전신 스탠딩 컷 1장 + 다음 [CLOTHES]까지 뒤 컷에 승계"),
    "CLOTHES2": dict(mode="rec", kind="card", speaker="partner",
                     desc="상대방이 갈아입은 의상 한 문장",
                     support="대응됨: 컷을 늘리지 않고 이후 컷의 p_clothes 로만 승계, 자막이 되지 않는다"),
    "CLOTHES3": dict(mode="full", kind="card", speaker="other",
                     desc="서브/군중이 갈아입은 의상 한 문장",
                     support="대응됨: 컷 없이 군중 묘사로 승계"),
    "ACTION": dict(mode="min", kind="cut", speaker="-",
                   desc="인물의 행동/표정/감각 한 동작",
                   support="대응됨: 큰 장면(wide) 컷 1장, 지문 = 원문 그대로"),
    "ACTION_LARGE": dict(mode="rec", kind="cut", speaker="-",
                         desc="장면의 터닝포인트 — 컷을 크게 받아야 하는 행동(최대 대면·배신·탈의·결단 직전)",
                         support="신규: 지금은 TEXT 로 떨어져 [ACTION] 과 구별되지 않고 라벨이 자막으로 샌다"
                                " → _special_specs 에서 kind=wide + 슬롯 전폭"),
    "STANDING": dict(mode="rec", kind="cut", speaker="protagonist",
                     desc="주인공 전신(서 있는) 한 컷 — episode_N_standing_*.png 를 재사용한다",
                     support="신규: 자산을 재사용하면 렌더 0컷. 자막·풍선 없이 그림만"),
    "STANDING2": dict(mode="full", kind="cut", speaker="partner",
                      desc="상대방 전신 한 컷(또는 주인공 두 번째 복장의 standing)",
                      support="신규(STANDING과 같은 갈래)"),
    "TALK": dict(mode="min", kind="cut", speaker="protagonist",
                 desc="주인공 대사(말풍선, 화면 왼쪽)",
                 support="대응됨: portrait 컷(말풍선)"),
    "TALK2": dict(mode="rec", kind="cut", speaker="partner",
                  desc="상대방 대사(말풍선, 화면 오른쪽)",
                  support="신규: portrait + who=partner. 지금은 라벨째 지문이 된다"),
    "INNER": dict(mode="min", kind="cut", speaker="protagonist",
                  desc="주인공 속마음(타원 풍선)",
                  support="대응됨: portrait 컷(속마음)"),
    "INNER2": dict(mode="rec", kind="cut", speaker="partner",
                   desc="상대방 속마음(타원 풍선)",
                   support="신규: thought + who=partner (현 렌더러는 속마음 화자를 주인공으로 고정해 둔다)"),
    "NARR": dict(mode="rec", kind="cut", speaker="author",
                 desc="작가 지문·시간 경과·장면 전환 — 인물 없는 설명 박스 컷",
                 support="신규: caption 만 있는 컷(TEXT 와 같은 결이지만 라벨을 벗겨야 한다)"),
    "SFX": dict(mode="rec", kind="cut", speaker="author",
                desc="의성어/의태어 한 단어(큰 흰 글씨 + 검은 윤곽)",
                support="렌더러는 이미 지원(컷 필드 sfx) — 헤더만 통과시키면 끝난다"),
    "INSERT": dict(mode="full", kind="cut", speaker="-",
                   desc="부분 클로즈업(손·입술·허벅지·각인·물건·옷감) 컷업",
                   support="신규: camera=close_up + 초점 부위"),
    "REACTION": dict(mode="full", kind="cut", speaker="-",
                     desc="대사 없는 표정 반응 컷",
                     support="신규: portrait + 풍선 없음"),
    "POV": dict(mode="full", kind="cut", speaker="protagonist",
                desc="주인공 1인칭 시점 컷",
                support="신규(anima_gen 은 pov 를 이미 안다)"),
    "FLASHBACK": dict(mode="full", kind="cut", speaker="author",
                      desc="회상 컷(톤 처리)",
                      support="신규"),
    "PAGE_TURN": dict(mode="full", kind="flag", speaker="-",
                      desc="이다음 컷을 페이지 첫 컷으로(넘김 강조)",
                      support="신규(레이아웃 지시 — 컷을 만들지 않는다)"),
}
MODE_RANK = {"min": 0, "rec": 1, "full": 2}
SPEECH_TAGS = ("TALK", "TALK2", "INNER", "INNER2")

# --compat-progress : llm_comic_gen 이 오늘 이해하는 형태로 강등한다 (None = 그 컷을 버린다)
COMPAT_DOWN = {
    "ACTION_LARGE": "ACTION",
    "STANDING": None,          # [CLOTHES] 가 이미 '전신 스탠딩 컷'이다 — 둘이면 슬롯만 밀린다
    "STANDING2": None,
    "CLOTHES3": "CLOTHES2",
    "NARR": "ACTION",
    "SFX": "ACTION",
    "INSERT": "ACTION",
    "REACTION": "ACTION",
    "POV": "ACTION",
    "FLASHBACK": "ACTION",
    "PAGE_TURN": None,
    # 발화는 화자 이름을 본문에 붙여서 보낸다(novel_progress 의 화자 추정기가 이름 언급을 본다)
    "TALK2": "TALK",
    "INNER2": "INNER",
}


# =====================================================================
# 로그
# =====================================================================

VERBOSE = True


def log(msg: str) -> None:
    print(f"[{datetime.datetime.now():%H:%M:%S}] {msg}", flush=True)


def vlog(msg: str) -> None:
    if VERBOSE:
        log(msg)


def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# =====================================================================
# 1. 입력 수집
# =====================================================================

def find_episode_files(input_dir: str) -> dict:
    """episode_NN_reviewed.md 우선, 없으면 episode_NN.md → {ep_num: path}"""
    found = {}
    for p in sorted(glob.glob(os.path.join(input_dir, "episode_*.md"))):
        m = re.match(r"^episode_(\d+)(_reviewed)?\.md$", os.path.basename(p), re.I)
        if not m:
            continue
        ep = int(m.group(1))
        if m.group(2):
            found[ep] = p                                  # reviewed 는 무조건 우선(덮어쓴다)
        else:
            found.setdefault(ep, p)                        # 일반본은 reviewed 가 없을 때만
    # 독립 프롤로그/에필로그 파일은 회차로 오해하지 않는다(episode_*.md 패턴이라 이미 걸러짐)
    return found


def strip_markdown(text: str) -> str:
    """만화 지문으로 쓰지 않을 마크다운 조각 제거(이미지 링크·강조·제목 표지)"""
    out = MD_IMG_RE.sub("", text or "")
    out = re.sub(r"(?m)^\s*#{1,6}\s*", "", out)
    out = re.sub(r"\*\*|__|~~", "", out)
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def split_frame_sections(raw: str) -> tuple:
    """원고 파일 하나 → (프롤로그, 에피소드 본문, 에필로그)

    '# 프롤로그' / '# Episode N' / '# 에필로그' 라벨을 인정한다(# 개수·대소문자 무관).
    라벨이 없으면 파일 전체를 에피소드 본문으로 본다(구형 원고).
    """
    prologue, epilogue, body, buf = [], [], [], []
    target = body                                    # 라벨之前的 텍스트는 본문(구형 원고)으로 본다

    def flush_to(tgt):
        txt = "\n".join(buf).strip()
        if txt:
            tgt.append(txt)

    for ln in (raw or "").splitlines():
        s = ln.strip()
        fm, em = FRAME_HEAD_RE.match(s), EP_HEAD_RE.match(s)
        if fm or em:
            flush_to(target)
            buf = []
            if fm:
                target = prologue if fm.group(1).lower() in ("프롤로그", "prologue") else epilogue
            else:
                target = body
            continue
        buf.append(ln)
    flush_to(target)
    join = lambda xs: "\n\n".join(x for x in xs if x).strip()      # noqa: E731
    return join(prologue), join(body), join(epilogue)


def collect_assets(input_dir: str, ep: int, md_refs: list) -> dict:
    """회차 이미지 자산(standing/event) + 본문 마크다운이 참조한 이미지"""
    standing, event = [], []
    for n in {str(ep), f"{ep:02d}"}:
        standing += glob.glob(os.path.join(input_dir, f"episode_{n}_standing*.png"))
        standing += glob.glob(os.path.join(input_dir, f"episode_{n}_standing*.jpg"))
        event += sorted(glob.glob(os.path.join(input_dir, f"episode_{n}_event*.png")))
    rel = lambda ps: sorted({os.path.basename(p) for p in ps})       # noqa: E731
    # 본문이 참조한 이미지: 'episode_1_standing...' / 'ep01_standing...' 처럼 회차 번호가 섞인 것만
    hit = re.compile(rf"(?:^|[^0-9]){ep:02d}(?:[^0-9]|$)|(?:^|_)episode_{ep}_(?:[^0-9]|$)")
    return {"ep": ep,
            "standing": rel(standing),
            "event": rel(event),
            "md_refs": [r for r in md_refs if hit.search(r)]}


# =====================================================================
# 2. 막(기승전결) 분할
# =====================================================================

def split_beats(body: str) -> list:
    """본문 → [(act, text), ...] — '#####' 기준. 4개를 넘는 조각은 '결'에 합친다."""
    chunks = [c.strip() for c in DIV_H_RE.split(body or "") if c.strip()]
    if len(chunks) < 2:
        chunks = [c.strip() for c in DIV_D_RE.split(body or "") if c.strip()]
    beats = []
    for i, c in enumerate(chunks):
        c = HASH_ACT_RE.sub("", c, count=1).strip()                  # 원고에 '기:' 라벨이 있으면 제거
        if not c:
            continue
        if i < len(ACTS):
            beats.append([ACTS[i], c])
        elif beats:
            beats[-1][1] = beats[-1][1] + "\n\n" + c                 # 막이 5개 이상이면 결론에 합류
    return [(a, t) for a, t in beats]


# =====================================================================
# 3. LLM 프롬프트
# =====================================================================

def allowed_tags(mode: str) -> list:
    rank = MODE_RANK.get(mode, 1)
    return [t for t, v in HEADERS.items() if MODE_RANK[v["mode"]] <= rank]


def build_beat_prompt(act: str, beat_text: str, ep: int, names: dict, prev_cards: dict,
                      mode: str, max_cuts: int, standing_asset: bool) -> str:
    tags = allowed_tags(mode)
    tag_doc = "\n".join(f"  [{t}]: {HEADERS[t]['desc']}" for t in tags)
    card_tags = ", ".join(t for t in tags if HEADERS[t]["kind"] == "card")
    pname = names.get("protagonist", {}).get("name") or "주인공"
    oname = names.get("partner", {}).get("name") or "상대방"

    ctx = []
    if prev_cards.get("LOCATION"):
        ctx.append(f"직전 장면: {prev_cards['LOCATION']} ({prev_cards.get('TIME', '')})")
    if prev_cards.get("CLOTHES"):
        ctx.append(f"직전 주인공 복장: {prev_cards['CLOTHES']}")
    ctx_block = ("".join(c + "\n" for c in ctx)) if ctx else ""

    standing_rule = ""
    if "STANDING" in tags:
        standing_rule = ("6. 인물이 처음 등장하거나 복장·자세가 통째로 드러나는 자리에는 [STANDING] 한 컷을 둔다"
                         "(내용 = 전신 실루엣·자세·표정 한 문장). 이 막에 최대 1개"
                         + (", 미리 뽑아 둔 standing 이미지가 있으니 남발하지 않는다" if standing_asset else "") + ".\n")

    return f"""너는 웹툰 스토리보드 어시스턴트다. 아래 소설 본문({ep}화 '{act}' 막)을 만화 컷 목록으로만 번역한다.

[허용 헤더 — 이 목록에 없는 글자는 절대 출력하지 않는다]
{tag_doc}

[원칙]
1. 장면이 바뀌었으면 맨 앞에 장면 카드({card_tags})부터 놓는다. 같은 장소·같은 시간이라면 카드를 다시 쓰지 않는다.
2. [CLOTHES] 는 주인공의 복장만 쓴다. 주인공이 옷을 갈아입거나 옷이 찢어지면 그 자리에서 [CLOTHES] 를 **한 번 더** 출력한다(번호를 붙이지 않는다). 상대방이 갈아입었을 때만 [CLOTHES2].
3. 모든 헤더는 한 줄에 하나, 태그 뒤에 콜론(:)을 반드시 찍고, 태그는 위 대소문자 그대로. 줄을 나누지 말고 설명·머리말·번호·마크다운을 쓰지 않는다.
4. 따옴표로 묶인 대사는 **원문 그대로** 한 컷. 화자가 {pname}이면 [TALK], {oname}이면 [TALK2]. 따옴표는 벗긴다.
5. 혼잣말·속마음도 **원문 그대로** 한 컷. {pname}이면 [INNER], {oname}이면 [INNER2]. 한 문장이 아주 길면 말하려는 결의 중간에서 두 줄(두 컷)로 나눠도 된다.
{standing_rule}7. 서술은 인물의 행동/표정/감각 **한 동작**을 [ACTION] 한 컷으로 옮긴다. 두 동작이 붙어 있으면 두 컷으로 나눈다.
8. [ACTION_LARGE]는 이 장면의 터닝포인트(최초 대면·배신·첫 접촉·탈의·결단의 순간)에만 쓴다. 이 막에 최대 1개.
9. [SFX]는 원고에 의성어가 있을 때만, 단어만 쓴다. 대사를 새로 만들거나 요약하지 말고, 원고에 없는 사건·인물을 넣지 않는다.
10. 컷은 이 막에 최대 {max_cuts}개. 단 대사·속마음은 이 제한과 무관하게 전부 남긴다.
11. 한 헤더의 내용은 60자 이내로 짧게 쓴다(그림 한 장의 정보만). 대사는 예외지만 두 문장을 넘기지 않는다.

[본문]
{ctx_block}{beat_text}
"""


CHAR_PROMPT = """아래 소설 앞에서 주인공과 상대방(숙적/연인) 캐릭터를 뽑는다. 설명 없이 정확히 세 줄만:
주인공: 이름|직업|나이
상대방: 이름|직업|나이
서브: 이름|직업|나이      (없으면 서브: ||)
직업·나이가 원고에 없으면 빈칸으로 둔다.

[본문]
{}
"""


# =====================================================================
# 4. LLM 호출 / 캐릭터 정보
# =====================================================================

def call_llm(prompt: str, args, prefix: str) -> str:
    """plot.json 의 엔드포인트로 호출. 실패해도 빈 문자열(감사에서 처리하고 전체는 죽지 않는다)."""
    try:
        if args.endpoint == "agent":
            out = OAC.call_openai_for_client(prompt_text=prompt, log_fn=log, temperature=args.temperature)
            out = out[0] if isinstance(out, tuple) else out
        elif args.endpoint == "text":
            out, _ = OAC.call_openai_for_text(prompt, log_fn=log, temperature=args.temperature,
                                              enable_thinking=False, reasoning_effort=None)
        else:
            out, _ = OAC.call_openai_for_plot(prompt, log_fn=log, temperature=args.temperature,
                                              enable_thinking=False, reasoning_effort=None)
        return (out or "").strip()
    except Exception as e:                                       # noqa: BLE001
        log(f"{prefix} LLM 호출 실패: {e}")
        return ""


def parse_char_lines(raw: str) -> dict:
    info = {"protagonist": {}, "partner": {}, "sub": {}}
    keymap = {"주인공": "protagonist", "상대방": "partner", "서브": "sub"}
    for ln in (raw or "").splitlines():
        m = re.match(r"^(주인공|상대방|서브)\s*[:：]\s*(.*)$", ln.strip().strip("*-# "))
        if not m:
            continue
        parts = [p.strip() for p in m.group(2).split("|")]
        info[keymap[m.group(1)]] = {"name": parts[0], "job": parts[1] if len(parts) > 1 else "",
                                    "age": parts[2] if len(parts) > 2 else ""}
    return info


def sheet_to_names(sheet: dict) -> dict:
    """progress/character_sheet_epNN_*.json 사본 → ep 헤더 재료(있으면 LLM을 생략한다)"""
    out = {"protagonist": {}, "partner": {}, "sub": {}}
    if not isinstance(sheet, dict):
        return out

    def get(d, *keys):
        for k in keys:
            v = d.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""
    pr = sheet.get("protagonist") or sheet.get("주인공") or {}
    pa = sheet.get("partner") or sheet.get("상대방") or {}
    out["protagonist"] = {"name": get(pr, "name", "이름"), "job": get(pr, "job", "직업"),
                          "age": str(get(pr, "age", "나이"))}
    out["partner"] = {"name": get(pa, "name", "상대방 이름", "이름"),
                      "job": get(pa, "job", "상대방 직업", "직업"),
                      "age": str(get(pa, "상대방 나이", "나이"))}
    return out


def sheet_plain_text(sheet: dict) -> str:
    """시트 JSON → 평문 시트(ep 파일 꼬리 '--- 주인공 캐릭터 시트 ---' 용)"""
    if not isinstance(sheet, dict):
        return ""
    n = sheet_to_names(sheet)
    rows = []
    for label, key in (("주인공", "protagonist"), ("상대방", "partner")):
        d = n.get(key) or {}
        if not d.get("name"):
            continue
        rows.append(f"{label}: {d['name']} / 직업: {d.get('job','')} / 나이: {d.get('age','')}")
    return "\n".join(rows)


def parse_cli_person(spec: str) -> dict:
    """"이름|직업|나이" 형식의 CLI 오버라이드"""
    parts = [p.strip() for p in (spec or "").split("|")]
    return {"name": parts[0], "job": parts[1] if len(parts) > 1 else "",
            "age": parts[2] if len(parts) > 2 else ""}


# =====================================================================
# 5. LLM 응답 → 컷 항목
# =====================================================================

def downgrade_tag(tag: str, mode: str) -> str | None:
    """mode 가 허용하지 않는 태그는 같은 계열의 낮은 태그로 강등하고, 없으면 버린다(None)."""
    if tag not in HEADERS:
        return None
    if MODE_RANK[HEADERS[tag]["mode"]] <= MODE_RANK.get(mode, 1):
        return tag
    tgt = COMPAT_DOWN.get(tag, "ACTION" if HEADERS[tag]["kind"] != "card" else None)
    if tgt and MODE_RANK[HEADERS[tgt]["mode"]] <= MODE_RANK.get(mode, 1):
        return tgt
    return None


def parse_items(raw: str, act: str, mode: str, notes: list) -> list:
    """LLM 응답 텍스트 → [{'tag','text','act'}] (형식 밖 줄은 버리고 노트에 남긴다)"""
    items, junk = [], {}
    for ln in (raw or "").splitlines():
        s = BULLET_RE.sub("", ln.strip())
        if not s or DIVIDER_LINE_RE.match(s):
            continue
        m = TAG_LINE_RE.match(s)
        if not m:
            junk.setdefault("(형식아님)", 0)
            junk["(형식아님)"] += 1
            continue
        tag = m.group(1).upper().replace("-", "_").strip("[]")
        tag = {"TALK_2": "TALK2", "INNER_2": "INNER2", "TALK_": "TALK"}.get(tag, tag)
        text = re.sub(r"\s+", " ", m.group(2)).strip().strip('"“”\'’')
        text = FILE_NOISE_RE.sub("", text).strip(" -,·")              # 지문에 섞인 이미지 파일명은 버린다
        if not text:
            continue
        tgt = downgrade_tag(tag, mode)
        if tgt is None:
            junk.setdefault(tag, 0)
            junk[tag] += 1
            continue
        items.append({"tag": tgt, "text": text, "act": act})
    for t, n in junk.items():
        notes.append(f"[{act}] 버림 {n}줄: {t} (허용 헤더 밖이거나 형식 오류)")
    return items


def split_long_speech(item: dict, limit: int, max_parts: int) -> list:
    """발화가 길면 두 컷으로(요구 3d). 끊는 우선순위: 문장부호 → 접속어 → 공백.

    말풍선은 컷 폭 25%·면적 25% 상한에 글자도 11px 까지밖에 안 줄어드니,
    긴 대사는 한 칸에서 아주 작아지거나 풍선이 얼굴을 덮는다. 두 칸으로 나누면 글자 크기를 지킨다.
    """
    text = item["text"]
    if len(text) <= limit or item["tag"] not in SPEECH_TAGS or max_parts < 2:
        return [item]

    def ends(pat):
        return [m.end() for m in re.finditer(pat, text) if 0 < m.end() < len(text)]

    hard = ends(r"[.!?…]+[”\"'’]?\s*")
    soft = ends(r"(?:,|·|그리고|하지만|그런데|그래서|그러나)\s*")
    blank = ends(r"\s")
    mid = len(text) / 2.0
    floor = limit * 0.4                     # 잘라 한쪽이 너무 짧으면 풍선이 두 개 다 볼품없어진다

    def best_in(pools):
        """문장부호 → 접속어 → 공백 순. 단 두 조각이 모두 한계 이상은 되어야 한다."""
        for p in pools:
            ok = [x for x in p if floor <= x <= len(text) - floor]
            if ok:
                return min(ok, key=lambda x: abs(x - mid))
        return None

    best = best_in([hard, soft, blank, hard + soft + blank])
    if best is None:
        return [item]
    head, tail = text[:best].strip(" …"), text[best:].strip(" …")
    if not head or not tail:
        return [item]
    out = [dict(item, text=head, split_part=1),
           dict(item, text=tail, split_part=2, split_from=text)]
    vlog(f"      발화 분리({len(text)}자 → {len(head)}+{len(tail)}): {item['tag']}")
    return out


CARD_STATE = ("LOCATION", "SITUATION", "TIME", "CLOTHES")     # 막을 건너 이어가는 장면 상태


def normalize(items: list, opts: dict, notes: list, prev_cards: dict, budget: dict | None = None) -> tuple:
    """정규화: 카드 상태 유지(안 바뀐 카드는 다시 쓰지 않는다) → 발화 분리 → 컷 상한 → standing 절제

    returns (items, cards)   # cards: 이 막까지 확정된 장면 카드 상태(다음 막 프롬프트/감사용)
    """
    cards = dict(prev_cards or {})
    seen_card = dict(cards)
    out, cut_n = [], 0
    budget = budget if budget is not None else {"large_left": 99, "standing_left": 99}
    drop = {"card": 0, "cap": 0, "large": 0, "standing": 0}
    act = items[0]["act"] if items else ""
    limit = int(opts.get("max_cuts") or 0)

    for it in items:
        tag, text = it["tag"], it["text"]
        spec = HEADERS[tag]

        if spec["kind"] == "card":
            if seen_card.get(tag) == text:
                # 같은 문장이 다시 나왔다 = 장면 전환이 아니다. 확립 컷/스탠딩 컷만 중복되므로 넘긴다.
                drop["card"] += 1
                continue
            seen_card[tag] = text
            if tag in CARD_STATE:
                cards[tag] = text
            out.append(it)
            continue

        if tag == "ACTION_LARGE":
            if int(budget.get("large_left", 99)) <= 0:
                drop["large"] += 1
                it = dict(it, tag="ACTION")
            else:
                budget["large_left"] = int(budget.get("large_left", 99)) - 1

        if spec["kind"] == "cut":
            if tag in ("STANDING", "STANDING2"):
                if int(budget.get("standing_left", 99)) <= 0:
                    drop["standing"] += 1
                    continue
                budget["standing_left"] = int(budget.get("standing_left", 99)) - 1
            if tag in SPEECH_TAGS:                              # 발화는 컷 상한과 무관하게 전량 보존
                out.extend(split_long_speech(it, int(opts.get("max_speech_chars", 55)),
                                             int(opts.get("max_speech_parts", 2))))
                continue
            if limit and cut_n >= limit:
                drop["cap"] += 1
                continue
            cut_n += 1
            out.extend(split_long_speech(it, int(opts.get("max_speech_chars", 55)),
                                         int(opts.get("max_speech_parts", 2))))
            continue
        out.append(it)                                       # flag(PAGE_TURN) 등

    # 노트는 막당 한 줄로 뭉친다(같은 경고 40줄이면 리포트를 nobody 읽지 않는다)
    if drop["card"]:
        notes.append(f"[{act}] 안 바뀐 장면 카드 {drop['card']}장 생략(확립/스탠딩 컷 중복 방지)")
    if drop["large"]:
        notes.append(f"[{act}] [ACTION_LARGE] 회차 한도({opts.get('max_large')}) 소진 → {drop['large']}개 [ACTION] 강등")
    if drop["standing"]:
        notes.append(f"[{act}] [STANDING] 회차 한도({opts.get('max_standing')}) 소진 → {drop['standing']}개 삭제"
                     f"(자산 재사용이라 많아도 이득이 없다)")
    if drop["cap"]:
        notes.append(f"[{act}] 컷 상한({limit}) 초과 → 서술 컷 {drop['cap']}개 삭제(대사·속마음은 보존)")
    return out, cards


def audit_beat(act: str, src: str, items: list, has_card: bool) -> list:
    """원고와 컷을 견둔다 — 발화가 유실되면 화면에서 사라진 것과 같다(이 프로그램의 유일한 검증)"""
    notes = []
    q = [m.group(1).strip() for m in DQUOTE_RE.finditer(src)]
    sq = [m.group(1).strip() for m in SQUOTE_RE.finditer(src)]
    talk = [x for x in items if x["tag"] in ("TALK", "TALK2")]
    inner = [x for x in items if x["tag"] in ("INNER", "INNER2")]
    if q and len(talk) + 1 < len(q):
        notes.append(f"[{act}] 원고 따옴표 대사 {len(q)}개 → [TALK]/[TALK2] {len(talk)}컷 (대사 유실 의심)")
    if sq and not inner and not talk:
        notes.append(f"[{act}] 원고 혼잣말 {len(sq)}개 → [INNER] 컷 없음(속마음 유실 의심)")
    if not any(HEADERS[x["tag"]]["kind"] == "cut" for x in items):
        notes.append(f"[{act}] 컷이 0개 — 이 막은 렌더에서 통째로 빠진다")
    if not has_card:
        notes.append(f"[{act}] [LOCATION] 카드가 없고 직전 장면도 없다 — 배경 확립 컷을 못 만든다")
    return notes


# =====================================================================
# 6. 결정론 폴백(--no-llm): 따옴표 규칙만 믿고 자른다
# =====================================================================

def fallback_beat_to_items(act: str, beat_text: str) -> list:
    """따옴표 대사는 [TALK],홑따옴표는 [INNER], 나머지는 [ACTION].
    book1 처럼 따옴표 없는 원고는 전부 [ACTION] 이 되어 대사가 지문으로 섞인다(감사에서 경고)."""
    items = []
    for para in re.split(r"\n\s*\n", beat_text):
        for ln in [x.strip() for x in para.splitlines() if x.strip()]:
            s = BULLET_RE.sub("", ln)
            m = QUOTED_LINE_RE.match(s)
            if m:
                items.append({"tag": "TALK", "text": re.sub(r"\s+", " ", m.group(1)).strip(), "act": act})
                continue
            m = INNER_LINE_RE.match(s)
            if m:
                items.append({"tag": "INNER", "text": re.sub(r"\s+", " ", m.group(1)).strip(), "act": act})
                continue
            qm = DQUOTE_RE.search(s)
            if qm:
                pre = (s[:qm.start()] + " " + s[qm.end():]).strip(" ,.-—–")
                if len(pre) > 6:
                    items.append({"tag": "ACTION", "text": re.sub(r"\s+", " ", pre).strip(), "act": act})
                items.append({"tag": "TALK", "text": re.sub(r"\s+", " ", qm.group(1)).strip(), "act": act})
                continue
            items.append({"tag": "ACTION", "text": re.sub(r"\s+", " ", s).strip(), "act": act})
    return items


def compat_downgrade(blocks: list, names: dict) -> list:
    """--compat-progress : 오늘짜리 llm_comic_gen 이 읽는 헤더([LOCATION]/[CLOTHES]/[ACTION]/[TALK]/[INNER])로만."""
    oname = names.get("partner", {}).get("name") or ""
    out = []
    for act, items in blocks:
        rows = []
        for it in items:
            tgt = COMPAT_DOWN.get(it["tag"], it["tag"])
            if tgt is None:
                continue
            row = dict(it, tag=tgt)
            if it["tag"] in ("TALK2", "INNER2") and oname:
                row["text"] = f"{oname}: {it['text']}"        # 화자 추정기가 이름 언급을 본다
            rows.append(row)
        out.append((act, rows))
    return out


# =====================================================================
# 7. 출력
# =====================================================================

def render_ep_file(ep: int, names: dict, blocks: list, sheet_text: str = "") -> str:
    """progress/epNN_hash.txt 와 같은 형태 — llm_comic_gen/novel_progress.py 가 그대로 읽는다.
    카드는 항목 사이에 낀 자리 그대로 쓴다(막 중간 카드 = 그 지점의 장면 전환이라 순서가 의미 있다)."""
    head = [f"=== Episode {ep} ===", ""]
    for label, key in (("# 주인공", "protagonist"), ("# 상대방", "partner"), ("# 서브 캐릭터", "sub")):
        d = names.get(key) or {}
        if d.get("name"):
            head += [f"{label} ({d['name']})", f"직업: {d.get('job', '')}", f"나이: {d.get('age', '')}", ""]
    head += [BODY_MARK, ""]

    parts = []
    for act, items in blocks:
        blk = ["#####", f"{act}:"]
        blk += [f"[{x['tag']}]: {x['text']}" for x in items]
        parts.append("\n".join(blk))

    txt = "\n".join(head) + "\n" + "\n\n".join(parts).strip() + "\n"
    if sheet_text.strip():
        txt += f"\n{TAIL_SHEET_MARK}\n\n{sheet_text.strip()}\n"
    return txt


def cut_index(ep: int, blocks: list, assets: dict) -> dict:
    """컷 구조화 색인 — 만화를 만드는 쪽이 표로 쓰는 용도(스크립트 .txt 와 1:1 순서)"""
    rows, n = [], 0
    for act, items in blocks:
        for it in items:
            n += 1
            spec = HEADERS[it["tag"]]
            row = {"cut": n, "act": act, "tag": it["tag"], "kind": spec["kind"], "speaker": spec["speaker"],
                   "text": it["text"], "chars": len(it["text"]),
                   "caption": it["tag"] in ("ACTION", "ACTION_LARGE", "NARR", "STANDING", "STANDING2",
                                            "INSERT", "POV", "REACTION", "FLASHBACK"),
                   "balloon": it["tag"] in SPEECH_TAGS,
                   "asset": (assets["standing"][0] if (it["tag"] in ("STANDING", "STANDING2")
                                                       and assets.get("standing")) else None),
                   "injected": bool(it.get("injected")),
                   "inherited": bool(it.get("inherited")), "split_part": it.get("split_part")}
            rows.append(row)
    tally = {}
    for r in rows:
        tally[r["tag"]] = tally.get(r["tag"], 0) + 1
    return {"ep": ep, "cut_total": len(rows), "tally": tally, "cuts": rows}


# =====================================================================
# 8. main
# =====================================================================

def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="소설 원고(episode_NN_reviewed.md) → 만화 컷 스크립트(comic/) 생성")
    ap.add_argument("--input", required=True, help="episode_NN_reviewed.md 가 있는 디렉토리")
    ap.add_argument("--out", default="comic", help="컷 출력 디렉토리(기본 ./comic)")
    ap.add_argument("--hash", default="", help="산출물 해시(비우면 입력의 기존 해시 재사용 또는 자동 생성)")
    ap.add_argument("--eps", default="", help="회차 필터(예: 1,2,10). 비우면 전부")
    ap.add_argument("--beats", default="", help="막 필터(예: 기 또는 기,전) — LLM 비용을 줄이는 디버그용(막이 빠진 채로 저장된다)")
    ap.add_argument("--headers-mode", default="rec", choices=("min", "rec", "full"),
                    help="허용 헤더 범위. min=llm_comic_gen 이 오늘 읽는 것만 / "
                         "rec(기본)=ACTION_LARGE·STANDING·TALK2·INNER2·NARR·SFX 까지 / full=확장 전부")
    ap.add_argument("--compat-progress", action="store_true",
                    help="오늘짜리 llm_comic_gen 이 그대로 읽게 헤더를 강등한다(병합 전 임시 실행)")
    ap.add_argument("--max-speech-chars", type=int, default=55, help="이보다 긴 발화는 2컷으로 분리(기본 55)")
    ap.add_argument("--max-speech-parts", type=int, default=2, help="발화 분리 최대 조각(기본 2)")
    ap.add_argument("--max-cuts", type=int, default=24, help="한 막 컷 상한(0=무제한, 기본 24)")
    ap.add_argument("--max-large", type=int, default=2, help="한 회차 [ACTION_LARGE] 상한(기본 2)")
    ap.add_argument("--max-standing", type=int, default=2, help="한 회차 [STANDING] 상한(기본 2)")
    ap.add_argument("--no-llm", action="store_true", help="LLM 없이 따옴표 규칙으로만 자른다")
    ap.add_argument("--endpoint", default="main", choices=("main", "agent", "text"),
                    help="LLM 엔드포인트(기본 main = plot.json ip_main:port_main)")
    ap.add_argument("--temperature", type=float, default=0.4, help="정형 변환이라 낮게(기본 0.4)")
    ap.add_argument("--dry-run", action="store_true", help="프롬프트만 출력하고 저장하지 않는다")
    ap.add_argument("--no-copy-sheet", action="store_true",
                    help="입력에 character_sheet_ep* 사본이 있어도 comic/ 에 복사하지 않는다(재사용만)")
    ap.add_argument("--protagonist", default="", help="주인공 지정 오버라이드: 이름|직업|나이")
    ap.add_argument("--partner", default="", help="상대방 지정 오버라이드: 이름|직업|나이")
    ap.add_argument("--sub", default="", help="서브 캐릭터 지정 오버라이드: 이름|직업|나이")
    ap.add_argument("--quiet", action="store_true", help="줄 단위 로그 생략")
    return ap.parse_args(argv)


def reuse_hash_or_new(input_dir: str, given: str) -> str:
    """입력에 progress 식 산출물이 있으면 같은 해시를 쓴다(회차가 한 작품으로 묶인다). 없으면 새로 만든다."""
    if given:
        return given
    for pat in ("ep*_*.txt", "character_sheet_ep*_*.json"):
        for p in glob.glob(os.path.join(input_dir, pat)):
            m = re.search(r"_([0-9a-f]{6,})\.", os.path.basename(p))
            if m:
                log(f"[HASH] 입력의 기존 해시 재사용: {m.group(1)} ({os.path.basename(p)})")
                return m.group(1)
    new = uuid.uuid4().hex[:16]
    log(f"[HASH] 새 해시 생성: {new}")
    return new


def main(argv=None) -> int:
    global VERBOSE
    args = parse_args(argv)
    VERBOSE = not args.quiet
    t0 = time.time()

    if not os.path.isdir(args.input):
        log(f"[오류] 입력 디렉토리 없음: {args.input}")
        return 2
    os.makedirs(args.out, exist_ok=True)
    hash_code = reuse_hash_or_new(args.input, args.hash)
    jv = config.get_json_value()
    host = {"main": (jv.get("ip_main"), jv.get("port_main"), jv.get("mainLLM")),
            "agent": (jv.get("ip_agent"), jv.get("port_agent"), jv.get("agent")),
            "text": (jv.get("text_ip"), jv.get("text_port"), jv.get("textLLM"))}[args.endpoint]
    log(f"[설정] input={args.input} out={args.out} hash={hash_code} headers={args.headers_mode} "
        f"LLM={'off' if args.no_llm else f'{host[2]} @ {host[0]}:{host[1]}'} temp={args.temperature}")

    # ---- 1) 회차 파일(reviewed 우선) ---------------------------------------
    files = find_episode_files(args.input)
    if not files:
        log(f"[오류] episode_NN_reviewed.md / episode_NN.md 를 찾지 못했다: {args.input}")
        return 2
    want = {int(x) for x in re.findall(r"\d+", args.eps)} if args.eps else set(files)
    eps = [e for e in sorted(files) if e in want]

    md_refs = []
    for p in files.values():
        for href in MD_IMG_RE.findall(read_text(p)):
            if href not in md_refs:
                md_refs.append(href)

    prologue_parts, epilogue_parts, bodies = [], [], {}
    for e in eps:
        pro, body, epi = split_frame_sections(read_text(files[e]))
        if pro:
            prologue_parts.append(pro)
        if epi:
            epilogue_parts.append(epi)
        bodies[e] = body
    log(f"[입력] 회차 {len(eps)}개: {', '.join(f'EP{e:02d}' for e in eps)}"
        f" (reviewed 우선) / 이미지 참조 {len(md_refs)}건")

    # ---- 2) 프롤로그·에필로그 분리 저장(요구 3a) ----------------------------
    saved = []
    for kind, parts in (("prologue", prologue_parts), ("epilogue", epilogue_parts)):
        txt = strip_markdown("\n\n".join(parts))
        if not txt:
            log(f"[FRAME] {kind} 원고가 없다 — 파일 저장을 생략한다"
                f" (원고에 '# {('프롤로그' if kind == 'prologue' else '에필로그')}' 라벨이 있는지 확인)")
            continue
        p = os.path.join(args.out, f"{kind}_{hash_code}.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write(txt + "\n")
        saved.append(p)
        log(f"[FRAME] {kind} 분리 저장: {p} ({len(txt)}자)")

    # ---- 3) 캐릭터 정보: CLI > 시트 사본 > LLM ------------------------------
    names = {"protagonist": {}, "partner": {}, "sub": {}}
    sheet_txt = ""
    sheet_files = sorted(glob.glob(os.path.join(args.input, "character_sheet_ep*_*.json")))
    if sheet_files:
        try:
            sheet = json.loads(read_text(sheet_files[0]))
            names = sheet_to_names(sheet)
            sheet_txt = sheet_plain_text(sheet)
            if args.no_copy_sheet:
                log(f"[SHEET] 사본 재사용만(복사 안 함): {sheet_files[0]}")
            else:
                dst = os.path.join(args.out, os.path.basename(sheet_files[0]))
                with open(dst, "w", encoding="utf-8") as f:
                    f.write(read_text(sheet_files[0]))
                saved.append(dst)
                log(f"[SHEET] 사본을 그대로 옮긴다(이 프로그램은 시트를 새로 만들지 않는다): {dst}")
        except Exception as ex:                                # noqa: BLE001
            log(f"[SHEET] 사본 해석 실패({ex}) → LLM 추출로 넘어간다")
    for spec, key in ((args.protagonist, "protagonist"), (args.partner, "partner"), (args.sub, "sub")):
        if spec:
            names[key] = parse_cli_person(spec)
    if not names["protagonist"].get("name") and not args.no_llm and not args.dry_run:
        log("[CHAR] 캐릭터 정보 LLM 추출...")
        got = parse_char_lines(call_llm(CHAR_PROMPT.format(
            (prologue_parts and "\n\n".join(prologue_parts) or bodies.get(eps[0], ""))[:1800]), args, "[CHAR]"))
        for k in names:
            names[k] = names[k] or got.get(k, {})
    # 이름 중복 정리: LLM이 서브를 주인공과 같은 사람으로 적는 원고가 있다(그럼 ep 헤더에 같은 사람이 두 번 찍힌다)
    used = set()
    for key in ("protagonist", "partner", "sub"):
        nm = (names.get(key) or {}).get("name", "")
        if not nm or nm in used:
            names[key] = {}
        else:
            used.add(nm)
    log("[CHAR] 주인공={} / 상대방={} / 서브={}".format(
        names["protagonist"].get("name") or "(모름)",
        names["partner"].get("name") or "(없음)",
        names["sub"].get("name") or "(없음)"))

    # ---- 4) 회차 → 컷 -------------------------------------------------------
    opts = {"max_cuts": args.max_cuts, "max_speech_chars": args.max_speech_chars,
            "max_speech_parts": args.max_speech_parts, "max_large": args.max_large,
            "max_standing": args.max_standing}
    report, total_cuts = [], 0

    for e in eps:
        notes = []
        beats = split_beats(bodies[e])
        if args.beats:
            pick = [x.strip() for x in re.split(r"[,， ]+", args.beats) if x.strip()]
            beats = [b for b in beats if b[0] in pick]
        if not beats:
            log(f"[EP{e:02d}] 본문이 비었거나 --beats 필터로 남아 막이 없다 — 건너뛴다")
            continue
        if len(beats) != 4:
            notes.append(f"막 구분선이 {len(beats)}개였다(기승전결 4개가 표준) — 원고의 '#####' 개수를 확인하세요")
        assets = collect_assets(args.input, e, md_refs)
        budget = {"large_left": args.max_large, "standing_left": args.max_standing}   # 회차 단위 예산
        blocks, cards, first_cards = [], {}, {}
        for bi, (act, beat_text) in enumerate(beats):
            vlog(f"[EP{e:02d}] {act} 막 변환 ({len(beat_text)}자)...")
            if args.dry_run:
                print("\n" + "=" * 78)
                print(build_beat_prompt(act, beat_text, e, names, cards, args.headers_mode,
                                        args.max_cuts, bool(assets["standing"])))
                continue
            if args.no_llm:
                raw_items = fallback_beat_to_items(act, beat_text)
            else:
                raw = call_llm(build_beat_prompt(act, beat_text, e, names, cards,
                                                 args.headers_mode, args.max_cuts,
                                                 bool(assets["standing"])), args, f"[EP{e:02d}/{act}]")
                raw_items = parse_items(raw, act, args.headers_mode, notes)
                if not raw_items:
                    notes.append(f"[{act}] LLM 응답이 비었거나 형식이 깨져서 따옴표 파싱으로 대체했다")
                    raw_items = fallback_beat_to_items(act, beat_text)
            items, cards = normalize(raw_items, opts, notes, cards, budget)
            if bi == 0:
                first_cards = dict(cards)                    # 첫 막 복장으로 [STANDING] 지문을 쓴다
            blocks.append((act, items))
            notes += audit_beat(act, beat_text, items, bool(cards.get("LOCATION")))
        if args.dry_run:
            continue

        if args.compat_progress:
            before = sum(len(x) for _, x in blocks)
            blocks = compat_downgrade(blocks, names)
            log(f"[EP{e:02d}] [compat-progress] 헤더 강등: 항목 {before} → {sum(len(x) for _, x in blocks)}")

        # standing 자산이 있는데 [STANDING]이 한 개도 없으면 첫 막에 한 컷 얹는다(렌더 1컷이 공짜다)
        if (assets["standing"] and args.headers_mode != "min" and not args.compat_progress
                and budget["standing_left"] > 0
                and not any(it["tag"] in ("STANDING", "STANDING2") for _, bs in blocks for it in bs)):
            act0, first = blocks[0]
            at = next((i for i, x in enumerate(first) if HEADERS[x["tag"]]["kind"] == "cut"), len(first))
            who = names["protagonist"].get("name") or "주인공"
            first.insert(at, {"tag": "STANDING", "act": act0, "injected": True,
                              "text": f"{who}는 {(first_cards or cards).get('CLOTHES', '지금 복장')} 차림으로 그 자리에 서 있다"})
            notes.append("standing 자산은 있는데 [STANDING] 컷이 없어 1컷을 자동 주입했다(렌더 절약)")

        ep_path = os.path.join(args.out, f"ep{e:02d}_{hash_code}.txt")
        with open(ep_path, "w", encoding="utf-8") as f:
            f.write(render_ep_file(e, names, blocks, sheet_txt))
        saved.append(ep_path)

        idx = cut_index(e, blocks, assets)
        with open(os.path.join(args.out, f"episode_{e:02d}_cuts.json"), "w", encoding="utf-8") as f:
            json.dump(idx, f, ensure_ascii=False, indent=2)
        with open(os.path.join(args.out, f"episode_{e:02d}_assets.json"), "w", encoding="utf-8") as f:
            json.dump(assets, f, ensure_ascii=False, indent=2)

        total_cuts += idx["cut_total"]
        splits = len([1 for _, bs in blocks for it in bs if it.get("split_part")])
        log(f"[EP{e:02d}] 컷 {idx['cut_total']}개(발화 분리 {splits}건, standing 자산 {len(assets['standing'])}장)"
            f" → {os.path.basename(ep_path)}")
        log(f"[EP{e:02d}] 태그 분포: " + ", ".join(f"{k}:{v}" for k, v in sorted(idx["tally"].items())))
        report.append({"ep": e, "tally": idx["tally"], "notes": notes, "splits": splits,
                       "cuts": idx["cut_total"]})

    if args.dry_run:
        print("\n[dry-run] 프롬프트만 출력했습니다(저장 없음)")
        return 0

    # ---- 5) 리포트 ----------------------------------------------------------
    rep = os.path.join(args.out, "summary.txt")
    with open(rep, "w", encoding="utf-8") as f:
        f.write(f"# comic 컷 생성 리포트 {datetime.datetime.now():%Y-%m-%d %H:%M:%S}\n")
        f.write(f"입력 {os.path.abspath(args.input)} / hash {hash_code} / headers {args.headers_mode} / "
                f"LLM {'off' if args.no_llm else args.endpoint}\n")
        f.write(f"발화 분리 {args.max_speech_chars}자(최대 {args.max_speech_parts}컷) / "
                f"컷 상한 막당 {args.max_cuts} / LARGE 회차당 {args.max_large} / STANDING 회차당 {args.max_standing}\n\n")
        for r in report:
            f.write(f"===== EP{r['ep']:02d} : 컷 {r['cuts']}개 / 발화 분리 {r['splits']}건 =====\n")
            f.write("  태그: " + ", ".join(f"{k}×{v}" for k, v in sorted(r["tally"].items())) + "\n")
            for n in r["notes"]:
                f.write(f"  ! {n}\n")
            f.write("\n")
        f.write("----- 저장 파일 -----\n")
        for p in saved:
            f.write(f"  {p}\n")
        f.write("\n----- llm_comic_gen 연결 -----\n"
                f"  python3 run_comic.py --special --all-eps --episode {args.out} "
                f"--plot-hash {hash_code} --book 1 --safety nsfw --start-llm\n")
    log(f"[완료] 컷 총 {total_cuts}개 / 파일 {len(saved)}개 → {args.out}/ (summary.txt 참조) / {time.time() - t0:.1f}s")
    warn = 0
    for r in report:
        for n in r["notes"]:
            warn += 1
            log(f"  ! EP{r['ep']:02d} {n}")
    if warn:
        log(f"[감사] 주의 {warn}건 — summary.txt 의 ! 줄을 모두 확인하세요")
    return 0


if __name__ == "__main__":
    sys.exit(main())
