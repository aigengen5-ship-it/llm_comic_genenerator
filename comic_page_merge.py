#!/usr/bin/env python3
"""comic_page_merge.py — 만화 컷 이미지를 격자 페이지로 합성하는 순수 PIL 모듈 (LLM/anima 의존 없음)

프레임 스타일: 페이지 배경 흰색 + **굵은 검은 선이 그림 경계에 곧장** 붙는다(흰 여백·회색 이중선 없음).

[2026-09-09] 화면 문법 v4 (사용자 지시) — 텍스트를 그림 위에 만화 규약으로 그린다:
  1) **설명(지문)** : 컷 **하단 왼쪽** 흰 박스 + 검은 테두리 + 검은 글씨 (`_draw_caption_box`).
     박스는 **글자 덩치에 맞추어** 작아지고, 대사가 있는 컷(`narrow`)은 폭·줄 수를 더 줄여
     대화 자리를 남긴다. ★서두 요약/에필로그만 예외로 컷 면적의 ~70%를 채운다(`narr_large`).
  2) **대사** : 만화 말풍선 = 직사각형 / **속마음** : 타원 (`_draw_balloon`, ≤BALLOON_MAX개).
     자리는 화자별로 고정 — `speaker="me"`(주인공)는 **왼쪽 위 → 왼쪽 아래**, `speaker="other"`
     (상대방)는 **오른쪽 위 → 오른쪽 아래**(`_balloon_slot_pref`).
     [2026-09-10] 사용자 지시: 꼬리(화살표)·속마음 화살표·생각 물방울(작은 원)은 정상 동작하지
     않아 **전부 삭제** — 풍선은 몸체(직사각형/타원)만 그린다.
  3) **의성어/의태어** : 대형 흰 글씨 + 검은 윤곽, 살짝 기운 각도 (`_draw_sfx`)
  4) **감정 표시** : 풍선 곁의 작은 이모티콘(`_draw_emotif`) — anger/surprise/sweat/heart/
     gloom/sparkle/question, 감정마다 색이 다르다. PIL 벡터라 폰트 설치와 무관하다.
  → 이미지 안 오른쪽 흰 플레이트는 폐기(화면 문법 통일). 모든 요소는 컷 안에서만 쓰이고 서로 안
    겹치며 배치는 결정론(같은 입력 → 같은 자리). ★에필로그 컷은 `apply_fade`로 반투명해진다.

[2026-09-07] 레이아웃 v2 (사용자 지시):
  1) portrait 컷은 "앞을 봄(front)" 또는 "왼쪽→오른쪽 시선(right)" 중 하나로 뽑는다(comic_gen에서 강제).
     facing은 지금은 텍스트 위치가 아니라 **풍선 배치 쪽**을 정하는 데 쓰인다.
  2) portrait(face 클로즈업) 컷은:event(액션) 컷과 한 행에 붙을 때 **축소(기본 42% 폭)**되어
     일반 이벤트 신과 합쳐진다. 나머지 폭은 이벤트 신이 쓴다.

행(行) 문법
  - wide 컷 → 페이지 폭 전체 스플래시
  - (face, action) / (action, face) 페어 → 혼합 행: face 42% + action 58% (세로는 행 높이로 맞춘다)
  - 그 외 → 2열 균등. 짝 없는 마지막 컷은 2열 폭(레거시 규칙 유지)
  - [2026-09-09] 행 높이를 텍스트로 쓰지 않는다 — 텍스트는 전부 이미지 '안' 오버레이.
    행 안의 컷은 **행 높이로 함께** 그려진다(그림이 셀 높이로 끝나면 프레임 안에 흰 띠가 남는다).
    거터는 검정선 두 개가 맞붙는 폭(기본 6 = 2*lw)이라 컷 사이에 흰 실금이 생기지 않는다.

사용 예:
    import comic_page_merge as C
    texts = [{"narration": "深夜 2시, 옥상 물탱크가 끊기는 소리가 났다.",          # 설명
              "balloons": [{"kind": "speech",  "text": "무거운 건 저에게 맡기세요.",
                           "speaker": "other", "emo": "surprise"},                  # 상대방=오른쪽 위
                           {"kind": "thought", "text": "이 사람, 알고 있었다.",
                           "speaker": "me", "emo": "heart"}],                       # 주인공=왼쪽 위
              "sfx": "두근", "fade": 0.0}, ...]
    C.compose_pages(paths, texts, "comic/book001", "episode_03",
                    panels_per_page=5, panel_wide=[...], panel_face=[...],
                    panel_facing=["front"|"right", ...])
    # 옛 입력도 그대로 받는다: ["상황 묘사", "유즈키: 으…", "소타: 봐."] → 설명 + 말풍선 2개
"""
import math
import os
import re
from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------- 기본 파라미터
DEFAULT_CELL_W = 820          # 그리드 단위 폭 (px)
# [2026-09-07] 최종 페이지 크기 고정(1024x1454 세로): 행 높이를 남는 높이에 맞춰 스케일하고
#   컷은 fit_cover로 슬롯에 맞춰 자른다(잘림/여백 없음). page_size=None이면 옛 자동 크기.
DEFAULT_PAGE_W = 1024
DEFAULT_PAGE_H = 1454
DEFAULT_COLS = 2
DEFAULT_PANELS_PER_PAGE = 5
DEFAULT_GUTTER = 6            # 컷 사이 여백 [2026-09-09] 8→6: 검정선이 그림에 붙으므로
                              #   두 컷의 선이 만나 곧장 굵은 경계 하나가 된다(흰 빈틈 없음)
DEFAULT_BG = (255, 255, 255)      # 페이지 배경 = 흰색 프레임
DEFAULT_FRAME = (0, 0, 0)         # 그림 경계 검은 선
DEFAULT_FRAME_WIDTH = 3           # 굵은 검정선
DEFAULT_FRAME_PAD = 0             # [2026-09-09] 사용자 지시: 그림↔검은 선 사이 흰 여백 폐지(0)
                                  #   → 경계선과 컷 사이 빈틈이 없고 굵은 검정선만 보인다
DEFAULT_KEYLINE = None            # [2026-09-09] 옅은 회색 이중선 폐지 — 검정선만 남긴다
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
    # 대사 = Poor Story(부드러운 손글씨 · 화면 실측 문자를 전부 그린다) → Jua → Gaegu 순.
    #   [2026-09-09] Jua/Gaegu/NanumPen/Stylish 는 U+2026('…') 글리프가 없어 말풍선이 □로 깨졌다.
    #   Itim은 테스트 결과 한글 자체로 없어서 탈락(자세히는 selftest의 폰트 커버리지 검사).
    "dialog":    ("PoorStory-Regular.ttf", "Jua-Regular.ttf", "Gaegu-Regular.ttf",
                  "DoHyeon-Regular.ttf", "NotoSansKR-Bold.ttf", "NotoSansKR-Regular.ttf"),
    # 속마음 = Gaegu(손글씨·표정 있는 붓체) → Do Hyeon 순. 속마음은 글씨체가 제일 살아나는 자리다.
    "thought":   ("Gaegu-Regular.ttf", "Gaegu-Bold.ttf", "DoHyeon-Regular.ttf",
                  "NanumPenScript-Regular.ttf", "NotoSansKR-Regular.ttf", "NotoSansKR-Bold.ttf"),
    "sfx":       ("BlackHanSans-Regular.ttf", "Gaegu-Bold.ttf", "Jua-Regular.ttf",
                  "NotoSansKR-Bold.ttf", "NotoSansKR-Regular.ttf"),
}
font_role_paths = {}                     # role → 사용자 지정 경로(--font-narration 등)

# 화면 문법 상수
BALLOON_MAX = 3                          # 컷당 풍선 최대 3개(2026-09-11 사용자 지시 2→3)
                                           #   컷 스크립트 쪽 상한(comic_gen.DIALOG_LINES)이 이 값을 그대로 따른다
BALLOON_FONT_SIZE = 18                   # 말풍선/속마음 글자 — 풍선이 좁아졌으니 크게 둘 이유가 없다(2026-09-09)
NARR_LARGE_FONT_SIZE = 25                # 서두 요약/에필로그의 큰 지문(2026-09-11 사용자 지시 30→25)
SFX_FONT_SIZE = 54                       # 의성어 대형
NARR_LARGE_COVER = 0.70                  # 요약 지문이 컷 면적의 70%를 채운다(사용자 지시)
NARR_MAX_LINES = 99                      # [2026-09-09] 줄 수로 설명을 자르지 않는다(글자가 다 보여야 한다)
NARR_MAX_LINES_WITH_BALLOON = 99         #   대신 대사가 있는 컷은 **높이 비율**로 설명을 제한한다
NARR_BALLOON_H_RATIO = 0.55              #   대사가 있으면 설명 박스는 컷 높이의 55% 이내(나머지는 풍선)
# [2026-09-10] 사용자 지시: 풍선 폭을 **절반으로**(20%→10%) — 세로가 더 길어지고 얼굴을 덜 가린다.
#   (최소 폭 하한도 96→48px로 함께 내려, 하한이 비율을 삼키지 않게 한다)
# [2026-09-11] 사용자 지시: 10%는 세로를 지나치게 늘린다 → **15%로 완화**(풍선을 3개로 늘린 같은 조정)
BALLOON_W_RATIO = 0.25                   # 말풍선(직사각형) 폭 = 컷 폭의 25% [2026-09-15] 30→25
THOUGHT_W_RATIO = 0.25                   # 속마음(타원) 폭 = 컷 폭의 25% [2026-09-15] 30→25
# [2026-09-15] 사용자 지시: "대화창/속마음 창이 너무 크다 — 컷(!) 대비 25%".
#   폭만 줄여도 세로가 늘면 면적이 다시 커진다(실측: 몸통 보스트 2.2 × 최소 높이 42% → 컷의 절반).
#   그래서 **몸통 면적 ≤ 컷 면적 × 이 값**을 하나의 권위로 둔다(글자 수보다 이 상한이 먼저 이긴다).
BALLOON_MAX_AREA_RATIO = 0.25
#   [2026-09-14] 사용자 지시로 가로 확대(15%→30%) — portrait 컷에서 글자가 너무 좁게 접혔다.

# [2026-09-10] 말풍선·속마음 **이미지 은행** — 형태를 미리 그린 RGBA 자산으로 붙인다.
#  · 크기 변형이 아니라 **모양·분위기 변형**을 은행으로 둔다(크기는 9슬라이스가 처리한다).
#  · 자산은 몸통뿐이다 — 꼬리·생각 물방울은 정상 동작하지 않아 삭제됐다(사용자 지시).
#  · 자산이 없으면 지금의 벡터 그리기로 조용히 폴백한다(기본값도 vector).
BALLOON_ART_DIR_DEFAULT = os.path.join("data", "balloons")
BALLOON_ART_MANIFEST = "manifest.json"
BALLOON_ART_W, BALLOON_ART_H = 640, 560       # 자리표시 자산 제작 크기 (꼬리 자리를 아래에 둔다)
BALLOON_ART_MARGIN = 22                        # 모양이 캔버스 밖으로 나가지게 두는 최소 여백
BALLOON_TAIL_ROOM = 64                         # [2026-09-14] 꼬리가 박스 아래로 **튀어나올** 공간(몸통은 줄이지 않는다)
BALLOON_ART_SLICE = 18                         # 9슬라이스 절선 = 굽혀진 테투리 밴드 폭
BALLOON_SIZE_BOOST = 1.4                       # 몸통을 컷 폭 기준 이 배수로 키워 만든다(글자보다 작아짐 방지)
BALLOON_MIN_H_RATIO = 0.26                     # 몸통 최소 높이 = 컷 높이 × 이 값(보스트 반영)
BALLOON_ART_FIT = 0.88                         # 자산 몸통은 사각에 가까워 타원보다 넓게 쓴다
BALLOON_ART_SAFE = (34, 30, 34, 30)            # 글자 안전 여백 (l,t,r,b)
BALLOON_ART_PLATE_ALPHA = 255                  # [2026-09-14] 플레이트(내부)는 **pure white** (예전 210 반투명)
# [2026-09-13] 방향성 자산: 꼬리(대화)·물방울(생각)을 PNG에 **구워서**_runtime_에는 아무것도 안 그린다.
#   풍선을 놓는 자리가 정해졌으니(왼쪽 열/오른쪽 열) 꼬리 방향도 정해진다 → 자산 9종 × 2방향 = 18장.
BALLOON_TAIL_SUFFIX = ("_l", "_r")              # 파일명 접미사 = 꼬리가 나오는 쪽(_l = 왼쪽 아래)
BALLOON_TAIL_SHORT = 30                        # 꼬리 짧은 변(부착 변) 길이 — 이 변에는 검정 선을 안 그린다
BALLOON_TAIL_RATIO = 1.3                       # 긴 변 = 짧은 변 × 1.3 (사용자 지시 1:1.3)
BALLOON_TAIL_BAND = 96                         # 꼬리를 품는 모서리 패치(9슬라이스 절선을 이만큼 벌린다)
BALLOON_BAND_CAP = 0.45                        # 패치가 상자 대비 이 비율을 넘으면 자산 통째로 축소
BALLOON_TAIL_REACH = 0.82                      # 꼬리 끝이 컷 바깥으로 향하는 정도(자산 대비)
# (id, kind, 모양, moods) — id는 파일명이 된다(영문만)
BALLOON_ART_VARIANTS = (
    ("speech_plain", "speech", "round",  ""),
    ("speech_soft",  "speech", "wavy",   "gentle warm soft sad"),
    ("speech_sharp", "speech", "spiky",  "anger"),
    ("speech_shout", "speech", "star",   "surprise sparkle"),
    ("speech_flat",  "speech", "box",    "gloom question"),
    ("thought_cloud",  "thought", "cloud",  ""),
    ("thought_dreamy", "thought", "puff",   "heart sparkle"),
    ("thought_knot",   "thought", "scallop", "sweat question anger"),
    ("thought_void",   "thought", "thin",   "gloom"),
)
_balloon_style = "vector"          # vector | image  (run_comic가 set_balloon_style로 배선)
_balloon_art_dir = BALLOON_ART_DIR_DEFAULT
_balloon_cache = None              # manifest 로딩 1회


def set_balloon_style(style: str = None, art_dir: str = None):
    """'image'로 켜면 자산 은행을 쓰고, 자산이 없는 환경은 자동으로 vector로 돌아간다."""
    global _balloon_style, _balloon_art_dir, _balloon_cache
    if style:
        _balloon_style = str(style).strip().lower()
    if art_dir:
        _balloon_art_dir = str(art_dir)
        _balloon_cache = None
    return _balloon_style, _balloon_art_dir


def balloon_style() -> str:
    return _balloon_style


def _balloon_shapes_dir():
    return os.path.abspath(_balloon_art_dir)

THOUGHT_W_RELIEF = 0.45                  # 단, 세로가 아래 비율을 넘으면 폭을 이 정도까지 넓힌다(좁은 폭은 세로를 부른다)
#   [2026-09-14] 구제 폭은 기본 폭(30%)보다 **커야** 실제로 동작한다(예전 0.14 < 기본 0.15라 죽은 코드였다)

THOUGHT_H_CAP = 0.36                     # 속마음 세로가 컷 높이의 이 비율을 넘지 않게 한다
FONT_FLOOR = 11                          # 화면 글자의 최소 크기 — 이 아래로 안 줄인다
# [2026-09-19] 대화·속마음은 **한 줄로 넓게** 나가지 않게 합니다(사용자 지시). 말풍선은 폭이 컷의
#   25%까지인데 한 줄로 길면 글자를 FONT_FLOOR까지 줄여야 하고, 그러면 화면에서 안 보입니다.
#   그래서 한글 단어가 2개 이상이거나, 한글 1단어라도 5자 이상이면 반드시 두 줄 이상으로 눕힙니다.
_KO_SYL = re.compile(r"[\uac00-\ud7a3]")
_BALLOON_LAST: dict = {}        # 마지막 풍선 계산 결과(테스트·디버깅용): lines/fs/box_w/box_h/xy
ELLIPSE_FIT = 1.45                       # (폴백) 타원 ⇄ 사각 글자 블록의 대각 배율(√2≈1.414 + 안전)
BALLOON_PAD = 8                          # 풍선 안쪽 여백 [2026-09-10] 폭 절반(10%)에 맞춰 12→8 — 글자 자리를 남긴다
NARR_W_RATIO = 0.80                      # 설명 박스 폭 상한(컷 폭 대비) — 글자 수에 맞춰 다시 줄어든다
NARR_W_RATIO_WITH_BALLOON = 0.62         # 대사가 있으면 설명 폭 상한을 더 낮춘다(풍선 자리)
# [2026-09-09] 사용자 지시 2건: ★큰 지문은 글자가 다른 컷 대비 너무 크고, 박스가 컷의 절반만 써서 글자가 잘린다.
NARR_W_RATIO_LARGE = 1.00                # ★회차 도입·에필로그 지문은 **컷 폭을 다 쓴다**(짧은 글자도 박스를 당기지 않는다)
NARR_LARGE_FONT_RATIO = 1.25             # ★큰 지문의 글자 배율 (예전 1.5 → 지나치게 컸다)
EMOTIF_SIZE = 19                         # 감정 이모티콘 한 변 기본 크기
EMOTIF_KINDS = ("anger", "surprise", "sweat", "heart", "gloom", "sparkle", "question")
EMOTIF_COLORS = {                        # 감정마다 색을 다르게(사용자 지시)
    "anger": (198, 32, 40), "surprise": (240, 162, 2), "sweat": (46, 123, 214),
    "heart": (232, 64, 122), "gloom": (106, 106, 114), "sparkle": (245, 197, 24),
    "question": (46, 123, 214)}
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
    # [2026-09-09] Gaegu(개인 손글씨·아이 같고 표정 있는 붓체, OFL) — 속마음 1순위/대사·의성어 후보.
    #   3종(Light/Regular/Bold) 다 합치면 9MB가 넘어 Basic 2종만 받습니다(Light은 직접 넣어 쓰시면 됩니다).
    ("Gaegu-Regular.ttf",
     "https://github.com/google/fonts/raw/main/ofl/gaegu/Gaegu-Regular.ttf"),
    ("Gaegu-Bold.ttf", "https://github.com/google/fonts/raw/main/ofl/gaegu/Gaegu-Bold.ttf"),
    # [2026-09-09] 대사 1순위 Poor Story(OFL) — 말풍선 글자가 □로 깨지지 않는 것으로 실측 확인된 것만 쓴다.
    ("PoorStory-Regular.ttf",
     "https://github.com/google/fonts/raw/main/ofl/poorstory/PoorStory-Regular.ttf"),
    ("OFL-gowunbatang.txt", "https://raw.githubusercontent.com/google/fonts/main/ofl/gowunbatang/OFL.txt"),
    ("OFL-jua.txt", "https://raw.githubusercontent.com/google/fonts/main/ofl/jua/OFL.txt"),
    ("OFL-dohyeon.txt", "https://raw.githubusercontent.com/google/fonts/main/ofl/dohyeon/OFL.txt"),
    ("OFL-blackhansans.txt",
     "https://raw.githubusercontent.com/google/fonts/main/ofl/blackhansans/OFL.txt"),
    ("OFL-gaegu.txt", "https://raw.githubusercontent.com/google/fonts/main/ofl/gaegu/OFL.txt"),
    ("OFL-poorstory.txt",
     "https://raw.githubusercontent.com/google/fonts/main/ofl/poorstory/OFL.txt"),
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


# ── 글리프 커버리지 (2026-09-09) ────────────────────────────────────────────
# 만화 화면의 글씨는 LLM이 마음대로 섞는다 — '…'(U+2026), '♡', '★' 같은 기호가 대사 한복판에 들어온다.
# 한글 폰트는 이런 기호가 빠진 게 흔하다(Jua·Gaegu·NanumPen·Stylish 전부 U+2026 없음) → 말풍선이 □로 깨진다.
# 그래서 글자마다 "이 폰트가 그 글자를 그리는지" 보고, 못 그리면 그리는 폰트로 바꿔 그린다.
_NOTDEF = "\ue000"            # 어디에도 배정되지 않은 사영 문자 — 이걸과 화선이 같으면 "못 그린다"
_GLYPH_CACHE = {}
_FALLBACK_CACHE = {}


def _glyph_sig(font, ch: str) -> bytes:
    im = Image.new("L", (80, 48), 0)
    ImageDraw.Draw(im).text((4, 4), ch, font=font, fill=255)
    return im.tobytes()


def _font_id(f) -> str:
    return f"{getattr(f, 'path', '') or ''}@{getattr(f, 'size', 0)}"


def font_can_draw(font, ch: str) -> bool:
    """폰트가 그 글자를 그리는지 (토방 = .notdef 와 같은 화선이면 False)."""
    if not ch or not ch.strip():
        return True                      # 공백은 그려도 안 그려도 상관 없다
    key = (_font_id(font), ch)
    hit = _GLYPH_CACHE.get(key)
    if hit is None:
        hit = _glyph_sig(font, ch) != _glyph_sig(font, _NOTDEF)
        _GLYPH_CACHE[key] = hit
    return hit


def glyph_fallback(font, ch: str, role: str = None, font_path: str = None):
    """그 글자를 그릴 수 있는 다음 후보 폰트 (용도 후보 → 설명 폰트 → OS 폰트)."""
    size = int(getattr(font, "size", DEFAULT_FONT_SIZE) or DEFAULT_FONT_SIZE)
    key = (role or "", size, font_path or "", ch)
    hit = _FALLBACK_CACHE.get(key)
    if hit is None:
        hit = font
        tried = {_font_id(font)}
        for r in [role, "narration", "dialog", "sfx", "thought", None]:
            if not r or r in tried:
                continue
            tried.add(r)
            cand = load_font(size, font_path, role=r)
            if cand is not None and font_can_draw(cand, ch):
                hit = cand
                break
        _FALLBACK_CACHE[key] = hit
    return hit


def coverage_runs(text: str, font, role: str = None, font_path: str = None) -> list:
    """텍스트 → [(연속 조각, 그 조각을 그릴 폰트)] — 못 그리는 글자만 폴백으로 바꾼다."""
    runs, cur, curf = [], "", font
    for ch in str(text):
        f = font if font_can_draw(font, ch) else glyph_fallback(font, ch, role, font_path)
        if f is not curf and cur:
            runs.append((cur, curf))
            cur = ""
        cur, curf = cur + ch, f
    if cur:
        runs.append((cur, curf))
    return runs or [(str(text), font)]


def draw_text_runs(d, x: int, y: int, text: str, *, font, fill=DEFAULT_TEXT,
                   role: str = None, font_path: str = None, center_w: int = 0) -> float:
    """글자마다 그릴 수 있는 폰트로 바꿔 그린다(□ 토방 방지). 반환: 그린 폭."""
    text = str(text)
    if font_can_draw(font, text) if len(text) == 1 else all(font_can_draw(font, c) for c in text):
        if center_w:
            x = x - int(_run_w(text, font) / 2)
        d.text((x, y), text, font=font, fill=fill)
        return _run_w(text, font)
    runs = coverage_runs(text, font, role, font_path)
    total = sum(_run_w(c, f) for c, f in runs)
    cx = x - int(total / 2) if center_w else x
    for chunk, f in runs:
        d.text((int(cx), y), chunk, font=f, fill=fill)
        cx += _run_w(chunk, f)
    return total


def _run_w(chunk: str, font) -> float:
    try:
        return float(font.getlength(chunk))
    except Exception:
        return len(chunk) * float(getattr(font, "size", 20) or 20) * 0.6


def text_w(text: str, font, probe=None, role: str = None, font_path: str = None) -> float:
    """폭 잰도 폰트를 바꿔 그리므로 조각별로 합산한다(박스 크기가 실제 폭과 어긋나면 안 된다)."""
    text = str(text)
    if all(font_can_draw(font, c) for c in text):
        try:
            return float(probe.textlength(text, font=font)) if probe else float(font.getlength(text))
        except Exception:
            return len(text) * float(getattr(font, "size", 20) or 20) * 0.6
    return sum(_run_w(c, f) for c, f in coverage_runs(text, font, role, font_path))


def has_cjk_font() -> bool:
    return (any(os.path.exists(p) for p in FONT_CANDIDATES)
            or any(font_for_role(r) for r in BUNDLED_FONTS))


def wrap_text(text: str, font, max_width: int, draw, max_lines: int = MAX_CAPTION_LINES,
              keep_lines: bool = False):
    """텍스트를 max_width에 맞춰 줄바꿈(문자 단위 — 한국어 공백 없이도 동작).
    max_lines를 넘으면 마지막 줄을 … 로 잘린다.

    [2026-09-13] `keep_lines=True`면 원문의 **줄바꿈을 존중**한다(★프롤로그·에필로그 지문은
    여러 줄로 쓴다 — 사용자 지시). 일반 지문·풍선은 예전대로 개행을 공백으로扱는다.
    """
    text = (text or "").strip().replace("\r", "")
    if not text:
        return []
    try:
        max_lines = max(1, int(max_lines))
    except Exception:
        max_lines = MAX_CAPTION_LINES
    if keep_lines and "\n" in text:
        out = []
        for seg in text.split("\n"):
            if not seg.strip():
                out.append("")                      # 빈 줄 = 문단 사이 **줄 띄움** (렌더는 line_h만큼 쉰다)
                continue
            out.extend(wrap_text(seg, font, max_width, draw, max_lines, keep_lines=False))
        if len(out) > max_lines:
            out = out[:max_lines]
            out[-1] = (out[-1][:-1] if out[-1].endswith("…") else out[-1]) + "…"
        return out
    text = text.replace("\n", " ")
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


_SPEAKER_ALIASES = {"me": "me", "protagonist": "me", "mc": "me", "1": "me", "주인공": "me",
                    "other": "other", "partner": "other", "rival": "other", "2": "other",
                    "상대방": "other", "oppa": "other"}


def _norm_speaker(v) -> str:
    """풍선 화자 표기 → "me"(주인공) | "other"(상대방) | ""(모름)."""
    k = str(v or "").strip().lower()
    return _SPEAKER_ALIASES.get(k, "") if k else ""


def _norm_emo(v) -> str:
    """감정 표기 → EMOTIF_KINDS 중 하나 (모르면 "")."""
    k = str(v or "").strip().lower()
    return k if k in EMOTIF_KINDS else ""


def _balloon(kind, text, side=None, speaker="", emo="") -> dict:
    return {"kind": "thought" if str(kind).startswith("t") else "speech",
            "text": str(text).strip(), "side": side or None,
            "speaker": _norm_speaker(speaker), "emo": _norm_emo(emo)}


def text_payload(item) -> dict:
    """컷 화면 텍스트 입력(str/list/dict) → 화면 문법 페이로드로 정규화.

      {"narration": 설명(지문), "narr_large": 컷 70% 큰 지문(서두 요약/에필로그),
       "balloons": [{"kind":"speech|thought","text":…, "side":"left|right|None",
                     "speaker":"me(주인공)|other(상대방)|None", "emo":"anger|surprise|…"}] ≤BALLOON_MAX,
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
            out["balloons"].append(_balloon("speech", s))
        return out
    if not isinstance(item, dict):
        return out
    out["narration"] = str(item.get("narration") or item.get("caption_ko")
                           or item.get("caption") or "").strip()
    out["narr_large"] = bool(item.get("narr_large") or item.get("summary") or item.get("epilogue"))
    for b in list(item.get("balloons") or [])[:BALLOON_MAX]:
        if isinstance(b, str) and b.strip():
            out["balloons"].append(_balloon("speech", b.strip()))
        elif isinstance(b, dict):
            txt = str(b.get("text") or b.get("line") or "").strip()
            if txt:
                out["balloons"].append(_balloon(
                    b.get("kind") or "speech", txt,
                    str(b.get("side") or "").strip().lower() or None,
                    b.get("speaker") or b.get("who") or "", b.get("emo") or b.get("emotion") or ""))
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


def must_wrap(text: str) -> bool:
    """이 발화는 반드시 두 줄 이상으로 눕혀야 하나(한글 기준)."""
    toks = [t for t in re.split(r"[\s,.!?…~·\-]+", str(text or "")) if t]
    ko = [t for t in toks if _KO_SYL.search(t)]
    if len(ko) >= 2:
        return True
    if len(ko) == 1:
        return len(_KO_SYL.findall(ko[0])) >= 5
    return False


def _wrap_balloon_lines(text: str, font, avail_w: int, draw, cap_lines: int) -> list:
    """풍선 줄바꿈 — `must_wrap`이면 폭을 더 좁혀서라도 두 줄 이상으로 만듭니다(결정론)."""
    ls = wrap_text(text, font, max(24, int(avail_w)), draw, max_lines=cap_lines)
    if not ls or len(ls) >= 2 or not must_wrap(text):
        return ls
    for shrink in (0.85, 0.7, 0.55, 0.42):
        alt = wrap_text(text, font, max(20, int(int(avail_w) * shrink)), draw, max_lines=cap_lines)
        if alt and len(alt) >= 2:
            return alt
    mid = max(1, len(str(text)) // 2)          # 띄어쓰기 없는 한 단어는 음절 경계 절반에서 자른다
    a, b = str(text)[:mid].strip(), str(text)[mid:].strip()
    return [a, b] if a and b else ls


def _raise_up(xy, ix: int, iy: int, iw: int, ih: int, w: int, h: int, avoid=(), margin: int = 8):
    """x는 두고 **위까지** 올려봅니다 — 같은 열 안에서 풍선을 최대한 천장에 붙입니다."""
    x, y = xy
    top = iy + margin
    yy = top
    while yy < y:
        r = (x, yy, x + w, yy + h)
        if r[3] <= iy + ih and not any(_rect_hit(r, a) for a in (avoid or ())):
            return (x, yy)
        yy += 2
    return xy


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
        return _raise_up(xy, ix, iy, iw, ih, w, h, avoid=avoid, margin=margin)
    return None


# ---------------------------------------------------------------- [2026-09-09] 풍선 자리·감정 표시
def _balloon_slot_pref(balloon, facing: str = None):
    """(풍선 자리 순서, 꼬리 방향 'l'|'r') — [2026-09-13] 사용자 지시 4칙:

      · 대화·생각은 **오른쪽 위·오른쪽 중간·왼쪽 위·왼쪽 중간** 4칸에만 놓는다(아래 칸은 쓰지 않는다).
      · 주인공(me)   = 왼쪽 열(위 → 중간)      · 상대방(other) = 오른쪽 열(위 → 중간)
      · 1개이면 위부터. POV 컷에서도 상대방은 오른쪽(화자 규칙은 그림 위치와 무관하게 고정).
      · 꼬리는 자리에 **고정**으로 굽혀진다. [2026-09-15] 사용자 지시로 왼쪽 열은 **반대로**
        돌렸다 — 풍선이 컷 왼쪽에 있으면 인물이 그 오른쪽/아래에 서 있으므로 꼬리가
        오른쪽 아래(`_r`)를 향해야 화자를 가리킨다(예전 `_l`은 화자 반대편을 가리켰다).
        오른쪽 열은 실측으로 방향이 맞으므로 그대로 `_r`을 쓴다.
      · 화자 모름(레거시) : 예전 시선(facing) 규칙을 그대로 따른다.
    """
    sp = str((balloon or {}).get("speaker") or (balloon or {}).get("who") or "").strip().lower()
    sp = {"me": "me", "other": "other", "protagonist": "me", "partner": "other"}.get(sp, sp)
    if sp == "me":
        return ("tl", "ml"), "r"        # [2026-09-15] 왼쪽 열은 꼬리를 반대로(오른쪽 아래로)
    if sp == "other":
        return ("tr", "mr"), "r"
    side = str((balloon or {}).get("side") or facing or "").strip().lower()
    if side in ("", "left"):
        return ("tr", "mr"), "r"
    return ("tl", "ml"), "r"            # [2026-09-15] 왼쪽 열은 꼬리를 반대로


def _draw_emotif(d, x0: int, y0: int, x1: int, y1: int, ix: int, iy: int, iw: int, ih: int,
                 kind: str, *, size: int = EMOTIF_SIZE, font_path=None):
    """[2026-09-09] 감정 이모티콘 — 풍선 바깥 위 모서리에 **감정마다 다른 색**으로 그린다.

      anger(분노 X표) surprise(!) sweat(땀) heart(하트) gloom(음영선) sparkle(반짝) question(?)
    폰트 이모지와 달리 PIL 벡터로 그려서 폰트 설치와 무관하게 나오고 색도 자유롭게 바꾼다.
    → 그린 상자 (x0,y0,x1,y1) 또는 None
    """
    kind = _norm_emo(kind)
    if not kind:
        return None
    col = EMOTIF_COLORS[kind]
    r = max(7, int(size) // 2)
    lw = max(2, int(size) // 6)
    # 풍선 바깥쪽(컷 안쪽) 모서리에 붙이고, 컷 밖으로는 나가지 않는다
    out_right = (x0 + x1) / 2.0 < ix + iw / 2.0
    cx = min(x1 + r + 4, ix + iw - r - 2) if out_right else max(x0 - r - 4, ix + r + 2)
    cy = max(y0 + r, iy + r + 2)
    if kind == "anger":                      # 혈관 X표(💢) — 빨강
        for ang in (45, 135, 225, 315):
            rr = math.radians(ang)
            d.line([(cx, cy), (cx + r * math.cos(rr), cy + r * math.sin(rr))], fill=col, width=lw)
    elif kind == "surprise":                 # 느낌표 + 방사선 — 주황
        d.line([(cx, cy - r), (cx, cy + int(r * 0.35))], fill=col, width=lw)
        d.ellipse([cx - lw // 2, cy + r - lw, cx + lw - lw // 2, cy + r], fill=col)
        for ang in (150, 30):
            rr = math.radians(ang)
            d.line([(cx + r * 1.1 * math.cos(rr), cy - r * 1.1 * math.sin(rr)),
                    (cx + r * 1.5 * math.cos(rr), cy - r * 1.5 * math.sin(rr))], fill=col,
                   width=max(2, lw - 1))
    elif kind == "sweat":                    # 땀방울(💧) — 파랑
        d.polygon([(cx, cy - r), (cx - r * 0.8, cy + r * 0.2), (cx + r * 0.8, cy + r * 0.2)],
                  fill=col)
        d.ellipse([cx - r * 0.8, cy - r * 0.2, cx + r * 0.8, cy + r], fill=col)
    elif kind == "heart":                    # 하트(♥) — 분홍
        rr = r * 0.55
        d.ellipse([cx - rr * 2, cy - rr * 1.6, cx, cy], fill=col)
        d.ellipse([cx, cy - rr * 1.6, cx + rr * 2, cy], fill=col)
        d.polygon([(cx - rr * 1.9, cy - rr * 0.25), (cx + rr * 1.9, cy - rr * 0.25),
                   (cx, cy + r * 0.95)], fill=col)
    elif kind == "gloom":                    # 음영선 — 회색
        for i in range(4):
            gx = cx - r + i * (r * 2 // 3)
            d.line([(gx, cy - r), (gx - r // 3, cy + r)], fill=col, width=max(2, lw - 1))
    elif kind == "sparkle":                  # 반짝이 별 — 노랑
        pts = []
        for i in range(8):
            ang = math.radians(i * 45)
            rad = r if i % 2 == 0 else r * 0.34
            pts.append((cx + rad * math.cos(ang), cy + rad * math.sin(ang)))
        d.polygon(pts, fill=col)
    else:                                    # question — 파랑 물음표
        fnt = load_font(int(size * 1.5), font_path, role="dialog")
        try:
            d.text((cx - size * 0.4, cy - size * 0.75), "?", font=fnt, fill=col)
        except Exception:
            d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=col, width=2)
    pad = 2
    return (max(ix, cx - r - pad), max(iy, cy - r - pad), min(ix + iw, cx + r + pad),
            min(iy + ih, cy + r + pad))


# ---------------------------------------------------------------- 이미지 맞춤
# [2026-09-09] 컷 크롭을 '얼굴 중심'으로 — 사용자: "만화 컷에 얼굴이 많이 나오게"
#   자체 실측(image/ 원본 컷 150장, OpenCV 5 YuNet):
#     - 얼굴 검출률 36%(score>=0.5) — 애니메이션 얼굴은 학습 분포 밖이라 검출만 믿을 수 없다.
#     - 검출된 얼굴 중심의 세로 위치: 중앙값 32%, 10~90퍼센타일 16~57% (전신 컷은 더 위).
#     - 전폭 행(1008x755) 크롭에서 **현재(세로 가운데 자르기)는 얼굴이 통째로 남는 경우가 51%**에
#       지나지 않았고, 위에서 20% 지점부터 자르면 55%. 격자 탐색最优은 **위에서 8% 부근, 배율 1.0**
#       (=79%)였다 — 크게 자를수록 창이 좁아져 오히려 잘리므로 배율은 올리지 않는다.
FACE_CROP_ENABLE = True       # comic_gen이 config.comic_face_crop 값으로 덮어쓴다 (--no-face-crop)
FACE_H_TARGET = 0.40          # 검출 성공 시: 얼굴 높이를 컷 높이의 이 비율까지 당긴다(실측 1.35x에서 보존 97%)
FACE_ZOOM_MAX = 1.35          # 배율 상한 — 그 이상은 얼굴이 잘리기 시작해 이득이 준다고 실측됐다
FACE_CROP_TOP = 0.08          # 얼굴 검출 실패 시: 세로 여유의 시작점을 원본 위에서 8%로 잡는다
FACE_SLACK_MIN = 0.10         # 세로 여유가 원본 높이의 10% 이상일 때만 위 규칙을 적용(세로 컷은 그대로)
FACE_MODEL_URL = ("https://github.com/opencv/opencv_zoo/raw/main/models/"
                  "face_detection_yunet/face_detection_yunet_2023mar.onnx")
FACE_MODEL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "models",
                               "face_detection_yunet_2023mar.onnx")
FACE_SCORE_MIN = 0.60
_face_det_cache, _anchor_cache = {}, {}


def face_model_available() -> bool:
    return os.path.exists(FACE_MODEL_FILE)


def download_face_model(dest: str = None, timeout: int = 60, log=None) -> bool:
    """YuNet ONNX(227KB) 다운로드 — 없어도 크롭은 동작한다(추정치로 대체)."""
    dest = dest or FACE_MODEL_FILE
    if os.path.exists(dest):
        return True
    try:
        import urllib.request
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        urllib.request.urlretrieve(FACE_MODEL_URL, dest + ".part")
        os.replace(dest + ".part", dest)
        if log:
            log(f"  (받음) 얼굴 검출 모델 {os.path.getsize(dest) // 1024}KB → {dest}")
        return True
    except Exception as e:
        if log:
            log(f"  (실패) 얼굴 검출 모델: {e}")
        return False


def face_anchor(img: Image.Image):
    """cv2 + YuNet이 있을 때만 얼굴 박스를 돌려준다 (없으면 None → 추정치 사용).

    OpenCV 5에서는 Haar CascadeClassifier가 빠졌다 — FaceDetectorYN(YuNet)을 쓴다.
    """
    try:
        import cv2
    except Exception:
        return None
    if not face_model_available():
        return None
    w, h = img.size
    if not w or not h:
        return None
    key = (w, h)
    det = _face_det_cache.get(key)
    if det is None:
        try:
            det = cv2.FaceDetectorYN_create(FACE_MODEL_FILE, "", [w, h], FACE_SCORE_MIN, 0.3, 5000)
        except Exception:
            det = False
        _face_det_cache[key] = det
    if not det:
        return None
    try:
        import numpy as np
        arr = cv2.cvtColor(np.asarray(img.convert("RGB"), dtype=np.uint8), cv2.COLOR_RGB2BGR)
        _n, boxes = det.detect(arr)
    except Exception:
        return None
    if boxes is None or not len(boxes):
        return None
    b = max(boxes, key=lambda x: float(x[2]) * float(x[3]))      # 가장 큰 얼굴 = 화면의 주인공
    return ((float(b[0]) + float(b[2]) / 2) / w, (float(b[1]) + float(b[3]) / 2) / h,
            float(b[2]) / w, float(b[3]) / h)


def fit_cover(img: Image.Image, w: int, h: int, bias_x: float = 0.5, path: str = None,
              anchor: bool = True) -> Image.Image:
    """비율 유지 후 (w,h)를 정확히 채운다(크롭). bias_x=크롭 창 좌우 위치.

    [2026-09-09] 세로는 항상 가운데로 자르던 것을 얼굴 위치로 잡는다 — 세로 여유가 큰 컷(세로로 긴
    원본을 가로 컷에 넣는 경우)에서 얼굴이 잘려 나가는 일이 절반을 넘었다(실측 51%).
      ① 얼굴 검출 성공 → 얼굴이 컷 세로의 30% 부근에 오게 잡고, 얼굴 높이가 컷의 40%가 될 때까지
         배율을 올린다(상한 1.35x). 실측: 전폭 행 얼굴 보존 76%→100%, 얼굴 높이 36%→39%.
      ② 검출 실패/모듈 없음 → 원본 위에서 FACE_CROP_TOP(8%) 지점부터 자른다(배율 1.0 유지 —
         위치를 모른 채 당기면 오히려 잘린다). 실측: 전폭 행 51%→79%, 2단 전폭 39%→70%.
      ③ 세로 여지가 원본 높이의 10% 미만(세로 슬롯) → 예전처럼 가운데 자르기(보존 100%라 이득 없음).
    """
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
    # 세로 크롭 창 정하기 — 검출은 원본 비율 좌표에서 먼저 한다(배율과 무관하다)
    ax_ay = None
    if anchor and path:
        if path in _anchor_cache:
            ax_ay = _anchor_cache[path]
        else:
            ax_ay = face_anchor(img)
            _anchor_cache[path] = ax_ay
    base = scale
    slack_base = ih - h / base
    zoom = 1.0
    if ax_ay and slack_base > FACE_SLACK_MIN * ih:            # 얼굴을 안다 → 가까이 당겨도 된다
        fh_px = max(1.0, float(ax_ay[3]) * ih)
        zoom = min(FACE_ZOOM_MAX, max(1.0, (FACE_H_TARGET * h / base) / fh_px))
    if zoom != 1.0:
        scale = base * zoom
        nw, nh = max(1, int(iw * scale)), max(1, int(ih * scale))
        r = img.resize((nw, nh), Image.LANCZOS)
        left = int(round((nw - w) * bias))
    slack = nh - h
    top = slack // 2
    if anchor and slack > FACE_SLACK_MIN * nh and slack > 0:      # anchor=False는 예전 그대로(가운데)
        if ax_ay:                                             # 얼굴이 컷 세로의 30% 부근에 오게
            top = int(round(float(ax_ay[1]) * ih * scale - 0.30 * h))
        elif zoom == 1.0:                                     # 모를 때는 위에서 8%(격자 탐색 最优)
            top = int(round(FACE_CROP_TOP * ih * scale))
        top = max(0, min(slack, top))
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
               font_size: int, font_path: str = None, frame_width: int = DEFAULT_FRAME_WIDTH,
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
    inset = _frame_inset(pad, frame_width)        # 검은 선이 그림을 감싸는 폭
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
            ph = int(round((pw_cell - 2 * inset) * aspect)) + 2 * inset
            zone = zone_of(p_idx)
            blocks = caption_blocks(caps[p_idx])
            cell = {"idx": p_idx, "w": pw_cell, "h": ph, "zone": zone,
                    "face": bool(flags_f[p_idx]), "blocks": blocks, "cap_lines": 0,
                    "text": caps[p_idx],           # 화면 문법 페이로드 원본(렌더가 그대로 읽는다)
                    "font_size": int(font_size)}
            if zone == "right":
                img_w = pw_cell - 2 * inset
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
                                       pw_cell - 2 * inset - 32, probe, caption_lines)
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
def _frame_inset(pad, line) -> int:
    """그림이 셀 안에서 물리는 폭 — 검은 선 두께만큼은 무조건 물린다(선과 그림 사이 빈틈 없음)."""
    return max(int(pad or 0), max(1, int(line or 1)))


def _draw_framed_panel(canvas, d, x: int, y: int, w: int, h: int, img=None, *,
                       frame=DEFAULT_FRAME, pad=DEFAULT_FRAME_PAD, line=DEFAULT_FRAME_WIDTH,
                       bg=DEFAULT_BG, keyline=DEFAULT_KEYLINE):
    """[2026-09-09] 컷 프레임 — 굵은 검정선이 그림 경계에 **곧장** 붙는다(흰 여백 없음).

    PIL rectangle(outline, width)은 주어진 사각형 **바깥쪽에서 안쪽으로** 그려지므로,
    그림(ix..ix+iw-1)보다 정확히 lw 바깥에 사각형을 잡으면 선이 그림을 덮지 않고 빈틈도 없다.
    이웃한 컷과는 거터(=2*lw)만 두고이라 두 선이 맞붙어 굵은 경계 하나가 된다.
    → 그림 사각형 (ix,iy,iw,ih)
    """
    lw = max(1, int(line))
    inset = _frame_inset(pad, lw)
    ix, iy = x + inset, y + inset
    iw, ih = max(1, w - 2 * inset), max(1, h - 2 * inset)
    canvas.paste(img, (ix, iy)) if img is not None else d.rectangle(
        [ix, iy, ix + iw - 1, iy + ih - 1], fill=bg)
    d.rectangle([ix - lw, iy - lw, ix + iw + lw - 1, iy + ih + lw - 1],
                outline=frame, width=lw)
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
                      text_color=DEFAULT_TEXT, font_path=None, max_lines: int = 99,
                      narrow: bool = False):
    """[2026-09-09] 설명(지문) 박스 — 컷 **하단 왼쪽**, 흰 배경 + 검은 테두리 + 검은 글씨.

    크기는 글자 덩치에 맞추되 **글자가 전부 들어가야 한다**(사용자 지시: 설명이 잘리면 안 된다).
      · 박스 폭·높이는 실제 글자 폭/줄 수 만큼만 쓴다 (빈 공간으로 컷을 채우지 않는다)
      · 컷 안에 안 들어가면 줄 수를 늘리고, 그래도 모자라면 폰트를 13px까지 줄인다
      · 대사가 있는 이벤트 컷(narrow)은 폭 상한·줄 수를 더 낮춰 풍선 자리를 남긴다
      · large(★회차 도입·★에필로그)만 예외로 글자를 크게 쓰고 **컷 폭을 다 쓴다**(그래야 줄 수가 줄어 안 잘린다)
    → (x0, y0, x1, y1, font_size, 그은 줄 목록) 또는 None
    """
    text = str(text or "").strip()
    if not text or iw <= 40 or ih <= 40:
        return None
    margin = 8
    probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    # 폭 상한: 컷 폭의 일정 비율 이내 (대사가 있으면 더 좁게)
    w_cap = min(iw - 2 * margin,
                int(round(iw * (NARR_W_RATIO_WITH_BALLOON if narrow else NARR_W_RATIO))))
    if large:                                        # ★지문은 컷 폭 전체가 무대다(폭이 넓어야 줄 수가 줄어 잘리지 않는다)
        w_cap = min(iw - 2 * margin, int(round(iw * NARR_W_RATIO_LARGE)))
    # 높이 상한: 컷 안. large는 여기서도 70%를 넘기지 않는다.
    box_cap = ih - 2 * margin
    if narrow and not large:
        # 대사가 있는 이벤트 컷: 설명이 컷을 다 채우면 풍선 자리가 없다 → 높이로만 제한한다
        box_cap = min(box_cap, max(72, int(round(ih * NARR_BALLOON_H_RATIO))))
    if large:
        box_cap = min(box_cap, max(96, int(round(ih * NARR_LARGE_COVER))))
    fs_hi = max(16, int(font_size * NARR_LARGE_FONT_RATIO)) if large else int(font_size)
    lines_cap = max(1, min(int(max_lines),
                           NARR_MAX_LINES_WITH_BALLOON if narrow and not large else 10 ** 6))
    fs, lines, line_h, font = fs_hi, [], _text_line_height(fs_hi), None
    # [2026-09-09] '줄 수'로 먼저 자르면 안 된다 — wrap_text가 max_lines에서 '…'로 잘라 버린다.
    #   그래서 **전부를 접어 보고** 컷 높이(box_cap)에 안 들어가면 글자 크기를 한 단계씩 줄인다.
    for fs_try in range(fs_hi, FONT_FLOOR - 1, -1):       # 최악의 경우 폰트를 줄여 다 담는다(지금이도 충분히 크다)
        fnt = load_font(fs_try, font_path, role="narration")
        lh = _text_line_height(fs_try)
        ls = wrap_text(text, fnt, w_cap - 20, probe, max_lines=lines_cap,
                       keep_lines=bool(large))            # ★도입·에필로그는 여러 줄 허용
        if not ls:
            return None
        fs, lines, line_h, font = fs_try, ls, lh, fnt
        if len(ls) * line_h + 16 <= box_cap and not any(str(x).endswith("…") for x in ls):
            break                                          # 다 들어간다 — 이 크기로 쓴다
    if not lines:
        return None
    # 박스는 실제 글자 폭만큼만 (짧은 설명이 컷을 채우지 않는다)
    tw = 0.0
    for ln in lines:
        tw = max(tw, text_w(ln, font, probe, "narration", font_path))
    # 박스는 실제 글자 폭만큼만(짧은 설명이 컷을 채우지 않는다) — 단 ★지문은 컷 폭을 그대로 쓴다
    box_w = w_cap if large else int(max(72, min(w_cap, tw + 20)))
    box_h = int(min(box_cap, len(lines) * line_h + 16))   # 바닥(FONT_FLOOR)에서도 넘칠 때만 컷 높이로 잘린다
    x0 = ix + margin
    y1 = iy + ih - margin
    y0 = max(iy + margin, y1 - box_h)
    x1 = x0 + box_w
    d.rectangle([x0, y0, x1, y1], fill=plate)
    d.rectangle([x0, y0, x1 - 1, y1 - 1], outline=frame, width=max(1, int(line)))
    ty = y0 + 8
    for ln in lines:
        draw_text_runs(d, x0 + 10, ty, ln, font=font, fill=text_color,
                       role="narration", font_path=font_path)
        ty += line_h
    return x0, y0, x1, y1, fs, lines


def bh_ratio(th: float, bw: float, tw: float) -> float:
    """글자 블록(height th, width tw)을 폭 bw 타원에 넣을 때 필요한 세로의 '컷 대비 느낌값'.

    (tw/2)²/a² + (th/2)²/b² = 1 → b = (th/2)/√(1-k²). 크게 반환할수록 그 폭에서 세로가 넘친다.
    """
    a = max(1.0, bw / 2.0 - BALLOON_PAD * 0.35)
    k = min(0.90, (tw / 2.0) / a)
    return (th / max(0.40, (1.0 - k * k) ** 0.5)) / 1000.0


# ── 말풍선·속마음 자산 은행 (2026-09-10) ─────────────────────────────────────
def _superellipse_pts(cx, cy, a, b, n, k, amp, steps=280):
    """모양 만들기 — n이 크면 모서리 각진 사각형에 가까워지고(speech), 작으면 타원(thought).
    k·amp가 가장자리의 곱슬(구름·물결·뾰족·별)을 만든다."""
    import math as _m
    pts = []
    for i in range(steps):
        t = 2 * _m.pi * i / steps
        ct, st = _m.cos(t), _m.sin(t)
        r = 1.0 + amp * _m.sin(k * t)
        x = (abs(ct) ** (2.0 / n)) * (1 if ct >= 0 else -1) * a
        y = (abs(st) ** (2.0 / n)) * (1 if st >= 0 else -1) * b
        pts.append((cx + x * r, cy + y * r))
    return pts


_SHAPE_SPEC = {            # (n, k, amp) — 말풍선은 각진 기반 + 날선, 속마음은 둥근 기반
    "round":   (9.0, 0, 0.0), "box": (14.0, 0, 0.0),
    "wavy":    (6.0, 14, 0.020), "spiky": (7.0, 26, 0.040), "star": (5.0, 12, 0.075),
    "cloud":   (2.4, 9, 0.055), "puff": (2.4, 16, 0.034),
    "scallop": (2.3, 24, 0.024), "thin": (2.2, 0, 0.0),
}


def _bake_tail(layer, dl, W: int, H: int, bw: int, kind: str, side: str):
    """[2026-09-14] 몸통 테투리에 1:1.3 직각삼각형 꼬리를 **붙여** 굽는다. 꼬리 외곽 사각을 돌려준다.

    사용자 지시:
      · 짧은 변(부착 변)에는 검정 선을 그리지 않고, 그 변을 몸통 테투리에 붙인다
        → 테투리가 그 자리에서 끊기고 꼬리가 박스에서 **튀어나온** 느낌으로 이어진다.
      · 나머지 두 변에만 검정 선. 안쪽에 남은 줄이 보이지 않는다.
      · 속은 pure white.
    부착 지점의 테투리 y는 **알파에서 잰다**(구름·별·뾰족 등 모양이 제각각이라 수식은 안 맞는다).
    """
    al = layer.split()[3]

    def border_y(x):                       # 열 x에서 몸통 아래 테투리 바깥 끝 y
        for y in range(H - 1, int(H * 0.35), -1):
            if al.getpixel((x, y)) > 200:
                return y
        return int(H * 0.5)

    if kind == "thought":                  # 생각은 작은 원 2개 (테투리 바깥, 온전히 겉에 붙는다)
        box = [1e9, 1e9, -1e9, -1e9]
        for ux, uy, rr in ((BALLOON_ART_MARGIN + 34, H - BALLOON_ART_MARGIN - 30, 17.0),
                           (BALLOON_ART_MARGIN + 14, H - BALLOON_ART_MARGIN - 12, 9.0)):
            cxx = ux if side == "l" else (W - ux)
            dl.ellipse([cxx - rr - bw, uy - rr - bw, cxx + rr + bw, uy + rr + bw],
                       fill=tuple(DEFAULT_FRAME) + (255,))
            dl.ellipse([cxx - rr, uy - rr, cxx + rr, uy + rr],
                       fill=tuple(DEFAULT_PLATE) + (255,))
            box[0] = min(box[0], cxx - rr - bw); box[1] = min(box[1], uy - rr - bw)
            box[2] = max(box[2], cxx + rr + bw); box[3] = max(box[3], uy + rr + bw)
        return tuple(int(v) for v in box)

    L = float(BALLOON_TAIL_SHORT)
    xa = int(W * 0.19)
    xb = int(xa + L)
    ya, yb = border_y(xa), border_y(xb)
    A = [float(xa), float(ya) - bw * 1.1]              # 부착 변의 두 끝 = 테투리 **안쪽 가장자리**
    B = [float(xb), float(yb) - bw * 1.1]
    span = ((A[0] - B[0]) ** 2 + (A[1] - B[1]) ** 2) ** 0.5
    C = [A[0], A[1] + max(L, span * BALLOON_TAIL_RATIO)]   # 직각은 A, 긴 변은 A→C
    if side == "r":
        A, B, C = [[W - p[0], p[1]] for p in (A, B, C)]
    dl.polygon([tuple(A), tuple(B), tuple(C)], fill=tuple(DEFAULT_PLATE) + (255,))   # 속은 흰색
    # 검정 선은 바깥 두 변만 — 끝은 부착 변보다 살짝 아래에서 시작해 몸통 안에 점점이 남지 않는다
    def _trim(p, q, t):
        dx, dy = q[0] - p[0], q[1] - p[1]
        d = max(1e-6, (dx * dx + dy * dy) ** 0.5)
        return [p[0] + dx / d * t, p[1] + dy / d * t]
    dl.line([_trim(A, C, bw * 1.2), tuple(C), _trim(B, C, bw * 1.2)],
            fill=tuple(DEFAULT_FRAME) + (255,), width=bw, joint="curve")
    xs = [p[0] for p in (A, B, C)]
    ys = [p[1] for p in (A, B, C)]
    return (int(min(xs) - bw), int(min(ys) - bw), int(max(xs) + bw), int(max(ys) + bw))


def generate_balloon_set(dest: str = None, force: bool = False) -> dict:
    """data/balloons/에 자리표시 자산 9종 + manifest.json을 만든다(코드로 그림).

    실제 작화 자산을 같은 파일명·같은 스펙으로 덮어넣으면 코드 수정이 필요 없다.
    [2026-09-10] 꼬리·물방울 자산은 기능 폐지와 함께 만들지 않는다(몸통 9종뿐).
    """
    import json as _json
    d = os.path.abspath(dest or _balloon_art_dir)
    os.makedirs(d, exist_ok=True)
    man = {"plate_alpha": BALLOON_ART_PLATE_ALPHA, "assets": {}}
    made = []
    for vid, kind, shape, moods in BALLOON_ART_VARIANTS:
        _sfx_list = ("",) + BALLOON_TAIL_SUFFIX        # 무방향 9장 + 방향 18장
        for sfx in _sfx_list:
            _tail = sfx.lstrip("_")                     # '' | 'l' | 'r'
            path = os.path.join(d, vid + sfx + ".png")
            _sl = [BALLOON_ART_SLICE] * 4
            if _tail == "l":
                _sl[0] = _sl[3] = max(BALLOON_ART_SLICE, BALLOON_TAIL_BAND)
            elif _tail == "r":
                _sl[2] = _sl[3] = max(BALLOON_ART_SLICE, BALLOON_TAIL_BAND)
            man["assets"][vid + sfx] = {"kind": kind, "file": vid + sfx + ".png", "moods": moods,
                                        "tail": _tail,
                                        "slice": _sl,                       # 테투리 밴드 폭(9슬라이스 절선)
                                        "safe": [x + 8 for x in _sl],       # 글자 안전 여백
                                        "border": 7, "alpha": BALLOON_ART_PLATE_ALPHA}
        vid_key = vid
        man["assets"][vid_key]["dirs"] = {s.lstrip("_"): vid + s + ".png"
                                          for s in BALLOON_TAIL_SUFFIX}
        for sfx in _sfx_list:
            _tail = sfx.lstrip("_")
            path = os.path.join(d, vid + sfx + ".png")
            if os.path.exists(path) and not force:
                made.append(vid + sfx)
                continue
            layer = Image.new("RGBA", (BALLOON_ART_W, BALLOON_ART_H), (0, 0, 0, 0))
            dl = ImageDraw.Draw(layer)
            n, k, amp = _SHAPE_SPEC.get(shape, (3.0, 0, 0.0))
            cx, cy = BALLOON_ART_W / 2.0, BALLOON_ART_H / 2.0
            a = BALLOON_ART_W / 2.0 - BALLOON_ART_MARGIN
            b = BALLOON_ART_H / 2.0 - BALLOON_ART_MARGIN - BALLOON_TAIL_ROOM   # 아래는 꼬리 자리
            bw = 7 if shape != "thin" else 3
            # 테투리(불투명) → 내부(반투명) 순서로 겹쳐 그린다(테투리 두께를 자산에 굽는다)
            dl.polygon(_superellipse_pts(cx, cy, a, b, n, k, amp),
                       fill=tuple(DEFAULT_FRAME) + (255,))
            inn = max(0.02, bw * 2.0 / min(2 * a, 2 * b))
            dl.polygon(_superellipse_pts(cx, cy, a * (1 - inn), b * (1 - inn), n, k, amp),
                       fill=tuple(DEFAULT_PLATE) + (BALLOON_ART_PLATE_ALPHA,))
            if _tail:
                _tb = _bake_tail(layer, dl, BALLOON_ART_W, BALLOON_ART_H, bw,
                                 "thought" if kind == "thought" else "speech", _tail)
                man["assets"][vid + sfx]["tail_bbox"] = [int(v) for v in _tb]
            layer.save(path)
            made.append(vid + sfx)
    try:
        with open(os.path.join(d, BALLOON_ART_MANIFEST), "w", encoding="utf-8") as f:
            _json.dump(man, f, ensure_ascii=False, indent=1)
    except Exception:
        pass
    global _balloon_cache
    _balloon_cache = None
    return {"dir": d, "made": made, "manifest": os.path.join(d, BALLOON_ART_MANIFEST)}


def _auto_safe(img, thresh=200):
    """자산에서 **플레이트가 실제로 덮인** 최대 중앙 사각지의 여백 (l,t,r,b).

    모양(별·뾰족·구름)은 모서리가 파여 있어 정해진 여백 하나로 안전영역을 말할 수 없다.
    로딩 때 한 번만 이진탐색으로 재고, 이후 텍스트 정렬은 이 값을 쓴다.
    """
    try:
        w, h = img.size
        al = img.split()[3]
        hi = max(2, int(0.30 * min(w, h)))          # 너무 파인 모양은 30%까지만 양보하지 않는다
        covered = lambda d: al.crop((d, d, w - d, h - d)).getextrema()[0] >= thresh
        if covered(1):
            return 1, 1, 1, 1
        if not covered(hi):                          # 30%까지 물러나도 안 덮이는 모양
            return hi, hi, hi, hi
        lo = 1
        while lo + 1 < hi:                          # '덮이는 최소 여백'을 찾는다(여백이 클수록 잘 덮인다)
            mid = (lo + hi) // 2
            if covered(mid):
                hi = mid
            else:
                lo = mid
        return hi, hi, hi, hi
    except Exception:
        return tuple(BALLOON_ART_SAFE)


def _art_use_whole(art_size, box_w: int, box_h: int, slice_px) -> bool:
    """[2026-09-13] 방향 자산은 모서리 패치(꼬리 자리)가 크다. 붙일 상자가 패치에 비해
    작으면 9슬라이스가 오히려 글자 자리를 없앤다 → 그럴 때는 **자산 통째로 축소**가 최선이다.
    합성(`paste_balloon_art`)과 안전여백(`_balloon_art_safe`)이 같은 판단을 공유한다."""
    try:
        aw, ah = art_size
        l, t, r, b = [max(1, int(x)) for x in slice_px]
    except Exception:
        return False
    if box_w <= 0 or box_h <= 0:
        return False
    if box_w < l + r + 2 or box_h < t + b + 2:
        return True
    return (l + r) > BALLOON_BAND_CAP * box_w or (t + b) > BALLOON_BAND_CAP * box_h


def _balloon_art_safe(variant: str, out_w: int = 0, out_h: int = 0, tail: str = ""):
    """자산의 글자 안전 여백 (l,t,r,b) — **9슬라이스로 붙였을 때의 실제 여백**으로 돌려준다.

    자산에서 잰 여백을 그대로 쓰면 틀린다: 9슬라이스는 모서리·가장자리 스트립을 그대로 두고
    가운데만 늘리므로, 몸통이 자산보다 작아지면(실제 대부분의 컷) 여백의 상대 비중이 달라진다.
    out_w/out_h를 주면 그 크기로 붙였을 때의 여백을 계산한다.
    """
    a = (_balloon_bank().get(variant or "") or {})
    if tail in ("l", "r") and (a.get("dirs") or {}).get(tail):
        a = a["dirs"][tail]              # 방향 자산은 절선·여백이 비대칭이다
    safe = a.get("safe") or list(BALLOON_ART_SAFE)
    slc = a.get("slice") or [BALLOON_ART_SLICE] * 4
    try:
        safe = [int(x) for x in safe]
        slc = [max(1, int(x)) for x in slc]
        l, t, r, b = (safe + [0, 0, 0, 0])[:4]
        cl, ct, cr, cb = (slc + [1, 1, 1, 1])[:4]
    except Exception:
        return tuple(BALLOON_ART_SAFE)
    if out_w and out_h and a.get("img") is not None:
        try:
            aw, ah = a["img"].size
            if _art_use_whole((aw, ah), int(out_w), int(out_h), slc):
                # 통째로 축소하는 경우: 여백도 그 배율 그대로다
                fx, fy = int(out_w) / float(max(1, aw)), int(out_h) / float(max(1, ah))
                return (max(0, int(l * fx)), max(0, int(t * fy)),
                        max(0, int(r * fx)), max(0, int(b * fy)))
            sx = max(0.02, (int(out_w) - cl - cr) / float(max(1, aw - cl - cr)))
            sy = max(0.02, (int(out_h) - ct - cb) / float(max(1, ah - ct - cb)))
            l = int(cl + max(0, l - cl) * sx)
            r = int(cr + max(0, r - cr) * sx)
            t = int(ct + max(0, t - ct) * sy)
            b = int(cb + max(0, b - cb) * sy)
        except Exception:
            pass
    return max(0, l), max(0, t), max(0, r), max(0, b)


def _balloon_bank() -> dict:
    """manifest + PNG를 한 번만 읽어 {id: {"img": RGBA, ...}}로 cache한다."""
    global _balloon_cache
    if _balloon_cache is not None:
        return _balloon_cache
    out = {}
    try:
        import json as _json
        d = _balloon_shapes_dir()
        mp = os.path.join(d, BALLOON_ART_MANIFEST)
        if os.path.exists(mp):
            man = _json.load(open(mp, encoding="utf-8")) or {}
            for vid, spec in (man.get("assets") or {}).items():
                fp = os.path.join(d, str(spec.get("file") or (vid + ".png")))
                if not os.path.exists(fp):
                    continue
                try:
                    img = Image.open(fp).convert("RGBA")
                    bb = img.getbbox()                     # [2026-09-10] 투명 여백을 버린다 —
                    if bb and bb[2] - bb[0] > 8 and bb[3] - bb[1] > 8:
                        img = img.crop(bb)                 # 여백까지 박스에 늘리면 플레이트가
                    #   글자보다 작아져 글자가 몸통 밖으로 넘친다(사용자 실측 불만).
                except Exception:
                    continue
                sl = [int(x) for x in (spec.get("slice") or [BALLOON_ART_SLICE] * 4)]
                # 안전여백은 manifest 값이 아니라 **자산의 실제 알파**에서 계산한다(모양이 제각각)
                sf = list(_auto_safe(img))
                tail = str(spec.get("tail") or "").strip().lower()
                tb = spec.get("tail_bbox")
                if tail in ("l", "r") and tb and len(tb) == 4:
                    # 절선은 꼬리 외곽사각으로 **정확히** 벌린다(자른 좌표를 따라간다).
                    #   벌이지 않으면 몸통을 늘릴 때 꼬리가 같이 늘어나 글자 자리가 빈다.
                    cb0, cb1 = (bb[0], bb[1]) if bb else (0, 0)
                    sl = [BALLOON_ART_SLICE] * 4
                    if tail == "l":
                        sl[0] = max(sl[0], int(tb[2] - cb0) + 2)
                    else:
                        sl[2] = max(sl[2], int(img.size[0] - (tb[0] - cb0)) + 2)
                    sl[3] = max(sl[3], int(img.size[1] - (tb[1] - cb1)) + 2)
                    # [2026-09-14] 자산을 **포토샵으로 손질**하면 manifest의 tail_bbox가 낡는다.
                    #   아래 절선만이라도 실제 알파로 재계산해 덮어쓴다(꼬리만 튀어나온 픽셀 수).
                    try:
                        al = img.split()[3]
                        w_, h_ = img.size
                        half = max(1, w_ // 2)
                        side_cols = range(0, half) if tail == "l" else range(half, w_)
                        other_cols = range(half, w_) if tail == "l" else range(0, half)

                        def _last_row(cols):
                            cs = list(cols)[::3] or [0]
                            for yy in range(h_ - 1, -1, -1):
                                if any(al.getpixel((xx, yy)) > 200 for xx in cs if xx < w_):
                                    return yy
                            return 0
                        pro = _last_row(side_cols) - _last_row(other_cols)
                        if pro > 0:
                            sl[3] = max(sl[3], min(h_ - 2, pro + BALLOON_ART_SLICE))
                    except Exception:
                        pass
                if tail in ("l", "r") and str(vid).endswith("_" + tail):
                    # 방향 자산은 별도 변형이 아니라 **본 체형의 방향 한 벌**로 붙인다
                    base = str(vid)[: -(len(tail) + 1)]
                    if base in out:
                        out[base].setdefault("dirs", {})[tail] = {"img": img, "slice": sl, "safe": sf}
                        continue
                out[str(vid)] = {"img": img, "kind": str(spec.get("kind") or "").lower(),
                                 "slice": sl, "safe": sf,
                                 "alpha": int(spec.get("alpha") or BALLOON_ART_PLATE_ALPHA),
                                 "moods": str(spec.get("moods") or "")}
    except Exception:
        out = {}
    _balloon_cache = out
    return out


def pick_balloon_variant(kind: str, emo: str = "", ix: int = 0, iy: int = 0,
                         idx: int = 0, used=None) -> str:
    """감정 → 결정론 회전 순으로 변형을 고른다 (같은 원고 → 같은 풍선).

    used: 같은 페이지에서 이미 쓴 id 리스트 — 2회 사용은 은행이 1종일 때만 허용한다.
    """
    bank = _balloon_bank()
    pool = [vid for vid, m in bank.items() if m["kind"] == kind]
    if not pool:
        return ""
    pool.sort()
    used = used if used is not None else []
    mood = str(emo or "").strip().lower()
    if mood:
        for vid in pool:
            if mood in bank[vid]["moods"].split():
                if vid not in used or len(pool) == 1:
                    return vid
    key = (int(ix) // 97, int(iy) // 97, int(idx), len(used))
    for off in range(len(pool) * 2):
        vid = pool[(key[0] + key[1] * 3 + key[2] * 7 + off) % len(pool)]
        if vid not in used or len(pool) == 1:
            return vid
    return pool[0]


def _nine_slice(dst, art: "Image.Image", box, slice_px):
    """9슬라이스 확대 — 모서리·가장자리는 픽셀 유지, 가운데만 늘린다(테투리 두께 일정)."""
    l, t, r, b = [max(1, int(x)) for x in slice_px]
    x0, y0, x1, y1 = [int(v) for v in box]
    w, h = max(1, x1 - x0), max(1, y1 - y0)
    aw, ah = art.size
    l, t = min(l, max(1, aw // 2 - 1)), min(t, max(1, ah // 2 - 1))
    r, b = min(r, max(1, aw // 2 - 1)), min(b, max(1, ah // 2 - 1))
    if w < l + r + 2 or h < t + b + 2:          # 너무 작으면 자산 전체를 축소가 최선
        whole = art.resize((max(2, w), max(2, h)), Image.LANCZOS)
        dst.alpha_composite(whole, (x0, y0))
        return
    cw, ch = max(1, aw - l - r), max(1, ah - t - b)
    mw, mh = max(1, w - l - r), max(1, h - t - b)
    cen = art.crop((l, t, l + cw, t + ch)).resize((mw, mh), Image.LANCZOS)
    dst.alpha_composite(art.crop((0, 0, l, t)), (x0, y0))
    dst.alpha_composite(art.crop((aw - r, 0, aw, t)), (x0 + w - r, y0))
    dst.alpha_composite(art.crop((0, ah - b, l, ah)), (x0, y0 + h - b))
    dst.alpha_composite(art.crop((aw - r, ah - b, aw, ah)), (x0 + w - r, y0 + h - b))
    dst.alpha_composite(art.crop((l, 0, l + cw, t)).resize((mw, t), Image.LANCZOS), (x0 + l, y0))
    dst.alpha_composite(art.crop((l, ah - b, l + cw, ah)).resize((mw, b), Image.LANCZOS), (x0 + l, y0 + h - b))
    dst.alpha_composite(art.crop((0, t, l, t + ch)).resize((l, mh), Image.LANCZOS), (x0, y0 + t))
    dst.alpha_composite(art.crop((aw - r, t, aw, t + ch)).resize((r, mh), Image.LANCZOS), (x0 + w - r, y0 + t))
    dst.alpha_composite(cen, (x0 + l, y0 + t))


def paste_balloon_art(canvas, variant: str, region, box, flip: bool = False, tail: str = ""):
    """컷 영역 RGBA 레이어에 몸통(9슬라이스)을 합성해 한 번에 붙인다.

    텍스트는 이 함수가 끝난 **뒤**에 그린다(그렇지 않으면 글자까지 반투명해진다).
    성공 시 사용 변형 id, 실패(자산 없음) 시 None → 호출자가 벡터로 그린다.
    [2026-09-13] `tail`('l'/'r')을 주면 꼬리·물방울이 **구워진** 방향 자산을 쓴다.
      방향 자산이 없는 환경(옛 자산 9장뿐)에서는 아래처럼 좌우 반전으로 견딘다.
    """
    bank = _balloon_bank()
    a = bank.get(variant or "")
    if canvas is None or not a:
        return None
    use = (a.get("dirs") or {}).get(tail) if tail in ("l", "r") else None
    art = (use or a)["img"]
    slc = (use or a)["slice"]
    if use is None and flip:
        art = art.transpose(Image.FLIP_LEFT_RIGHT)
        slc = [slc[2], slc[1], slc[0], slc[3]]
    ix, iy, iw, ih = [int(v) for v in region]
    x0, y0, x1, y1 = [int(v) for v in box]
    if iw < 24 or ih < 24 or x1 - x0 < 24 or y1 - y0 < 24:
        return None
    try:
        layer = Image.new("RGBA", (iw, ih), (0, 0, 0, 0))
        if _art_use_whole(art.size, x1 - x0, y1 - y0, slc):
            # 패치가 상자를 다 먹는다 — 통째로 축소해 **그 자리에** 붙인다(꼬리 비율도 같이 줄어든다)
            layer.alpha_composite(art.resize((max(2, x1 - x0), max(2, y1 - y0)), Image.LANCZOS),
                                  (x0 - ix, y0 - iy))
        else:
            _nine_slice(layer, art, (x0 - ix, y0 - iy, x1 - ix, y1 - iy), slc)
        canvas.paste(layer, (ix, iy), layer)
        return variant
    except Exception:
        return None


def _vector_tail(d, kind: str, x0: int, y0: int, x1: int, y1: int, column: str, iy: int, ih: int,
                 fs: int, *, plate, frame, line: int):
    """벡터로 그리는 풍선의 꼬리 — 말풍선은 삼각형, 속마음은 생각 물방울.

    자산 모드(`--balloon-style image`)에는 꼬리가 굽혀 있지만 벡터 모드에는 없어 화자를 가리키는
    표지가 없었습니다(사용자 지시). 방향 규칙은 자산과 같습니다 — 꼬리는 **화자(컷 가운데) 쪽으로
    아래로** 향합니다. 열(`column`: 주인공='l' · 상대방='r') 안쪽 아래로 내밀고, 컷 바닥을 넘으면
    꼬리를 짧게 줄입니다. 확장된 상자(꼬리 포함)를 돌려줍니다(겹침 검사·이모티콘 자리에 쓰입니다).
    """
    bw = max(1, x1 - x0)
    room = max(6, (iy + ih - 2) - y1)                       # 컷 안에서 꼬리에 허락된 세로
    inner = (column != "r")                                 # 주인공 열(왼쪽)은 안쪽이 오른쪽
    if kind == "speech":
        w = max(10, min(int(bw * 0.24), 28))
        inset = max(3, int(bw * 0.06))
        bx0, bx1 = (x1 - inset - w, x1 - inset) if inner else (x0 + inset, x0 + inset + w)
        tl = max(8, min(int(fs * 1.15), 24, room))
        ax = (bx0 + bx1) // 2 + (max(3, int(bw * 0.10)) if inner else -max(3, int(bw * 0.10)))
        ay = y1 + tl
        d.polygon([(bx0, y1), (bx1, y1), (ax, ay)], fill=plate, outline=frame,
                  width=max(1, int(line)))
        d.line([(bx0, y1), (bx1, y1)], fill=frame, width=max(1, int(line)))
        return (x0, y0, x1, ay)
    r = max(4, int(fs * 0.34))
    cx = (x1 - max(r * 2, int(bw * 0.20)) - 4) if inner else (x0 + max(r * 2, int(bw * 0.20)) + 4)
    yy = y1 + max(3, r // 2)
    out = (x0, y0, x1, y1)
    for i, rad in enumerate((r, max(3, r - 2), max(2, r - 4))[:2]):
        cy = yy + rad + i * (rad * 2 + 2)
        if cy + rad > iy + ih - 2:
            break
        d.ellipse([cx - rad, cy - rad, cx + rad, cy + rad], fill=plate, outline=frame,
                  width=max(1, int(line)))
        out = (min(out[0], cx - rad), out[1], max(out[2], cx + rad), max(out[3], cy + rad))
    return out


def _draw_balloon(d, ix: int, iy: int, iw: int, ih: int, balloon, *, avoid=(),
                  plate=DEFAULT_PLATE, frame=DEFAULT_FRAME, line: int = DEFAULT_FRAME_WIDTH,
                  text_color=DEFAULT_TEXT, font_size: int = BALLOON_FONT_SIZE, font_path=None,
                  facing: str = None, canvas=None, bank_used=None, idx: int = 0):
    """[2026-09-09] 말풍선(speech) / 속마음 풍선(thought).

    speech  : **직사각형** — 폭은 컷의 BALLOON_W_RATIO, 글자는 그 폭에 맞춰 접고 세로로 늘린다(얼굴 가림 방지)
    thought : **타원** — 폭은 컷의 THOUGHT_W_RATIO, 세로는 그 폭에 글자를 넣는 데 필요한 만큼만
    [2026-09-10] 꼬리(화살표)·생각 물방울(작은 원)은 정상 동작하지 않아 삭제 — 몸체만 그린다.
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
    prefer, _dkey = _balloon_slot_pref(balloon, facing)   # _dkey = 꼬리 방향('l'|'r') — 자리 4칸 제한과 짝이다
    emo = _norm_emo((balloon or {}).get("emo"))
    mg = 8
    probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    box_w = box_h = 0
    lines, font, fs, line_h = [], None, font_size, _text_line_height(font_size)
    # [2026-09-10] 사용자 지시: 풍선은 **가로로 좁게 · 세로로 길게**(20%→10%). 글자를 그 폭에 맞춰 접는다.
    #   [2026-09-11] 10%는 세로를 너무 늘려 BALLOON_W_RATIO/THOUGHT_W_RATIO = 15%로 완화(사용자 지시).
    #   (예전은 글자 폭에 맞춰 가로로 넓어지고 4줄에서 접혀, 얼굴을 덮는 넓은 풍선이 나왔다)
    pad = BALLOON_PAD
    wr = THOUGHT_W_RATIO if kind == "thought" else BALLOON_W_RATIO
    # [2026-09-10] 이미지 모드에서는 **변형을 먼저** 고르고 그 자산의 글자 안전여백으로
    #   박스를 잰다. 예전은 일반 패드(8px)로만 재서, 자산의 테투리·곡선 안쪽에 글자가
    #   못 들어가고 몸통이 글자보다 작아졌다(사용자 실측 불만: "글씨보다 작아서 안 보인다").
    art_id = pick_balloon_variant(kind, emo, ix, iy, idx, bank_used) \
        if (_balloon_style == "image" and canvas is not None) else ""
    # 붙일 크기를 먼저 어림잡고, 그 크기로 9슬라이스했을 때의 안전여백을 계산한다
    _est_w = max(48, int(iw * wr * (BALLOON_SIZE_BOOST if art_id else 1.0)))
    _est_h = int(min(max(iw, ih) - 16, max(ih * BALLOON_MIN_H_RATIO, 120))) if art_id else 120
    _sl, _st, _sr, _sb = _balloon_art_safe(art_id, _est_w, _est_h) if art_id else (0, 0, 0, 0)
    pad_h = max(pad, _sl, _sr) if art_id else pad
    pad_v = max(pad, _st, _sb) if art_id else pad
    bst = float(BALLOON_SIZE_BOOST) if art_id else 1.0
    fit = BALLOON_ART_FIT if art_id else ELLIPSE_FIT
    for fs_try in range(int(font_size), FONT_FLOOR - 1, -2):     # 글자가 많으면 폰트를 줄여 담는다
        fnt = load_font(fs_try, font_path, role=role)
        lh = _text_line_height(fs_try)
        bw_target = max(48, int(iw * wr * bst))
        avail_w = int(bw_target / (fit if kind == "thought" else 1.0)) - 2 * pad_h
        cap_lines = max(2, int((ih - 2 * pad_v - 12) // lh))          # 줄 수 상한은 컷 높이에서 계산한다
        ls = _wrap_balloon_lines(text, fnt, max(24, avail_w), probe, cap_lines)
        if not ls:
            return None
        tw = 0
        for ln in ls:
            try:
                tw = max(tw, probe.textlength(ln, font=fnt))
            except Exception:
                tw = max(tw, len(ln) * fs_try * 0.6)
        th = len(ls) * lh
        if kind == "thought":
            bw = int(min(max(bw_target, tw + 2 * pad_h), iw - 16))
            # [2026-09-10] 좁은 폭은 세로를 부른다 — 세로가 컷 높이의 THOUGHT_H_CAP을 넘으면
            #   폭을 THOUGHT_W_RELIEF까지 넓혀 줄 수를 줄인다(글자를 버리는 대신 폭을 쓴다).
            if bh_ratio(th, bw, tw) > THOUGHT_H_CAP and bw_target < iw * THOUGHT_W_RELIEF:
                bw2 = int(min(iw * THOUGHT_W_RELIEF, iw - 16))
                ls2 = wrap_text(text, fnt, max(24, int(bw2 / fit) - 2 * pad_h), probe, max_lines=cap_lines)
                if ls2 and len(ls2) <= len(ls):
                    th2 = len(ls2) * lh
                    if bh_ratio(th2, bw2, tw) <= THOUGHT_H_CAP or bh_ratio(th2, bw2, tw) < bh_ratio(th, bw, tw):
                        ls, th, bw = ls2, th2, bw2
            # 폭을 정해 두고 타원에 글자 블록을 넣으면: (w/2)²/a² + (h/2)²/b² = 1 → b는 여기서 나온다.
            #   폭이 좁을수록 세로가 는다 — 그래서 세로를 '필요한 만큼만' 쓰는 것이 이 계산의 전부다.
            a = max(1.0, bw / 2.0 - pad_h * 0.35)
            k = min(0.90, (tw / 2.0) / a)
            bh = int(th / max(0.40, (1.0 - k * k) ** 0.5) + 2 * pad_v * 0.5)
            if art_id:                # 자산은 몸통 전체가 플레이트 — 최소한 이만큼은 크게
                bh = max(bh, int(ih * BALLOON_MIN_H_RATIO), int(th + 2 * pad_v))
        else:
            bw = int(max(bw_target, min(iw - 16, tw + 2 * pad_h)))     # 직사각형은 컷 폭 비율(BALLOON_W_RATIO)을 쓴다
            bh = int(th + 2 * pad_v)
            if art_id:
                bh = max(bh, int(ih * BALLOON_MIN_H_RATIO))
        # [2026-09-15] 컷 대비 25% 상한 — 박스가 넘치면 글자 크기를 한 단계 더 줄인다.
        _over = bw * bh > BALLOON_MAX_AREA_RATIO * iw * ih
        if _over:
            _sc = (BALLOON_MAX_AREA_RATIO * iw * ih / float(max(1, bw * bh))) ** 0.5
            bw, bh = int(bw * _sc), int(bh * _sc)
        if bw <= iw - 16 and bh <= ih - 16:
            lines, font, fs, line_h, box_w, box_h = ls, fnt, fs_try, lh, bw, bh
            if not _over or fs_try <= FONT_FLOOR + 2:
                break
            continue
        lines, font, fs, line_h = ls, fnt, fs_try, lh
        box_w, box_h = bw, bh
        if fs_try <= FONT_FLOOR + 2:               # 끝까지 좁아도 안 들어가면 최소 글자로 강행
            break
    if not lines or box_w <= 0 or box_h <= 0:
        return None
    xy = _place_in_panel(ix, iy, iw, ih, box_w, box_h, avoid=avoid, prefer=prefer, margin=mg)
    if not xy:
        xy = _place_in_panel(ix, iy, iw, ih, box_w, box_h, avoid=avoid, margin=mg,
                             prefer=tuple(k for k in ("tl", "ml", "tr", "mr")
                                          if k not in prefer) + prefer)
    if not xy:
        return None                       # 자리가 없으면 겹쳐 쓰지 않고 생략한다
    x0, y0 = xy
    x1, y1 = x0 + box_w, y0 + box_h
    _BALLOON_LAST.clear()
    _BALLOON_LAST.update({"lines": list(lines), "fs": int(fs), "box_w": int(box_w), "box_h": int(box_h),
                          "line_h": int(line_h), "xy": (int(x0), int(y0)), "kind": kind, "text": text})
    # 말풍선은 **직사각형**, 속마음은 **타원**. 형태 그 자체로 화자를 구분한다(꼬리·물방울은 폐지).
    art = ""
    if art_id:
        # 미리 골라 둔 변형을 붙인다(위에서 안전여백을 이 변형 기준으로 잰다).
        art = paste_balloon_art(canvas, art_id, (ix, iy, iw, ih), (x0, y0, x1, y1),
                                flip=(_dkey == "r"), tail=_dkey)
        if art:
            if bank_used is not None:
                bank_used.append(art)
            _sl, _st, _sr, _sb = _balloon_art_safe(art, box_w, box_h, _dkey)   # 실측 크기로 다시 잰다
            if kind == "speech":
                ty0 = y0 + max(pad, _st)
            else:
                ty0 = y0 + _st + max(0, (box_h - _st - _sb - len(lines) * line_h) // 2)
        else:
            art = ""
    _col = "l" if str(prefer[0])[-1] == "l" else "r"        # 화자 열(주인공=왼쪽, 상대방=오른쪽)
    if not art and kind == "speech":
        d.rectangle([x0, y0, x1, y1], fill=plate, outline=frame, width=max(1, int(line)))
        box_tail = _vector_tail(d, "speech", x0, y0, x1, y1, _col, iy, ih, fs,
                                plate=plate, frame=frame, line=line)
        ty0 = y0 + pad
    elif not art:
        d.ellipse([x0, y0, x1, y1], fill=plate, outline=frame, width=max(1, int(line)))
        box_tail = _vector_tail(d, "thought", x0, y0, x1, y1, _col, iy, ih, fs,
                                plate=plate, frame=frame, line=line)
        ty0 = y0 + int(box_h * 0.5 - len(lines) * line_h / 2)        # 타원 안에서는 글자 블록을 세로 가운데에 둔다
    ty = ty0
    for ln in lines:
        try:
            wln = probe.textlength(ln, font=font)
        except Exception:
            wln = len(ln) * fs * 0.6
        d.text(((x0 + x1 - wln) // 2, ty), ln, font=font, fill=text_color)
        ty += line_h
    box = locals().get("box_tail") or (x0, y0, x1, y1)
    if emo:
        e = _draw_emotif(d, x0, y0, x1, y1, ix, iy, iw, ih, emo, font_path=font_path)
        if e:
            box = (min(x0, e[0]), min(y0, e[1]), max(x1, e[2]), max(y1, e[3]))
    return box


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
                     facing: str = None, bank_used=None) -> list:
    """[2026-09-09] 컷 하나에 화면 문법을 그린다 — 의성어 → 설명 박스 → 풍선(≤BALLOON_MAX개).

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
                              line=frame_width, text_color=text_color, font_path=font_path,
                              narrow=bool(tp["balloons"]))        # 대사가 있으면 설명을 좁게
        if r:
            placed.append(r[:4])
    for _bi, b in enumerate(tp["balloons"][:BALLOON_MAX]):
        r = _draw_balloon(d, ix, iy, iw, ih, b, avoid=placed, plate=plate, frame=frame,
                          line=frame_width, text_color=text_color, font_path=font_path,
                          facing=facing, canvas=canvas, bank_used=bank_used, idx=_bi)
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
        panel_facing: 컷별 시선("left"/"right"/"front") — 풍선 배치 쪽을 정하는 데 쓴다.
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
        frame_width=frame_width,
        panel_aspect=panel_aspect, wide_aspect=wide_aspect, wide_odd_last=wide_odd_last,
        row_spec=row_spec, page_size=page_size, label_h=label_h)

    page_h = gutter + label_h + sum(
        r["h"] + r["bottom_lines"] * line_h + (cap_gap if r["bottom_lines"] else 0) + gutter
        for r in geom_rows)
    if page_size:
        page_h = int(page_size[1])
        page_w = int(page_size[0])
    bank_used = []      # [2026-09-10] 이 페이지에서 쓴 말풍선 변형 (같은 페이지 중복 회피)
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
            inset = _frame_inset(pad, frame_width)
            # 행 안의 컷은 행 높이로 함께 그린다(c["h"]는 계획값) — 그림이 셀 높이로 끝나면
            # 프레임이 행 높이까지 내려가 프레임 안에 흰 띠가 남는다(사용자: 경계선과 컷 사이 빈틈).
            aw, ah = max(1, c["w"] - 2 * inset), max(1, r["h"] - 2 * inset)
            try:
                img = fit_cover(_load_rgb(panel_paths[p_idx]), aw, ah,
                                bias_x=0.42 if (panel_wide or [False] * n)[p_idx] else 0.5,
                                path=panel_paths[p_idx], anchor=bool(FACE_CROP_ENABLE))
            except Exception:
                img = Image.new("RGB", (aw, ah), bg)
            tp = text_payload(c.get("text") if c.get("text") is not None else c["blocks"])
            img = apply_fade(img, tp["fade"])                    # 에필로그 이벤트신 = 반투명
            ix, iy, iw, ih = _draw_framed_panel(canvas, d, cx, y, c["w"], r["h"], img,
                                                frame=frame, pad=pad, line=frame_width,
                                                bg=bg, keyline=keyline)
            # [2026-09-09] 화면 문법은 존(zone)과 무관하게 하나로: 설명 박스(하단 왼쪽) + 풍선 + 의성어
            item = c["text"] if c.get("text") is not None else c["blocks"]
            _draw_panel_text(canvas, d, ix, iy, iw, ih, item, bank_used=bank_used,
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


def white_to_alpha(img, tol: int = None):
    """흰 배경 스프라이트를 RGBA로 만듭니다 — **가장자리에서 들어가는** 연결된 흰 영역만 투명화합니다.

    간단히 "흰색 = 투명"으로 하면 흰 셔츠·흰 양말까지 사라집니다. 그래서 1/4 축약본에서
    테두리 기원 flood-fill로 '바깥'을 찾고(86k 화소 수준이라 충분합니다), 다시 늘릴 때 LINEAR로
    올려 테두리가 살짝 softer하게 퍼지도록 했습니다(흰 테두리 잔상이 남지 않습니다).
    """
    from collections import deque
    import numpy as _np
    tol = int(getattr(__import__("anima_gen"), "STANDING_WHITE_TOL", 26)) if tol is None else int(tol)
    src = img.convert("RGB")
    w, h = src.size
    sw, sh = max(2, w // 4), max(2, h // 4)
    a = _np.asarray(src.resize((sw, sh), Image.BOX)).astype("int16")
    near = a.min(axis=2) >= (255 - max(0, min(200, tol)))          # 세 채널이 다 흰색에 가까운 곳
    outside = _np.zeros(near.shape, dtype=bool)
    dq = deque()
    for x in range(sw):
        for y in (0, sh - 1):
            if near[y, x] and not outside[y, x]:
                outside[y, x] = True
                dq.append((y, x))
    for y in range(sh):
        for x in (0, sw - 1):
            if near[y, x] and not outside[y, x]:
                outside[y, x] = True
                dq.append((y, x))
    while dq:
        y, x = dq.popleft()
        for yy, xx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
            if 0 <= yy < sh and 0 <= xx < sw and near[yy, xx] and not outside[yy, xx]:
                outside[yy, xx] = True
                dq.append((yy, xx))
    keep = Image.fromarray(((~outside).astype("uint8")) * 255).resize((w, h), Image.BILINEAR)
    out = img.convert("RGBA")
    out.putalpha(keep)
    return out


def _standing_backdrop(w: int, h: int):
    """배경 컷이 없을 때의 폴백 — 윗쪽이 밝은 단조 그라디언트(톤 처리처럼 보입니다)."""
    import numpy as _np
    t = _np.linspace(0.0, 1.0, h, dtype="float32")[:, None]
    top = _np.array([238, 236, 244], dtype="float32")
    bot = _np.array([176, 172, 190], dtype="float32")
    rows = (top + (bot - top) * t).astype("uint8")
    return Image.fromarray(_np.repeat(rows[:, None, :], int(w), axis=1), "RGB")


def compose_standing(sprite_path: str, bg_path: str, out_path: str, tol: int = None) -> str:
    """흰 배경 전신 스프라이트를 컷 배경 위에 얹어 **컷 이미지 한 장**으로 만듭니다.

    배경은 같은 장면의 배경(확립) 컷을 재사용합니다 — 그래서 전신 컷 한 장이 스프라이트 1장 +
    이미 렌더된 배경으로 채워집니다. 배경이 없으면 단조 그라디언트로 대체합니다(화면은 성립합니다).
    """
    sp = Image.open(sprite_path)
    w, h = sp.size
    if bg_path and os.path.isfile(bg_path):
        bg = Image.open(bg_path).convert("RGB")
        s = max(w / float(bg.width), h / float(bg.height))
        bg = bg.resize((max(w, int(bg.width * s + 0.5)), max(h, int(bg.height * s + 0.5))), Image.LANCZOS)
        x0, y0 = (bg.width - w) // 2, (bg.height - h) // 2
        canvas = bg.crop((x0, y0, x0 + w, y0 + h))
    else:
        canvas = _standing_backdrop(w, h)
    rgba = white_to_alpha(sp, tol=tol)
    canvas = canvas.convert("RGB")
    canvas.paste(rgba, (0, 0), rgba)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    canvas.save(out_path, "PNG")
    return out_path


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
        if not chunk:
            # [2026-09-11] ComfyUI가 렌더 도중 죽으면 레이아웃 슬롯은 있는데 파일이 없는 페이지가
            #   생깁니다(실측: 27슬롯 중 22장만 렌더 → 5페이지 슬롯이 빈 채로 들어옴). 예전은 여기서
            #   ValueError로 회차를 통째로 죽여 앞의 4페이지까지 '미완'으로 만들었습니다. 빈 슬롯은
            #   건너뛰고 남은 페이지를 살립니다(사유는 comic_gen이 로그에 남깁니다).
            continue
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
