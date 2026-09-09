#!/usr/bin/env python3
"""comic_input.py — [llm_comic_gen 독립 환경] 평문 입력 → config 채우기

입력은 오직 두 평문 파일이다:
  1) 에피소드 본문 (inputs/epNN.txt)      — 한국어 산문, 형식 제약 없음
  2) 캐릭터 시트 (평문)                   — "유즈키는 21세 여성 경찰. 밝은 갈색 머리를 묶고…" 같은 문장

구조화된 필드(이름/성별/외모 태그/가이드 4줄/$행동)는 **LLM이 두 입력에서 추출**한다.
추출된 값은 본 repo의 `config` 모듈에 그대로 주입된다 — 아래 10개 필드는
`anima_gen.init_anima_tags`의 필수 검증 목록이고, 없으면 렌더가 즉시 중단된다.

설계 원칙
  - 그랩필드(외모·복장)는 **Anima가 아는 영문 태그**로 받는다 (한글이면 모델이 무시한다).
  - [2026-09-08] 시트의 **#…#** 은 설명문이 아니라 **캐릭터 공식 태그(트리거)**다 → LLM 없이
    `extract_char_tags()`로 뽑아 config.char_tags에 직결하고, 컷 프롬프트에 무조건 주입한다.
  - 이름/가이드/행동 키워드는 한국어 유지(대사·캡션·$키워드 매칭에 쓰인다).
  - 추출은 텍스트 전용 엔드포인트(`call_openai_for_text` = plot.json text_* = gemma-4-31B).
  - 캐시 금지: 매 실행마다 파일을 읽고 LLM을 부른다.
  - [2026-09-08] **본문 길이 무제한**: 컨텍스트(ollama num_ctx)는 하드 제약이다. 넘기면 ollama가
    프롬프트 **앞부분을 조용히 잘라내서**(규칙 블록이 증발) 손실로 돌아온다. 따라서
    ① `episode_char_budget()`가 num_ctx에서 넣어도 되는 글자 수를 계산하고
    ② 긴 본문은 `split_beats()`로 장면 창(창)으로 쪼개 **창마다 추출 1회 → 병합**(map-reduce)한다.
    컷 스크립트도 같은 창을 쓴다(comic_gen이 `split_beats`/`allocate`를 여기서 가져간다).

실행 순서(런처 run_comic.py가 부른다):
  prepare_texts()/load_inputs() → extract() → apply_to_config() → (이후 comic_gen.comic_gen_episode가 렌더)

[2026-09-08] 로컬 전용 포맷 어댑터: 단편 생성기 GUI의 progress/ 산출물(epNN_hash.txt +
  character_sheet_epNN_hash.json)은 이 계약보다 구조화되어 있다. 그 번역은 novel_progress.py가
  전담하고, 코어는 **두 평문**만 계속 안다. 진입점은 load_inputs() 하나이며 어댑터는 lazy import.
"""
import json
import os
import re

import config
import anima_gen
from openAPI_control import call_openai_for_text, OLLAMA_DEFAULT_NUM_CTX

# ---------------------------------------------------------------- 컨텍스트 예산 (본문 무제한의 기반)
SHEET_TEXT_CAP = 4000
CHARS_PER_TOKEN = 1.8           # 한국어 실측 ≈1.8자/토큰 (run.log: 707자 본문 → 프롬프트 1531토큰)
EXTRACT_RESERVE_TOKENS = 2000   # 추출 프롬프트 지문 + 출력(JSON) 여백
EPISODE_TEXT_CAP_MIN = 1500     # num_ctx가 작아도 이 정도는 넣는다(짧은 회차는 1회 호출)
EPISODE_TEXT_CAP = 12000        # num_ctx 미확인 시 폴백 (레거시 값)

# ---------------------------------------------------------------- 컷 수 / 장면(창) 분할
CHARS_PER_PANEL = 600           # 본문 이 정도 분량 = 컷 1개 (auto 모드 컷 수 역산 기준)
MIN_PANELS_AUTO = 6             # 회차 컷 하한
BEAT_MAX_CHARS = 1800           # 장면(=컷 스크립트 LLM 1호출) 본문 상한
PANELS_PER_BEAT_MAX = 6         # 장면당 컷 상한 (호출당 JSON 손상·num_ctx 보호)
GUIDE_LINES_MAX = 24            # 창 병합 후 기승전결 가이드 상한
MAX_ACTIONS = 6                 # $ 행동 키워드 상한 (긴 본문은 등장 행동이 많다)

VALID_SEX = ("female", "male")
VALID_RATING = ("safe", "sensitive", "nsfw", "explicit")
APPEARANCE_KEYS = ("hair_color", "hair_style", "eye_color", "skin_color",
                   "face_style", "clothes", "body_shape")
_RATING_RANK = {"": 0, "safe": 1, "sensitive": 2, "nsfw": 3, "explicit": 4}

# ------------------------------------------------------------------ 시트의 #캐릭터 태그#
# 시트에서 #…# 로 감싼 조각은 설명문이 아니라 **anima가 아는 캐릭터 공식 태그(트리거)**다.
#   예) #Usagi Tsukino from Sailor Moon#
# LLM에게 판단시키면 외모 필드 외의 태그를 임의로 빼버린다(=렌더에서 캐릭터가 달라진다).
# 그래서 여기서는 LLM을 거치지 않고 **문자 그대로** 뽑아 config.char_tags에 박고,
# comic_gen.build_panel_prompt가 정제·LLM 재작성 이후에 **무조건** 프롬프트로 되돌려 넣는다.
CHAR_TAG_RE = re.compile(r"#([^#\n]{2,200})#")
PARTNER_SECTION_RE = re.compile(r"^\s*(?:[-*]\s*)?(?:상대방|상대|파트너|partner)", re.I)
_KO_RE = re.compile(r"[\u3131-\u318F\u31A0-\u31BF\uAC00-\uD7A3]")
MAX_CHAR_TAGS = 6

_LOG_DIR = "log"
_LOG_FILE = os.path.join(_LOG_DIR, "comic_input.log")


def clog(msg: str):
    try:
        os.makedirs(_LOG_DIR, exist_ok=True)
        with open(_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{msg}\n")
    except Exception:
        pass
    try:
        print(f"[INPUT] {msg}")
    except Exception:
        pass


# ------------------------------------------------------------------ 입력 파일
def read_text(path: str) -> str:
    """평문 입력 읽기(utf-8 → cp949 순차 시도, BOM 제거). 없으면 예외."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"입력 파일 없음: {path}")
    for enc in ("utf-8-sig", "utf-8", "cp949"):
        try:
            with open(path, "r", encoding=enc) as f:
                return f.read().strip()
        except UnicodeDecodeError:
            continue
    with open(path, "rb") as f:
        return f.read().decode("utf-8", "replace").strip()


def episode_char_budget(reserve_tokens: int = EXTRACT_RESERVE_TOKENS) -> int:
    """LLM 호출 1회에 넣어도 안전한 본문 글자 수 (plot.json ollama_num_ctx → 글자).

    num_ctx를 넘기면 ollama는 에러 없이 프롬프트 앞부분을 버린다 → 규칙/스키마가 증발한 채
    답변만 돌아온다. 그래서 '최대 몇 글자까지 한 번에'는 모델이 아니라 서빙 설정이 정한다.
    """
    nc = 0
    try:
        nc = int(str((config.get_json_value() or {}).get("ollama_num_ctx") or "").strip() or 0)
    except Exception:
        nc = 0
    if nc <= 0:
        nc = OLLAMA_DEFAULT_NUM_CTX
    return max(EPISODE_TEXT_CAP_MIN, int((nc - int(reserve_tokens)) * CHARS_PER_TOKEN))


# 문장 종결 직후(또는 개행)에서 자른다. re lookbehind는 고정폭(1문자)만 허용된다.
_SENT_SPLIT_RE = re.compile(r"(?<=[。！？!?.])\s*|\n+")


def _sentence_units(text: str) -> list:
    """빈 줄 없는 입력용: 문장 종결(다./요./?/!/.) 기준으로 토막낸다."""
    parts = _SENT_SPLIT_RE.split(str(text or ""))
    return [p.strip() for p in parts if p and p.strip()]


def _hard_split(unit: str, max_chars: int) -> list:
    """max_chars를 크게 넘기는 단일 문단/문장을 문장 단위로 더 쪼갠다."""
    if len(unit) <= max_chars * 1.35:
        return [unit]
    out, cur = [], ""
    for s in _sentence_units(unit) or [unit]:
        if cur and len(cur) + len(s) > max_chars:
            out.append(cur)
            cur = s
        else:
            cur = f"{cur} {s}".strip() if cur else s
        while len(cur) > max_chars * 1.8:            # 문장 자체가 초장문(빈 줄 없음)이면 강제 절단
            out.append(cur[:max_chars])
            cur = cur[max_chars:]
    if cur.strip():
        out.append(cur.strip())
    return out


def split_beats(text: str, n_beats: int = 0, max_chars: int = BEAT_MAX_CHARS) -> list:
    """본문을 장면 순서 그대로 유지하며 창(block)으로 나눈다 → [str, ...] (순서 = 서사 순서)

    n_beats>0: 정확히 그 개수(≤ 단위 수)로 글자수 균등 배분 — 컷 수에서 역산한 장면에 쓴다.
    n_beats<=0: max_chars 이하가 될 때까지 쪼갠다 — 추출 map-reduce 창에 쓴다.
    경계는 빈 줄(장면전환) → 없으면 문장 종결. 순서를 절대 섞지 않는다(컷 흐름 = 본문 흐름).
    """
    t = re.sub(r"\r\n?", "\n", str(text or "")).strip()
    if not t:
        return []
    units = [u.strip() for u in re.split(r"\n[ \t]*\n", t) if u.strip()]
    if len(units) <= 1:
        units = _sentence_units(t) or [t]
    gross = sum(len(u) for u in units)
    want = int(n_beats or 0)
    auto = want <= 0
    if auto:
        want = max(1, -(-gross // max(200, int(max_chars or BEAT_MAX_CHARS))))
    # 단위(문단/문장)가 목표 폭보다 크면 균등 배분이 무너진다(아예 못 자른다) → 목표 폭 이상은 쪼갠다.
    # 창(map-reduce) 모드: cap = 창 상한. n_beats 모드: cap = min(창 상한, 목표 폭).
    cap = int(max_chars or BEAT_MAX_CHARS) if auto \
        else max(200, min(int(max_chars or 10 ** 9), int(gross / max(1, want))))
    units = [x for u in units for x in _hard_split(u, cap)] or units
    n = min(max(1, want), len(units))
    if n <= 1:
        return ["\n\n".join(units)]
    target = max(1.0, sum(len(u) for u in units) / n)
    groups, cur, cursz = [], [], 0
    for i, u in enumerate(units):
        cur.append(u)
        cursz += len(u)
        left = len(units) - i - 1
        if len(groups) < n - 1 and cursz >= target and left >= (n - 1 - len(groups)):
            groups.append("\n\n".join(cur))
            cur, cursz = [], 0
    if cur:
        groups.append("\n\n".join(cur))
    return groups


def split_by_segments(text: str, segments) -> list:
    """LLM이 본문에서 그대로 베껴 준 막 앵커(기/승/전/결 각 첫 문장)로 본문을 자른다 → [막, ...] or []

    [2026-09-08] 자수가 적은 회차는 글자수 균등 분할이 기승전결을 마음대로 가로질렀다.
    앵커가 원문에서 순서대로 전부 찾아질 때만 분할한다(하나라도 어긋나면 [] → split_beats 폴백).
    첫 앵커 앞의 메타 줄(주인공:/상대방:…)은 1막에 속한다(장면 정보로 유용하다).
    """
    t = str(text or "")
    segs = [str(s).strip() for s in (segments or []) if str(s or "").strip()]
    if len(segs) < 2:
        return []
    # 공백 무관 매칭: LLM이 줄바꿈/공백을 조금 바꿔 베껴도 붙는다. 정규화 인덱스를 원문 인덱스로 역상사.
    idx = [k for k, ch in enumerate(t) if not ch.isspace()]
    norm_t = "".join(t[k] for k in idx)
    bounds, npos = [], 0
    for s in segs:
        ns = re.sub(r"\s+", "", s)
        j = norm_t.find(ns, npos)
        if j < 0:
            return []                              # 앵커 하나가でも 안 붙면 전체 분할 포기
        b = idx[j]
        # 앵커 바로 앞이 "결: " 같은 라운 라벨뿐이면 줄 맨 앞까지 그 막으로 옮긴다(라벨이 앞막에 남는 것 방지)
        ls = t.rfind("\n", 0, b) + 1
        gap = t[ls:b].strip()
        if gap and gap.rstrip(" :：").strip("기승전결") == "":
            b = ls
        if bounds and b <= bounds[-1]:
            return []
        bounds.append(b)
        npos = j + max(1, len(ns) - 1)
    if len(bounds) < 2 or sorted(bounds) != bounds or len(set(bounds)) != len(bounds):
        return []
    cuts = bounds + [len(t)]
    windows = [t[cuts[k]:cuts[k + 1]].strip() for k in range(len(cuts) - 1)]
    pre = t[:bounds[0]].strip()
    if pre and windows:                               # [2026-09-08] 첫 앵커 앞 메타(장소/복장)는 1막 소속 —
        windows[0] = f"{pre}\n\n{windows[0]}".strip()  #   docstring의 약속과 달리 버려졌다(ep01 실측 480자 소실)
    windows = [w for w in windows if w]
    return windows if len(windows) >= 2 else []


def target_panels(text: str, chars_per_panel: int = CHARS_PER_PANEL,
                  min_panels: int = MIN_PANELS_AUTO, max_panels: int = 0) -> int:
    """본문 길이 → 회차 컷 수 (레이아웃이 아니라 본문이 컷 수를 결정한다)."""
    cpp = max(80, int(chars_per_panel or CHARS_PER_PANEL))
    n = max(int(min_panels), -(-len(str(text or "").strip()) // cpp))
    return min(n, int(max_panels)) if max_panels and max_panels > 0 else n


def allocate(total: int, weights, minimum: int = 0) -> list:
    """total을 weights에 비례 배분(큰 나머지 방식, 합은 반드시 total). 각 칸 ≥ minimum."""
    w = [max(0.0, float(x or 0)) for x in (weights or [])]
    n = len(w)
    if not n:
        return []
    total = max(0, int(total))
    if sum(w) <= 0:
        w = [1.0] * n
    raw = [total * x / sum(w) for x in w]
    base = [int(x) for x in raw]
    order = sorted(range(n), key=lambda i: raw[i] - base[i], reverse=True)
    for k in range(total - sum(base)):
        base[order[k % n]] += 1
    minimum = max(0, int(minimum))
    for i in range(n):
        short = minimum - base[i]
        if short <= 0:
            continue
        base[i] = minimum
        donors = sorted((j for j in range(n) if base[j] > minimum), key=lambda j: -base[j])
        for j in donors[:short]:
            base[j] -= 1
    return base


def strip_markdown(text: str) -> str:
    """본문이 .md로 들어와도 상관없게 헤딩/이미지/강조만 걷어낸다(평문화)."""
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text or "")          # ![alt](img)
    t = re.sub(r"^#{1,6}(?=\s)", "", t, flags=re.M)              # ## 헤딩 (#다음 공백만 — #캐릭터태그# 보존)
    t = re.sub(r"^\s*#{1,12}\s*$", "", t, flags=re.M)             # [2026-09-08] '#####' 단독 구분선(위 정규식은
    t = re.sub(r"^\s*[-*]{3,}\s*$", "", t, flags=re.M)            # 구분선 '# 다음 공백'만 잡아 로컬 포맷이 통과했다)
    t = re.sub(r"\*\*|__|\*(?=\S)", "", t)                        # 강조
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


# ------------------------------------------------------------------ LLM 추출
def build_extract_prompt(episode_text: str, sheet_text: str, ep_num: int,
                         need_segments: bool = True) -> str:
    """평문 2종 → 구조화 JSON 추출 프롬프트

    need_segments=False: 막 앵커를 파서(novel_progress)가 이미 확보했을 때. LLM에게
      "一字不사본"을 요구할수록 오히려 한 글자 어긋나 분할이 죽으므로 요구 자체를 내린다.
    """
    ep = (episode_text or "")[:episode_char_budget()]
    sh = (sheet_text or "")[:SHEET_TEXT_CAP]
    seg_schema = ('"segments": ["기/승/전/결 각 첫 문장의 **앞부분 ~40자를 본문에서 그대로 복사**(접두 조각도 OK, 총 4개)"],'
                  if need_segments else '"segments": [],')
    rule6 = ("6. segments는 기/승/전/결 부분의 **첫 문장을 본문에서一字不사본**(의역/요약을 넣으면 앵커 매칭이\n"
             "   깨져 장면 분할이 사라진다). 문장이 길면 **앞 40자만 잘라 복사해도 된다**(부분 복사 OK, 총 4개).\n"
             "   본문에 뚜렷한 4부 구조가 없으면 빈 배열."
             if need_segments else
             "6. segments는 항상 빈 배열 []로 두세요(막 경계는 이미 프로그램이 본문에서 확정했습니다).")
    # [2026-09-09] 수위 정책 분기 — 기본은 청년향(상한 nsfw), --allow-explicit(local)에서만 explicit를 연다.
    #   상한을 문자열로 못 박아둔 자리라 여기서 나눠야 LLM이 explicit를 고를 수 있다.
    if anima_gen.explicit_allowed():
        role_line = "당신은 **성인(explicit) 만화** 제작 파이프라인의 정보 추출기입니다."
        rating_schema = '"rating": "safe | sensitive | nsfw | explicit 중 하나"'
        rule3 = ("3. actions는 행동 **이름만** 쓴다: 어절·서술·조사 붙은 문장 조각 금지. 허용 어휘 예시(explicit 허용) —\n"
                 "   터치, 애무, 페팅, 키스, 딥키스, 포옹, 밀착, 손잡음, 안마, 무릎베개 등,\n"
                 "   본문에 실제로 등장하는 강한 행동(체위·클라이맥스 계열)도 그대로 적는다.\n"
                 "   본문에 없는 행동을 지어내지 말고, 그럴 바엔 빈 배열.\n"
                 "   (이 값은 컷 pose에 주입되는 $키워드다 → 짧게 정확히)")
        rule5 = ("5. rating은 본문 수위 기준(local explicit 모드 — 상한=explicit). 강한 성행위가\n"
                 "   실제로 묘사되면 explicit로 평가한다.")
    else:
        role_line = "당신은 청년향 만화 제작 파이프라인의 정보 추출기입니다."
        rating_schema = '"rating": "safe | sensitive | nsfw 중 하나"'
        rule3 = ("3. actions는 행동 **이름만** 쓴다: 어절·서술·조사 붙은 문장 조각 금지. 허용 어휘 예시(청년향) —\n"
                 "   터치, 애무, 페팅, 키스, 딥키스, 포옹, 밀착, 손잡음, 이마 키, 볼뽀뽀, 안마, 무릎베개.\n"
                 "   강한 성행위는 본문에 있어도 위 목록의 가까운 스킨십으로 격하해 적는다.\n"
                 "   본문에 없는 행동을 지어내지 말고, 그럴 바엔 빈 배열.\n"
                 "   (이 값은 컷 pose에 주입되는 $키워드다 → 짧게 정확히)")
        rule5 = ("5. rating은 본문 수위 기준(청년향 상한=nsfw). 강한 성행위가 나와도 nsfw로만 평가하고\n"
                 "   explicit는 쓰지 않는다.")
    return f"""{role_line}
아래 [캐릭터 시트(평문)]와 [에피소드 {ep_num} 본문]만 읽고 JSON 하나만 출력하세요. 설명문/코드펜스 금지.

출력 스키마 (키 이름 정확히 유지):
{{
 "protagonist": {{
   "name": "한국어 이름",
   "sex": "female 또는 male",
   "hair_color": "영문 태그 (예: light brown hair)",
   "hair_style": "영문 태그 (예: messy hair, ponytail, hair framing face)",
   "eye_color": "영문 태그 (예: brown eyes)",
   "skin_color": "영문 태그 (예: fair skin)",
   "face_style": "영문 태그 (예: crying, blushing, teardrop)",
   "clothes": "영문 태그 (예: police uniform, utility belt)",
   "body_shape": "영문 태그 (예: loli, child, aged down)",
   "job": "한국어 직업",
   "breasts_size": -1에서 5 사이 정수,
   "hip_size": -1에서 5 사이 정수
 }},
 "partner": {{"name": "한국어 이름", "sex": "female 또는 male", "clothes": "영문 태그"}},
 "guides": {{"protagonist": ["기", "승", "전", "결 각 1문장 한국어"], "partner": ["상대방 시선 1~2문장"], "sub": []}},
 {seg_schema}
 "actions": ["본문에 실제로 등장하는 스킨십/로맨스 행동 이름만** 최대 4개 (2~6글자 명사구)"],
 {rating_schema}
}}

규칙:
1. 외모·복장 필드(APPEARANCE)는 반드시 **소문자 영문 태그**만. 한글/설명문 금지. 시트에 없으면 본문에서推断, 그래도 없으면 가장 무난한 태그.
2. guides.protagonist 4문장은 본문의 사건의 순서를 그대로 요약(도입→위기→클라이맥스→마무리), 각 문장에 **장소·복장·구체 행동**을 포함한다.
{rule3}
4. 성별은 문맥(대명사/서술) 기준으로 판단, 애매하면 female.
{rule5}
{rule6}
7. 시트의 #…# 로 감싼 글자는 그 캐릭터의 **공식 캐릭터 태그(트리거)**입니다. 이름·정체성 판단에만
   참고하고 hair_color/clothes 같은 외모 태그 필드에는 **복사하지 마세요**(그 태그는 별도 주입됩니다).

[캐릭터 시트(평문)]
{sh}

[에피소드 {ep_num} 본문]
{ep}
"""


def _extract_json_obj(text: str) -> dict:
    """LLM 응답에서 첫 JSON 객체를 관대하게 추출"""
    if not text:
        return {}
    t = text.strip()
    t = re.sub(r"^```[a-zA-Z]*", "", t).strip()
    t = re.sub(r"```$", "", t).strip()
    i, j = t.find("{"), t.rfind("}")
    if 0 <= i < j:
        for cand in (t[i:j + 1],):
            try:
                return json.loads(cand)
            except Exception:
                # trailing comma / 말줄임 같은 가벼운 JSON 오류 완화
                relaxed = re.sub(r",\s*([}\]])", r"\1", cand)
                try:
                    return json.loads(relaxed)
                except Exception:
                    return {}
    return {}


def _norm_char_tag(raw) -> str:
    """#…# 알맹이 정규화: 줄바꿈/연속 공백 정리, anima가 읽지 못하는 '_'는 공백으로(대소문자 원문 유지)"""
    s = re.sub(r"\s+", " ", str(raw or "").replace("_", " ")).strip()
    s = re.sub(r"\s*,\s*", ", ", s)
    return s.strip(" .,;:")


def extract_char_tags(sheet_text: str) -> dict:
    """시트 → {"protagonist": [...], "partner": [...], "warn": [...]} (쓰기 순서 유지·중복 제거·≤6개)

    상대방 섹션(줄 머리가 '상대방/상대/파트너') 안에서 쓴 #…# 만 상대방 태그로 봅니다.
    ( comic 프롬프트는 [AAA …]=주인공 / [BBB …]=상대방으로 물리 분리되어 있어, 뒤섞으면 오염됩니다.)
    닫는 #이 없는 줄("#Usagi Tsukino")은 태그로 보지 않고 경고만 남깁니다(마크다운 헤딩 # 제목 제외).
    """
    text = str(sheet_text or "")
    proto, partner, warn = [], [], []

    def add(bucket: list, raw: str):
        tag = _norm_char_tag(raw)
        if len(tag) < 2 or any(tag.lower() == t.lower() for t in bucket):
            return
        bucket.append(tag)
        if _KO_RE.search(tag):
            warn.append(f"#태그에 한글이 있습니다 — anima는 영문 태그만 압니다: #{tag}#")

    in_partner = False
    for line in text.splitlines():
        if PARTNER_SECTION_RE.match(line):
            in_partner = True
        for m in CHAR_TAG_RE.finditer(line):
            add(partner if in_partner else proto, m.group(1))
    for line in text.splitlines():
        s = line.strip()
        if re.match(r"^#[^\s#]", s) and "#" not in s[1:]:
            warn.append(f"닫는 #이 없어 태그로 읽지 않았습니다: {s[:40]}")
            break
    return {"protagonist": proto[:MAX_CHAR_TAGS], "partner": partner[:MAX_CHAR_TAGS], "warn": warn}


def _clean_tag(v: str) -> str:
    """태그 필드 정규화: 개행/여백 정리, 남은 한글 조각은 버리지 않고 그대로 두되 대소문자만 정리"""
    s = str(v or "").strip().replace("\n", " ")
    s = re.sub(r"\s*,\s*", ", ", s)
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip(" ,.")


def _normalize_extract(data: dict) -> dict:
    """LLM 원시 JSON 1창 분량 → 스키마 검증/보정"""
    proto = dict(data.get("protagonist") or {})
    part = dict(data.get("partner") or {})
    for k in APPEARANCE_KEYS:
        proto[k] = _clean_tag(proto.get(k))
    proto["name"] = str(proto.get("name") or "").strip()
    proto["job"] = str(proto.get("job") or "").strip()
    sex = str(proto.get("sex") or "").strip().lower()
    proto["sex"] = "male" if sex in ("male", "m", "남자", "남성") else "female"
    for k in ("breasts_size", "hip_size"):
        try:
            proto[k] = max(-1, min(5, int(proto.get(k, -1))))
        except Exception:
            proto[k] = -1
    psex = str(part.get("sex") or "").strip().lower()
    part["sex"] = "male" if psex in ("male", "m", "남자", "남성") else "female"
    part["clothes"] = _clean_tag(part.get("clothes"))

    guides = data.get("guides") or {}
    g_pro = [str(x).strip() for x in (guides.get("protagonist") or []) if str(x).strip()]
    g_part = [str(x).strip() for x in (guides.get("partner") or []) if str(x).strip()]
    acts = []
    for a in (data.get("actions") or []):
        for piece in re.split(r"[,、/]|\s과\s|\s를\s|\s을\s", str(a)):
            a2 = re.sub(r"^[$\s]+", "", piece).strip()
            # LLM이 서술문을 통째로 넣으면 버린다 — $키워드는 pose 문자열 부분일치로 쓰인다
            # (조사가 섞인 어절은 pose에 들어가도 매칭/생성이 어그러진다)
            if not a2 or not (2 <= len(a2) <= 6) or len(a2.split()) > 2:
                continue
            if re.search(r"에서|으로|에게|부터|까지|면서|해서|하고|때문|그리|그러", a2):
                continue
            if a2 not in acts:
                acts.append(a2)
    rating = anima_gen._cap_safety(data.get("rating"))   # [2026-09-09] 상한 관문은 anima_gen._cap_safety 하나
    if rating not in VALID_RATING:
        rating = ""

    segs = []
    for s in (data.get("segments") or []):
        s2 = re.sub(r"\s+", " ", str(s or "")).strip()
        # [2026-09-08] 상한 80→160: 결 막이 한 문장 96자였던 회차(16:59 로그)에서 앵커를
        #   버려 기승전 3막으로 흡수됐다. 접두 복사 권장으로도 안 줄면 긴 채로 받아=find는 성공한다.
        if 3 <= len(s2) <= 160 and s2 not in segs:
            segs.append(s2)

    out = dict(data)
    out["protagonist"], out["partner"] = proto, part
    out["guides"] = {"protagonist": g_pro, "partner": g_part, "sub": []}
    out["actions"] = acts[:MAX_ACTIONS]
    out["segments"] = segs[:12]
    out["rating"] = rating
    return out


def _merge_extracts(parts: list) -> dict:
    """창별 추출 결과 병합 — 정체성은 첫 유효 값, 가이드/행동은 순서대로 누적, 수위는 최대값.

    protagonist 외모/이름을 창마다 다르게 적으면 렌더에서 캐릭터가 갈라진다 → **첫 창 값 우선**,
    빈 칸만 다른 창으로 채운다. 가이드만 창 수만큼 늘어난다(장면 순서 그대로).
    """
    base = dict(parts[0])
    proto = dict(base.get("protagonist") or {})
    part = dict(base.get("partner") or {})
    for p in parts[1:]:
        pp, pt = p.get("protagonist") or {}, p.get("partner") or {}
        for k in list(proto.keys()):
            if proto.get(k) in ("", None, -1) and pp.get(k) not in ("", None, -1):
                proto[k] = pp[k]
        for k in list(part.keys()):
            if part.get(k) in ("", None) and pt.get(k) not in ("", None):
                part[k] = pt[k]
    g_pro, g_part, acts, segs, rating = [], [], [], [], ""
    for p in parts:
        g = p.get("guides") or {}
        for x in g.get("protagonist") or []:
            if x and x not in g_pro:
                g_pro.append(x)
        for x in g.get("partner") or []:
            if x and x not in g_part:
                g_part.append(x)
        for a in p.get("actions") or []:
            if a not in acts:
                acts.append(a)
        for s in p.get("segments") or []:          # 창별 4막 앵커를 서사 순서대로 누적
            if s and s not in segs:
                segs.append(s)
        r = str(p.get("rating") or "")
        if _RATING_RANK.get(r, 0) > _RATING_RANK.get(rating, 0):
            rating = r
    base["protagonist"], base["partner"] = proto, part
    base["guides"] = {"protagonist": g_pro[:GUIDE_LINES_MAX], "partner": g_part[:8], "sub": []}
    base["actions"] = acts[:MAX_ACTIONS]
    base["segments"] = segs[:12]
    base["rating"] = rating
    return base


def _extract_once(episode_text: str, sheet_text: str, ep_num: int, log_fn=None,
                  need_segments: bool = True) -> dict:
    """본문 1창 → LLM 1회 → 정규화 dict (실패 시 {})"""
    prompt = build_extract_prompt(episode_text, sheet_text, ep_num, need_segments=need_segments)
    try:
        raw, _ = call_openai_for_text(prompt, messages=None, log_fn=log_fn or clog,
                                      temperature=0.2, repeat_penalty=1.05)
    except Exception as e:
        clog(f"추출 API 예외: {e}")
        return {}
    data = _extract_json_obj(raw or "")
    if not isinstance(data, dict) or not data:
        clog(f"추출 JSON 파싱 실패 (응답 앞 120자): {str(raw)[:120]}")
        return {}
    return _normalize_extract(data)


def extract(episode_text: str, sheet_text: str, ep_num: int = 1, log_fn=None,
            char_budget: int = 0, need_segments: bool = True) -> dict:
    """본문 전체 → 구조화 dict. 컨텍스트 예산을 넘기면 창(beat)으로 쪼개 창당 1회 → 병합.

    통째로 한 번에 넣으려다 num_ctx에 넘어가면 프롬프트 앞부분(=추출 규칙)이 잘려
    추출 자체가 썩은 답을 뱉는다. 예산 안에서는 1회, 넘으면 여러 창(map-reduce).
    """
    ep_num = max(1, int(ep_num or 1))        # 회차 번호는 1기준(--ep 0 습관 흡수) — 키가 0으로 박히면
                                             # 조회가 1기준이라 기승전결 가이드가 통째로 '(가이드 없음)'이 된다
    budget = int(char_budget or episode_char_budget())
    body = episode_text or ""
    windows = [body] if len(body) <= budget else (split_beats(body, max_chars=budget) or [body])
    parts = []
    for wi, w in enumerate(windows, 1):
        d = _extract_once(w, sheet_text, ep_num, log_fn, need_segments=need_segments)
        if d:
            parts.append(d)
            if len(windows) > 1:
                clog(f"추출 창 {wi}/{len(windows)} ({len(w)}자): "
                     f"가이드 {len(d['guides']['protagonist'])}줄 · 행동 {d['actions']} · "
                     f"rating={d['rating'] or '(자동)'}")
    if not parts:
        return {}
    data = parts[0] if len(parts) == 1 else _merge_extracts(parts)
    data["windows"] = len(windows)
    proto = data.get("protagonist") or {}
    missing = [k for k in ("name", "hair_color", "clothes", "body_shape") if not proto.get(k)]
    if missing:
        clog(f"⚠ 추출 결과 필수 태그 누락: {missing} (렌더 필수 검증 실패 가능)")
    clog(f"추출 완료({len(windows)}창 병합): {proto.get('name')}/{proto.get('sex')} · "
         f"상대방 {(data.get('partner') or {}).get('name')} · "
         f"가이드 {len(data['guides']['protagonist'])}줄 · 행동 {data['actions']} · "
         f"rating={data['rating'] or '(자동)'}")
    return data


# ------------------------------------------------------------------ config 주입
def _merge_overrides(base: dict, overrides: dict) -> dict:
    """'원작이 정답'인 필드(시트 JSON)를 LLM 추출보다 우선 병합한다. 빈 값만 LLM 것을 남긴다.

    10화를 한 결로 그리려면 창마다 달라졌던 외모 태그가 캐릭터를 갈라놓는다 — 회차 시트의
    머리/눈/피부/표정/몸매 태그를 결정론으로 박아버리는 것이 이 함수의 목적이다.
    (clothes는 넣지 않는다: 한글 산문이어서 Anima가 못 읽고, 번역은 추출 LLM의 몫이다.)
    """
    out = dict(base or {})
    for sec in ("protagonist", "partner"):
        d = dict(out.get(sec) or {})
        for k, v in ((overrides or {}).get(sec) or {}).items():
            if v not in ("", None, -1):
                d[k] = v
        out[sec] = d
    return out


def apply_to_config(data: dict, episode_text: str, sheet_text: str, ep_num: int = 1,
                    panels_per_page: int = 5, book_num: int = 0, total_episodes: int = 1,
                    overrides: dict = None, segments: list = None, safety: str = "") -> None:
    """추출 결과 → config 주입 (init_anima_tags 필수 10필드 + 컷 스크립트 입력)

    overrides : 시트 JSON 등에서 온 우선값 {"protagonist":{…},"partner":{…}} — LLM보다 앞선다
    segments  : 막 앵커. 파서(novel_progress)가 본문에서 확정해 주면 LLM 사본보다 우선한다
    safety    : "safe" 등으로 박으면 수위를 강제로 고정한다(이 repo의 기본 정책 = safe 전용).
                "explicit"는 local 스위치(--allow-explicit)가 켜진 때만 살아남고, 아니면 nsfw로 낮아진다.
    """
    data = _merge_overrides(data, overrides)
    safety = anima_gen._cap_safety(safety)      # [2026-09-09] --safety explicit의 상한도 같은 관문을 지난다
    if safety in VALID_RATING:
        data["rating"] = safety
    proto = (data or {}).get("protagonist") or {}
    part = (data or {}).get("partner") or {}
    guides = (data or {}).get("guides") or {}

    ep_num = max(1, int(ep_num or 1))        # 조회 측(comic_gen._guides_for/anima_gen)은 1기준 — 저장 키도 맞춘다
    idx = max(0, ep_num - 1)
    # [2026-09-08] --ep 2 --total-episodes 1 처럼 회차 번호가 총 회차 수 크면
    #   init_anima_tags가 episode >= total_episodes로 'no_episode'를 뱉어 렌더가 통째로 건너뛰어진다.
    #   번호 쪽으로 맞춰 확장한다(사용자 인자 실수 흡수).
    config.total_episodes = max(1, int(total_episodes), int(ep_num))

    # [2026-09-07] EP-index config 배열은 config import 때 data/episode_setup.json 기준으로
    # 사이즈가 정해진다(기본 1). --ep 2 이상을 넘기면 인덱스 범위를 벗어나므로 먼저 확장한다.
    # (comic_gen dry-run의 review_safety는 try/except로 통과했지만 init_anima_tags/_build_tag_block은
    #  config.face_tag[ep] 등을 직접 인덱싱한다 — EP2 첫 실행에서 IndexError로 터진 원인이 여기.)
    _need = idx + 1
    _ep_arrays = ("episode_content", "face_tag", "makeup_tag", "marks_tag", "body_tag",
                  "bodystyle_tag", "exposure_tag", "p_exposure_tag", "pubic_hair_tag",
                  "background_tag", "partner_exposure_tag", "partner_expression_tag",
                  "expression_arr", "review_safety")
    for _attr in _ep_arrays:
        _arr = getattr(config, _attr, None)
        if isinstance(_arr, list):
            while len(_arr) < _need:
                _arr.append("")
    _rs = getattr(config, "review_stats", None)
    if isinstance(_rs, list):
        while len(_rs) < _need:
            _rs.append([0] * 7)

    # anima_gen.init_anima_tags 필수 10필드
    config.name = proto.get("name") or "주인공"
    config.sex = proto.get("sex") or "female"
    config.hair_color = proto.get("hair_color") or "black hair"
    config.hair_style = proto.get("hair_style") or "long hair"
    config.eye_color = proto.get("eye_color") or "brown eyes"
    config.skin_color = proto.get("skin_color") or "fair skin"
    config.face_style = proto.get("face_style") or "blushing"
    config.clothes = proto.get("clothes") or "school uniform"
    config.body_shape = proto.get("body_shape") or "realistic"
    config.job = proto.get("job") or ""
    config.breasts_size = proto.get("breasts_size", -1)
    config.hip_size = proto.get("hip_size", -1)

    # [2026-09-08] 시트 #…# = 캐릭터 공식 태그 → LLM 경유 없이 config로 바로 (렌더가 무조건 넣는다)
    ct = extract_char_tags(sheet_text or "")
    config.char_tags = ct["protagonist"]
    config.partner_char_tags = ct["partner"]
    for w in ct["warn"]:
        clog(f"⚠ #태그: {w}")
    if ct["protagonist"] or ct["partner"]:
        clog(f"#캐릭터 태그 인식: 주인공={ct['protagonist'] or '(없음)'} "
             f"상대방={ct['partner'] or '(없음)'} — 모든 컷 프롬프트에 강제 주입됩니다")

    config.name2 = part.get("name") or "상대"
    config.sex2 = "남자" if part.get("sex") == "male" else "여자"
    config.outfit2 = part.get("clothes") or "casual"

    # 컷 스프프트가 읽는 시트/가이드/$키워드 (comic_gen._sheet_texts / _guides_for)
    while len(config.episode_protagonist_sheets) <= idx:
        config.episode_protagonist_sheets.append("")
        config.episode_partner_sheets.append("")
    config.episode_protagonist_sheets[idx] = (sheet_text or "").strip() or f"{config.name} ({config.job})"
    config.episode_partner_sheets[idx] = (f"{config.name2} ({config.sex2}) — "
                                          f"복장: {config.outfit2}")
    while len(config.episode_sub_sheets) <= idx:
        config.episode_sub_sheets.append("")
    config.ep_corruption_guides_map = {**(getattr(config, "ep_corruption_guides_map", {}) or {}),
                                       ep_num: {"protagonist": guides.get("protagonist") or [],
                                                "partner": guides.get("partner") or [],
                                                "sub": guides.get("sub") or []}}
    config.special_writing_req = {**(getattr(config, "special_writing_req", {}) or {}),
                                  ep_num: list((data or {}).get("actions") or [])}
    # [2026-09-08] 기승전결 막 앵커(본문 원문 사본) — 컷 스크립트가 장면 골격으로 쓴다 (comic_gen)
    #   segments 인자가 오면 그것을 우선한다(local progress 포맷은 막 경계를 원문으로 갖고 있다).
    _segs = list(segments) if segments else list((data or {}).get("segments") or [])
    config.ep_beat_segments = {**(getattr(config, "ep_beat_segments", {}) or {}), ep_num: _segs}

    # 에피소드 본문: progress/ 가 없으므로 config 폴백이 유일한 원천
    while len(config.episode_content) <= idx:
        config.episode_content.append("")
    config.episode_content[idx] = episode_text or ""
    if (data or {}).get("rating") and idx < len(config.review_safety):
        config.review_safety[idx] = data["rating"]

    # 만화 출력 설정
    config.comic_enable = True
    config.comic_panels_per_page = max(1, int(panels_per_page))
    config.comic_book_num = max(0, int(book_num))
    clog(f"config 주입: {config.name}/{config.sex} · 컷/페이지={config.comic_panels_per_page} · "
         f"book={config.comic_book_num or '(자동)'} · safety={config.review_safety[idx] or '(자동)'}")


def load_inputs(episode_path: str, sheet_path: str = "", special=None) -> dict:
    """입력 파일 → 코어가 먹을 입력 묶음 (평문 계약이 아닌 로컬 포맷의 유일한 진입점)

      {"episode_text","sheet_text","segments","overrides","ep_num",
       "format":"plain|novel_progress","notes":[…]}

    special=None(기본) : 내용 스니핑으로 자동 감지 — --special 없이 물어도 원작 메타가
                         본문에 섞여 들어가지 않도록 안전하게 어댑터를 탄다.
    special=True        : 강제 (파일자가 꼬이는 로컬 포맷을 명시적으로 태울 때)
    special=False       : 어댑터 무시 — 배송된 평문 계약 그대로
    """
    ep_raw = read_text(episode_path)
    sh_raw = read_text(sheet_path) if sheet_path and os.path.exists(sheet_path) else ""
    looks = bool(sh_raw.lstrip()[:1] == "{" and '"protagonist"' in sh_raw) or sniff_progress(ep_raw)
    use = looks if special is None else bool(special)
    if special is None and looks:
        clog("progress/ 로컬 포맷을 감지했습니다 — 어댑터(novel_progress)로 평문화합니다 "
             "(권장: run_comic.py --special)")
    if not use:
        return {"episode_text": strip_markdown(ep_raw), "sheet_text": strip_markdown(sh_raw),
                "segments": [], "overrides": {}, "ep_num": 0, "format": "plain", "notes": []}
    import novel_progress as NP                        # 어댑터는 선택적 — 코어 import 그래프에 넣지 않는다
    info = NP.load(episode_path, sheet_path)
    body = strip_markdown(info.get("episode_text") or "")
    sheet = strip_markdown(info.get("sheet_text") or "")
    for n in info.get("notes") or []:
        clog(f"⚠ 어댑터: {n}")
    clog(f"입력[progress]: {os.path.basename(episode_path)} → 본문 {len(body)}자"
         f"(원문 {len(ep_raw)}자) / 시트 {len(sheet)}자 / 막 앵커 {len(info.get('segments') or [])}개"
         f" / 시트 우선주입 {sorted(((info.get('overrides') or {}).get('protagonist') or {}).keys())}")
    return {"episode_text": body, "sheet_text": sheet,
            "segments": list(info.get("segments") or []),
            "overrides": info.get("overrides") or {}, "ep_num": int(info.get("ep_num") or 0),
            "format": info.get("format") or "novel_progress", "notes": info.get("notes") or []}


def prepare_texts(episode_path: str, sheet_path: str, special=None):
    """입력 파일 → (에피소드 평문, 시트 평문) — 평문/로컬 포맷 겸용 (호환 유지 엔트리)"""
    d = load_inputs(episode_path, sheet_path, special=special)
    clog(f"입력: {episode_path}({len(d['episode_text'])}자) / {sheet_path or '(시트 없음)'}"
         f"({len(d['sheet_text'])}자)")
    return d["episode_text"], d["sheet_text"]


def sniff_progress(text: str) -> bool:
    """progress/ 포맷인지 내용으로 판단(novel_progress가 보일 때만 참)"""
    try:
        import novel_progress as NP
        return NP.sniff(text)
    except Exception:
        return False
