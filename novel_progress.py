#!/usr/bin/env python3
"""novel_progress.py — [local 전용] llm_shortnovel_generator_gui/progress/ 포맷 어댑터

이 repo의 계약은 오직 두 평문입니다: `에피소드 본문 평문 + 캐릭터 시트 평문`(comic_input.prepare_texts).
단편 생성기 GUI가 `progress/`에 떨어뜨리는 산출물은 그보다 구조화되어 있습니다.

  ep03_2218f2f3797744fe.txt              character_sheet_ep03_2218f2f3797744fe.json
    === Episode 3 ===                      {"protagonist": {"hair_color": "light brown hair", …},
    # 주인공 (오카다 유즈키) / 직업…          "partner": {"appearance": "회색 중간 머리", …}}
    --- 에피소드 내용 ---
    ##EPISODE 3:                           ← 구간 헤더(지문 아님)
    [LOCATION]: …  [SITUATION]: …          ← 장면 카드 (2026-09-11 신형: 키마다 한 줄)
    [TIME]: …  [CLOTHES]: …
    #####
    기:
    [ACTION] …  [TALK] …  [INNER] …
    승: … 전: … 결: …
    --- 주인공 캐릭터 시트 --- (꼬리에 시트가 한 번 더 있다)

  장면 카드는 **회차 시작뿐 아니라 기승전결 중간에도** 올 수 있습니다(막 라벨 직전 = 그 막의 장면).
  구형은 한 줄에 `[장소: … / 상황: … / 시간: … / …의 복장: …]`로 붙어 있었고, 두 종을 함께 받습니다.

어댑터가 하는 일 (코어 파이프라인은 아무것도 모른다)
  1) 본문 평문화 : 머리/꼬리 블록 제거, `#####` 구분선·`##EPISODE N:` 제거, `[ACTION]`→서술,
     `[TALK]`→`이름: 대사`, `[INNER]`→`(속마음) …`. 장면 카드는 **지문이 아니라 상태 줄**로
     잡은 자리에 그대로 놓는다(`장소: …` / `시간: …` / `…의 복장: …`).
  2) 기승전결 앵커 결정론 확보 : 막 첫 행을 **본문 원문 사본**으로 그대로 돌려준다.
     (지금까지 4줄 앵커는 LLM이 一字不사본으로 베껴와야 했고, 한 글자만 어긋나도
      기승전결 분할이 글자수 균등 분할로 후퇴했다 — comic_gen.py의 `ep_beat_segments` 경로)
  3) 시트 JSON → 평문 시트 : LLM에게 보낼 지문. 키 이름이 회차마다 달라서(ep01은 한글키,
     ep02~는 영문키) 별칭 테이블로 흡수하고, 모르는 키는 버리지 않고 `기타`로 남긴다.
  4) 시트 JSON → config 우선값 : 이름/성별/머리/눈/피부/표정/몸매 태그는 **원작이 정답**이라
     LLM 추정보다 우선한다. 10화 동안 캐릭터가 갈라지지 않는 실익이 있다.
     단 `clothes`(한글 산문)는 넣지 않는다 — Anima는 영문 태그만 알아, 번역은 LLM 몫으로 남긴다.

여기서 만들지 못하는 것(=LLM에 남기는 일): guides 요약, $행동 키워드, 수위, 한글 복장·장소→영문 태그.
장면 카드의 한글 지문을 영문 태그로 옮기는 일은 추출/컷 스크립트 LLM이 한다(`config.ep_scene_cards`).

실행: run_comic.py --special --episode <ep파일 또는 progress/ 디렉터리> [--sheet 시트.json]
"""
import glob
import json
import os
import re

import config

# ------------------------------------------------------------------ 파일명/본문 문법
EP_FILE_RE = re.compile(r"^ep(\d+)_([0-9a-f]{6,})\.txt$", re.I)
SHEET_FILE_RE = re.compile(r"^character_sheet_ep(\d+)_([0-9a-f]{6,})\.json$", re.I)

BODY_MARK = "--- 에피소드 내용 ---"
TAIL_SHEET_MARK = "--- 주인공 캐릭터 시트 ---"
ACTS = ("기", "승", "전", "결")
ACT_RE = re.compile(r"^\s*(기|승|전|결)\s*[:：]\s*$")
LINE_RE = re.compile(r"^\s*\[(ACTION|TALK|INNER)\]\s*[:：]?\s*(.*)$", re.S)
DIVIDER_RE = re.compile(r"^\s*(?:#{1,12}|[-=*_]{3,}|={3,})\s*$")     # '#####' · '===' 구분선
META_HEAD_RE = re.compile(r"^\s*[（(\[]?\s*장소\s*[:：]", re.S)
EP_HEAD_RE = re.compile(r"^\s*#+\s*EPISODE\b", re.I)                 # '##EPISODE 1:' — 지문이 아니라 구간 헤더
# 장면 카드: [LOCATION]/[SITUATION]/[TIME]/[CLOTHES] (2026-09-11 신형) = 장소/상황/시간/복장 (구형)
#   신형은 키마다 한 줄, 구형은 한 줄에 '장소: … / 상황: …'로 붙어 있다. 둘 다 받는다.
_CARD_KEY_RE = re.compile(
    r"^[\[\(（]?\s*(?P<k>시간의 흐름|시간|상황|장소|LOCATION|SITUATION|TIME|CLOTHES|복장"
    r"|(?P<owner>.{1,20}?)의\s*복장)\s*[\]\)）]?\s*[:：]\s*(?P<v>.*)$", re.S | re.I)
CARD_KEYS = {"location": "장소", "situation": "상황", "time": "시간", "clothes": "복장",
             "장소": "장소", "상황": "상황", "시간": "시간", "복장": "복장"}
ANCHOR_MAX = 120            # split_by_segments는 접두사 매칭이라 잘라도 붙는다( comic_input 주석)

# ------------------------------------------------------------------ 시트 키 별칭 (회차마다 다른 두 종을 함께 받는다)
ALIAS = {
    "name":        ("이름", "주인공 이름", "name"),
    "age":         ("나이", "주인공 나이", "age"),
    "sex":         ("성별", "주인공 성별", "sex"),
    "personality": ("성격/말투", "성격 (내면)", "성격", "personality_real", "personality"),
    "job":         ("직업", "주인공 직업", "job"),
    "job_attr":    ("직업에 대한 평가", "직업 특성", "job_attribute"),
    "objective":   ("인생 목표", "목표", "objective"),
    "happiness":   ("행복도", "happiness"),
    "hair_color":  ("머리색", "hair_color"),
    "hair_style":  ("헤어스타일", "hair_style"),
    "eye_color":   ("눈 색깔", "눈 색", "eye_color"),
    "skin_color":  ("피부 색깔", "피부색", "skin_color"),
    "face_style":  ("얼굴 스타일", "face_style"),          # "얼굴 스타일 1/2"는 접두로 붙여 합친다
    "acc":         ("액서서리", "액세서리", "acc"),
    "breasts":     ("가슴크기", "가슴 크기", "breasts_size"),
    "hips":        ("엉덩이 크기", "hip_size"),
    "body":        ("몸매", "body_size"),
    "clothes":     ("복장", "clothes"),
    "state":       ("성격 (요약)", "personality_text", "성경험", "기타 특징"),
    "love":        ("애정도", "love_value"),
}
# 집계 항목 이름(로컬 단편 생성기 산출물의 키) — 공개 repo에는 중립 항목만 기본으로 싣는다.
# 나머지 항목 이름은 local_settings.yaml의 `counter_alias`에 둔다(.gitignore 대상).
#   예) counter_alias: { "표시이름": ["산출물 키 1", "산출물 키 2"], … }
# 파일이 없으면(공개 클론) 그 항목은 집계 없이 넘어간다(가이드에서 그 줄만 빠진다).
COUNTER_ALIAS = {
    "성관계": ("성관계 횟수", "sex_count"),
    "패팅": ("패팅 횟수", "patting_count"),
    "포즈": ("포즈 성관계", "pose_sex_count"),
}


def _counter_alias() -> dict:
    """기본 집계 항목 + local_settings.yaml의 counter_alias(로컬 전용 이름)."""
    out = {k: tuple(v) for k, v in COUNTER_ALIAS.items()}
    loc = getattr(config, "counter_alias", {}) or {}
    if isinstance(loc, dict):
        for k, v in loc.items():
            k = str(k).strip()
            if not k:
                continue
            out[k] = tuple(str(x).strip() for x in v if str(x).strip()) \
                if isinstance(v, (list, tuple)) else (str(v).strip(),)
    return out
PARTNER_PREFIX_RE = re.compile(r"^\s*(?:상대방|상대|파트너)\s*[:：]?\s*")
PARTNER_ALIAS = {"이름": ("이름", "name"), "나이": ("나이", "age"), "성별": ("성별", "sex"),
                 "직업": ("직업", "job"), "외모": ("외모", "appearance"),
                 "성격": ("성격", "personality"), "말투": ("말투", "personality"),
                 "복장": ("복장", "clothes")}
_KO_RE = re.compile(r"[\u3131-\u318F\uAC00-\uD7A3]")
_TAG_DROP_RE = re.compile(r"[（(][^（()）]*[\u3131-\u318F\uAC00-\uD7A3][^（()）]*[）)]")   # 태그 뒤 한글 주석

MAX_SHEET_CHARS = 3600            # comic_input.SHEET_TEXT_CAP(4000) 안에 들어가야 지문이 안 잘린다


def _read(path: str) -> str:
    if not path or not os.path.exists(path):
        return ""
    for enc in ("utf-8-sig", "utf-8", "cp949"):
        try:
            with open(path, "r", encoding=enc) as f:
                return f.read().strip()
        except UnicodeDecodeError:
            continue
    with open(path, "rb") as f:
        return f.read().decode("utf-8", "replace").strip()


def _tag(v) -> str:
    """외모 태그 정규화: 한글 괄호 주석 제거, 공백/쉼표 정리 (anima는 소문자 영문 태그만 안다)"""
    if v is None or isinstance(v, (int, float, bool)):
        return ""
    s = _TAG_DROP_RE.sub(" ", str(v))
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\s*,\s*", ", ", s)
    return s.strip(" ,.;.")


def _flat(v) -> str:
    """dict/list 값도 평문 한 줄로 (원작 필드를 유실시키지 않는다)"""
    if isinstance(v, dict):
        return ", ".join(f"{k}={_flat(x)}" for k, x in v.items())
    if isinstance(v, (list, tuple)):
        return ", ".join(_flat(x) for x in v)
    return re.sub(r"\s+", " ", str(v or "")).strip().strip(" ,.")


# ------------------------------------------------------------------ 디스커버리
def discover(progress_dir: str, plot_hash: str = "") -> list:
    """progress/ 디렉터리 → [{"ep","episode","sheet","plot_hash"}, ...] (회차 오름차순)

    회차 수는 **ep*.txt 기준**입니다. 실측: progress json의 total_episodes=10, ep*.txt 10개,
    character_sheet_ep11.json은 11개 — 시트가 더 많이 남아도 본문 없는 회차는 만들지 않습니다.
    """
    if not progress_dir or not os.path.isdir(progress_dir):
        return []
    out = {}
    for p in sorted(glob.glob(os.path.join(progress_dir, "ep*_*.txt"))):
        m = EP_FILE_RE.match(os.path.basename(p))
        if not m:
            continue
        ep, h = int(m.group(1)), m.group(2)
        if plot_hash and h != plot_hash:
            continue
        out[ep] = {"ep": ep, "episode": p, "sheet": "", "plot_hash": h}
    for p in sorted(glob.glob(os.path.join(progress_dir, "character_sheet_ep*_*.json"))):
        m = SHEET_FILE_RE.match(os.path.basename(p))
        if not m:
            continue
        ep, h = int(m.group(1)), m.group(2)
        if ep in out and (not plot_hash or h == plot_hash):
            out[ep]["sheet"] = p
    if not plot_hash and out:                       # 해시가 없으면 최다 회차 해시로 좁힌다
        hashes = {}
        for v in out.values():
            hashes[v["plot_hash"]] = hashes.get(v["plot_hash"], 0) + 1
        best = max(sorted(hashes), key=lambda k: hashes[k])
        out = {k: v for k, v in out.items() if v["plot_hash"] == best}
    return [out[k] for k in sorted(out)]


def sniff(text: str) -> bool:
    """progress/ 포맷인지 내용으로 판정한다 (확장자·플래그 없이도 안전망으로 동작)"""
    t = str(text or "")
    if not t.strip():
        return False
    if BODY_MARK in t or t.lstrip().startswith("=== Episode"):
        return True
    acts = len(re.findall(r"^\s*(?:기|승|전|결)\s*[:：]\s*$", t, re.M))
    return acts >= 2 and t.count("[ACTION]") >= 3


# ------------------------------------------------------------------ 본문 파서
def _parse_card_line(line: str) -> dict:
    """장면 카드 한 줄 → {장소,상황,시간,복장,(복장 주인)} — 카드가 아니면 {}

    2026-09-11 신형: `[LOCATION]: …` 처럼 **키마다 한 줄** (값에 '/'가 들어가도 쪼개지 않는다).
    그 앞 구형: `[장소: … / 상황: … / 아야의 복장: …]` 처럼 **한 줄에 여러 필드**.
    키는 한/영 모두 받는다 (LOCATION·SITUATION·TIME·CLOTHES / 장소·상황·시간·복장).
    """
    s = str(line or "").strip()
    if not s:
        return {}
    cand = [p.strip() for p in re.split(r"\s*/\s*", s) if p.strip()]
    pieces = cand if len(cand) > 1 and all(_CARD_KEY_RE.match(p) for p in cand) else [s]

    def _put(out, m):
        k = str(m.group("k") or "").strip()
        # 값 꼬리에 남은 닫는 괄호(구형은 한 줄 전체가 [ … ] 로 감싸 있다)와 쉼표만 버린다.
        #   '심야 (밤)' 처럼 짝이 맞는 괄호는 내용이다 — 열린 괄호 수보다 닫힌 것이 많을 때만 지운다.
        v = re.sub(r"\s+", " ", str(m.group("v") or "")).strip().strip(" ,;")
        _open_of = {")": "(", "）": "（", "]": "[", "］": "［", "〕": "［", "»": "«"}
        while v and v[-1] in _open_of and v.count(v[-1]) > v.count(_open_of[v[-1]]):
            v = v[:-1].rstrip().rstrip(" ,;")
        ku = k.upper()
        if ku == "LOCATION" or k == "장소":
            key = "장소"
        elif ku == "SITUATION" or k == "상황":
            key = "상황"
        elif ku == "TIME" or k.startswith("시간"):
            key = "시간"
        else:                                            # "복장" / "오카다 유즈키의 복장" / CLOTHES
            key = "복장"
            own = re.sub(r"\s+", " ", str(m.group("owner") or "")).strip()
            if own and not out.get("복장 주인"):
                out["복장 주인"] = own
        if v:
            out[key] = (out.get(key, "") + " " + v).strip() if out.get(key) else v

    out = {}
    m0 = _CARD_KEY_RE.match(pieces[0])
    if not m0:
        return {}                                        # 첫 조각이 키가 아니면 카드가 아니다
    _put(out, m0)
    misc = []
    for p in pieces[1:]:
        m = _CARD_KEY_RE.match(p)
        if m:
            _put(out, m)
        else:
            misc.append(p)
    if misc:
        out["비고"] = " / ".join(misc)
    return out


def _parse_meta(line: str) -> dict:
    """[장소: … / 상황: … / …] → {키: 값} — 구형 한 줄 카드를 위한 호환 엔트리"""
    return _parse_card_line(line)


def parse_episode(path: str) -> dict:
    """epNN_hash.txt → {"ep","plot_hash","names","meta","cards","acts","tail_sheet","head"}

    `cards`는 **장면 카드**(장소·상황·시간·복장) 목록이고 잡은 자리를 함께 갖는다:
      {"장소":…, "상황":…, "시간":…, "복장":…, "act": "승"|"", "idx": 0}
    회차 시작(막 라벨 앞) 카드는 `act=""`, 막 중간 카드는 그 막의 `idx`(몇 번재 항목 앞).
    """
    raw = _read(path)
    m = EP_FILE_RE.match(os.path.basename(path or ""))
    head, rest = (raw.split(BODY_MARK, 1) + [raw])[:2] if BODY_MARK in raw else (raw, "")
    body, tail = (rest.split(TAIL_SHEET_MARK, 1) + [""])[:2] if rest else ("", "")

    names, cur = {}, ""
    for ln in head.splitlines():
        s = ln.strip()
        nm = re.match(r"^#+\s*(주인공|protagonist|상대방|상대|partner)\s*[(（]([^)）]*)[)）]", s, re.I)
        if nm:
            cur = "partner" if nm.group(1).lower().startswith(("상대", "partner")) else "protagonist"
            names[cur] = nm.group(2).strip()
            continue
        kv = re.match(r"^(직업|나이|성별)\s*[:：]\s*(.+)$", s)
        if kv and cur:
            names[f"{cur}_{kv.group(1)}"] = kv.group(2).strip()

    lines = [x.rstrip() for x in body.splitlines()]
    meta, acts, act, stray = {}, {}, "", []
    cards, pending, seen_acts = [], None, []
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not s or DIVIDER_RE.match(s) or EP_HEAD_RE.match(s):
            continue                                    # '#####' · '=== Episode' · '##EPISODE 1:'
        am = ACT_RE.match(s)
        if am:
            act = am.group(1)
            acts.setdefault(act, [])
            if pending:
                # 회머리 카드(첫 막 라벨 앞)는 act="" — 본문 맨 앞에 놓고 막 앵커는 실제 사건 줄에 둔다.
                # 그 뒤 막 라벨 직전의 카드는 **그 막의 장면 전환**(act에 붙인다).
                cards.append(dict(pending, act="" if not seen_acts else act, idx=0))
                pending = None
            seen_acts.append(act)
            continue
        lm = LINE_RE.match(s)
        if lm:
            tag, txt = lm.group(1).upper(), re.sub(r"\s+", " ", lm.group(2)).strip()
            if txt:
                _cur = act or "기"
                # 막 라벨 **뒤**에 온 카드도 있다(실 입력은 라벨 앞/뒤 둘 다 쓴다) — 다음 항목이
                # 시작하기 전에 반드시 흘려야 그 항목 앞에 카드가 놓인다. 안 그러면 카드가 다음 막으로 밀린다.
                if pending:
                    cards.append(dict(pending, act="" if not seen_acts else _cur,
                                      idx=len(acts.get(_cur, []))))
                    pending = None
                acts.setdefault(_cur, []).append((tag, txt))
            continue
        card = _parse_card_line(s)                      # [LOCATION]/[TIME]/[CLOTHES] … (키별 한 줄)
        if card:
            if pending is None:
                pending = dict(card)
            else:                                       # 키마다 한 줄씩 → 한 장으로 합친다
                for k, v in card.items():
                    pending[k] = (pending.get(k, "") + " " + v).strip() if pending.get(k) else v
            continue
        stray.append(s)                                   # 라벨 없는 자유 서술 → 뒤에서 첫 막에 붙인다
    if pending:                                           # 회차 끝에 카드만 남은 경우
        cards.append(dict(pending, act="" if not seen_acts else act,
                          idx=len(acts.get(act or "기", []))))
    for s in stray:
        acts.setdefault("기", []).append(("TEXT", s))
    meta = next((dict(c) for c in cards if not c.get("act")), {})

    return {"ep": int(m.group(1)) if m else 1, "plot_hash": m.group(2) if m else "",
            "names": names, "meta": meta, "cards": cards, "acts": acts,
            "tail_sheet": tail.strip(), "head": head.strip()}


# ------------------------------------------------------------------ 평문화
def _tokens(name: str) -> list:
    """'오카다 유즈키' → ['오카다 유즈키','유즈키','오카다'] (호명 검출용)"""
    n = re.sub(r"\s+", " ", str(name or "")).strip()
    return [n] + [t for t in n.split() if len(t) >= 2]


def _tokens_all(name: str) -> list:
    """1음절 이름도 남긴다 ('카즈키 렌' → ['카즈키 렌','카즈키','렌']) — **조사 검출용**.

    호명/언급 검출은 2음절 이상만 쓴다(1음절은 '렌즈' 같은 단어 일부에 빨려 들어간다).
    반면 조사와 붙는 검출(`렌이`/`렌가`)은 단어가 끝나야 걸리므로 1음절도 안전하다 —
    이걸 빼면 '렌'이 아예 안 잡혀 화자가 뒤바뀐다(2026-09-11 실측).
    """
    n = re.sub(r"\s+", " ", str(name or "")).strip()
    return [n] + [t for t in n.split() if t]


class _Speakers:
    """[TALK]에는 화자 표기가 없습니다 — **호명 > 직전 서술의 주어 > 교대 순서**로 추정합니다.
    (컷 스크립트는 `주인공:`/`상대방:` 2줄을 강제하지만, 본문에 이름이 없으면 화자가 뒤바뀝니다)"""

    def __init__(self, names: dict):
        self.disp = {"protagonist": names.get("protagonist") or "주인공",
                     "partner": names.get("partner") or "상대방"}
        pro, par = _tokens(names.get("protagonist", "")), _tokens(names.get("partner", ""))
        pro_s, par_s = _tokens_all(names.get("protagonist", "")), _tokens_all(names.get("partner", ""))
        self.pro = [t for t in pro if t and t not in par]
        self.par = [t for t in par if t and t not in pro]
        # 조사(이/가/께서)와 붙어 쓸 토큰은 **1음절도 넣는다** ('렌이'를 놓치면 화자가 뒤바뀐다).
        #   단 '렌즈'처럼 단어 일부로 빨려 들어가는 호명 검출(plain substring)은 2음절 이상만 쓴다.
        self.pro_s = [t for t in pro_s if t and t not in par_s]
        self.par_s = [t for t in par_s if t and t not in pro_s]
        self.last = ""                      # 화자 미확정 상태 (회차 첫 발화는 주인공으로 본다)
        self.actor = ""                     # 직전 서술([ACTION]/[INNER])의 **주어** — 발화 단서

    def _mentioned(self, text: str) -> str:
        hit_pro = any(t in text for t in self.pro)
        hit_par = any(t in text for t in self.par)
        if hit_pro and not hit_par:
            return "protagonist"
        if hit_par and not hit_pro:
            return "partner"
        return ""

    def observe(self, text: str) -> None:
        """[ACTION]/[INNER] 줄의 **주어**를 적어 둔다 — 화자 단서가 없는 [TALK]의 3순위 근거.

        이름을 '불른 것'(목적어·관형어)과 '움직인 것'(주어)은 조사로 구분한다. 실측
        (ep01_c09a…): `그 뒤를 따라 들어온 카즈키 렌이 … 소이치로의 가냘픈 뒷모습을 관찰한다.`
        에서 화자는 렌(주어)인데, 이름 언급만 보면 둘 다 걸려 단서가 사라진다.
        """
        subj = set()
        for who, toks in (("protagonist", self.pro_s), ("partner", self.par_s)):
            for t in toks:
                if re.search(re.escape(t) + r"\s*(?:이|가|께서)(?!\w)", text):
                    subj.add(who)
        if len(subj) == 1:
            self.actor = next(iter(subj))
            return
        if not subj:
            who = self._mentioned(text)          # 조사 단서가 없으면 홀로 언급된 인물을 따른다
            if who:
                self.actor = who

    def who(self, text: str) -> str:
        """화자 추정: ① 발화 안의 호명(그 인물을 불렀다 → 상대 발화) → ② 직전 서술의 주어 → ③ 교대 순서.

        2026-09-11 실측: `[ACTION] 카즈키 렌이 … 관찰한다.` 뒤의 발화가 "후훗, 저 고결해 보이는
        선도부장님이…" — '선도부장님'은 사전에 없어 ③만 타면 회차 첫 발화가 주인공에 박혀
        **두 발화가 통째로 뒤바뀐다**(렌 대사가 주인공 말풍선이 된다).
        """
        hit_pro = any(t in text for t in self.pro)          # 주인공을 불렀다 → 상대방 발화
        hit_par = any(t in text for t in self.par)
        if hit_pro and not hit_par:
            spk = "partner"
        elif hit_par and not hit_pro:
            spk = "protagonist"
        elif self.actor:
            spk = self.actor                                # ② 화면에서 움직인 인물
        elif not self.last:
            spk = "protagonist"                             # ③ 화자 단서 없음 & 회차 첫 발화 → 주인공
        else:
            spk = "partner" if self.last == "protagonist" else "protagonist"
        self.last = spk
        return spk


def _card_lines(card: dict, sp: "_Speakers") -> list:
    """장면 카드 → 본문에 들어갈 상태 줄 (한글 원문 그대로 — 영문 태그로 옮기는 일은 LLM 몫)"""
    out = []
    for k in ("장소", "상황", "시간"):
        if card.get(k):
            out.append(f"{k}: {card[k]}")
    if card.get("복장"):
        owner = card.get("복장 주인") or sp.disp["protagonist"]
        out.append(f"{owner}의 복장: {card['복장']}")
    if card.get("비고"):
        out.append(f"비고: {card['비고']}")
    return out


def render(parsed: dict) -> tuple:
    """본문 평문화 → (text, segments)

    segments는 **이 함수가 만든 본문 원문 사본**이라 split_by_segments가 반드시 찾습니다.
    장면 카드는 잡은 자리에 그대로 놓는다 — 회차 시작 카드는 맨 앞, 막 카드(막 라벨 직전)는
    라벨 바로 아래. 막 앵커는 라벨 다음에 오는 **첫 줄**(카드일 수도 있다)을 취하므로,
    카드를 앞에 둬도 막 분할이 어긋나지 않는다.
    """
    sp = _Speakers(parsed.get("names") or {})
    lines, segments = [], []
    pre, per_act = [], {}
    for c in parsed.get("cards") or []:
        (per_act.setdefault(str(c.get("act") or ""), []).append(c)
         if c.get("act") else pre.append(c))
    for c in pre:
        lines += _card_lines(c, sp)
    for act in ACTS:
        items = (parsed.get("acts") or {}).get(act) or []
        by_idx = {}
        for c in per_act.get(act) or []:
            try:
                at = int(c.get("idx", 0) or 0)
            except Exception:
                at = 0
            by_idx.setdefault(max(0, min(at, len(items))), []).append(c)
        block = []
        for j, (tag, txt) in enumerate(items):
            for c in by_idx.get(j, []):               # 항목 앞에 낀 카드 = 이 지점의 장면 전환
                block += _card_lines(c, sp)
            if tag == "TALK":
                out = f"{sp.disp[sp.who(txt)]}: {txt}"
            elif tag == "INNER":
                sp.observe(txt)                             # 속마음의 주어는 다음 발화의 단서가 된다
                out = f"(속마음) {txt}"
            else:
                sp.observe(txt)                             # 서술의 주어(누가 움직이나)를 기억한다
                out = txt
            block.append(out)
        for c in by_idx.get(len(items), []):          # 막 끝에 온 카드
            block += _card_lines(c, sp)
        if not block:
            continue
        if lines:
            lines.append("")
        lines.append(f"{act}:")
        lines += block
        # 앵커에 막 라벨 줄을 함께 넣는다 — split_by_segments의 경계가 라벨 줄 맨 앞에서 잡히므로
        # "승:"가 앞 막 꼬리에 남지 않는다(공백 무관 매칭이라 개행이 들어가도 붙는다).
        first = next((b for b in block if b.strip()), "")
        segments.append(f"{act}:\n{first}"[:ANCHOR_MAX])
    body = "\n".join(lines).strip()
    return body, segments[:4] if len(segments) >= 2 else []


# ------------------------------------------------------------------ 시트 JSON
def _norm_key(k) -> str:
    """시트 키 정규화: 공백 제거 + '상대방 ' 접두 제거 — 두 종의 명명(ep01/ep02+)을 한 갈래로 받는다"""
    return re.sub(r"\s+", "", PARTNER_PREFIX_RE.sub("", str(k)))


def _key_hits(src: dict, aliases):
    """별칭에 걸린 (정규화 키, 값). **정확일치를 먼저**, 그다지 앞접두로 확대한다.

    정확일치를 먼저 두는 이유: 별칭 'sex'로 'sex_count'를 앞접두로 잡으면 집계 숫자가
    성별 자리에 들어가버린다(ep02+ 시트에는 sex_count가 있다). 앞접두는 '얼굴 스타일 2'처럼
    번호가 갈라진 태그를 위해 필요하다.
    """
    pats = [re.sub(r"\s+", "", a) for a in (aliases or ())]
    keys = [(_norm_key(k), v) for k, v in (src or {}).items()]
    for exact in (True, False):
        for kk, v in keys:
            if any((kk == a) if exact else (kk != a and kk.startswith(a)) for a in pats):
                yield kk, v
        if exact and any(kk == a for kk, _ in keys for a in pats):
            return                                # 정확일치가 있으면 앞접두는 다른 필드의 것이다


def _sheet_get(src: dict, field: str) -> str:
    """별칭으로 값 찾기 — 여러 키가 걸리면 **먼저 발견된 것**을 택한다('성격' vs '성격 (요약)' 등)"""
    for _, v in _key_hits(src, ALIAS.get(field, ())):
        s = _flat(v)
        if s:
            return s
    return ""


def _sheet_join(src: dict, field: str) -> str:
    """별칭에 걸린 값을 **모두** 합친다 — ep01의 '얼굴 스타일 1' + '얼굴 스타일 2'처럼 갈라진 태그 복구"""
    got = []
    for _, v in _key_hits(src, ALIAS.get(field, ())):
        for piece in re.split(r",\s*", _tag(_flat(v))):
            if piece and piece not in got:
                got.append(piece)
    return ", ".join(got)


def _sheet_join_pa(src: dict, aliases) -> str:
    """파트너 필드: 별칭에 걸린 값을 모두 합친다(ep01은 외모/머리/안경/눈/피부로 나뉘어 있다)"""
    got = []
    for _, v in _key_hits(src, aliases):
        s = _flat(v)
        if s and s not in got:
            got.append(s)
    return " / ".join(got)


def _sheet_all(src: dict, fields_alias: dict) -> dict:
    """별칭에 걸리지 않는 키도 유실시키지 않는다 → {"_기타": ["키: 값", ...]}"""
    used = set()
    for fs in (fields_alias or {}).values():
        for al in fs:
            used.add(re.sub(r"\s+", "", al))
    counters = {re.sub(r"\s+", "", c) for cs in _counter_alias().values() for c in cs}
    extra = []
    for k, v in (src or {}).items():
        kk = _norm_key(k)
        if kk in used or any(kk.startswith(a) for a in used) or kk in counters:
            continue
        s = _flat(v)
        if s:
            extra.append(f"{str(k).strip()}: {s}")
    return {"_기타": extra}


def _counters(src: dict) -> str:
    """행동 횟수 집계 — 타락 진행도라 가이드 품질에 쓰인다 (항목 이름은 위 별칭 + 로컬 설정)"""
    got = []
    for label, als in _counter_alias().items():
        for _, v in _key_hits(src, als):
            try:
                got.append(f"{label} {int(float(v))}")
            except Exception:
                got.append(f"{label} {_flat(v)}")
            break
    return " / ".join(got)


def sheet_to_text(sheet: dict, tail_sheet: str = "") -> str:
    """시트 JSON → LLM용 평문 시트. 두 종의 키 명명(ep01 한글 / ep02+ 영문)을 함께 받습니다."""
    if not isinstance(sheet, dict):
        return (tail_sheet or "").strip()
    pr = sheet.get("protagonist") or sheet.get("주인공") or {}
    pa = sheet.get("partner") or sheet.get("상대방") or {}
    out = ["[주인공]"]
    for label, field in (("이름", "name"), ("나이", "age"), ("성별", "sex"), ("직업", "job"),
                         ("직업 평가", "job_attr"), ("목표", "objective"), ("행복도", "happiness"),
                         ("애정도", "love"), ("성격", "personality"),
                         ("머리색", "hair_color"), ("헤어스타일", "hair_style"), ("눈", "eye_color"),
                         ("피부", "skin_color"), ("얼굴 표정", "face_style"), ("액세서리", "acc"),
                         ("가슴", "breasts"), ("엉덩이", "hips"), ("몸매", "body"),
                         ("복장", "clothes"), ("상태", "state")):
        v = _sheet_join(pr, field) if field == "face_style" else _sheet_get(pr, field)
        if v:
            out.append(f"{label}: {v}")
    cnt = _counters(pr)
    if cnt:
        out.append(f"집계: {cnt}")
    ex = _sheet_all(pr, ALIAS)["_기타"]
    out += [f"기타: {x}" for x in ex[:8]]
    out.append("")
    out.append("[상대방]")
    for label, als in PARTNER_ALIAS.items():
        v = _sheet_join_pa(pa, als)
        if v:
            out.append(f"{label}: {v}")
    for x in _sheet_all(pa, PARTNER_ALIAS)["_기타"][:8]:
        out.append(f"기타: {x}")
    txt = "\n".join(out).strip()
    if len(txt) > MAX_SHEET_CHARS:                 # SHEET_TEXT_CAP(4000) 안에 들어야 지문이 안 잘린다
        txt = txt[:MAX_SHEET_CHARS].rsplit("\n", 1)[0]
    return txt


def _sex(v) -> str:
    s = str(v or "").strip().lower()
    if s in ("male", "m", "남자", "남성"):
        return "male"
    if s in ("female", "f", "여자", "여성"):
        return "female"
    return ""


def sheet_overrides(sheet: dict) -> dict:
    """시트 JSON → config 주입 우선값 (LLM 추정보다 원작이 우선). 빈 필드는 넣지 않는다."""
    if not isinstance(sheet, dict):
        return {}
    pr = sheet.get("protagonist") or sheet.get("주인공") or {}
    pa = sheet.get("partner") or sheet.get("상대방") or {}
    body_bits = []
    for f in ("body", "breasts", "hips"):
        t = _tag(_sheet_get(pr, f))
        if t and not _KO_RE.search(t) and t not in body_bits:
            body_bits.append(t)
    pro = {"name": _flat(_sheet_get(pr, "name")).strip(),
           "sex": _sex(_sheet_get(pr, "sex")),
           "job": _flat(_sheet_get(pr, "job")).strip(),
           "hair_color": _tag(_sheet_get(pr, "hair_color")),
           "hair_style": _tag(_sheet_get(pr, "hair_style")),
           "eye_color": _tag(_sheet_get(pr, "eye_color")),
           "skin_color": _tag(_sheet_get(pr, "skin_color")),
           "face_style": _sheet_join(pr, "face_style"),
           "body_shape": ", ".join(body_bits)}
    # clothes는 일부러 넣지 않는다: 원작의 복장은 한글 산문("…실크 슬립 원피스")이고
    # anima는 영문 태그만 읽는다 → 번역은 build_extract_prompt가 LLM에게 맡긴다.
    part = {"name": _flat(_sheet_get(pa, "name")).strip(), "sex": _sex(_sheet_get(pa, "sex"))}
    ov = {"protagonist": {k: v for k, v in pro.items() if v},
          "partner": {k: v for k, v in part.items() if v}}
    return {k: v for k, v in ov.items() if v}


def head_overrides(parsed: dict) -> dict:
    """시트 JSON이 없을 때의 폴백: 머리 블록('# 주인공 (이름) / 직업 / 나이')"""
    n = parsed.get("names") or {}
    pro = {"name": n.get("protagonist", ""), "job": n.get("protagonist_직업", "")}
    part = {"name": n.get("partner", "")}
    ov = {"protagonist": {k: v for k, v in pro.items() if v},
          "partner": {k: v for k, v in part.items() if v}}
    return {k: v for k, v in ov.items() if v}


# ------------------------------------------------------------------ 한 번에
def load(episode_path: str, sheet_path: str = "") -> dict:
    """progress/ 파일 한 회차 → 코어가 먹을 입력 묶음

    {"episode_text","sheet_text","segments","overrides","ep_num","format","notes"}
    """
    parsed = parse_episode(episode_path)
    body, segments = render(parsed)
    notes = []
    raw_sheet = _read(sheet_path) if sheet_path else ""
    sheet = {}
    if raw_sheet:
        try:
            sheet = json.loads(raw_sheet)
        except Exception as e:
            notes.append(f"시트 JSON 파싱 실패({e}) → 본문 꼬리 시트를 사용합니다")
    overrides = sheet_overrides(sheet) or head_overrides(parsed)
    if sheet:
        sheet_text = sheet_to_text(sheet, parsed.get("tail_sheet", ""))
    elif parsed.get("tail_sheet"):
        sheet_text = parsed["tail_sheet"]
        notes.append("시트 JSON이 없어 본문 꼬리의 '--- 캐릭터 시트 ---'를 사용했습니다")
    else:
        sheet_text = ""
    # [2026-09-11] 실측 ep05: 원작 생성기가 본문 자리에 "서버 응답 실패 (…)" 한 줄만 남긴 회차가
    #   있었습니다. 이걸로 돌리면 근거 없는 컷 6개가 지어지니 런너가 회차를 건너뛸 수 있게 알립니다.
    empty_body = bool(not segments and len(body) < 300)
    if empty_body:
        notes.append(f"에피소드 본문이 {len(body)}자(기승전결 앵커 없음)로 사실상 비어 있습니다"
                     " — 원작 생성 실패로 보입니다(이 회차는 건너뜁니다)")
    elif not segments:
        notes.append("기승전결 막 라벨을 찾지 못해 컷 분할은 글자수 균등 분할로 돌아갑니다")
    cards = list(parsed.get("cards") or [])
    if cards:
        mid = [c for c in cards if c.get("act")]
        notes.append(f"장면 카드 {len(cards)}장(회차 시작 {len(cards) - len(mid)}장"
                     + (f" · 막 중간 {len(mid)}장" if mid else "")
                     + " — 장소·시간·복장 상태의 원작 지정값입니다")
    if parsed.get("ep") and not parsed.get("names"):
        notes.append("머리 블록에서 이름을 찾지 못했습니다 — 시트 JSON의 이름을 씁니다")
    return {"episode_text": body, "sheet_text": sheet_text, "segments": segments,
            "overrides": overrides, "ep_num": parsed.get("ep", 1), "meta": parsed.get("meta", {}),
            "cards": cards, "empty_body": empty_body,
            "format": "novel_progress", "notes": notes}
