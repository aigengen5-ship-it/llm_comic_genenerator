# -*- coding: utf-8 -*-
"""
주인공 시트 → 닮은 캐릭터 태그 선택기 (2026-09-16).

쓸모: 회차를 넘겨 주인공 얼굴이 흔들리는 문제. 속성 태그만으로는 "같은 인물"이 안 되고,
     our audit가 밝힌 대로 프롬프트 문장 재배열로도 고정되지 않습니다. 그래서 **학습량이
     확인된 캐릭터 태그**를 하나 빌려 씁니다 — 단, 사람이 고르지 않고 시트 속성으로 고릅니다.

  data/chara_tags.yaml ← analysis_chara/build.py 가 Danbooru 실측으로 채운 DB (네트워크 불필요)
  pick_char_lookalike(proto, skin=None) → {"tag","series","score","why"} | None

판정 원칙 두 가지:
  ① 점수는 "요청한 속성에서 그 캐릭터가 실제로 얼마나 강하게 학습됐나"의 가중 평균입니다.
     (DB 값 = 그 캐릭터 태그에 해당 속성 태그가 함께 붙은 비율. 사람의 기억이 아니라 측정치.)
  ② 임계값 아래면 **태그를 쓰지 않습니다.** 없는 얼굴을 억지로 빌리면 원작 캐릭터가 섞여
     들어옵니다 — 속성 태그만 쓰는 편이 낫습니다.
"""
import os
import random
import re

DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "chara_tags.yaml")
_DB = None
MIN_POSTS = 600          # 학습량 하한 — 이 아래면 얼굴이 아니라 그림체로 새어나온다
#   단 나이대 코드는 모수가 원래 작습니다. 성인 태그는 수만 글이어도 나이 태그를 안 붙이는
#   Danbooru 관례가 있어서, elder/child는 같은 신호로 더 적은 글을 먹습니다.
MIN_POSTS_BY_BAND = {"adult": 600, "child": 300, "elder": 150}   # 나이대 코드는 모수가 원래 작다
THRESHOLD = 0.50         # 이보다 낮으면 태그 사용 중단(속성 태그만)

# ── 후보 고름 · 태그 표기 ────────────────────────────────────────────────────
PAREN_ESCAPE = True      # 태그의 ( ) 를 \( \) 로 — anima는 괄호를 가중치로 읽는다
PICK_TOPK = 1            # 최고점과 근소차 후보 중 몇 개를 풀로 보나 (1 = 오직 최고점)
PICK_MARGIN = 0.05       # 이 안에서만 '같은 급'으로 본다 (유사도는 0~1 정규화)
PICK_RANDOM = False      # True 면 풀 안에서 랜덤 (출연진 다양화용)
_PICK_RND = random.Random()
MAX_EXPLICIT = 60        # 이 태그가 끌어오는 성인 콘텐츠 비율 상한 (safe 기본 정책)

# 요청 속성(시트의 영문 태그 문자열) → DB 속성 키. 점수 가중치.
W = {"hair_color": 0.35, "hair_length": 0.20, "traits": 0.15,
     "eyes": 0.10, "age_band": 0.10, "skin": 0.05, "outfit_avoid": 0.05}

_LENGTH_WORDS = [("very long", "very_long_hair"), ("발목", "very_long_hair"), ("허리", "very_long_hair"),
                 ("long", "long_hair"), ("긴", "long_hair"), ("medium", "medium_hair"),
                 ("어깨", "medium_hair"), ("턱", "medium_hair"),
                 ("short", "short_hair"), ("짧", "short_hair"), ("초단발", "very_short_hair")]
_COLOR_WORDS = {"black_hair": ("black hair", "black_hair", "jet black", "흑발"),
                "brown_hair": ("brown hair", "brown_hair", "brunette", "갈색"),
                "blonde_hair": ("blonde", "blond hair", "금발"),
                "pink_hair": ("pink hair", "pink_hair", "분홍"),
                "red_hair": ("red hair", "red_hair", "빨강", "적발"),
                "blue_hair": ("blue hair", "blue_hair", "파랑", "청발"),
                "purple_hair": ("purple hair", "violet hair", "purple_hair", "보라"),
                "green_hair": ("green hair", "green_hair", "초록"),
                "grey_hair": ("grey hair", "gray hair", "silver hair", "grey_hair", "회색", "은발"),
                "orange_hair": ("orange hair", "orange_hair", "주황")}
_EYE_WORDS = {"brown_eyes": ("brown eyes", "갈색"), "blue_eyes": ("blue eyes", "파랑"),
              "green_eyes": ("green eyes", "초록"), "red_eyes": ("red eyes", "빨강", "적발"),
              "purple_eyes": ("purple eyes", "violet eyes", "보라"), "grey_eyes": ("grey eyes", "gray eyes", "회색")}
_SKIN_WORDS = {"dark_skin": ("dark skin", "tan skin", "brown skin", "태닝", "검은 피부"),
               "tanned_skin": ("tanned", "tan skin", "태닝", "그을린")}
_CHILD_WORDS = ("loli", "child", "child body", "flat chest", "petite", "어린", "로리", "child_")
_MATURE_WORDS = ("mature", "milf", "aged up", "adult woman", "성숙", "미시", "부인", "아내", "기혼")
_ELDER_WORDS = ("old_woman", "old woman", "obasan", "grandmother", "grandma", "baking", "senior",
                "주부", "노년", "할머니", "아줌마", "연로", "노파", "노모", "부인")


def load_db(path: str = None) -> dict:
    """DB를 읽어 {tag: attrs}. 없으면 {} — 이 모듈은 DB가 없을 때도 조용히 죽어야 옳습니다."""
    global _DB
    if _DB is not None and path is None:
        return _DB
    p = path or DB_FILE
    if not os.path.isfile(p):
        _DB = {}
        return _DB
    try:
        import yaml
        data = yaml.safe_load(open(p, encoding="utf-8")) or {}
        _DB = data.get("characters") or {}
    except Exception:
        _DB = {}
    return _DB


def _req(proto: dict, skin: str = None) -> dict:
    """시트(또는 LLM 시트 JSON)의 protagonist dict → 요청 속성 세트."""
    def flat(*vals):
        return " , ".join(str(v) for v in vals if v).lower()
    hc = flat(proto.get("hair_color"), proto.get("hair_style"), proto.get("hair"))
    eye = flat(proto.get("eye_color"), proto.get("eyes"))
    body = flat(proto.get("body_shape"), proto.get("age"), proto.get("appearance"))
    sk = flat(proto.get("skin_color"), proto.get("skin"), skin or "")
    r = {"color": set(), "length": set(), "eyes": set(), "skin": set(), "traits": set(),
         "kid": None, "mature": None}
    for tag, words in _COLOR_WORDS.items():
        if any(w in hc for w in words):
            r["color"].add(tag)
    for w, tag in _LENGTH_WORDS:
        if w in hc:
            r["length"].add(tag)
    for tag, words in _EYE_WORDS.items():
        if any(w in eye for w in words):
            r["eyes"].add(tag)
    for tag, words in _SKIN_WORDS.items():
        if any(w in sk for w in words):
            r["skin"].add(tag)
    # 스타일(투테일/포니테일 등)은 시트 태그 문자열에 적힌 그대로 DB 키와 대조
    for t in re.split(r"[,;\s]+", re.sub(r"[^a-z_ ]", " ", hc)):
        if "_" in t:
            r["traits"].add(t)
    r["kid"] = any(w in body for w in _CHILD_WORDS)
    r["mature"] = bool(any(w in body for w in _MATURE_WORDS) or re.search(r"(3[5-9]|[4-9][0-9])\s*세", body))
    r["elder"] = bool(any(w in body for w in _ELDER_WORDS) or re.search(r"(6[0-9]|[7-9][0-9])\s*세", body))
    return r


def _frac(db_set: dict, want: set) -> float:
    """요청 속성(want)이 그 캐릭터에서 얼마나 강하게 학습됐나 (0.0~1.0, 비율 가중)."""
    if not want:
        return 0.0
    hits = [db_set.get(w, 0) for w in want]
    return max(hits) / 100.0 if hits else 0.0


def score_one(tag: str, d: dict, req: dict) -> tuple:
    """(점수, 사유) — 하드 필터를 통과한 캐릭터만."""
    floor = MIN_POSTS_BY_BAND.get(d.get("age_band") or "adult", MIN_POSTS)
    if int(d.get("posts") or 0) < floor:
        return 0.0, f"posts 부족({d.get('posts')} < {floor})"
    if int(d.get("explicit_pct") or 0) > MAX_EXPLICIT:
        return 0.0, f"성인 콘텐츠 {d.get('explicit_pct')}% — safe 정책에 맞지 않음"
    band = d.get("age_band") or "adult"
    if req["kid"] and band == "elder":
        return 0.0, "시트는 어린 체형, 캐릭터는 성숙"
    if (not req["kid"]) and band == "child":
        return 0.0, "아동 코드 캐릭터 — 성인 징후와 만나면 화면이 갈라집니다(실측 P1-9)"
    parts, why = [], []

    def add(key, got, w):
        parts.append(got * w)
        if got >= 0.6:
            why.append(f"{key}✓")
        elif got >= 0.3:
            why.append(f"{key}~")
    add("hair_color", _frac(d.get("hair_color") or {}, req["color"]), W["hair_color"])
    add("hair_length", _frac(d.get("hair_length") or {}, req["length"]), W["hair_length"])
    add("traits", _frac(d.get("traits") or {}, req["traits"]), W["traits"])
    add("eyes", _frac(d.get("eyes") or {}, req["eyes"]), W["eyes"])
    add("skin", _frac(d.get("skin") or {}, req["skin"]), W["skin"])
    # 나이대 — 시트가 나이대를 요구하면 가중치를 3배로 올립니다(미시 요청에 20대 얼굴이 오면 안 되니까요)
    asked_age = bool(req["kid"] or req["mature"] or req["elder"])
    if req["elder"]:
        got = 1.0 if band == "elder" else (0.15 if band == "adult" else 0.0)
    elif req["kid"]:
        got = 1.0 if band == "child" else (0.30 if band == "adult" else 0.0)
    elif req["mature"]:
        got = 1.0 if band == "elder" else (0.50 if band == "adult" else 0.0)
    else:
        got = 1.0 if band == "adult" else 0.40
    add("age_band", got, W["age_band"] * (3 if asked_age else 1))
    # 정장/교복 등 복장 유입은 컷별 의상과 싸울 수 있다 → 감점(0점 처리는 안 함)
    of = d.get("outfit") or {}
    pen = 0.0
    for k, v in of.items():
        if v >= 50:
            pen = max(pen, 0.04)
    want = [W[k] for k, rk in (("hair_color", "color"), ("hair_length", "length"), ("traits", "traits"),
                               ("eyes", "eyes"), ("skin", "skin")) if req.get(rk)]
    want.append(W["age_band"] * (3 if asked_age else 1))
    s = sum(parts) / (sum(want) or 1)
    return max(0.0, s - pen), ",".join(why) + (",복장 유입 감점" if pen else "")


def prompt_safe(tag: str) -> str:
    """태그를 anima 요청 규격으로 보냅니다 — `(` `)` 는 `\\(` `\\)`.

    anima/SDXL 계열은 괄호를 **가중치 구문**으로 읽습니다. Danbooru 태그는 동명이인 구분자로
    괄호를 쓰므로(`zero_two_(darling_in_the_franxx)`) 그대로 보내면 `darling in the franxx`가
    가중치로 해석되어 캐릭터가 빠집니다. 밑줄→공백 치환과 순서가 무엇이든 결과는 같습니다.
    """
    t = str(tag or "")
    return t.replace("(", "\\(").replace(")", "\\)") if PAREN_ESCAPE else t


def set_pick(topk: int = None, margin: float = None, randomize: bool = None, seed=None):
    """후보 고름 규칙을 바꿉니다(프로브/CLI용). None 은 건드리지 않음."""
    global PICK_TOPK, PICK_MARGIN, PICK_RANDOM, _PICK_RND
    if topk is not None:
        PICK_TOPK = max(1, int(topk))
    if margin is not None:
        PICK_MARGIN = max(0.0, float(margin))
    if randomize is not None:
        PICK_RANDOM = bool(randomize)
    if seed is not None:
        _PICK_RND = random.Random(seed)


def pick_char_lookalike(proto: dict, skin: str = None, db: dict = None, top: int = 3) -> dict:
    """시트 protagonist 속성으로 가장 닮은(학습량 확인된) 캐릭터 태그를 고릅니다. 없으면 None.

    후보가 여러 개면(유사도가 최고점과 PICK_MARGIN 이내) 그중 랜덤으로 고릅니다(기본은 최고점 하나).
    반환 태그는 anima 규격으로 escaped 되어 있습니다(`\\(` `\\)`). 원본 태그가 궁금하면 candidates 의 raw 를 보세요.
    """
    db = db if db is not None else load_db()
    if not db or not proto:
        return None
    req = _req(proto, skin)
    if not (req["color"] or req["length"] or req["eyes"]):
        return None                       # 속성을 하나도 못 읽었으면 억지 선택 금지
    scored = []
    for tag, d in db.items():
        s, why = score_one(tag, d or {}, req)
        if s > 0:
            scored.append((s, int((d or {}).get("posts") or 0), tag, why))
    if not scored:
        return None
    scored.sort(key=lambda x: (-x[0], -x[1], x[2]))
    best = scored[0]
    out = {"candidates": [{"tag": prompt_safe(t), "raw": t, "score": round(s, 3), "why": w}
                          for s, p, t, w in scored[:top]],
           "threshold": THRESHOLD}
    if best[0] < THRESHOLD:
        out["rejected"] = f"최고 유사도 {best[0]:.2f} < {THRESHOLD} — 속성 태그만 사용"
        return out
    # 동률(이내 점수) 후보 — 랜덤 선택은 여기서만 일어난다
    pool = [c for c in scored[:max(1, PICK_TOPK)] if c[0] >= best[0] - PICK_MARGIN]
    chosen = _PICK_RND.choice(pool) if PICK_RANDOM else pool[0]
    out.update({"tag": prompt_safe(chosen[2]), "raw": chosen[2], "score": round(chosen[0], 3),
                "why": chosen[3], "posts": chosen[1],
                "series": prompt_safe((db.get(chosen[2]) or {}).get("series") or ""),
                "pool": [prompt_safe(c[2]) for c in pool] if len(pool) > 1 else []})
    return out
