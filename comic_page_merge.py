#!/usr/bin/env python3
"""comic_page_merge.py — 만화 컷 이미지를 격자 페이지로 합성하는 순수 PIL 모듈 (LLM/anima 의존 없음)

프레임 스타일: 페이지 배경 흰색 + 그림 경계 검은 선 (옅은 회색 keyline 보조).

[2026-09-09] 화면 문법 v3 (사용자 지시) — 텍스트를 그림 위에 만화 규약으로 그린다:
  1) **설명(지문)** : 컷 **하단 왼쪽** 흰 박스 + 검은 테두리 + 검은 글씨 (`_draw_caption_box`).
     ★서두 요약/에필로그는 지문이 컷 면적의 ~70%를 채운다(`narr_large`).
  2) **대사** : 만화 말풍선(꼬리가 화자 방향) / **속마음** : 타원 + 물방울 꼬리 (`_draw_balloon`, ≤2개)
  3) **의성어/의태어** : 대형 흰 글씨 + 검은 윤곽, 살짝 기운 각도 (`_draw_sfx`)
  → 이미지 안 오른쪽 흰 플레이트는 폐기(화면 문법 통일). 모든 요소는 컷 안에서만 쓰이고 서로 안
    겹치며 배치는 결정론(같은 입력 → 같은 자리). ★에필로그 컷은 `apply_fade`로 반투명해진다.

[2026-09-07] 레이아웃 v2 (사용자 지시):
  1) portrait 컷은 "앞을 봄(front)" 또는 "왼쪽→오른쪽 시선(right)" 중 하나로 뽑는다(comic_gen에서 강제).
     facing은 지금은 텍스트 위치가 아니라 **풍선 꼬리/배치 쪽**을 정하는 데 쓰인다.
  2) portrait(face 클로즈업) 컷은:event(액션) 컷과 한 행에 붙을 때 **축소(기본 42% 폭)**되어
     일반 이벤트 신과 합쳐진다. 나머지 폭은 이벤트 신이 쓴다.

행(行) 문법
  - wide 컷 → 페이지 폭 전체 스플래시
  - (face, action) / (action, face) 페어 → 혼합 행: face 42% + action 58% (세로는 행 높이로 맞춘다)
  - 그 외 → 2열 균등. 짝 없는 마지막 컷은 2열 폭(레거시 규칙 유지)
  - [2026-09-07] 행 높이를 텍스트로 쓰지 않는다(간격 8+6*2 → 그림 사이 20px) — 텍스트는 전부 이미지 '안' 오버레이.

사용 예:
    import comic_page_merge as C
    texts = [{"narration": "深夜 2시, 옥상 물탱크가 끊기는 소리가 났다.",          # 설명
              "balloons": [{"kind": "speech",  "text": "무거운 건 저에게 맡기세요."},
                           {"kind": "thought", "text": "이 사람, 알고 있었다."}],   # 풍선 ≤2
              "sfx": "두근", "fade": 0.0}, ...]
    C.compose_pages(paths, texts, "comic/book001", "episode_03",
                    panels_per_page=5, panel_wide=[...], panel_face=[...],
                    panel_facing=["front"|"right", ...])
    # 옛 입력도 그대로 받는다: ["상황 묘사", "유즈키: 으…", "소타: 봐."] → 설명 + 말풍선 2개
"""
import os
from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------- 기본 파라미터
DEFAULT_CELL_W = 820          # 그리드 단위 폭 (px)
# [2026-09-07] 최종 페이지 크기 고정(1024x1454 세로): 행 높이를 남는 높이에 맞춰 스케일하고
#   컷은 fit_cover로 슬롯에 맞춰 자른다(잘림/여백 없음). page_size=None이면 옛 자동 크기.
DEFAULT_PAGE_W = 1024
DEFAULT_PAGE_H = 1454
DEFAULT_COLS = 2
DEFAULT_PANELS_PER_PAGE = 5
DEFAULT_GUTTER = 8            # 컷 사이 여백 (흰 프레임의 일부) [2026-09-07] 16→8 (사용자: 간격 과함)
DEFAULT_BG = (255, 255, 255)      # 페이지 배경 = 흰색 프레임
DEFAULT_FRAME = (0, 0, 0)         # 그림 경계 검은 선
DEFAULT_FRAME_WIDTH = 3
DEFAULT_FRAME_PAD = 6             # 검은 선 바깥 흰 여백 [2026-09-07] 10→6 (그림↔그림 36→20px)
DEFAULT_KEYLINE = (150, 150, 156) # 프레임 바깥 옅은 회색 1px (흰 화면에서 컷 경계 표시)
DEFAULT_PLATE = (255, 255, 255)   # 오른쪽 텍스트 플레이트
DEFAULT_TEXT = (16, 16, 18)
DEFAULT_LABEL = (110, 110, 118)
# [2026-09-07] 페이지가 1024x1454로 고정된 뒤 26px가 과하게 컸다 → 넣는 텍스트 축소(사용자 지시)
DEFAULT_FONT_SIZE = 20
DEFAULT_CAPTION_LINES = 3         # 컷 텍스트 최대 줄 수
MAX_CAPTION_LINES = DEFAULT_CAPTION_LINES
DEFAULT_PANEL_ASPECT = 1344 / 1024    # portrait 세로/가로 (1024x1344)
DEFAULT_WIDE_ASPECT = 1024 / 1366     # wide 세로/가로 (1366x1024)
DEFAULT_PLATE_W_RATIO = 0.34          # 넓은 패널(≥600px)의 오른쪽 플레이트 폭 비율
SMALL_PLATE_W_RATIO = 0.52            # 축소 패널의 오른쪽 플레이트 폭 비율(글자 자리가 좁다)
PLATE_MIN_IMG_W = 600                 # 이보다 좁은 패널은 플레이트 비율 확대
FACE_SMALL_SHARE = 0.42               # 혼합 행에서 face(축소) 컷이 먹는 폭 비율

# 폰트 후보는 OS별로 다르다 — 이 목록 하나만 있어면 Windows에서 한글 캡션이 모두 □(tofu)로 렌더된다.
# 순서는 “진하게 → 보통”이고, repo 번들(data/fonts)이 가장 먼저 보인다(OFL 라이선스 폰트를 넣으면
# 어느 OS에서도 결과물이 같다). 맑은 고딕은 Windows 설치본을 **읽기만** 하고 재배포하지 않는다.
FONT_CANDIDATES = [
    # 0) repo 번들(선택): data/fonts/*.ttf|ttf|otf — 배포자가 넣으면 이게 先발
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "fonts", "NotoSansKR-Bold.ttf"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "fonts", "NotoSansKR-Regular.ttf"),
    # 1) Linux (Noto CJK)
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
    # 2) Windows (맑은 고딕 = malgun, 굴림 = gulim, 바탕 = yumin)
    r"C:\Windows\Fonts\malgunbd.ttf",
    r"C:\Windows\Fonts\malgun.ttf",
    r"C:\Windows\Fonts\gulim.ttc",
    r"C:\Windows\Fonts\msgothic.ttc",
    r"C:\Windows\Fonts\yumin.ttc",
    # 3) macOS (Apple SD 산돌고딕 Neo)
    "/System/Library/Fonts/AppleSDGothicNeo-B.ttf",
    "/System/Library/Fonts/AppleSDGothicNeo-Bold.otf",
    "/System/Library/Fonts/Supplemental/AppleMyungjo.ttf",
]

# ---------------------------------------------------------------- [2026-09-09] 용도별 폰트 (만화 화면 문법)
# 설명(지문) / 대사(말풍선) / 속마음(풍선) / 의성어 — 화면 역할이 다르므로 폰트도 다르다.
# 우선순위: --font-* 지정 > data/fonts/(자동 다운로드 스크립트: scripts/get_fonts.sh) > OS 폰트.
#동봉 폰트는 전부 OFL(재배포 가능)입니다 — 맑은 고딕 같은 설치본은 '읽기'만 가능해 동봉 금지.
BUNDLED_FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "fonts")
BUNDLED_FONTS = {
    # 설명(지문) = 세리프( Gowun Batang ) / 대사 = 둥글고 만화스러운 Jua / 속마음 = 손글씨 Do Hyeon
    # 의성어 = 굵은 디스플레이 Black Han Sans — 전부 OFL. MaruBuri는 직접 넣어 쓰실 수 있게 후보만 남깁니다.
    "narration": ("GowunBatang-Bold.ttf", "GowunBatang-Regular.ttf",
                  "MaruBuri-Bold.ttf", "MaruBuri-Regular.ttf",
                  "NotoSerifKR-Bold.ttf", "NotoSerifKR-Regular.ttf",
                  "NotoSansKR-Bold.ttf", "NotoSansKR-Regular.ttf"),
    "dialog":    ("Jua-Regular.ttf", "DoHyeon-Regular.ttf",
                  "NotoSansKR-Bold.ttf", "NotoSansKR-Regular.ttf"),
    "thought":   ("DoHyeon-Regular.ttf", "NanumPenScript-Regular.ttf",
                  "NotoSansKR-Regular.ttf", "NotoSansKR-Bold.ttf"),
    "sfx":       ("BlackHanSans-Regular.ttf", "Jua-Regular.ttf",
                  "NotoSansKR-Bold.ttf", "NotoSansKR-Regular.ttf"),
}
font_role_paths = {}                     # role → 사용자 지정 경로(--font-narration 등)

# 화면 문법 상수
BALLOON_MAX = 2                          # 컷당 풍선 최대 2개(사용자 지시)
BALLOON_FONT_SIZE = 22                   # 말풍선/속마음 글자
NARR_LARGE_FONT_SIZE = 30                # 서두 요약/에필로그의 큰 지문
SFX_FONT_SIZE = 54                       # 의성어 대형
NARR_LARGE_COVER = 0.70                  # 요약 지문이 컷 면적의 70%를 채운다(사용자 지시)
FADE_ALPHA = 0.45                        # 에필로그 이벤트신을 반투명하게 하는 정도(흰 쪽 blend)


def set_font_role(role: str, path: str):
    """용도별 폰트 지정(narration/dialog/thought/sfx). 파일이 없으면 무시(OS 폴백)."""
    if role in BUNDLED_FONTS and path and os.path.exists(path):
        font_role_paths[role] = path


def font_for_role(role: str) -> str:
    """용도 폰트로 쓸 실존 경로(없으면 '' → OS 후보로 Falls back)."""
    p = font_role_paths.get(role)
    if p and os.path.exists(p):
        return p
    for fn in BUNDLED_FONTS.get(role or "", ()):
        fp = os.path.join(BUNDLED_FONT_DIR, fn)
        if os.path.exists(fp):
            return fp
    return ""


def missing_font_roles() -> list:
    """data/fonts에 없는 용도(안내용 — 없어도 OS 폰트로 그냥 돈다)."""
    return [r for r in ("narration", "dialog", "thought", "sfx") if not font_for_role(r)]


# 자동 다운로드 목록 — 전부 **OFL(SIL Open Font License)**라 재배포·임베딩 자유(상업적 이용 포함).
#   설명(지문) = MaruBuri(세리프, 지문감) / 대사 = Jua(둥글고 만화스럽다) / 속마음 = Do Hyeon(손글씨)
#   의성어 = Black Han Sans(굵은 디스플레이)   · 라이선스 원문은 OFL.txt로 함께 받는다.
FONTS_MANIFEST = (
    ("GowunBatang-Regular.ttf",
     "https://github.com/google/fonts/raw/main/ofl/gowunbatang/GowunBatang-Regular.ttf"),
    ("GowunBatang-Bold.ttf",
     "https://github.com/google/fonts/raw/main/ofl/gowunbatang/GowunBatang-Bold.ttf"),
    ("Jua-Regular.ttf", "https://github.com/google/fonts/raw/main/ofl/jua/Jua-Regular.ttf"),
    ("DoHyeon-Regular.ttf", "https://github.com/google/fonts/raw/main/ofl/dohyeon/DoHyeon-Regular.ttf"),
    ("BlackHanSans-Regular.ttf",
     "https://github.com/google/fonts/raw/main/ofl/blackhansans/BlackHanSans-Regular.ttf"),
    ("OFL-gowunbatang.txt", "https://raw.githubusercontent.com/google/fonts/main/ofl/gowunbatang/OFL.txt"),
    ("OFL-jua.txt", "https://raw.githubusercontent.com/google/fonts/main/ofl/jua/OFL.txt"),
    ("OFL-dohyeon.txt", "https://raw.githubusercontent.com/google/fonts/main/ofl/dohyeon/OFL.txt"),
    ("OFL-blackhansans.txt",
     "https://raw.githubusercontent.com/google/fonts/main/ofl/blackhansans/OFL.txt"),
)


def download_fonts(dest: str = None, timeout: int = 60, log=None) -> dict:
    """만화 화면 문법 폰트를 data/fonts/로 받는다 (있는 것은 건너뜀).

    네트워크가 없으면 실패로만 기록하고 끝낸다 — 파이프라인은 OS 폰트로 계속 돈다.
    """
    import urllib.request
    say = log or (lambda *a: None)
    dest = dest or BUNDLED_FONT_DIR
    os.makedirs(dest, exist_ok=True)
    res = {}
    for name, url in FONTS_MANIFEST:
        out = os.path.join(dest, name)
        if os.path.isfile(out) and os.path.getsize(out) > 4096:
            res[name] = "skip"
            say(f"  (건너뜀) {name} — 이미 있습니다")
            continue
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "llm-comic-gen/fonts"})
            with urllib.request.urlopen(req, timeout=timeout) as r, open(out, "wb") as w:
                w.write(r.read())
            res[name] = "ok"
            say(f"  (받음) {name} {os.path.getsize(out) // 1024}KB")
        except Exception as e:
            if os.path.isfile(out):
                os.remove(out)
            res[name] = f"fail:{type(e).__name__}"
            say(f"  (실패) {name} — {e} → 이 용도는 OS 폰트로 렌더됩니다")
    _FONT_CACHE.clear()
    return res


_FONT_CACHE = {}


# ---------------------------------------------------------------- 폰트/텍스트
def load_font(size: int = DEFAULT_FONT_SIZE, path: str = None, role: str = None):
    """한자/한글 폰트 로드. 실패 시 PIL 기본 폰트 (사이즈별 캐시).

    role(narration/dialog/thought/sfx)을 주면 그 용도 폰트를 먼저 시도한다.
    """
    key = (int(size), path, role)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    f = None
    cands = ([path] if path else []) + ([font_for_role(role)] if role else []) + list(FONT_CANDIDATES)
    for cand in cands:
        if not cand or not os.path.exists(cand):
            continue
        try:
            f = ImageFont.truetype(cand, int(size))
            break
        except Exception:
            continue
    if f is None:
        try:
            f = ImageFont.load_default()
        except Exception:
            f = None
    _FONT_CACHE[key] = f
    return f


def has_cjk_font() -> bool:
    return (any(os.path.exists(p) for p in FONT_CANDIDATES)
            or any(font_for_role(r) for r in BUNDLED_FONTS))


def wrap_text(text: str, font, max_width: int, draw, max_lines: int = MAX_CAPTION_LINES):
    """텍스트를 max_width에 맞춰 줄바꿈(문자 단위 — 한국어 공백 없이도 동작).
    max_lines를 넘으면 마지막 줄을 … 로 잘른다."""
    text = (text or "").strip().replace("\n", " ").replace("\r", "")
    if not text:
        return []
    try:
        max_lines = max(1, int(max_lines))
    except Exception:
        max_lines = MAX_CAPTION_LINES
    unit = None
    lines, cur = [], ""
    for ch in text:
        trial = cur + ch
        try:
            w = draw.textlength(trial, font=font)
        except Exception:
            if unit is None:
                unit = (getattr(font, "size", 24) or 24) * 0.55
            w = len(trial) * unit
        if w <= max_width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = ch
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][:-1].rstrip() + "…"
    return lines


def caption_blocks(cap) -> list:
    """캡션 인자를 텍스트 블록 리스트로 정규화 (str 허용 / dict는 화면 문법 페이로드)."""
    if cap is None:
        return []
    if isinstance(cap, str):
        return [cap.strip()] if cap.strip() else []
    if isinstance(cap, dict):
        tp = text_payload(cap)
        out = ([tp["narration"]] if tp["narration"] else [])
        out += [b["text"] for b in tp["balloons"]]
        if tp["sfx"]:
            out.append(tp["sfx"])
        return out
    out = []
    for c in list(cap):
        s = str(c or "").strip()
        if s:
            out.append(s)
    return out


def text_payload(item) -> dict:
    """컷 화면 텍스트 입력(str/list/dict) → 화면 문법 페이로드로 정규화.

      {"narration": 설명(지문), "narr_large": 컷 70% 큰 지문(서두 요약/에필로그),
       "balloons": [{"kind":"speech|thought","text":…, "side":"left|right|None"}] ≤2,
       "sfx": 의성어, "fade": 이벤트신을 반투명하게 하는 정도(0~0.9)}

    str → 설명 하나 / [a,b,c] → [설명, 대사, 대사] 하위호환(옛 selftest·재조립 경로).
    """
    out = {"narration": "", "narr_large": False, "balloons": [], "sfx": "", "fade": 0.0}
    if item is None:
        return out
    if isinstance(item, str):
        out["narration"] = item.strip()
        return out
    if isinstance(item, (list, tuple)):
        seq = [str(x).strip() for x in item if str(x).strip()]
        if seq:
            out["narration"] = seq[0]
        for s in seq[1:1 + BALLOON_MAX]:
            out["balloons"].append({"kind": "speech", "text": s, "side": None})
        return out
    if not isinstance(item, dict):
        return out
    out["narration"] = str(item.get("narration") or item.get("caption_ko")
                           or item.get("caption") or "").strip()
    out["narr_large"] = bool(item.get("narr_large") or item.get("summary") or item.get("epilogue"))
    for b in list(item.get("balloons") or [])[:BALLOON_MAX]:
        if isinstance(b, str) and b.strip():
            out["balloons"].append({"kind": "speech", "text": b.strip(), "side": None})
        elif isinstance(b, dict):
            kind = str(b.get("kind") or "speech").strip().lower()
            side = str(b.get("side") or "").strip().lower() or None
            txt = str(b.get("text") or b.get("line") or "").strip()
            if txt:
                out["balloons"].append({"kind": "thought" if kind.startswith("t") else "speech",
                                        "text": txt, "side": side})
    out["sfx"] = str(item.get("sfx") or item.get("oto") or "").strip()
    try:
        out["fade"] = max(0.0, min(0.9, float(item.get("fade") or 0.0)))
    except (TypeError, ValueError):
        out["fade"] = 0.0
    return out


def apply_fade(img: Image.Image, alpha: float) -> Image.Image:
    """이벤트신을 반투명하게(흰 쪽으로 날린다) — 에필로그 지문을 얹기 위한 처리."""
    try:
        a = max(0.0, min(0.95, float(alpha)))
    except (TypeError, ValueError):
        return img
    if a <= 0:
        return img
    return Image.blend(img.convert("RGB"), Image.new("RGB", img.size, DEFAULT_PLATE), a)


def _rect_hit(a, b, gap: int = 4) -> bool:
    """직사각형 (x0,y0,x1,y1) 겹침 판정(gap 만큼 서로 여유)"""
    return not (a[2] + gap <= b[0] or b[2] + gap <= a[0]
                or a[3] + gap <= b[1] or b[3] + gap <= a[1])


def _place_in_panel(ix: int, iy: int, iw: int, ih: int, w: int, h: int,
                    avoid=(), prefer=("tr", "tl", "br", "mr", "center"), margin: int = 8):
    """컷 안에서 w×h 상자를 둘 위치 — 회피 박스와 안 겹치는 첫 후보(결정론, 랜덤 없음)."""
    if w <= 0 or h <= 0 or w > iw - 2 or h > ih - 2:
        return None
    cx, cy = ix + (iw - w) // 2, iy + (ih - h) // 2
    cands = {"tr": (ix + iw - w - margin, iy + margin), "tl": (ix + margin, iy + margin),
             "br": (ix + iw - w - margin, iy + ih - h - margin),
             "bl": (ix + margin, iy + ih - h - margin),
             "mr": (ix + iw - w - margin, cy), "ml": (ix + margin, cy),
             "center": (cx, cy), "top": (cx, iy + margin)}
    for k in prefer:
        xy = cands.get(k)
        if not xy:
            continue
        r = (xy[0], xy[1], xy[0] + w, xy[1] + h)
        if r[0] < ix or r[1] < iy or r[2] > ix + iw or r[3] > iy + ih:
            continue
        if any(_rect_hit(r, a) for a in (avoid or ())):
            continue
        return xy
    return None


# ---------------------------------------------------------------- 이미지 맞춤
def fit_cover(img: Image.Image, w: int, h: int, bias_x: float = 0.5) -> Image.Image:
    """비율 유지 후 (w,h)를 정확히 채운다(크롭). bias_x=크롭 창 좌우 위치."""
    iw, ih = img.size
    if iw == 0 or ih == 0:
        return Image.new("RGB", (w, h), (255, 255, 255))
    scale = max(w / iw, h / ih)
    nw, nh = max(1, int(iw * scale)), max(1, int(ih * scale))
    r = img.resize((nw, nh), Image.LANCZOS)
    try:
        bias = min(1.0, max(0.0, float(bias_x)))
    except Exception:
        bias = 0.5
    left = int(round((nw - w) * bias))
    top = (nh - h) // 2
    return r.crop((left, top, left + w, top + h))


def _load_rgb(path: str) -> Image.Image:
    im = Image.open(path)
    if im.mode != "RGB":
        im = im.convert("RGB")
    return im


def _text_line_height(font_size: int) -> int:
    return int(font_size * 1.18)


def _plate_ratio(img_w: int) -> float:
    return DEFAULT_PLATE_W_RATIO if img_w >= PLATE_MIN_IMG_W else SMALL_PLATE_W_RATIO


def _plate_font_size(font_size: int, txt_w: int) -> int:
    """플레이트가 좁으면 글자도 축약(한글 1자 ≈ 0.62em 가정, 최소 15px)."""
    return max(15, min(int(font_size), int(txt_w / 6.5)))


def _wrap_blocks(blocks, font, width: int, d, max_lines: int) -> list:
    """블록 리스트 → [[줄,…], …] (전체 줄 수가 max_lines를 넘지 않게 뒤 블록부터 버린다)"""
    out, used = [], 0
    for b in blocks:
        remain = max_lines - used
        if remain <= 0:
            break
        ln = wrap_text(b, font, width, d, remain)
        if not ln:
            continue
        out.append(ln)
        used += len(ln)
    return out


# ---------------------------------------------------------------- 행 배치(plan)
def _plan_rows(n: int, captions, wide_flags, face_flags, zone_flags, *,
               unit_w: int, cols: int, gutter: int, pad: int, border: int,
               font_size: int, font_path: str = None,
               caption_lines: int = MAX_CAPTION_LINES,
               panel_aspect: float = DEFAULT_PANEL_ASPECT,
               wide_aspect: float = DEFAULT_WIDE_ASPECT,
               wide_odd_last: bool = True,
               row_spec=None, page_size=None, label_h: int = 0):
    """컷 목록 → (page_w, geom_rows). 렌더와 분리해 레이아웃만 단위 테스트할 수 있다.

    row_spec(cut.yaml 모드): [{"cells": [{"idx": 페이지로컬 int, "share": float}], "center": bool,
                                "h_share": float}, ...]
      share = 행 내 좌→우 폭 비율(합 1.0). 미지정이면 wide/face 플래그 기반 자동 행 문법(레거시).
      h_share = (선택) 페이지 높이 중 이 행 비율. 페이지 안 행들이 하나라도 가진 않으면 그 페이지
                행 높이를 이 비율로 나눈다(예: climax_impact 2단 4:6).
    cell: {"idx","w","h","zone":"right"|"bottom","blocks","cap_lines","font_size","face"}
    """
    probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    page_w = unit_w * cols + gutter * (cols + 1)
    if page_size:
        # 페이지 폭 고정: 단위 폭을 페이지 폭에서 역산(격자가 페이지 폭과 정확히 일치)
        page_w = int(page_size[0])
        unit_w = (page_w - gutter * (cols + 1)) // cols
    full_w = page_w - 2 * gutter                      # 2열 전체 콘텐츠 폭
    caps = list(captions or []) + [None] * max(0, n - len(captions or []))
    flags_w = [bool(w) for w in (wide_flags or [])][:n] + [False] * max(0, n - len(wide_flags or []))
    flags_f = [bool(f) for f in (face_flags or [])][:n] + [False] * max(0, n - len(face_flags or []))
    zones = list(zone_flags or [])[:n] + [None] * max(0, n - len(zone_flags or []))

    def zone_of(i):
        if zones[i]:
            return "right" if str(zones[i]).lower().startswith("r") else "bottom"
        return "right" if flags_w[i] else "bottom"

    # ── 1단계) 행 정의: (idx목록, 컷폭목록, 중앙정렬) — spec 모드면 그대로, 아니면 자동 문법
    row_defs = []
    if row_spec:
        used = set()
        for r in row_spec:
            cells = [c for c in (r.get("cells") or []) if 0 <= int(c["idx"]) < n]
            if not cells:
                continue
            k = len(cells)
            center = bool(r.get("center")) and k == 1
            avail = full_w - gutter * (k - 1)
            ws = [int(round(avail * float(c.get("share", 1.0 / k)))) for c in cells]
            if k > 1:
                ws[-1] += avail - sum(ws)             # 반올림 오차 보정(합 유지)
            elif center:
                ws = [int(round(full_w * float(cells[0].get("share", 1.0))))]
            used.update(int(c["idx"]) for c in cells)
            row_defs.append(([int(c["idx"]) for c in cells], ws, center,
                             float(r.get("h_share") or 0.0)))
        missing = [i for i in range(n) if i not in used]        # spec 밖 컷은 2열로 토막(안전장치)
        for i in range(0, len(missing), 2):
            row_defs.append((missing[i:i + 2], [unit_w] * len(missing[i:i + 2]), False))
    else:
        i = 0
        while i < n:
            if flags_w[i]:
                row_defs.append(([i], [full_w], False))         # wide 스플래시
                i += 1
                continue
            nxt_ok = i + 1 < n and not flags_w[i + 1]
            if nxt_ok and (flags_f[i] != flags_f[i + 1]):       # 혼합 행: face 축소(42%)
                fw = int(round((full_w - gutter) * FACE_SMALL_SHARE))
                ew = full_w - gutter - fw
                a, b = (fw, ew) if flags_f[i] else (ew, fw)
                row_defs.append(([i, i + 1], [a, b], False))
                i += 2
                continue
            if nxt_ok:
                row_defs.append(([i, i + 1], [unit_w, unit_w], False))
                i += 2
                continue
            solo_full = bool(wide_odd_last and i > 0 and not flags_f[i])
            row_defs.append(([i], [full_w if solo_full else unit_w], False))
            i += 1

    # ── 2단계) 폭/높이/텍스트 배치 계산
    geom_rows = []
    for idxs, widths, center, *rest in row_defs:
        h_share = float(rest[0]) if rest else 0.0
        w_cells = sum(widths) + gutter * (len(idxs) - 1)
        x0 = gutter + (page_w - 2 * gutter - w_cells) // 2

        cells, row_h, bottom_lines = [], 0, 0
        for p_idx, pw_cell in zip(idxs, widths):
            aspect = wide_aspect if flags_w[p_idx] else panel_aspect
            ph = int(round((pw_cell - 2 * pad) * aspect)) + 2 * pad
            zone = zone_of(p_idx)
            blocks = caption_blocks(caps[p_idx])
            cell = {"idx": p_idx, "w": pw_cell, "h": ph, "zone": zone,
                    "face": bool(flags_f[p_idx]), "blocks": blocks, "cap_lines": 0,
                    "text": caps[p_idx],           # 화면 문법 페이로드 원본(렌더가 그대로 읽는다)
                    "font_size": int(font_size)}
            if zone == "right":
                img_w = pw_cell - 2 * pad
                pl_w = int(img_w * _plate_ratio(img_w))
                txt_w = pl_w - 2 * (pad + 8)
                pf = _plate_font_size(font_size, txt_w)
                cell["blocks"] = _wrap_blocks(blocks, load_font(pf, font_path), txt_w, probe,
                                              caption_lines)
                cell["font_size"] = pf
            else:
                # [2026-09-07] bottom 존도 이제 이미지 '안' 하단 박스에 든다 → 행 높이 미사용.
                # (캡션은 렌더 단계 _draw_inner_caption이 그림 위에 오버레이)
                wrapped = _wrap_blocks(blocks, load_font(font_size, font_path),
                                       pw_cell - 2 * pad - 32, probe, caption_lines)
                cell["blocks"] = wrapped
                cell["cap_lines"] = 0
            cells.append(cell)
            row_h = max(row_h, ph)
        geom_rows.append({"x": x0, "w": w_cells, "h": row_h, "bottom_lines": 0,
                          "h_share": h_share, "cells": cells})

    if page_size and geom_rows:
        # 세로도 고정: (page_h - 위/아래 거터 - 라벨)에 행 높이를 비례 스케일, 마지막 행으로 오차 보정
        # 단, 행에 h_share(cut.yaml tier.height)가 있으면 자연 높이 대신 그 비율로 나눈다.
        avail = int(page_size[1]) - 2 * gutter - int(label_h)
        content = sum(r["h"] for r in geom_rows)
        shares = [float(r.get("h_share") or 0.0) for r in geom_rows]
        tot_h = sum(shares)
        if avail > 0 and tot_h > 0:
            acc = 0
            for i, r in enumerate(geom_rows):
                r["h"] = max(64, int(round(avail * shares[i] / tot_h)))
                acc += r["h"]
                for c in r["cells"]:
                    c["h"] = r["h"]
            last = geom_rows[-1]
            last["h"] += avail - acc
            for c in last["cells"]:
                c["h"] = last["h"]
        elif avail > 0 and content > 0:
            scale = avail / content
            acc = 0
            for r in geom_rows:
                r["h"] = max(64, int(round(r["h"] * scale)))
                acc += r["h"]
                for c in r["cells"]:
                    c["h"] = r["h"]
            last = geom_rows[-1]
            last["h"] += avail - acc
            for c in last["cells"]:
                c["h"] = last["h"]
    return page_w, geom_rows


# ---------------------------------------------------------------- 프레임/플레이트 그리기
def _draw_framed_panel(canvas, d, x: int, y: int, w: int, h: int, img=None, *,
                       frame=DEFAULT_FRAME, pad=DEFAULT_FRAME_PAD, line=DEFAULT_FRAME_WIDTH,
                       bg=DEFAULT_BG, keyline=DEFAULT_KEYLINE):
    """흰 프레임 + 그림 바로 밖 검은 선 (+옅은 keyline). → 그림 사각형 (ix,iy,iw,ih)"""
    ix, iy = x + pad, y + pad
    iw, ih = max(1, w - 2 * pad), max(1, h - 2 * pad)
    canvas.paste(img, (ix, iy)) if img is not None else d.rectangle(
        [ix, iy, ix + iw - 1, iy + ih - 1], fill=bg)
    d.rectangle([ix - 1, iy - 1, ix + iw, iy + ih], outline=frame, width=max(1, int(line)))
    if keyline:
        d.rectangle([x, y, x + w - 1, y + h - 1], outline=keyline, width=1)
    return ix, iy, iw, ih


def _draw_right_plate(d, ix, iy, iw, ih, blocks, *, plate=DEFAULT_PLATE, frame=DEFAULT_FRAME,
                      pad=DEFAULT_FRAME_PAD, line=DEFAULT_FRAME_WIDTH, font_size=DEFAULT_FONT_SIZE,
                      text_color=DEFAULT_TEXT, font_path=None):
    """[ deprecated 2026-09-09 ] 이미지 안 오른쪽 플레이트 — 화면 문법을 '설명 박스 + 풍선'으로
    통일하면서 렌더 경로에서 빠졌다. (옛 스크립트 재조립용 호환 함수로만 남긴다.)"""
    if not blocks:
        return
    pl_w = int(iw * _plate_ratio(iw))
    txt_w = pl_w - 2 * (pad + 8)
    pf = _plate_font_size(font_size, txt_w)
    font = load_font(pf, font_path)
    line_h = _text_line_height(pf)
    total = sum(len(x) for x in blocks)
    pl_h = total * line_h + 2 * (pad + 6)
    pl_x = ix + iw - pl_w - pad - 6
    pl_y = iy + max(0, (ih - pl_h) // 2)
    d.rectangle([pl_x, pl_y, pl_x + pl_w, pl_y + pl_h], fill=plate)
    d.rectangle([pl_x, pl_y, pl_x + pl_w - 1, pl_y + pl_h - 1],
                outline=frame, width=max(1, int(line)))
    ty = pl_y + pad + 6
    for block in blocks:
        for ln in block:
            d.text((pl_x + pad + 8, ty), ln, font=font, fill=text_color)
            ty += line_h
        ty += int(line_h * 0.25)
    if not blocks:
        return
    pl_w = int(iw * _plate_ratio(iw))
    txt_w = pl_w - 2 * (pad + 8)
    pf = _plate_font_size(font_size, txt_w)
    font = load_font(pf, font_path)
    line_h = _text_line_height(pf)
    total = sum(len(x) for x in blocks)
    pl_h = total * line_h + 2 * (pad + 6)
    pl_x = ix + iw - pl_w - pad - 6
    pl_y = iy + max(0, (ih - pl_h) // 2)
    d.rectangle([pl_x, pl_y, pl_x + pl_w, pl_y + pl_h], fill=plate)
    d.rectangle([pl_x, pl_y, pl_x + pl_w - 1, pl_y + pl_h - 1],
                outline=frame, width=max(1, int(line)))
    ty = pl_y + pad + 6
    for block in blocks:
        for ln in block:
            d.text((pl_x + pad + 8, ty), ln, font=font, fill=text_color)
            ty += line_h
        ty += int(line_h * 0.25)


def _draw_caption_box(d, ix: int, iy: int, iw: int, ih: int, text, *,
                      large: bool = False, font_size: int = DEFAULT_FONT_SIZE,
                      plate=DEFAULT_PLATE, frame=DEFAULT_FRAME, line: int = DEFAULT_FRAME_WIDTH,
                      text_color=DEFAULT_TEXT, font_path=None, max_lines: int = 3):
    """[2026-09-09] 설명(지문) 박스 — 컷 **하단 왼쪽**, 흰 배경 + 검은 테두리 + 검은 글씨.

    large(각 기승전결 첫 컷·에필로그): 컷 면적의 ~70%를 채우는 큰 지문(사용자 지시).
    글자가 박스에 안 들어가면 폰트를 단계로 줄이고, 그래도 넘치면 … 로 자른다.
    → (x0, y0, x1, y1, font_size) 또는 None
    """
    text = str(text or "").strip()
    if not text or iw <= 40 or ih <= 40:
        return None
    margin = 8
    probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    if large:
        # [2026-09-09] 사용자 지시: ★요약/에필로그 지문은 컷의 ~70%를 채운다 → 박스 크기를 고정보다
        box_w = max(120, min(iw - 2 * margin, int(round(iw * 0.86))))
        box_h_fixed = max(96, min(ih - 2 * margin, int(round(ih * (NARR_LARGE_COVER / 0.86)))))
        box_cap = box_h_fixed
        fs_hi, lines_cap = max(16, int(font_size * 1.5)), 40
    else:
        box_w = max(110, min(iw - 2 * margin, int(round(iw * 0.60))))
        box_h_fixed = 0
        box_cap = ih - 2 * margin
        fs_hi, lines_cap = int(font_size), max(1, int(max_lines))
    fs, lines, line_h = fs_hi, [], _text_line_height(fs_hi)
    for fs_try in range(fs_hi, 13, -2):
        font = load_font(fs_try, font_path, role="narration")
        line_h = _text_line_height(fs_try)
        cap = min(lines_cap, max(1, (box_cap - 14) // line_h))
        lines = wrap_text(text, font, box_w - 20, probe, max_lines=cap)
        if lines and len(lines) <= cap:
            fs = fs_try
            break
        fs = fs_try
    if not lines:
        return None
    box_h = box_h_fixed if large else min(box_cap, len(lines) * line_h + 16)
    x0 = ix + margin
    y1 = iy + ih - margin
    y0 = max(iy + margin, y1 - box_h)
    x1 = x0 + box_w
    d.rectangle([x0, y0, x1, y1], fill=plate)
    d.rectangle([x0, y0, x1 - 1, y1 - 1], outline=frame, width=max(1, int(line)))
    font = load_font(fs, font_path, role="narration")
    if large:
        # 큰 박스에서는 위 여백을 두고 시작(아래로 떨어뜨리면 첫 줄이 잘려 보인다)
        ty = y0 + max(10, (box_h - len(lines) * line_h) // 5)
    else:
        ty = y0 + 8
    for ln in lines:
        d.text((x0 + 10, ty), ln, font=font, fill=text_color)
        ty += line_h
    return x0, y0, x1, y1, fs


def _draw_balloon(d, ix: int, iy: int, iw: int, ih: int, balloon, *, avoid=(),
                  plate=DEFAULT_PLATE, frame=DEFAULT_FRAME, line: int = DEFAULT_FRAME_WIDTH,
                  text_color=DEFAULT_TEXT, font_size: int = BALLOON_FONT_SIZE, font_path=None,
                  facing: str = None):
    """[2026-09-09] 말풍선(speech) / 속마음 풍선(thought).

    speech  : 모서리 둥근 사각 + 아래로 내린 꼬리(삼각) — 꼬리는 화자(입) 방향
    thought : 타원 + 화자 쪽으로 작아지는 물방울 3개 (속마음은 꼬리가 떨어진다)
    배치는 `_place_in_panel`(결정론 후보 순회) — 설명 박스·다른 풍선과 안 겹치게.
    → 그린 상자 (x0,y0,x1,y1) 또는 None(자리가 없으면 그리지 않는다)
    """
    kind = str((balloon or {}).get("kind") or "speech").lower()
    kind = "thought" if kind.startswith("t") else "speech"
    text = str((balloon or {}).get("text") or "").strip()
    if not text or iw <= 60 or ih <= 60:
        return None
    role = "thought" if kind == "thought" else "dialog"
    side = str((balloon or {}).get("side") or facing or "").strip().lower() or None
    probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    box_w = box_h = 0
    lines, font, fs, line_h = [], None, font_size, _text_line_height(font_size)
    for fs_try in range(int(font_size), 13, -2):
        fnt = load_font(fs_try, font_path, role=role)
        lh = _text_line_height(fs_try)
        avail_w = int(iw * (0.58 if side else 0.66))
        ls = wrap_text(text, fnt, avail_w, probe, max_lines=4)
        if not ls:
            return None
        pad = 14
        tw = 0
        for ln in ls:
            try:
                tw = max(tw, probe.textlength(ln, font=fnt))
            except Exception:
                tw = max(tw, len(ln) * fs_try * 0.6)
        bw, bh = int(tw + 2 * pad), int(len(ls) * lh + 2 * pad)
        if kind == "thought":                     # 타원은 모서리가 잘린다 → 여유를 둔다
            bw, bh = int(bw / 0.74), int(bh / 0.60)
        if bw <= iw - 16 and bh <= ih - 16:
            lines, font, fs, line_h, box_w, box_h = ls, fnt, fs_try, lh, bw, bh
            break
        lines, font, fs, line_h = ls, fnt, fs_try, lh
        box_w, box_h = bw, bh
        if fs_try <= 15:                          # 끝까지 좁아도 안 들어가면 최소 글자로 강행
            break
    if not lines or box_w <= 0 or box_h <= 0:
        return None
    prefer = (("tr", "br", "mr", "tl", "center") if side in (None, "left")
              else ("tl", "bl", "ml", "tr", "center"))
    xy = _place_in_panel(ix, iy, iw, ih, box_w, box_h, avoid=avoid, prefer=prefer)
    if not xy:
        xy = _place_in_panel(ix, iy, iw, ih, box_w, box_h, avoid=avoid,
                             prefer=("center", "top") + prefer)
    if not xy:
        return None                       # 자리가 없으면 겹쳐 쓰지 않고 생략한다
    x0, y0 = xy
    x1, y1 = x0 + box_w, y0 + box_h
    ax = ix + int(iw * (0.30 if side == "left" else 0.70 if side == "right" else 0.5))
    ay = iy + int(ih * 0.80)
    ax = max(ix + 6, min(ix + iw - 6, ax))
    ay = max(y1 + 8, min(iy + ih - 6, ay))
    if kind == "speech":
        bx = max(x0 + 20, min(x1 - 20, ax))
        d.polygon([(bx - 18, y1 - 6), (bx + 18, y1 - 6), (ax, ay)], fill=frame)   # 굵은 테두리용
        d.rounded_rectangle([x0, y0, x1, y1], radius=max(8, min(22, box_h // 3)),
                            fill=plate, outline=frame, width=max(1, int(line)))
        d.polygon([(bx - 8, y1 - 2), (bx + 8, y1 - 2), (ax, ay - 2)], fill=plate)  # 풍선 목(흰 부분)
        d.line([(bx - 9, y1 - 1), (bx + 9, y1 - 1)], fill=plate, width=max(2, int(line) + 1))
        ty0 = y0 + 12
    else:
        d.ellipse([x0, y0, x1, y1], fill=plate, outline=frame, width=max(1, int(line)))
        for i, (rr, frac) in enumerate(((10, 0.86), (7, 0.66), (5, 0.44))):
            tx = (x0 + x1) / 2 + (ax - (x0 + x1) / 2) * frac
            ty = y1 + (ay - y1) * (0.18 + 0.30 * i)
            d.ellipse([tx - rr, ty - rr, tx + rr, ty + rr], fill=plate, outline=frame,
                      width=max(1, int(line)))
        ty0 = y0 + int(box_h * 0.24)
    ty = ty0
    for ln in lines:
        try:
            wln = probe.textlength(ln, font=font)
        except Exception:
            wln = len(ln) * fs * 0.6
        d.text(((x0 + x1 - wln) // 2, ty), ln, font=font, fill=text_color)
        ty += line_h
    return x0, y0, x1, y1


def _draw_sfx(canvas, d, ix: int, iy: int, iw: int, ih: int, text, *,
              font_size: int = SFX_FONT_SIZE, avoid=(), frame=DEFAULT_FRAME,
              plate=DEFAULT_PLATE, font_path=None):
    """[2026-09-09] 의성어/의태어 — 대형 흰 글씨 + 검은 윤곽, 살짝 기운 각도(텍스트로 결정론)."""
    text = str(text or "").strip()
    if not text or iw <= 80 or ih <= 80:
        return None
    probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    fs = int(font_size)
    for fs_try in range(int(font_size), 17, -4):
        font = load_font(fs_try, font_path, role="sfx")
        try:
            tw = probe.textlength(text, font=font)
        except Exception:
            tw = len(text) * fs_try * 0.7
        if tw <= iw * 0.72:
            fs = fs_try
            break
        fs = fs_try
    font = load_font(fs, font_path, role="sfx")
    try:
        tw = int(probe.textlength(text, font=font))
        th = int(fs * 1.25)
    except Exception:
        tw, th = int(len(text) * fs * 0.7), int(fs * 1.25)
    stroke = max(3, fs // 12)
    pad = stroke * 3
    box_w, box_h = tw + 2 * pad, th + 2 * pad
    xy = _place_in_panel(ix, iy, iw, ih, box_w, box_h, avoid=avoid,
                         prefer=("tr", "tl", "br", "mr", "center"))
    if not xy:
        return None
    fill_rgba = tuple(plate)[:3] + (255,)
    frame_rgba = tuple(frame)[:3] + (255,)
    layer = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    dl = ImageDraw.Draw(layer)
    try:
        dl.text((pad, pad), text, font=font, fill=fill_rgba,
                stroke_width=stroke, stroke_fill=frame_rgba)
    except Exception:
        dl.text((pad, pad), text, font=font, fill=fill_rgba)
    ang = (sum(ord(c) for c in text) % 13) - 6            # -6~+6도 (실행마다 같은 각도)
    layer = layer.rotate(ang, expand=True, resample=Image.BICUBIC)
    px = min(ix + iw - layer.width, xy[0])
    py = min(iy + ih - layer.height, xy[1])
    canvas.paste(layer, (max(ix, px), max(iy, py)), layer)
    return (max(ix, px), max(iy, py), max(ix, px) + layer.width, max(iy, py) + layer.height)


def _draw_panel_text(canvas, d, ix: int, iy: int, iw: int, ih: int, item, *,
                     font_size: int = DEFAULT_FONT_SIZE, font_path: str = None,
                     plate=DEFAULT_PLATE, frame=DEFAULT_FRAME,
                     frame_width: int = DEFAULT_FRAME_WIDTH, text_color=DEFAULT_TEXT,
                     facing: str = None) -> list:
    """[2026-09-09] 컷 하나에 화면 문법을 그린다 — 의성어 → 설명 박스 → 풍선(≤2개).

    모든 요소는 컷 안에서만 쓰이고(컷 밖으로 안 나감), 서로 안 겹치게 배치한다.
    반환: 그린 상자 [(x0,y0,x1,y1), …] (자기 점검/진단용)
    """
    tp = text_payload(item)
    placed = []
    if tp["sfx"]:
        r = _draw_sfx(canvas, d, ix, iy, iw, ih, tp["sfx"], font_path=font_path,
                      avoid=placed, frame=frame, plate=plate)
        if r:
            placed.append(r[:4])
    if tp["narration"]:
        r = _draw_caption_box(d, ix, iy, iw, ih, tp["narration"], large=tp["narr_large"],
                              font_size=font_size, plate=plate, frame=frame,
                              line=frame_width, text_color=text_color, font_path=font_path)
        if r:
            placed.append(r[:4])
    for b in tp["balloons"][:BALLOON_MAX]:
        r = _draw_balloon(d, ix, iy, iw, ih, b, avoid=placed, plate=plate, frame=frame,
                          line=frame_width, text_color=text_color, font_path=font_path,
                          facing=facing)
        if r:
            placed.append(r)
    return placed


# ---------------------------------------------------------------- 페이지 합성
def compose_page(panel_paths, captions, *,
                 out_path: str = None,
                 page_label: str = None,
                 cell_w: int = DEFAULT_CELL_W,
                 cols: int = DEFAULT_COLS,
                 gutter: int = DEFAULT_GUTTER,
                 border: int = 4,
                 bg=DEFAULT_BG,
                 frame=DEFAULT_FRAME,
                 frame_width: int = DEFAULT_FRAME_WIDTH,
                 frame_pad: int = DEFAULT_FRAME_PAD,
                 keyline=DEFAULT_KEYLINE,
                 plate=DEFAULT_PLATE,
                 text_color=DEFAULT_TEXT,
                 font_size: int = DEFAULT_FONT_SIZE,
                 font_path: str = None,
                 panel_aspect: float = DEFAULT_PANEL_ASPECT,
                 wide_aspect: float = DEFAULT_WIDE_ASPECT,
                 caption_lines: int = MAX_CAPTION_LINES,
                 wide_odd_last: bool = True,
                 panel_wide=None,
                 panel_face=None,
                 panel_zone=None,
                 panel_facing=None,
                 row_spec=None,
                 page_size=(DEFAULT_PAGE_W, DEFAULT_PAGE_H),
                 text_plate_on_wide: bool = True):
    """컷 리스트를 한 장의 페이지로 합성.

    Args:
        panel_paths: 컷 이미지 경로(순서 그대로 배치)
        captions: 컷별 화면 텍스트 — 화면 문법 dict({"narration","balloons","sfx","fade","narr_large"})
                    또는 옛 형식 str / ["설명","대사1","대사2"] 리스트(자동 변환).
        panel_wide: wide(전폭 스플래시) 여부
        panel_face: face 클로즈업 여부 (액션과 한 행이 될 때 축소 pairing)
        panel_zone: [호환] 옛 텍스트 위치("right"/"bottom") — 2026-09-09 화면 문법 통일으로
                    렌더에는 영향이 없고 값만 받는다(설명은 하단 왼쪽 + 풍선으로 고정).
        panel_facing: 컷별 시선("left"/"right"/"front") — 풍선 꼬리/배치 쪽을 정하는 데 쓴다.
    Returns: PIL.Image
    """
    if not panel_paths:
        raise ValueError("panel_paths가 비어 있습니다")
    n = len(panel_paths)
    line_h = _text_line_height(font_size)
    pad = max(0, int(frame_pad))
    cap_gap = int(gutter * 0.4)
    label_h = int(round(font_size * 1.6)) if page_label else 0
    page_w, geom_rows = _plan_rows(
        n, captions, panel_wide, panel_face, panel_zone,
        unit_w=int(cell_w), cols=cols, gutter=gutter, pad=frame_pad, border=border,
        font_size=font_size, font_path=font_path, caption_lines=caption_lines,
        panel_aspect=panel_aspect, wide_aspect=wide_aspect, wide_odd_last=wide_odd_last,
        row_spec=row_spec, page_size=page_size, label_h=label_h)

    page_h = gutter + label_h + sum(
        r["h"] + r["bottom_lines"] * line_h + (cap_gap if r["bottom_lines"] else 0) + gutter
        for r in geom_rows)
    if page_size:
        page_h = int(page_size[1])
        page_w = int(page_size[0])
    canvas = Image.new("RGB", (page_w, page_h), bg)
    d = ImageDraw.Draw(canvas)
    if page_label:
        d.text((gutter, gutter // 2), page_label, font=load_font(font_size, font_path),
               fill=DEFAULT_LABEL)

    y = gutter + label_h
    for r in geom_rows:
        cx = r["x"]
        for c in r["cells"]:
            p_idx = c["idx"]
            try:
                img = fit_cover(_load_rgb(panel_paths[p_idx]),
                                c["w"] - 2 * pad, c["h"] - 2 * pad,
                                bias_x=0.42 if (panel_wide or [False] * n)[p_idx] else 0.5)
            except Exception:
                img = Image.new("RGB", (max(1, c["w"] - 2 * pad), max(1, c["h"] - 2 * pad)), bg)
            tp = text_payload(c.get("text") if c.get("text") is not None else c["blocks"])
            img = apply_fade(img, tp["fade"])                    # 에필로그 이벤트신 = 반투명
            ix, iy, iw, ih = _draw_framed_panel(canvas, d, cx, y, c["w"], r["h"], img,
                                                frame=frame, pad=pad, line=frame_width,
                                                bg=bg, keyline=keyline)
            # [2026-09-09] 화면 문법은 존(zone)과 무관하게 하나로: 설명 박스(하단 왼쪽) + 풍선 + 의성어
            item = c["text"] if c.get("text") is not None else c["blocks"]
            _draw_panel_text(canvas, d, ix, iy, iw, ih, item,
                             font_size=c["font_size"] if c["zone"] == "right" else font_size,
                             font_path=font_path, plate=plate, frame=frame,
                             frame_width=frame_width, text_color=text_color,
                             facing=(panel_facing or [None] * n)[p_idx])
            cx += c["w"] + gutter
        y += r["h"] + r["bottom_lines"] * line_h + (cap_gap if r["bottom_lines"] else 0) + gutter

    if out_path:
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        canvas.save(out_path, "PNG")
    return canvas


def compose_pages(panel_paths, captions, out_dir: str, basename: str,
                  *, panels_per_page: int = DEFAULT_PANELS_PER_PAGE,
                  page_label_prefix: str = None,
                  panel_wide=None,
                  panel_face=None,
                  panel_zone=None,
                  panel_facing=None,
                  page_specs=None,
                  **kw):
    """컷 리스트를 여러 페이지로 저장 → 저장 경로 목록.

    page_specs(cut.yaml 모드): [{"size": int, "rows": [{"cells": [{"idx": 페이지로컬 idx, "share": float}],
                                  "center": bool}, ...]}, ...] — panels_per_page 대신 이 구성을 쓴다.
    captions: 컷별 화면 텍스트 — str / [설명, 대사, 대사] / 화면 문법 dict(설명·풍선·의성어) 모두 허용.
    """
    if not panel_paths:
        return []
    os.makedirs(out_dir, exist_ok=True)
    if page_specs:
        chunks_spec = []
        for ps in page_specs:
            mx = max([int(c["idx"]) + 1 for r in ps.get("rows", []) for c in r.get("cells", [])] or [0])
            chunks_spec.append(max(int(ps.get("size") or 0), mx))
        chunks, off = [], 0
        for sz in chunks_spec:
            chunks.append((panel_paths[off:off + sz], off, sz))
            off += sz
        if off < len(panel_paths):                     # spec 밖 잔여 컷은 기본 분할로 토막
            for i in range(off, len(panel_paths), panels_per_page):
                chunks.append((panel_paths[i:i + panels_per_page], i,
                               min(panels_per_page, len(panel_paths) - i)))
            chunks_spec += [None] * (len(chunks) - len(chunks_spec))
    else:
        chunks = [(panel_paths[i:i + panels_per_page], i,
                   min(panels_per_page, len(panel_paths) - i))
                  for i in range(0, len(panel_paths), panels_per_page)]
        chunks_spec = [None] * len(chunks)
    total = len(chunks)
    saved = []
    for pi, (chunk, off, k) in enumerate(chunks, start=1):
        caps = list(captions[off:off + k]) if captions else []
        sl = lambda lst: (list(lst[off:off + k]) if lst else None)
        label = f"{page_label_prefix} · P{pi}/{total}" if page_label_prefix else None
        out = os.path.join(out_dir, f"{basename}_page{pi:02d}.png")
        spec = page_specs[pi - 1]["rows"] if page_specs and chunks_spec[pi - 1] is not None else None
        compose_page(chunk, caps, out_path=out, page_label=label, panel_wide=sl(panel_wide),
                     panel_face=sl(panel_face), panel_zone=sl(panel_zone),
                     panel_facing=sl(panel_facing), row_spec=spec, **kw)
        saved.append(out)
    return saved


if __name__ == "__main__":
    print(__doc__)
    print("CJK 폰트:", "사용 가능" if has_cjk_font() else "없음(기본 폰트 fallback)")
