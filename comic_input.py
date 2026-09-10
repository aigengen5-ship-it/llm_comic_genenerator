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
import zlib
import re
import time
import difflib
import hashlib

import config
import runlog
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
    runlog.note(msg, "INPUT")
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


def find_anchor_hits(text: str, anchors, skip_missing: bool = False) -> list:
    """앵커(본문을 그대로 베낀 조각)를 순서대로 찾아 (앵커 번호, 시작 오프셋) 째로 돌려준다.

    skip_missing=False: 하나라도 안 붙으면 [] — 막 앵커는 전부 붙어야 분할할 가치가 있다.
    skip_missing=True : 이 조각에서 안 붙는 앵커는 건너뛴다 — 막 단위 분할에서는 다른 막의
                        유닛이 안 붙는 것이 정상이다(Strict면 막 분할이 통째로 꺼진다).
    좌표를 LLM에게 받지 않고 문자열로 찾는 이유는 이 모델이 글자 수를 세지 않기 때문이다.
    """
    t = str(text or "")
    anch = [str(a).strip() for a in (anchors or []) if str(a or "").strip()]
    if not anch:
        return []
    idx = [k for k, ch in enumerate(t) if not ch.isspace()]     # 공백 무관: 줄바꿈을 바꿔 베껴도 붙는다
    norm_t = "".join(t[k] for k in idx)
    hits, npos = [], 0
    for n, s in enumerate(anch):
        ns = re.sub(r"\s+", "", s)
        j = norm_t.find(ns, npos)
        if j < 0:
            if skip_missing:
                continue
            return []
        hits.append((n, idx[j]))
        npos = j + max(1, len(ns) - 1)
    return hits


def _snap_to_line_start(text: str, b: int) -> int:
    """앞이 '결: ' 같은 라벨뿐이면 줄 맨 앞까지 밀어, 라벨이 앞 막에 남는 것을 막는다."""""
    t = str(text or "")
    ls = t.rfind("\n", 0, b) + 1
    gap = t[ls:b].strip()
    return ls if (gap and gap.rstrip(" :：").strip("기승전결") == "") else b


def find_anchor_bounds(text: str, anchors, min_len: int = 2, skip_missing: bool = False) -> list:
    """find_anchor_hits → 시작 오프셋 목록. 요구 개수(min_len) 미만이면 []."""
    hits = find_anchor_hits(text, anchors, skip_missing=skip_missing)
    bounds = []
    for n, b in hits:
        b = _snap_to_line_start(text, b)
        if bounds and b <= bounds[-1]:
            if not skip_missing:
                return []
            continue
        bounds.append(b)
    if min_len <= 1:
        return bounds
    return bounds if len(bounds) >= int(min_len) else []


def anchor_units(text: str, units, strong_weight: int = 2) -> list:
    """이 조각 안에서 붙는 유닛만 골라 (시작 오프셋, LLM가 준 컷 수) 째를 돌려준다."""
    units = normalize_units(units, strong_weight=strong_weight)
    out = []
    for n, b in find_anchor_hits(text, [u["at"] for u in units], skip_missing=True):
        b = _snap_to_line_start(text, b)
        if out and b <= out[-1][0]:
            continue
        out.append((b, int(units[n]["cuts"])))
    return out


def _windows_from_bounds(text: str, bounds) -> list:
    """오프셋 목록 → 조각 목록 (첫 앵커 앞의 메타 줄은 1번째 조각에 붙인다)."""
    t = str(text or "")
    if len(bounds) < 2:
        return []
    cuts = list(bounds) + [len(t)]
    wins = [t[cuts[k]:cuts[k + 1]].strip() for k in range(len(cuts) - 1)]
    pre = t[:bounds[0]].strip()
    if pre and wins:
        wins[0] = f"{pre}\n\n{wins[0]}".strip()
    return [w for w in wins if w]


def split_by_segments(text: str, segments) -> list:
    """LLM이 본문에서 그대로 베껴 준 막 앵커(기/승/전/결 각 첫 문장)로 본문을 자른다 → [막, ...] or []

    [2026-09-08] 자수가 적은 회차는 글자수 균등 분할이 기승전결을 마음대로 가로질렀다.
    앵커가 원문에서 순서대로 전부 찾아질 때만 분할한다(하나라도 어긋나면 [] → split_beats 폴백).
    첫 앵커 앞의 메타 줄(주인공:/상대방:…)은 1막에 속한다(장면 정보로 유용하다).
    """
    bounds = find_anchor_bounds(text, segments, min_len=2)
    if len(bounds) < 2 or sorted(bounds) != bounds or len(set(bounds)) != len(bounds):
        return []
    windows = _windows_from_bounds(text, bounds)
    return windows if len(windows) >= 2 else []


# --------------------------------------------------------------------------- #
# [2026-09-09] 컷 배분을 '글자 수'에서 '일어난 사건(액션)'으로 옮긴다.
#   실측: 본문 2666자 → 목표 6컷 → 레이아웃 8컷 → 4막 × 최소 2컷이 8을 다 써서 [2,2,2,2].
#   기가 2컷인 이유는 본문이 짧아서가 아니라 **배분 저울이 글자 수**였기 때문이다.
# --------------------------------------------------------------------------- #
MIN_UNIT_CHARS = 90                  # 이보다 짧은 유닛 조각은 앞 유닛에 합친다(풍선 하나짜리 컷 남발 방지)
UNITS_PER_CALL_MAX = 6               # 장면(=LLM 1호출)당 유닛 상한 — PANELS_PER_BEAT_MAX와 같은 JSON 보호선
MAX_UNITS = 14                       # 회차 유닛 상한 (14개 × 강함 2컷 = 28컷이 물리적 상한)
ITEMS_MAX = 24                       # 항목 1:1 모드(--item-cuts)의 항목 상한 — 항목 하나가 컷 하나                       # 회차 유닛 상한 (14개 × 강함 2컷 = 28컷이 물리적 상한)

# '이 사건은 컷 2개짜리다'로 봐야 하는 큐 — 신체가 움직이거나 관계가 바뀌는 순간들.
# (민감어는 넣지 않는다: 이 목록은 공개 코드에 탄다)
_STRONG_CUES = (
    "키스", "입맞춤", "포옹", "껴안", "안을", "손을 잡", "손잡", "만지", "어깨를 잡", "볼을",
    "옷을", "단추", "벗", "상의", "침대로", "침대 위", "이불", "무릎",
    "밀어내", "밀쳤", "피한다", "피해", "고백", "거절", "차단", "화해", "약속", "용서",
    "무너", "울음", "울며", "눈물이", "후회", "고백하", "떠난", "뒤쫓", "잡으러",
    "숨이", "뜨겁", "거칠", "깊게", "허리를", "속도를", "파도", "이성을", "한계를",
    "문 열", "문이 열", "들어온", "들어난", "걸려", "불이 꺼", "비명이", "넘어",
)


def _item_mode() -> bool:
    """항목 1:1 모드 (--item-cuts, 기본 켬) — 본문을 시간 순 항목으로 나누고 항목 = 컷 1개."""
    try:
        import config
        return bool(getattr(config, "comic_item_cuts", True))
    except Exception:
        return False


def classify_device(text: str) -> str:
    """본문 조각을 화면 장치(행동/대사/속마음)로 분류한다 — LLM 없이 규칙으로 한다.

    만화에서 한 컷은 대개 한 가지 장치로 말한다(이 repo의 컷 규칙 8번과 같은 결).
      대사   : 인용부호 안의 말, '~라고 말했다/대답했다/외쳤다'
      속마음 : '~라고 생각했다', '속으로', '머릿속', 말줄임표가 달린 혼잣말
      행동   : 나머지 (지문으로 보여주기)
    """
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    if not t:
        return "행동"
    quoted = re.search(chr(34) + r".{2,}?" + chr(34), t) or re.search(r"[“”「」『』].{2,}?[”“」「』『]", t)
    think = re.search(r"(속으로|머릿속|마음속|생각했다|생각이|느껴졌다|라고 혼잣말|스스로에게)", t)
    said = re.search(r"(말했다|대답|답변|외쳤다|질렀|속삭|이야기|대화|대사를|불렀|소리)", t)
    if think:
        return "속마음"
    if quoted or said:
        return "대사"
    if t.endswith(("…", "...")):
        return "속마음"
    return "행동"


def split_for_cuts(text: str, n: int = 1) -> list:
    """장면 본문을 컷 n개에 해당하는 조각으로 시간 순서대로 나눈다(장치 판정용)."""
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    n = max(1, int(n or 1))
    if not t:
        return [""] * n
    sents = _sentence_units(t) or [t]
    if len(sents) <= n:
        out = sents + [""] * (n - len(sents))
        return out[:n]
    per = (len(sents) + n - 1) // n
    chunks = [" ".join(sents[k:k + per]) for k in range(0, len(sents), per)]
    while len(chunks) < n:
        chunks.append("")
    return chunks[:n]


# ------------------------------------------------------------------ 키 이름 접기 (2026-09-10)
# [2026-09-10] Q4 디코딩 사고는 **키 이름을 안에서** 찢기도 한다. 이 경우 JSON은 완벽히 parse된다.
#   실측(state/extract_cache.yaml): "eye_ ей_color": "brown eyes" / "eye_com_color": "brown eyes"
#   → 파서는 성공했는데 eye_color가 없는 답이 된다. 그래서 "빈 핵심 항목"으로 취급돼 추가 LLM 호출을
#   다시 하고, 엉뚱한 이름의 키는 체크포인트에 영구히 남는다. 파서가 웃으면서 넘기는 실패가 제일 위험하다.
#   → 스키마 키 이름으로 되돌린다. (json_soft_fix와 달리 **parse 성공 경로**에서 동작한다.)
_EXTRACT_KEYSETS = {
    "_top": ("protagonist", "partner", "guides", "segments", "units", "actions", "rating"),
    "protagonist": ("name", "sex", "hair_color", "hair_style", "eye_color", "skin_color",
                    "face_style", "clothes", "body_shape", "job", "breasts_size", "hip_size"),
    "partner": ("name", "sex", "clothes"),
    "guides": ("protagonist", "partner", "sub"),
}
UNIT_KEYS = ("at", "kind", "cuts", "anchor", "text", "device", "panels")


def _fold_key(key, allowed):
    """훼손된 키 이름 → 스키마 키 (실측: 'eye_ ей_color' / 'eye_com_color' / 'ncuts') 아니면 ''"""
    k = str(key or "").strip().lower()
    if not k or k in allowed:
        return ""
    clean = re.sub(r"[^a-z_]", "", k)                    # 비ASCII·공백 제거 ('eye_ ей_color' → 'eyecolor')
    if clean in allowed:
        return clean
    for a in allowed:                                    # 조각이 붙어 깨진 경우 ('ncuts' → 'cuts')
        if a and (a in clean or clean in a) and min(len(a), len(clean)) >= 3:
            return a
    near = difflib.get_close_matches(clean, list(allowed), n=1, cutoff=0.62)
    return near[0] if near else ""


def _fold_dict(d, allowed, where, log):
    """dict 한 개의 키를 스키마로 접는다 — 값은 살리고 왜키는 버린다(이미 값이 있으면 접지 않는다)"""
    if not isinstance(d, dict):
        return d
    out = {}
    for k, v in (d or {}).items():
        tgt = _fold_key(k, allowed)
        if tgt and tgt not in d:
            out[tgt] = v
            log.append((where, str(k), tgt))
        elif tgt and out.get(tgt) in ("", None, -1, [], {}):
            out[tgt] = v
            log.append((where, str(k), tgt))
        else:
            out[k] = v
    return out


def fold_extract_keys(data):
    """추출 JSON 전역(주인공/상대방/가이드/units)의 키를 스키마로 접는다 → (data, [(곳, 옛키, 새키)])"""
    log = []
    if not isinstance(data, dict):
        return data, log
    out = _fold_dict(data, _EXTRACT_KEYSETS["_top"], "root", log)
    for sec in ("protagonist", "partner", "guides"):
        if isinstance(out.get(sec), dict):
            out[sec] = _fold_dict(out[sec], _EXTRACT_KEYSETS[sec], sec, log)
    if isinstance(out.get("units"), list):
        out["units"] = [_fold_dict(u, UNIT_KEYS, "units", log) if isinstance(u, dict) else u
                        for u in out["units"]]
    return out, log


def normalize_units(raw, max_units: int = MAX_UNITS, strong_weight: int = 2) -> list:
    """LLM이 나눈 본문 항목 목록을 [{at, cuts, kind}]로 정규화한다 (units가 지나는 유일한 관문).

    받는 형태는 둘 다 허용한다(26B Q4는 객체를 깨뜨린다):
      {"at": "사건 시작 문장(본문 복사)", "cuts": 2}   ← 권장: 나누는 판단과 컷 수를 LLM이 한다
      "사건 시작 문장(본문 복사)"                       ← 컷 수는 코드가 문장에서 판정(폴백)

    항목 1:1 모드(기본, `comic_item_cuts`)에서는 `cuts`를 **항상 1로 고박**한다 — 프롬프트도 그
    키를 부탁하지 않는다(2026-09-10: 값이 항상 1인 자리가 디코딩 사고 1순위였다).
    """
    item = _item_mode()
    if item:
        max_units = max(int(max_units or 0), ITEMS_MAX)
    # [2026-09-10] 키 이름 훼손("ncuts" 등)도 여기서 접는다 — normalize_units가 units의 유일한 관문이다
    raw = [_fold_dict(u, UNIT_KEYS, "units", []) if isinstance(u, dict) else u for u in (raw or [])]
    out = []
    for u in raw:
        if isinstance(u, dict):
            at = re.sub(r"\s+", " ", str(u.get("at") or u.get("anchor") or u.get("text") or ""))
            kind = re.sub(r"\s+", "", str(u.get("kind") or u.get("device") or "")).strip()
            try:
                cuts = int(u.get("cuts") or u.get("panels") or 0)
            except Exception:
                cuts = 0
            cuts = cuts if 1 <= cuts <= 3 else 0          # 컷 3개를 넘는 유닛은 없다(넘기면 폴백 판정)
        else:
            at = re.sub(r"\s+", " ", str(u or "")).strip()
            cuts = 0
            kind = ""
        if kind not in ("행동", "대사", "속마음"):
            kind = ""                                   # LLM이 규칙 밖 값을 주면 장치 판정을 코드가 한다
        if not (3 <= len(at) <= 160) or any(at == o["at"] for o in out):
            continue
        _w = 1 if item else (cuts or action_weight(at, strong_weight))
        out.append({"at": at, "cuts": _w, "kind": kind})
    return out[:max_units]


def action_weight(text: str, strong_weight: int = 2) -> int:
    """액션 유닛 하나(요약 또는 본문 조각)의 컷 가중치 — 강하면 strong_weight, 아니면 1."""
    s = re.sub(r"\s+", " ", str(text or ""))
    if not s:
        return 1
    sw = max(1, int(strong_weight))
    return sw if any(c in s for c in _STRONG_CUES) else 1


def split_acts_by_units(acts, units, strong_weight: int = 2,
                        max_units_per_beat: int = UNITS_PER_CALL_MAX):
    """막 조각을 **액션 유닛 앵커**로 더 쪼갠다 → (beats, beat_acts, weights)

    유닛이 그 막 안에서 순서대로 찾아질 때만 쪼갠다(못 찾으면 막을 통째로 둔다).
    너무 짧은 조각은 앞 조각에 합치고, 막당 유닛 수는 JSON 보호선(기본 6)으로 누른다.
    컷 수는 **유닛을 나눈 LLM이 준 값**을 쓰고, 없으면(문자열로만 준 경우) 큐 판정으로 메꾼다.
    """
    beats, beat_acts, weights = [], [], []
    for ai, act in enumerate(acts or []):
        act = str(act or "").strip()
        if not act:
            continue
        pairs = anchor_units(act, units, strong_weight)           # 다른 막의 유닛은 조용히 건너뛴다
        cut_w = {b: w for b, w in pairs if b > 0}
        bounds = [b for b, _ in pairs if b > 0]                   # 막의 첫 유닛은 막 자체의 시작
        parts, pw = [], []
        if bounds:
            cuts = [0] + bounds + [len(act)]
            for k in range(len(cuts) - 1):
                seg = act[cuts[k]:cuts[k + 1]].strip()
                if seg:
                    parts.append(seg)
                    pw.append(int(cut_w.get(cuts[k + 1], 0) or action_weight(seg, strong_weight)))
        if not parts:
            parts, pw = [act], [action_weight(act, strong_weight)]
        if _item_mode():
            # [2026-09-09] 항목 1:1 모드에서는 짧은 항목도 컷 하나다. 짧은 것끼리 붙이면
            #   항목 20개가 장면 1개로 눌려 컷 6개로 압축됐다(실측 727자 원고). 그래서
            #   개수로만 묶는다 — 장면 1개(LLM 호출 1개)에 항목 max_units_per_beat개까지.
            merged, mw = [], []
            _g = max(1, int(max_units_per_beat))
            for k in range(0, len(parts), _g):
                merged.append("\n\n".join(parts[k:k + _g]).strip())
                mw.append(sum(pw[k:k + _g]))
            beats += merged
            beat_acts += [ai] * len(merged)
            weights += mw
            continue
        # 짧은 조각은 앞 조각에 합친다 (풍선 하나만 들어갈 컷이 넘치면 화면이 산으로 간다)
        merged, mw = [parts[0]], list(pw[:1])
        for p, w in zip(parts[1:], pw[1:]):
            if len(merged[-1]) < MIN_UNIT_CHARS:
                merged[-1] = f"{merged[-1]}\n\n{p}".strip()
                mw[-1] = mw[-1] + w        # [2026-09-09] 합쳐도 사건은 사라지지 않는다 — 컷 수는 합(최대값이면 짤린다)
            else:
                merged.append(p)
                mw.append(w)
        # 막당 유닛 상한: 같은 막 안에서 더 쪼갠 것끼리 다시 합친다(가중치는 큰 쪽을 따른다)
        while len(merged) > max_units_per_beat:
            j = len(merged) - 2
            merged[j] = f"{merged[j]}\n\n{merged[j + 1]}".strip()
            mw[j] = mw[j] + mw[j + 1]      # [2026-09-09] 위와 같은 이유(합 보존)
            del merged[j + 1], mw[j + 1]
        beats += merged
        beat_acts += [ai] * len(merged)
        weights += mw
    return beats, beat_acts, weights


def target_panels_from_weights(weights, strong_weight: int = 2, min_panels: int = MIN_PANELS_AUTO,
                               max_panels: int = 0, acts: int = 0, min_per_act: int = 2) -> int:
    """액션 가중치 합 → 회차 컷 예산 (본문 길이는 더 이상 저울이 아니다).

    하한: MIN_PANELS, 그리고 '막당 min_per_act컷' (기승전결이 1컷씩으로 납작해지는 것을 막는다).
    상한: max_panels(0=무제한). 강한 사건은 strong_weight컷을 쓴다.
    """
    w = [max(1, int(x or 1)) for x in (weights or [])]
    n = sum(w)
    if acts:
        n = max(n, int(min_per_act) * int(acts))
    n = max(int(min_panels), n)
    return min(n, int(max_panels)) if max_panels and int(max_panels) > 0 else n


def target_panels(text: str, chars_per_panel: int = CHARS_PER_PANEL,
                  min_panels: int = MIN_PANELS_AUTO, max_panels: int = 0) -> int:
    """본문 길이 → 회차 컷 수 (레이아웃이 아니라 본문이 컷 수를 결정한다)."""
    cpp = max(80, int(chars_per_panel or CHARS_PER_PANEL))
    n = max(int(min_panels), -(-len(str(text or "").strip()) // cpp))
    return min(n, int(max_panels)) if max_panels and max_panels > 0 else n


def _vary(key: str, salt: int = 0, mod: int = 1000) -> int:
    """문자열 → 0..mod-1 (프로세스마다 달라지는 hash() 대신 crc32 — 재현성이 목적이다)."""
    return zlib.crc32(f"{key}|{int(salt)}".encode("utf-8")) % max(1, int(mod))


def allocate(total: int, weights, minimum: int = 0, variation: int = 0) -> list:
    """total을 weights에 비례 배분(큰 나머지 방식, 합은 반드시 total). 각 칸 ≥ minimum.

    variation>0 이면 '나머지가 같은 칸' 중 누가 먼저인지가 그 값마다 달라진다(합은 보존).
    """
    w = [max(0.0, float(x or 0)) for x in (weights or [])]
    n = len(w)
    if not n:
        return []
    total = max(0, int(total))
    if sum(w) <= 0:
        w = [1.0] * n
    raw = [total * x / sum(w) for x in w]
    base = [int(x) for x in raw]
    order = sorted(range(n), key=lambda i: (raw[i] - base[i],
                                            _vary(f"alloc|{i}", variation) / 1000.0 if variation else 0.0),
                   reverse=True)
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
    # units는 항상 부탁한다 — 유닛은 컷 배분의 저울이라 막 앵커를 파서가 이미 갖고 있어도 필요하다.
    _pn, _pn2 = str(getattr(config, "pin_name", "") or "").strip(), str(getattr(config, "pin_name2", "") or "").strip()
    name_lock = ("7-c. 이름이 고정되었습니다: 주인공 = '{p}', 상대방 = '{q}' — 두 필드에 이 이름 외의 것을 넣지 말고, "
                 "가이드 문장에서도 이 호칭을 쓴다.".format(p=_pn or "(임의)", q=_pn2 or "(임의)")
                 if (_pn or _pn2) else "")
    if _item_mode():
        # [2026-09-09] 항목 1:1 모드 (--item-cuts 기본 켬) — 항목 하나가 컷 하나가 된다.
        # [2026-09-10] 'cuts'를 스키마에서 **뺐다** — 항목 모드는 값이 항상 1이고 코드도 버리는데
        #   (--item-cuts 실측: normalize_units가 1로 고정), 그 토큰 자리에서 Q4 디코딩이 회차당
        #   4~24번 반복해 깨졌다. 10회차 중 5회차가 '"cuts"' 자리 하나로 통째로 버려졌다
        #   (log/error.log 13:28:58·13:37:14·13:37:38·13:38:03·13:38:29).취약한 자리를 없앤다.
        unit_schema = ('"units": [{"at": "화면 항목이 시작하는 문장을 본문에서 그대로 복사", '
                       '"kind": "\ud589\ub3d9|\ub300\uc0ac|\uc18d\ub9c8\uc74c"}],')
        unit_rule = ("6-b. units는 본문을 **일어난 시간 순서대로 화면 항목 하나씩** 나눈 목록이다 — "
                     "항목 하나가 컷 하나가 된다. 항목은 셋 중 하나: "
                     "**\ud589\ub3d9**(무엇을 했다) / **\ub300\uc0ac**(누가 무엇을 말했다) / **\uc18d\ub9c8\uc74c**(누가 무엇을 생각했다).\n"
                     "   'at'에는 그 항목이 시작하는 문장을 본문에서 그대로 복사(의역 금지), 'kind'는 위 셋 중 하나만 쓴다.\n"
                     "   항목에는 'at'·'kind' **두 키만** 쓴다('cuts' 같은 키를 넣지 않는다 — 컷 수는 프로그램이 정한다).\n"
                     "   인사 몇 마디 같은 사소한 흐름은 한 항목으로 합치고, "
                     "행동\u00b7대사\u00b7속마음이 교차하는 흐름은 쪼개서 그대로 남긴다. 총 4~24\u1110, \uc77c\uc5b4\ub09c \uc21c\uc11c\ub300\ub85c.")
    else:
            # 사건 모드만 'cuts'를 부탁한다(1~2를 LLM이 고른다). 못 적으면 빼도 된다 — 프로그램이 1로 둔다.
            unit_schema = '"units": [{"at": "사건이 시작하는 문장을 본문에서 그대로 복사", "cuts": 1}],'
            unit_rule = ("6-b. units는 본문을 **사건(액션) 단위로 나눈 목록**이다 — 글자 수가 아니라 '누가 무엇을 했나'로 자른다.\n"
                         "   한 유닛 = 사건 하나(이동·호칭·대사만 있는 장면은 앞 유닛에 합친다). 'at'은 그 사건이 시작하는\n"
                         "   문장을 본문에서 그대로 복사(의역 금지), 'cuts'는 그 사건을 그릴 컷 수(평범한 사건 1,\n"
                         "   신체 접촉·관계 변화·클라이맥스는 2). **모르겠으면 'cuts' 키를 아예 빼도 된다**(1로 둔다).\n"
                         "   총 4~12개, 일어난 순서대로.")
    rule6 = ("6. segments는 기/승/전/결 부분의 **첫 문장을 본문에서一字不사본**(의역/요약을 넣으면 앵커 매칭이\n"
             "   깨져 장면 분할이 사라진다). 문장이 길면 **앞 40자만 잘라 복사해도 된다**(부분 복사 OK, 총 4개).\n"
             "   본문에 뚜렷한 4부 구조가 없으면 빈 배열.\n"
             + unit_rule if need_segments else
             "6. segments는 항상 빈 배열 []로 두세요(막 경계는 이미 프로그램이 본문에서 확정했습니다).\n"
             + unit_rule)
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
   "breasts_size": 영문 태그,
   "hip_size": 영문 태그
 }},
 "partner": {{"name": "한국어 이름", "sex": "female 또는 male", "clothes": "영문 태그"}},
 "guides": {{"protagonist": ["기", "승", "전", "결 각 1문장 한국어"], "partner": ["상대방 시선 1~2문장"], "sub": []}},
 {seg_schema}
 {unit_schema}
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
{name_lock}
7. 시트의 #…# 로 감싼 글자는 그 캐릭터의 **공식 캐릭터 태그(트리거)**입니다. 이름·정체성 판단에만
   참고하고 hair_color/clothes 같은 외모 태그 필드에는 **복사하지 마세요**(그 태그는 별도 주입됩니다).
7-b. protagonist.name / partner.name에는 **본문에서 불리는 한국어 호칭**만 쓴다. #캐릭터 태그#의 영문 이름(예: Kirisaki Chitoge)이나 작품 제목을 이름으로 옮기지 않는다 — #태그는 그림 참조일 뿐이고, 이름은 화면 지문·대사에 쓰인다. 본문에 이름이 안 나오면 시트에 적힌 호칭(예: 'AMD 소녀')을 그대로 쓴다.

[캐릭터 시트(평문)]
{sh}

"""

# 본문 미사용
#[에피소드 {ep_num} 본문]
#{ep}
# [2026-09-09] 추출 실패 실측 재현: gemma가 키 앞 따옴표를 이상한 유니코드 문자로 디코딩한다
#   "eye_color": "brown eyes",\n․  "skin_color": ...   → U+2024 ONE DOT LEADER
#   json.loads는 "Expecting property name enclosed in double quotes"로 죽고 우리는 {}를 받았다.
#   즉 원고가 문제인 게 아니라 **토큰 디코딩 사고**이므로 파서 쪽에서 주워拾는다.
_QUOTE_LIKE = "\u201c\u201d\u201e\u201f\u301d\u301e\uff02\u2018\u2019\u201a\u201b\uff40"   # 따옴표류
ODD_TOKEN_CHARS = "\u2024\u2025\u2026\u30fb\u00b7"                       # 키 자리에서 발견된 이상 문자


def json_soft_fix(text: str) -> str:
    """관대한 JSON 복구 — 파싱이 실패했을 때만 쓴다(성공한 응답은 절대 이걸 거치지 않는다)."""
    t = str(text or "")
    # [2026-09-10] 따옴표류 치환은 **키 자리만** 한다. 값 안의 “한국어 인용문”을 직따옴표로 바꾸면
    #   "“하아, 하아…”" 가 ""하아, 하아…"" 가 되어 **살아있던 JSON을 이 파서가 죽였다**(실측 EP05 응답).
    #   값 안의 곡선 따옴표는 JSON에서 그냥 문자다 — 그대로 두는 편이 언제나 안전하다.
    _ql = "[" + _QUOTE_LIKE + "]"
    t = re.sub(r'([\{\[,]\s*)' + _ql, r'\1"', t)                    # 키를 여는 자리
    t = re.sub(_ql + r'(\s*:)', r'"\1', t)                          # 키를 닫는 자리
    t = re.sub(r'"\s*' + _ql + r'\s*$', '"', t, flags=re.M)          # 값 끝에 따라온 따옴표류
    t = re.sub(r"([{\[,])\s*[" + ODD_TOKEN_CHARS + r"]+", r"\1", t)                 # 키 앞 이상 문자 제거
    # 이상 문자가 '쉼표 자리'에 들어온 경우(실측: "blushing"․ "makeup": )도 쉼표로 되돌린다 —
    #   따옴표가 열려 있으면 문자열을 닫고 쉼표를, 닫혀 있으면 쉼표만 보탠다.
    # [2026-09-10] 단, **구조 자리에서만** 고친다. 쉼표가 깨진 자리의 뒤에는 반드시 `"키":` 또는 요소
    #   구분(`, ] }`)이 온다. 값 안의 줄임표("하아… 소타 님….")는 정당한 내용이라 그대로 둔다 —
    #   예전처럼 무조건 고치면 살아있던 값이 죽었다(실측 EP05: "“하아, 하아… 소타 님…" → ""하아, 하아",소타…).
    for _m in reversed(list(re.finditer(r'[\u2024\u2025\u2026\u00b7\u30fb]+', t))):
        if not re.match(r'\s*(?:"[^"\n]{1,40}"\s*[:,\]\}]|[}\]])', t[_m.end():_m.end() + 80]):
            continue
        _open = t[:_m.start()].count('"') % 2
        t = t[:_m.start()] + ('",' if _open else ',') + t[_m.end():].lstrip()
    t = re.sub(r'([{\[,]\s*)([A-Za-z_\u00c0-\u318f][^"\n:]*?)(\s*":)', r'\1"\2\3', t)  # 따옴표 없는 키 감싸기
    # [2026-09-10] --special 실측 3종 — **키 자리** 이물질은 위 규칙이 못 잡는다(따옴표가 다른 글자로 바뀐다).
    #   `*cuts*: 1`(마크다운 강조) · ` উপস্থিত cuts: 1`(벵골·태국) · `ềncuts": 1`(베트남)
    #   → 셋 다 '"cuts": 1' 자리에서 재현됐다(EP02·05~08이 이 한 자리로 버려졌다). 키 이름은 여기서 살리고,
    #   스키마와 다른 이름(ncuts → cuts)은 접는 단추 fold_extract_keys가 따로 고친다.
    #   모두 **행 맨 앞(키 자리)**만 고친다 — 문자열 값은 절대 건드리지 않는다.
    t = re.sub(r'(?m)^(\s*)[*_~`]+([A-Za-z_][A-Za-z0-9_ ]{0,40})[*_~`]+(\s*):', r'\1"\2"\3:', t)       # *cuts*:
    t = re.sub(r'(?m)^(\s*)[^\x00-\x7F]{1,8}\s*([A-Za-z_][A-Za-z0-9_ ]{0,40})"(\s*):', r'\1"\2"\3:', t)    # ềncuts":
    t = re.sub(r'(?m)^(\s*)[^\x00-\x7F]{1,8}\s*([A-Za-z_][A-Za-z0-9_ ]{0,40})(\s*):', r'\1"\2"\3:', t)     # উপস্থিত cuts:
    t = re.sub(r'(?m)^(\s*)([A-Za-z_][A-Za-z0-9_ ]{0,40})"(\s*):', r'\1"\2"\3:', t)                    # 열림 따옴표 실종
    t = re.sub(r'(?m)^(\s*)([A-Za-z_][A-Za-z0-9_ ]{0,40})(\s*):', r'\1"\2"\3:', t)                     # mini: [  (따옴표 없는 키)
    t = re.sub(r'([{\[,]\s*)"([A-Za-z_][A-Za-z0-9_]{0,24})(:)', r'\1"\2"\3', t)                       # "I:4 → "I":4 (닫는 따옴표 실종)
    t = re.sub(r'(?m)^(\s*)[*\u2022\u00b7]+(?=\s*")', r'\1', t)                                        # * "문장",  (마크다운 총알)
    # 키 앞에 섞여 들어온 홀 글자(실측: ...,\n\ub7ec  "background": "shopping mall") — Q4 디코딩 사고
    t = re.sub(r'(?m)^(\s*)[\u1100-\u11ff\u3130-\u318f\uac00-\ud7af\u3040-\u30ff]{1,3}\s+(?=")', r"\1", t)
    t = re.sub(r",\s*([}\]])", r"\1", t)                                             # trailing comma
    return _repair_lines(t)


def _repair_lines(text: str) -> str:
    """줄 단위 구조 복구(2026-09-10 --special 실측 3가지). 문자열 **밖**에서만 판단한다.

      ① 배열 안에 키 없이 던져진 본문 문장   → {"at": "…"} 로 감싼다 (unit 객체를 씌우다 놓친 형태)
      ② 객체 키 자리에 혼자 따옴표만 있는 줄  → 버린다   ("kind": "행동", 아래  "유즈키는 기어갔다.",)
      ③ 객체 키 자리의 비문 줄(키 자체가 깨짐) → 버린다   (…  "kind": "행동",\n    을",\n    "cuts": 1)

    ①의 문장은 본문 앵커('at')라 지우면 그 장면 컷이 통째로 사라진다 → 감싸는 편이 정답이고,
    본문에 없는 문장이면 anchor_units가 조용히 버린다. ②③은 이미 값이 다른 필드에 있으니 버려도 정보 loss가 없다.
    """
    lines = str(text or "").split("\n")
    inside, stack, drop = False, [], []
    for i, ln in enumerate(lines):
        was_inside, esc = inside, False
        for ch in ln:
            if esc:
                esc = False
                continue
            if ch == "\\":
                esc = True
            elif ch == '"':
                inside = not inside
            elif not inside and ch in "{[":
                stack.append(ch)
            elif not inside and ch in "}]":
                if stack:
                    stack.pop()
        if was_inside:
            continue
        s = ln.strip()
        if not s or s[0].isdigit():
            continue
        top = stack[-1] if stack else ""
        if top == "{":                                    # 키 자리다 — 키가 아니면 버린다
            if ":" not in s and not re.fullmatch(r"[{}\[\]},\s]*", s):   # `{` `}` `],` 같은 마감/개시 줄은 예외
                drop.append(i)
            continue
        if top == "[" and not s[0] in '"{}[]-' and ":" not in s and 3 <= len(s) <= 400:
            prev = next((lines[k].strip() for k in range(i - 1, -1, -1)), "")
            nxt = next((lines[k].strip() for k in range(i + 1, len(lines))), "")
            if prev.endswith(("},", "],", "[", "{")) and (nxt.startswith("{") or nxt.startswith("]")):
                lines[i] = '   {"at": "%s"},' % s.replace('"', '\\\"')
    for i in reversed(drop):
        del lines[i]
    return "\n".join(lines)


def _extract_json_obj(text: str) -> dict:
    """LLM 응답에서 첫 JSON 객체를 관대하게 추출 → 실패 시 (dict, 오류문자열) 중 dict만"""
    obj, _err = extract_json_obj_checked(text)
    return obj


def extract_json_obj_checked(text: str):
    """({} 또는 객체, 실패 사유) — 실패 사유에 JSON 오류 위치 문맥까지 넣어 진단을 쉽게 한다."""
    if not text:
        return {}, "빈 응답"
    t = str(text).strip()
    t = re.sub(r"^```[a-zA-Z]*", "", t).strip()
    t = re.sub(r"```$", "", t).strip()
    i, j = t.find("{"), t.rfind("}")
    if not (0 <= i < j):
        return {}, "JSON 객체 기호({ … })를 찾지 못함"
    cand = t[i:j + 1]
    last = ""
    for fix in (False, True):
        src = json_soft_fix(cand) if fix else cand
        try:
            return json.loads(src), ""
        except Exception as e:
            last = str(e)
    p = ""
    m = re.search(r"char (\d+)", last)
    if m:
        k = int(m.group(1))
        p = " | 문제 부근: " + repr(cand[max(0, k - 70):k + 40])
    return {}, f"{last}{p}"


def _split_start_late(raw, default: str = ""):
    """쉼표 목록을 '회차 시작 상태 / 후반 상태'로 가른다(추출이 시간 순으로 쓴다는 전제).

    추출 프롬프트에 "먼저 입은 옷·먼저 짓는 표정 순으로" 적히도록 규칙이 있다. 순서를
    못 지킨 경우에도 *late는 클라이맥스 컷에만 쓰이므로(anima_gen) 초반 컷이 오염되지 않는다.
    """
    parts = [p.strip() for p in re.split(r"[,;]", str(raw or "")) if p.strip()]
    if not parts:
        return (default or ""), ""
    return parts[0], ", ".join(parts[1:])


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
    # [2026-09-10] parse는 성공했는데 키 이름만 깨진 답("eye_ ей_color")을 여기서 바로잡는다.
    #   이걸 놓으면 필드가 빈 줄 알리고 LLM을 한 번 더 부르고, 왜키는 체크포인트에 남는다.
    data, _folded = fold_extract_keys(data)
    if _folded:
        clog("추출 키 이름 접기: " + ", ".join(f"{w}.`{o}`→{n}" for w, o, n in _folded[:6])
             + (" 외" if len(_folded) > 6 else ""))
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
    out["units"] = normalize_units(data.get("units"))      # [2026-09-09] 사건 단위를 LLM이 나누고 컷 수까지 준다
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
    _u = []
    for p in parts:                                        # 창별 유닛은 순서대로 합친다(중복 앵커는 첫 개만)
        _u += [u for u in normalize_units(p.get("units")) if not any(u["at"] == o["at"] for o in _u)]
    base["units"] = _u[:MAX_UNITS]
    base["rating"] = rating
    return base


# ── 추출 체크포인트 (2026-09-10) ─────────────────────────────────────────────
# 초기 JSON 파싱이 깨지면(특수/일반 모드 모두) 회차 전체를 다시 물어먹는 것이 비싸다.
#   ① 채운 값은 state/extract_cache.yaml에 저장하고 다음 실행에서 **빈 칸만** 이어받는다.
#   ② 그래도 빈 항목은 본문·시트를 주고 **그 키만** 따로 다시 묻는다.
#   ③ 그것도 실패하면 최후로 **캐릭터 설정(직업·공식 캐릭터 태그)**으로 추론해 메운다 —
#      단 '추론'이라 로그에 남긴다. 직업으로 컷 1 복장을 지어내는 것은 본문 근거가 아니라
#      회차 상태 오염과 같은 종류의 사고라, 마지막 안전판으로만 쓴다.
EXTRACT_CACHE = os.path.join("state", "extract_cache.yaml")

# 비면 안 되는 핵심 항목 (빈 채로 진행하면 태그·컷이 회차 설정으로 대체된다)
EXTRACT_REQUIRED = (
    "protagonist.name", "protagonist.sex", "protagonist.hair_color", "protagonist.hair_style",
    "protagonist.eye_color", "protagonist.skin_color", "protagonist.clothes",
    "protagonist.face_style", "protagonist.body_shape", "guides.protagonist", "rating",
)
# 있으면 좋은 항목 (없어도 결정론 경로로 버틴다)
EXTRACT_OPTIONAL = ("protagonist.job", "protagonist.breasts_size", "protagonist.hip_size",
                    "partner.name", "partner.clothes", "guides.partner", "actions", "units", "segments")


def extract_key(episode_text: str, sheet_text: str, ep_num: int = 1, mode: str = "plain") -> str:
    """입력(본문+시트+회차+모드) 지문 — 원고를 고르면 체크포인트는 자동 폐기(키가 바뀐다)."""
    h = hashlib.sha1()
    h.update(str(episode_text or "").encode("utf-8", "ignore"))
    h.update(b"\x00")
    h.update(str(sheet_text or "").encode("utf-8", "ignore"))
    h.update(f"\x00{int(ep_num)}|{mode}".encode("utf-8"))
    return h.hexdigest()[:16]


def _dig(d, path: str):
    cur = d
    for k in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(k)
        elif isinstance(cur, list) and k.isdigit() and int(k) < len(cur):
            cur = cur[int(k)]
        else:
            return None
    return cur


def _put(d: dict, path: str, val):
    keys = path.split(".")
    cur = d
    for k in keys[:-1]:
        if not isinstance(cur.get(k), dict):
            cur[k] = {}
        cur = cur[k]
    cur[keys[-1]] = val


def _is_empty(v) -> bool:
    if v is None or (isinstance(v, str) and not v.strip()):
        return True
    if isinstance(v, (list, tuple)):
        return not [x for x in v if str(x or "").strip()]
    if isinstance(v, dict):
        return not any(not _is_empty(x) for x in v.values())
    return False


def missing_extract_fields(data) -> list:
    """핵심 항목 중 비어 있는 것 (dotted path 리스트) — 다음 실행이 채워야 할 목록."""
    d = data if isinstance(data, dict) else {}
    return [p for p in EXTRACT_REQUIRED if _is_empty(_dig(d, p))]


def optional_extract_fields(data) -> list:
    d = data if isinstance(data, dict) else {}
    return [p for p in EXTRACT_OPTIONAL if _is_empty(_dig(d, p))]


def _src_note(ep_path: str = "", sheet_path: str = "") -> dict:
    """체크포인트에 '어느 파일의 회차 몇'이었는지 적어 둔다(2026-09-10).

    `--special`의 본문은 다른 repo의 `progress/`에 있어 지문이 사라지면 대조 자체가 어렵고,
    생성기 특성상 책장 안의 다른 회차와 본문이 같을 수 있어 파일 이름 없이는 특정할 수 없었다.
    """
    return {"episode": os.path.basename(str(ep_path or "")),
            "sheet": os.path.basename(str(sheet_path or ""))} or {}


def save_extract_checkpoint(key: str, data, missing=None, source=None) -> str:
    """채운 값을 YAML로 남긴다 — 다음 실행은 **빈 칸만** 다시 묻는다(merge는 빈 칸만 채운다)."""
    try:
        import yaml
        os.makedirs(os.path.dirname(EXTRACT_CACHE) or ".", exist_ok=True)
        d = data if isinstance(data, dict) else {}

        def _prune(x):
            if isinstance(x, dict):
                return {k: _prune(v) for k, v in x.items() if not _is_empty(_prune(v)) or not isinstance(v, (dict, list))}
            if isinstance(x, list):
                return [ _prune(v) for v in x]
            return x

        payload = {"key": key, "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "missing": list(missing or []), "data": _prune(d)}
        if source:
            payload["source"] = source
        old = {}
        if os.path.exists(EXTRACT_CACHE):
            try:
                old = yaml.safe_load(open(EXTRACT_CACHE, encoding="utf-8").read()) or {}
            except Exception:
                old = {}
        allruns = old.get("runs") if isinstance(old.get("runs"), dict) else {}
        prev = allruns.get(key) or {}
        # 실패 이력과 파일 정보를 **덮어쓰지 않는다** — 성공 체크포인트를 쓰는 순간 지난 실패
        #   기록이 사라지면(실측으로 이 줄이 그랬다) 다음에 또 실패했을 때 원인을 조율 수 없다.
        payload["failed"] = list(prev.get("failed") or [])
        payload["source"] = source or prev.get("source") or {}
        allruns[key] = payload
        with open(EXTRACT_CACHE, "w", encoding="utf-8") as f:
            yaml.safe_dump({**old, "runs": allruns}, f, allow_unicode=True, sort_keys=False)   # 다른 최상위 키 보존
        return EXTRACT_CACHE
    except Exception as e:
        clog(f"추출 체크포인트 저장 실패: {e}")
        return ""


def save_extract_failure(key: str, reason: str, data=None, source=None) -> str:
    """추출이 **아무것도** 못 얻어왔을 때도 이력을 남긴다(2026-09-10).

    예전은 `if not data: return 2`로 끝나 체크포인트를 쓰지 않아, 같은 원고를 다시 돌리면
    아무 힌트 없이 처음부터 다시 물었습니다. 이번 실패 사유를 남겨 다음 실행이 알아보고
    안내하도록 합니다(부분 항목이 이미 있으면 그것도 함께 보관합니다).
    """
    try:
        import yaml
        os.makedirs(os.path.dirname(EXTRACT_CACHE) or ".", exist_ok=True)
        old = {}
        if os.path.exists(EXTRACT_CACHE):
            try:
                old = yaml.safe_load(open(EXTRACT_CACHE, encoding="utf-8").read()) or {}
            except Exception:
                old = {}
        runs = old.get("runs") if isinstance(old.get("runs"), dict) else {}
        prev = runs.get(key) or {}
        runs[key] = {"key": key, "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
                     "source": source or prev.get("source") or {},
                     "missing": list(prev.get("missing") or []),
                     "failed": (list(prev.get("failed") or []) + [str(reason)[:200]])[-5:],
                     "data": data if isinstance(data, dict) else (prev.get("data") or {})}
        with open(EXTRACT_CACHE, "w", encoding="utf-8") as f:
            yaml.safe_dump({**old, "runs": runs}, f, allow_unicode=True, sort_keys=False)
        return EXTRACT_CACHE
    except Exception as e:
        clog(f"추출 실패 기록 저장 실패: {e}")
        return ""


def load_extract_record(key: str) -> dict:
    """체크포인트 원레코드 — {"data":…, "missing":…, "failed":[…]} (없으면 {})"""
    try:
        import yaml
        if not key or not os.path.exists(EXTRACT_CACHE):
            return {}
        y = yaml.safe_load(open(EXTRACT_CACHE, encoding="utf-8").read()) or {}
        rec = (y.get("runs") or {}).get(key)
        return rec if isinstance(rec, dict) else {}
    except Exception:
        return {}


def load_extract_checkpoint(key: str) -> dict:
    """같은 입력 지문의 체크포인트 (없으면 {})"""
    try:
        import yaml
        if not key or not os.path.exists(EXTRACT_CACHE):
            return {}
        y = yaml.safe_load(open(EXTRACT_CACHE, encoding="utf-8").read()) or {}
        run = (y.get("runs") or {}).get(key) or {}
        d = run.get("data")
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def merge_extract_cached(data, cached) -> tuple:
    """빈 칸만 캐시로 메운다(이번 실행이 얻은 값을 덮지 않는다). → (data, 채운 경로)"""
    d = data if isinstance(data, dict) else {}
    filled = []
    for path in list(EXTRACT_REQUIRED) + list(EXTRACT_OPTIONAL):
        if _is_empty(_dig(d, path)) and not _is_empty(_dig(cached or {}, path)):
            _put(d, path, _dig(cached, path))
            filled.append(path)
    return d, filled


def fill_missing_extract(data, missing, episode_text: str, sheet_text: str, ep_num: int = 1,
                         attempts: int = 2) -> tuple:
    """빈 핵심 항목만 따로 LLM에 다시 묻는다 (전체 재추출보다 훨씬 싼 호출)."""
    d = data if isinstance(data, dict) else {}
    miss = list(missing or [])
    if not miss or not str(episode_text or "").strip():
        return d, miss
    body = str(episode_text or "")[:2400]
    for k in range(1, max(1, int(attempts)) + 1):
        prompt = ("평문 원고를 구조화하는 일입니다. **아래 빈 항목만** 채워 부분 JSON으로 돌려주세요.\n"
                  "외모·복장은 소문자 영문 danbooru 태그, 그 외는 한국어. 없는 정보는 지어내지 말고 빈 문자열.\n\n"
                  f"[채울 항목] {', '.join(miss)}\n"
                  '형식 예: {"protagonist": {"clothes": "school uniform"}, "rating": "safe"}\n\n'
                  f"[캐릭터 시트]\n{str(sheet_text or '')[:900]}\n\n[에피소드 본문]\n{body}")
        try:
            raw, _ = call_openai_for_text(prompt, messages=None, log_fn=clog,
                                          temperature=0.2 if k == 1 else 0.0, repeat_penalty=1.05)
        except Exception as e:
            clog(f"빈 항목 보충 {k}회 호출 실패: {e}")
            continue
        got, _err = extract_json_obj_checked(raw or "")
        got = got if isinstance(got, dict) else {}
        n = 0
        for path in miss:
            v = _dig(got, path)
            if not _is_empty(v) and _is_empty(_dig(d, path)):
                _put(d, path, v)
                n += 1
        if n:
            clog(f"빈 항목 보충 {k}회: {n}개 채움")
            break
        if k < int(attempts):
            clog(f"빈 항목 보충 {k}회는 건질 게 없습니다 → 재시도")
    return d, [p for p in EXTRACT_REQUIRED if _is_empty(_dig(d, p))]


# 추론 안전판: 공식 캐릭터 태그(#…#)는 '이 캐릭터의 평소 디자인'이므로 본문에 없는 외모·복장을
#   메우는 정당한 근거다. 회차 태그(회차 중반 이후 상태 포함)와 혼동하지 말 것.
_TRIGGER_BAGS = {
    "protagonist.clothes": ("uniform", "school uniform", "dress", "shirt", "blouse", "skirt", "jacket",
                            "coat", "suit", "tie", "bra", "panties", "swimsuit", "kimono", "apron",
                            "sweater", "jeans", "pants", "sailor", "gloves", "boots", "heels"),
    "protagonist.hair_style": ("hair,", " hair", "ponytail", "braid", "bangs", "bun"),
    "protagonist.hair_color": ("hair",),
    "protagonist.eye_color": ("eyes", "eye color"),
    "protagonist.skin_color": ("skin",),
    "protagonist.body_shape": ("body", "breasts", "hips", "loli", "child", "slim", "petite"),
}


def infer_missing_from_profile(data, missing=None) -> tuple:
    """최후 안전판 — 공식 캐릭터 태그·직업으로 빈 외모/수위 항목을 메운다 (로그에 '추론' 명시)."""
    d = data if isinstance(data, dict) else {}
    miss = list(missing if missing is not None else EXTRACT_REQUIRED)
    _ct = getattr(config, "char_tags", []) or []
    trig = ", ".join([", ".join(str(x) for x in _ct) if isinstance(_ct, (list, tuple)) else str(_ct)])
    bag = [t.strip().lower() for t in re.split(r"[,\n]", trig) if t.strip()]
    job = str(_dig(d, "protagonist.job") or getattr(config, "job", "") or "")
    filled = []

    def _pick(path, pred):
        if _is_empty(_dig(d, path)):
            hits = [t for t in bag if pred(t)]
            if path == "protagonist.clothes" and job:
                # 직업이 있으면 직업 복장을 먼저 시도(실측: '경찰' → police uniform 식으로 붙는다)
                jmap = {"경찰": "police uniform", "교사": "teacher outfit", "의사": "nurse uniform",
                        "간호사": "nurse uniform", "성우": "casual clothes", "점원": "store clerk uniform",
                        "학생": "school uniform", "형사": "suit", "변호사": "suit", "바리스타": "apron"}
                for k, v in jmap.items():
                    if k in job:
                        hits = [v] + hits
                        break
            if hits:
                _put(d, path, ", ".join(hits[:3]))
                filled.append(path)

    for path, keys in _TRIGGER_BAGS.items():
        _pick(path, lambda t, keys=keys: any(k in t for k in keys))
    if "protagonist.face_style" in miss and _is_empty(_dig(d, "protagonist.face_style")):
        _put(d, "protagonist.face_style", "neutral expression")     # '지금'을 모르면 중립(아헤가오 금지)
        filled.append("protagonist.face_style")
    if "protagonist.sex" in miss and _is_empty(_dig(d, "protagonist.sex")):
        _put(d, "protagonist.sex", "female")                        # 파서 규칙과 같은 기본값
        filled.append("protagonist.sex")
    if "rating" in miss and _is_empty(_dig(d, "rating")):
        _put(d, "rating", "safe")                                   # 기본 수위
        filled.append("rating")
    if filled:
        clog("캐릭터 설정(공식 태그·직업)으로 추론해 메운 항목: " + ", ".join(filled)
             + " — 본문 근거가 아니므로 확인이 필요합니다")
    return d, filled


def _extract_once(episode_text: str, sheet_text: str, ep_num: int, log_fn=None,
                  need_segments: bool = True, attempts: int = 2) -> dict:
    """본문 1창 → LLM → 정규화 dict. **JSON 파싱 실패/예외는 재시도**(2회는 temp 0.0으로).

    Q4 디코딩 사고(키 앞 이상 문자)는 재시도로 피하는 것이 가장 싸다 — 관대한 파서(`json_soft_fix`)를
    통과하지 못한 응답만 다시 묻는다.
    """
    prompt = build_extract_prompt(episode_text, sheet_text, ep_num, need_segments=need_segments)
    last, raw = "", ""
    for k in range(1, max(1, int(attempts)) + 1):
        try:
            raw, _ = call_openai_for_text(prompt, messages=None, log_fn=log_fn or clog,
                                          temperature=0.2 if k == 1 else 0.0, repeat_penalty=1.05)
        except Exception as e:
            last = f"API 예외: {e}"
            clog(f"추출 {k}회 실패: {e}" + (" → 재시도" if k < attempts else ""))
            continue
        data, _perr = extract_json_obj_checked(raw or "")
        if isinstance(data, dict) and data:
            if k > 1:
                clog(f"추출 {k}회 시도에서 성공")
            return _normalize_extract(data)
        last = _perr
        if k < int(attempts):
            clog(f"추출 JSON 파싱 실패({k}회): {_perr[:110]} → 재시도합니다")
    clog(f"추출 실패({max(1, int(attempts))}회 시도): {last[:150]} (응답 앞 120자): {str(raw)[:120]}")
    return {}


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
                # .get 체인 — 이 로그 한 줄이 KeyError로 창 결과를 통째로 버리면(실측 사고 유형) 손해다
                _g = ((d.get("guides") or {}).get("protagonist") or [])
                _u = d.get("units") or []
                clog(f"추출 창 {wi}/{len(windows)} ({len(w)}자): "
                     f"본문 항목 {len(_u)}개(만들 컷 {sum(int(u.get('cuts', 1) or 1) for u in _u)}) · "
                     f"가이드 {len(_g)}줄 · 행동 {d.get('actions')} · rating={d.get('rating') or '(자동)'}")
    if not parts:
        return {}
    data = parts[0] if len(parts) == 1 else _merge_extracts(parts)
    data["windows"] = len(windows)
    if len(parts) < len(windows):
        # [2026-09-10] 실패한 창.units는 배분 저울에서 **조용히 빠진다** — 컷이 그 장면을 놓친다
        clog(f"⚠ 추출 창 {len(windows)}개 중 {len(windows) - len(parts)}개가 비었습니다"
             f" — 그 창 사건은 컷 배분에서 빠집니다(본문을 조금 더 잘게 나눠 주세요)")
        data["windows_missing"] = len(windows) - len(parts)
    proto = data.get("protagonist") or {}
    missing = [k for k in ("name", "hair_color", "clothes", "body_shape") if not proto.get(k)]
    if missing:
        clog(f"⚠ 추출 결과 필수 태그 누락: {missing} (렌더 필수 검증 실패 가능)")
    _g, _u = ((data.get("guides") or {}).get("protagonist") or []), (data.get("units") or [])
    clog(f"추출 완료({len(windows)}창 병합): {proto.get('name')}/{proto.get('sex')} · "
         f"상대방 {(data.get('partner') or {}).get('name')} · "
         f"본문 항목 {len(_u)}개(만들 컷 {sum(int(u.get('cuts', 1) or 1) for u in _u)}) · "
         f"가이드 {len(_g)}줄 · 행동 {data.get('actions')} · rating={data.get('rating') or '(자동)'}")
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
    # [2026-09-09] 이름 고정(config.pin_name / local_settings name / COMIC_PIN_NAME / --name) —
    #   시트에 '#Kirisaki Chitoge from Nisekoi#' 같은 렌더 참조 태그가 있으면 추출 LLM이 그 캐릭터명을
    #   주인공 이름으로 주워온다(실측: 'AMD 소녀' → '치토게'). 태그는 그림 참조, 이름은 별도다.
    _pn = str(getattr(config, "pin_name", "") or "").strip()
    _pn2 = str(getattr(config, "pin_name2", "") or "").strip()
    config.name = _pn or proto.get("name") or "주인공"
    config.sex = proto.get("sex") or "female"
    config.hair_color = proto.get("hair_color") or "black hair"
    config.hair_style = proto.get("hair_style") or "long hair"
    config.eye_color = proto.get("eye_color") or "brown eyes"
    config.skin_color = proto.get("skin_color") or "fair skin"
    # [2026-09-09] 회차 시작 상태와 후반 상태를 가른다(실측: face_style "crying, blushing, ahegao",
    #   clothes "school uniform, gold bra, gold miniskirt"가 그대로 회차 기준이 돼 컷 1부터
    #   아헤가오·빔보 복장이 çıktı). 첫 항목 = 회차가 시작하는 상태, 나머지는 후반 전용.
    _fs, _fs_late = _split_start_late(proto.get("face_style"), "blushing")
    _cl, _cl_late = _split_start_late(proto.get("clothes"), "school uniform")
    config.face_style, config.face_style_late = _fs, _fs_late
    config.clothes, config.clothes_late = _cl, _cl_late
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

    config.name2 = _pn2 or part.get("name") or "상대"
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
    # [2026-09-09] 사건(액션) 단위 + 컷 수 — 컷 배분의 저울을 '글자 수'에서 '사건'으로 옮긴다 (comic_gen)
    config.ep_action_units = {**(getattr(config, "ep_action_units", {}) or {}),
                              ep_num: normalize_units((data or {}).get("units"))}

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
    _loss = (100.0 * (1 - len(body) / len(ep_raw))) if len(ep_raw) else 0.0
    clog(f"입력[progress]: {os.path.basename(episode_path)} → 본문 {len(body)}자"
         f"(원문 {len(ep_raw)}자, 어댑터 정리 {-_loss:.0f}%) / 시트 {len(sheet)}자 / 막 앵커 {len(info.get('segments') or [])}개"
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
