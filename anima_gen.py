"""
ANIMA (Standing/Simple) 이미지 생성 함수
llm_def.py의 anima_gen_standing, anima_gen_simple 함수를 기반으로 재구성
"""

import json
import os
import random as rand
import re
import time as time_mod
import zlib
import shutil
import config
import comic_input as CI
import urllib.request as request

from openai import OpenAI
from openAPI_control import openAI_response, get_openai_client_anima

# 로깅 설정: 모든 print를 log 파일로 출력
LOG_DIR = "log"
LOG_FILE = os.path.join(LOG_DIR, "anima_gen.log")

# data_comfyui 경로 (angle.txt 구도 프리셋용). actions.yaml은 이제 아무도 로드하지 않는다(레퍼런스 DB).
_ACTIONS_DIR = os.path.join(os.path.dirname(__file__), "data_comfyui")


def _parse_action_entry(raw_text):
    """actions.yaml 1행 → (pose_text, camera_view, aspect_ratio, position_sentence, climax_tag)

    태그 포지셔널 포맷(순서 중요):
        '<pose 문장> #camera_view #wide|tall #He is <position>.|#NONE [#climax]'   ← [2026-09-08] 4번째 슬롯 추가
    ※ 4번째 슬롯(climax 이벤트)은 **선택**이다. 없으면 '' → 기존 440항목/LLM 생성 pose 모두 호환.
    """
    raw_text = (raw_text or "").strip()
    if '#' not in raw_text:
        return (raw_text, "", "tall", "", "")
    pose_part, tags_part = raw_text.split('#', 1)
    tags = [t.strip() for t in tags_part.split('#') if t.strip()]
    camera_view = tags[0] if len(tags) > 0 else ""
    aspect_ratio = tags[1] if len(tags) > 1 else "tall"
    position_sentence = tags[2] if len(tags) > 2 else ""
    climax_tag = tags[3] if len(tags) > 3 else ""
    return (pose_part.strip(), camera_view, aspect_ratio, position_sentence, climax_tag)


def _is_no_position(position_sentence):
    """3번째 태그 '위치 지정 없음' 판정.

    [2026-09-08] 버그fix: yaml의 NONE 태그는 파싱 결과 문자열 'NONE'인데
    기존 코드는 position_sentence != "#NONE" 만 비교해 통과 → 최종 프롬프트에
    리터럴 'NONE'이 그대로 들어갔다(사이드뷰 헤더). ''/#NONE/NONE 모두 처리.
    """
    if not position_sentence:
        return True
    return position_sentence.strip().lstrip('#').strip().rstrip('.').upper() == 'NONE'


def _is_no_climax(climax_tag):
    """[2026-09-08] 4번째 태그 'climax 이벤트 없음' 판정 (''/#NONE/NONE)."""
    if not climax_tag:
        return True
    return climax_tag.strip().lstrip('#').strip().rstrip('.').upper() == 'NONE'


# =====================================================================================
# [2026-09-08] 카메라 뷰 태그 정석화 (A/B 실험용) — run_main.sh -camera_canon
# -------------------------------------------------------------------------------------
# actions.yaml/LLM이 쓰는 뷰 토큰(front_view/side_view/back_view/close_up)은 danbooru
# 정석 태그가 아니다. 정석은 from_front / from_side / from_back / close-up (pov는 동일).
#   A(legacy, 기본) : 원본 토큰 그대로 [ANGLE]/[CAMERA]에 주입
#   B(-camera_canon): 정석 태그로 치환
# aspect(wide/tall)는 분해능 선택에 쓰이는 별도 슬롯이라 치환 대상이 아니다.
# =====================================================================================
CAMERA_TAG_CANONICAL = {
    "front_view": "from_front",
    "side_view": "from_side",
    "back_view": "from_back",
    "close_up": "close-up",
    "pov": "pov",          # 정석도 pov (변경 없음 명시)
}

# fallback 앵글 문자열까지 함께 정석화
CAMERA_ANGLE_FALLBACK_LEGACY = {"pov": "pov", "side": "cowboy_shot, side_view"}
CAMERA_ANGLE_FALLBACK_CANONICAL = {"pov": "pov", "side": "cowboy_shot, from_side"}


def _camera_canon_active():
    """-camera_canon 모드 여부 (A/B 스위치)"""
    return bool(getattr(config, "camera_canon_cli", False))


def _canon_camera(token):
    r"""카메라 뷰 토큰 정석화 (B모드 OFF면 항등).

    'close_up, from behind' 같은 다중 토큰 문자열도 개별 토큰만 교체하며,
    이미 정석인 'from_side'를 다시 잡지 않도록 경계(?<![\w-])를 쓴다.
    """
    if not token or not _camera_canon_active():
        return token
    out = str(token)
    for src, dst in CAMERA_TAG_CANONICAL.items():
        if src == dst:
            continue
        out = re.sub(rf'(?<![\w-]){re.escape(src)}(?![\w-])', dst, out)
    return out


# =====================================================================================
# [2026-09-07] 카메라 앵글 프리셋 사전 — data_comfyui/angle.txt → LLM 선택 — run_main.sh -angle_llm
# ------------------------------------------------------------------------------------
# 배경: [ANGLE] 슬롯은 actions.yaml의 5개 토큰(front_view/side_view/back_view/close_up/pov)에
# 갇혀 있었고(실측 log/tag_out.txt 헤더 306건: close-up 50 / front_view 17 / close_up 14 /
# side_view 14 / pov 8 / back_view 4), LLM이 쓰는 composition 섹션은 '카메라 위치'가 아니라
# '영상미 수사'로 채워졌다(295개 섹션: depth of field 84%, cinematic composition 66%,
# immersive perspective 54%) → 컷이 단조. angle.txt의 실제 danbooru 구도 태그 66종
# (from behind / over the shoulder / dutch angle / worm's-eye view / head out of frame …)을
# 후보로 주고, **현재 상황에 가장 잘 맞는 하나를 LLM이 고르게** 한다.
#
# 설계 결정 (order/2026-09-07_comfyui_prompt_angle_review.md §5 — 전부 추천안 채택):
#   Q1 가이드는 현행 5섹션+BREAK4+가중치 구조 유지, prompt.txt의 '내용'만 이식
#   Q2 aspect 권위는 pose(actions.yaml wide/tall) → 같은 aspect 후보만, square 6종은 후보 제외
#   Q3 별도 LLM 호출 없이 같은 응답의 "ANGLE: <n>" 1줄(지연 0ms), 후보 8개
#   Q4 선택 실패 시 후보 중 랜덤 1개(다양성 우선), 후보 자체가 없으면 기존 폴백
#   Q5 camera_view 계열 필터 유지, 정합 후보 <3이면 aspect-only로 완화
#   Q6 kiss→face close-up 오버라이드는 존속(pov_camera가 close-up 계열로 필터에 반영됨)
#
# 파싱 안정성 원칙: LLM이 고른 건 '번호'뿐이다. 태그는 angle.txt에서 결정론적으로 꺼낸다
#   ( 태그 환각 차단). ANGLE 라인은 ##PROMPT## 마커 **외부**에 두므로 본문 파싱
# (_anima_extract_prompt)과 무관하고, 마커 누락 대비 _strip_angle_line()가 있다.
# =====================================================================================
ANGLE_PRESET_FILE = os.path.join(_ACTIONS_DIR, "angle.txt")
ANGLE_MENU_SIZE = 8                 # LLM에게 보여주는 후보 수
ANGLE_MIN_FAMILY_CANDIDATES = 3     # 이보다 적으면 family 필터를 포기하고 aspect-only로 완화
ANGLE_POOL_POV = "POV"              # 1인칭(prompt1.md)용 풀
ANGLE_POOL_SIDE = "NOPOV"           # 2인물 사이드뷰(prompt2.md)용 풀
ANGLE_ASPECT_TOKENS = ("landscape", "portrait", "square")
# pose aspect → angle.txt aspect 토큰 ('square'는 어떤 pose와도 매칭되지 않아 자동 제외)
ANGLE_ASPECT_MAP = {"wide": "landscape", "tall": "portrait", "landscape": "landscape", "portrait": "portrait"}

# actions.yaml camera_view → 정합하는 구도 태그 (부분 문자열 매칭)
ANGLE_FAMILY_KEYS = {
    "close_up": ("close-up", "close up", "face focus", "cropped", "head out of frame", "focus on",
                 "cowboy_shot", "upper body"),
    "back_view": ("from behind", "back-to-back", "looking away", "looking back", "over the shoulder"),
    "side_view": ("profile", "side-by-side", "from side", "face-to-face", "symmetry", "reflection"),
    "front_view": ("looking at viewer", "front view", "straight-on", "from above", "from below",
                   "high angle", "low angle", "eye contact", "full body", "full shot", "wide shot",
                   "standing", "sitting", "lying", "side-by-side"),
}

_ANGLE_CHOICE_RE = re.compile(r"^[ \t>*`\u2013#\u2022-]*angle[ \t]*[:=\->\u2192]+[ \t]*(\d+)", re.I | re.M)


def _load_angle_presets():
    """data_comfyui/angle.txt → [{pool, desc, aspect, tags}] (무캐시, 잘못된 줄 버림)

    포맷: #<POOL>#<한국어 구도 설명>#<aspect, danbooru 카메라 태그…>
      - 태그 필드의 첫 토큰은 항상 aspect(landscape/portrait/square) — 실측 66/66 정합
      - tags에는 aspect를 제외해 저장한다 (분해능은 pose aspect가 권위)
    """
    out = []
    try:
        with open(ANGLE_PRESET_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        log(f"[ANGLE] 앵글 프리셋 파일 없음: {ANGLE_PRESET_FILE}")
        return out
    for ln in lines:
        line = ln.strip()
        if not line or line.count("#") < 3:
            continue
        parts = line.split("#")
        pool = parts[1].strip().upper()
        desc = parts[2].strip()
        tag_str = "#".join(parts[3:]).strip()
        tags = [t.strip() for t in tag_str.split(",") if t.strip()]
        if pool not in (ANGLE_POOL_POV, ANGLE_POOL_SIDE) or not tags:
            continue
        aspect = tags[0].lower() if tags[0].strip().lower() in ANGLE_ASPECT_TOKENS else ""
        rest = tags[1:] if aspect else tags
        if not rest:
            continue
        out.append({"pool": pool, "desc": desc, "aspect": aspect, "tags": rest})
    return out


def _angle_camera_families(camera_view: str) -> tuple:
    """camera_view 문자열에서 인식되는 family 키 ('close_up, from behind' 같은 다중 토큰/정석 혼용 대응)"""
    if not camera_view:
        return ()
    low = str(camera_view).lower()
    fams = []
    for fam, kws in (("close_up", ("close_up", "close-up", "close up")),
                     ("back_view", ("back_view", "from back", "back view")),
                     ("side_view", ("side_view", "from side", "side view")),
                     ("front_view", ("front_view", "from front", "front view"))):
        if any(k in low for k in kws):
            fams.append(fam)
    return tuple(fams)


def _pick_angle_candidates(is_side: bool, camera_view: str, aspect_ratio: str,
                           n: int = ANGLE_MENU_SIZE) -> list:
    """앵글 후보 고르기: POOL → aspect → camera_view 계열(정합 <ANGLE_MIN_FAMILY_CANDIDATES 시 aspect-only 완화) → 랜덤 n개"""
    presets = _load_angle_presets()
    if not presets:
        return []
    pool = ANGLE_POOL_SIDE if is_side else ANGLE_POOL_POV
    rows = [p for p in presets if p["pool"] == pool]
    if not rows:
        return []
    want_aspect = ANGLE_ASPECT_MAP.get((aspect_ratio or "").strip().lower(), "")
    if want_aspect:
        same = [p for p in rows if p["aspect"] == want_aspect]
        if same:
            rows = same
        else:
            rows = []
    fams = _angle_camera_families(camera_view)
    if fams and rows:
        fam_rows = [p for p in rows
                    if any(kw in " ".join(p["tags"]).lower()
                           for fam in fams for kw in ANGLE_FAMILY_KEYS.get(fam, ()))]
        if len(fam_rows) >= ANGLE_MIN_FAMILY_CANDIDATES:
            rows = fam_rows
    if not rows:
        return []
    return rand.sample(rows, min(n, len(rows)))


def _resolve_angle_tags(preset: dict) -> str:
    """프리셋 → [ANGLE] 슬롯 문자열 (aspect 토큰 제외 + _canon_camera 정규화)"""
    if not preset or not preset.get("tags"):
        return ""
    return _canon_camera(", ".join(preset["tags"]))


def _ensure_log_files():
    """log 디렉토리 준비."""
    os.makedirs(LOG_DIR, exist_ok=True)


def log(*args, **kwargs):
    """메시지를 콘솔과 log 파일에 동시에 출력합니다."""
    msg = " ".join(str(a) for a in args)
    #print(msg, **kwargs)  # 콘솔에도 출력
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(msg + "\n")



def _strip_angle_line(text: str) -> str:
    """##PROMPT## 마커 누락으로 ANGLE 라인이 본문에 흘러들어온 경우 제거 (해당 줄을 통째로 지운다)

    줄째 제거하는 이유: BREAK 섹션 경계의 빈 줄을 살려야 `_analyze_tag_loop`/ComfyUI 전달 포맷이 유지된다.
    """
    if not text or not _ANGLE_CHOICE_RE.search(text):
        return text
    cleaned = re.sub(r"^[ \t>*`\u2013#\u2022-]*angle[ \t]*[:=\->\u2192]+[ \t]*\d+[ \t]*\n?", "", text, flags=re.I | re.M)
    return cleaned.lstrip("\n")


def _dedupe_weighted_tags(text: str) -> str:
    """프롬프트 본문에서 (tag:weight) 토큰 중복 제거(첫 출현만 보존). [2026-09-04]

    버그(log/tag_out.txt 6245행 사례): qwen 커레이션이 간헐적으로 퇴행 루프에 빠져
    "(tongue out:1.5) ... (tongue out:1.5) ..." 같은 같은 태그 블록을 2~3회 반복 출력했다.
    - 비교 키: 가중치 제거·공백 정규화·소문자 — '(wet hair:1.4)'와 '(wet hair:1.2)'도 동일 태그.
    - 제거 시 토큰 뒤 쉼표/공백까지 소비하고 고립·이중 쉼표는 사후 Repair.
    - bare 태그(가중치 없음)는 산문 토큰과 충돌하지 않도록 미처리.
    """
    if not text or "(" not in text:
        return text
    seen = set()

    def _repl(m):
        key = re.sub(r"\s+", " ", m.group(1).strip().lower())
        if key in seen:
            return ""
        seen.add(key)
        return m.group(0)

    out = re.sub(r"\(([^()]+?):\s*\d+(?:\.\d+)?\)\s*,?\s*", _repl, text)
    out = re.sub(r",\s*,+", ", ", out)   # 제거로 생긴 이중 쉼표
    out = re.sub(r"\s+,", ",", out)      # 쉼표 앞 잔여 공백
    return out

def get_episode_content(episode: int) -> str:
    """에피소드 본문 텍스트. comic 포크에서는 comic_input.apply_to_config가 주입한
    config.episode_content가 유일한 출처다(progress 디렉토리 없음)."""
    return str(config.episode_content[episode]) if 0 <= episode < len(config.episode_content) else ""


def _extract_json(text: str) -> dict:
    """
    텍스트에서 첫 번째 JSON 객체를 추출.
    ```json 마크다운 블록, 여러 줄 JSON 모두 처리.
    """
    text = text.strip()
    # ```json 또는 ``` 제거
    if text.startswith("```"):
        text = text[3:].strip()
        if text.startswith("json"):
            text = text[4:].strip()
    # 첫 번째 {부터 마지막 }까지 추출
    start = text.find("{")
    end = text.rfind("}") + 1
    if start >= 0 and end > start:
        json_str = text[start:end]
        try:
            return json.loads(json_str)
        except Exception:
            # [2026-09-09] 추출/컷 스크립트와 같은 관대한 파서를 쓴다(Q4는 키 앞 따옴표를 이상한
            #   유니코드로 디코딩한다) — 태그 생성이 잡문자 하나로 fallback에 떨어지면 태그가 빈다.
            obj, err = CI.extract_json_obj_checked(json_str)
            if obj:
                return obj
            raise ValueError(f"JSON parse failed: {err} :: {json_str[:120]}")
    raise ValueError(f"No JSON found in: {text[:100]}")



# ============================================================================
# 전역 변수
# ============================================================================
# [2026-09-09] LoRA 트리거는 이 앞단 뒤에 붙는다 (resolve_anima_lora → _sync_artist_trigger).
#   예: artist_anima = "1girl, solo, v4ss4g0x, cartoon, "
#   트리거가 빠지면 LoRA 파일만 올라가고 화풍은 안 변한다("LoRA가 안 먹힌다"의 두 번째 원인).
ANIMA_ARTIST_BASE = "1girl, solo, "
artist_anima = ANIMA_ARTIST_BASE  # artist/LoRA trigger (resolve_anima_lora가 EP별로 갱신)
anima_nametag = "standing"         # 컷별 파일명 prefix (comic_gen.render_panel이 세팅)


# ============================================================================
# ANIMA LoRA 통합 설정 (파일 + 강도 + 베이스모델 + trigger)
# ============================================================================
# 기존에는 comfyui_run_anima(파일)와 anima_setup(trigger)에 lora_map이 2개
# 따로 존재해(스파게티) 새 LoRA 추가 시 두 곳을 모두 수정해야 했다.
# 이제 아래 ANIMA_LORA_CONFIG 한 곳에서 관리한다.
#
# ★ 새 LoRA 추가 시: ANIMA_LORA_CONFIG에 1줄만 추가하면 됨.
#
# 항목 형식: (lora1파일, lora1강도, lora2파일, lora2강도, unet모델, trigger)
#   - lora1/lora2 : ComfyUI "Power Lora Loader"(노드 122) 슬롯
#   - 강도 0.0    : 해당 슬롯 OFF (파일명 비워도 됨)
#   - unet        : "UNETLoader"(노드 46) 베이스 모델
#   - trigger     : prompt 헤더 앞단(artist_anima)에 붙는 아티스트/트리거
# ============================================================================
ANIMA_LORA_CONFIG = {
    #  key                : (lora1파일, str1, lora2파일, str2, unet모델, trigger)
    # --- 기본 스타일 (2026-09-08부터 unet = anima_aestheticV11, 구 anima_baseV10 자리) ---
    "lora_hentai":        ("Hentai_Studio_Quality_Anima-step00001300.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r"hentai_studio_quality, shiny skin, "),
    "lora_tianl":         ("tianl.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r"@tianliang duohe fangdongye, "),
    "lora_mi1k":          ("anima_mi1k_v1.2.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r"@mi1k, "),
    "lora_fr9t":          ("anima_fr9t_v1.1.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r"@fr9t, "),
    "lora_saboten":       ("saboten.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r"@saboten, "),
    "lora_wagashi":       ("wagashi.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r"@wagashi_\(dagashiya\), "),
    "lora_gpt":           ("42.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r""),
    "lora_jewel":         ("jewel_milk-step00012000.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r""),
    # --- 믹스 (2개 LoRA 조합) ---
    #"lora_yzsss_nanashi": ("yzsss_anima1.0_v0.2.safetensors", 1.0, "나나시2(@nanash1).safetensors", 0.2, "anima_aestheticV11.safetensors", r"(@yzsss), (@nanashi1), "),
    #"lora_mix1":          ("AIIlustOjisan_AnimaB_v01(@411llust0j1s4n).safetensors", 1.0, "sex_style.safetensors", 1.0, "anima_aestheticV11.safetensors", r"@411llust0j1s4n, "),
    #"lora_mix2":          ("gpt-image-2_anima-base1_v1-1.safetensors", 1.0, "sex_style.safetensors", 1.0, "anima_aestheticV11.safetensors", r""),
    #"lora_mix3":          ("jewel_milk-step00012000.safetensors", 1.0, "sex_style.safetensors", 0.5, "waiANIMA_v10Base10.safetensors", r""),
    #"lora_mix4":          ("jewel_milk-step00012000.safetensors", 0.3, "sex_style.safetensors", 0.3, "animaBreaker_v10.safetensors", r""),
    #"lora_anima":         ("jewel_milk-step00012000.safetensors", 0.0, "sex_style.safetensors", 0.1, "animaBreaker_v10.safetensors", r""),
    # --- 에스테틱 계열 (anima_aestheticV11) ---
    "lora_aioyaji":       ("AIIlustOjisan_AnimaB_v01(@411llust0j1s4n).safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r"@411llust0j1s4n, "),
    #"lora_xipa":          ("xipa2026late03-05_AnimaB_v01.safetensors", 1.0, "sex_style.safetensors", 0.3, "anima_aestheticV11.safetensors", r"@xipa2026late03-05, "),
    # --- 타카미치/ALP 계열 (animaBreaker_v10) ---
    # lora_takamichi: lora1=xipa(0.2) + lora2=타카미치(1.0). trigger는 xipa 성분용.
    #"lora_takamichi":     ("xipa2026late03-05_AnimaB_v01.safetensors", 0.2, "TAKAMICHI2010_202881.safetensors", 1.0, "animaBreaker_v10.safetensors", r"@xipa2026late03-05, "),
    #"lora_alp":           ("ALPAnima2.safetensors", 1.0, "TAKAMICHISTYLE_T5_200245.safetensors", 0.0, "animaBreaker_v10.safetensors", r"ALPAnima, "),
    #"lora_break":         ("ALPAnima2.safetensors", 0.0, "TAKAMICHISTYLE_T5_200245.safetensors", 0.0, "animaBreaker_v10.safetensors", r""),
    # --- 단독 LoRA (lora_random 후보) ---
    # mix에 들어갔던 LoRA를 단독 KEY(lora2="")로 분리.
    # lora_random은 lora2=="" 인 KEY만 선택 → mix의 보조 LoRA(lora2)가 버려지는 문제 방지.
    # unet은 random 시 ANIMA_RANDOM_UNET_POOL로 덮어써짐 (직접 사용 시 기본값).
    "lora_yzsss":          ("yzsss_anima1.0_v0.2.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r"(@yzsss), "),
    "lora_nanashi":        ("나나시2(@nanash1).safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r"(@nanashi1), "),
    "lora_sex":            ("sex_style.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r""),
    "lora_gptimg2":        ("gpt-image-2_anima-base1_v1-1.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r""),
    "lora_xipa_solo":      ("xipa2026late03-05_AnimaB_v01.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r"@xipa2026late03-05, "),
    "lora_takamichi_solo": ("TAKAMICHI2010_202881.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r"TakamichiStyle, "),
    "lora_alp_solo":       ("ALPAnima2.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r"ALPAnima, "),
    "lora_takamichistyle": ("TAKAMICHISTYLE_T5_200245.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r"TakamichiStyle, "),
    # --- 2026-08-24 추가 (단독 LoRA, ~/AI/ComfyUI/models/loras) ---
    # trigger: safetensors __metadata__(ss_tag_frequency)에서 학습 캡션 기준으로 확인 (2026-08-24)
    "lora_deadalus7":      ("Deadalus7_anima_v10.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r"@deadalus7, "),  # 학습 캡션 소문자 확인
    "lora_hotate":         ("hotate3333333_anima_v10.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r"@hotate3333333, "),
    "lora_meow25":         ("meow25v8.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r"meow25, "),
    "lora_zxcvnmweruo":    ("zxcvnmweruo_anima_v10-000002.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r"@zxcvnmweruo, "),
    "lora_buanime":        ("BuAnime_NSFW_Style_Anima_BuAnime.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r""),  # trigger 없음 확인 (캡션에 trigger 태그 X)
    "lora_daioo":          ("daioo.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r"@daioo, "),
    "lora_ren45":          ("ren45_v1.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r""),  # LyCORIS 학습, 메타데이터 없음 → trigger 미확인
    "lora_wadustyle":      ("WaduStyle-Anima.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r""),  # 순수 스타일 LoRA (캡션 전부 빈 문자열) → trigger 없음, epoch_1(1500step) 저장본
    # --- 2026-08-25 추가 (단독 LoRA, ~/AI/ComfyUI/models/loras) ---
    # AG: Vassago - Flat Cartoon Style (학습일 2026-07-30, 67장, dim32/alpha32, base=anima)
    # trigger: ss_tag_frequency에서 'v4ss4g0x'가 67/67장 전부 등장 → dreambooth trigger 확인 (2026-08-25)
    "lora_vassago":        ("b529e7df-a73b-4735-8f70-a2377685975a.TA_trained.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r"v4ss4g0x, cartoon, "),
    # --- 2026-08-25 캐릭터 LoRA (상호배제: ANIMA_CHARACTER_LORA_KEYS, 랜덤 페어에 최대 1개) ---
    # trigger: ss_tag_frequency 전체 캡션 등장 확인 (2026-08-25)
    "lora_evie":           ("EvieStellarBlade_AnimaBaseV10_byKonan.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r"evie, "),
    "lora_hyeonbomi":      ("Hyeon Bom-i Anima.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r"hnbmib, "),
    "lora_imdana":         ("Im da-na Anima.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r"imdnab, "),
    "lora_jeongsooah":     ("Jeong Soo Ah Anima.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r"jngsoahb, "),
    "lora_parkSORIM":      ("Park So-rim Anima.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r"prksrmb, "),
    "lora_seoheeju":       ("Seo Heeju Anima.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r"sohjub, "),
    "lora_woojiyoung":     ("Woo Ji-young Anima.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r"wojyngb, "),
    "lora_lasihyeon":      ("La Sihyeon Anima.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r"lashnb, "),
    # --- 2026-08-25 스타일 LoRA (trigger 확인) ---
    "lora_anmshigui":      ("ANMShigUi_F4H_1620.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r"anmshigui_f4h, "),
    "lora_citizenk":       ("CitizenK_AnimaB_v01(@c1t1z3nk).safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r"@c1t1z3nk, "),
    "lora_meion":          ("Meion_anima_style_lokr_2-000034(@meionANIMAstyle).safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r"@meionanimastyle, "),
    "lora_zeebc0ck":       ("N9BMZFWQR63A8HGJPX2497K4G0.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r"zeebc0ck, "),  # 1장 학습, trigger 의심
    "lora_poju":           ("[Po-Ju] Promiscuity C Doujin Style Anima.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r"po-ju, "),
    "lora_akipeko":        ("akipekoanima_v1_1-step00003000(@AkioA_I).safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r"@akipekostyle, "),  # 파일명 @AkioA_I와 다름
    "lora_alpp":           ("alp.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r"alpp, "),
    "lora_fishine":        ("fishine_anima.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r"@fishine, "),
    "lora_ghibli":         ("ghibli_style_anima.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r"ghibli style, "),
    "lora_possummachine":  ("possummachine-A1_v1.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r"@possummachine, "),
    "lora_shexyo":         ("shexyo_AnimaB_v03.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r"sh3xy0 style, "),
    "lora_shinjiro":       ("shinjiro_AnimaB_v01.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r"@sh1nj1r0, "),
    "lora_xipaearly":      ("xipaearly2026_AnimaB_v01-1.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r"@x1p4early2026, "),
    "lora_yzs":            ("yzs_anima_v0.3.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r"@yzsss, "),
    "lora_m4me":           ("anima_m4me_v1.2-epoch16.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r"@m4me, "),  # civitai "Anima style: m4me"
    # --- 2026-08-25 스타일 LoRA (trigger 없음) ---
    "lora_buanime_ultra":  ("BuAnime_NSFW_Style_Anima_ultra.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r""),
    "lora_easonhot":       ("Eason HOT.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r""),
    "lora_inakamonor":     ("Inakamonor style2.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r""),
    "lora_memaaani":       ("MeMaAni V2 B.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r""),
    "lora_cunny":          ("cunny_animaV1.0-000009.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r""),
    "lora_rindou":         ("rindou.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r""),
    "lora_zoda":           ("zoda_anima_v2.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r""),
    "lora_bluearchive":    ("BlueArchiveStyleB1.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r""),
    "lora_ponchi":         ("ponchi_v1.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r""),
    "lora_flackstyle":     ("flackstyle-000034_edited.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r""),
    "lora_takeda2":        ("Takeda_Hiromitsu_Anima_v2.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r""),  # trigger 미확인 (W5 생성 테스트)
    "lora_ujinangel":      ("u-jin_angel_anima_v05.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r""),
    # --- 2026-08-25 스타일 LoRA (trigger 미확인, 파일명 @ 힌트) — W5 생성 테스트 후 수정 ---
    # (2anima oskar custom.safetensors는 4anima 버전과 동일 학습본 → 미등록)
    "lora_oskar":          ("4anima oskar custom@oskar custom).safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r"oskar custom, "),  # 미확인
    "lora_a10":            ("A10(@dpdlxps).safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r"@dpdlxps, "),  # 미확인
    "lora_1uxsumildo":     ("럭스수밀도3(@1uxsumildo).safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r"@1uxsumildo, "),  # 미확인
    "lora_amaduyu":        ("아마즈유3(@amaduyu).safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r"@amaduyu, "),  # 미확인
    "lora_bunnyslop":      ("BunnySlop_Ani_v5.56.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r""),  # 미확인 (TF 비어있음, civitai title @sl0p) — W5 생성 테스트
    # --- 2026-08-28 추가 (단독 LoRA, ~/AI/ComfyUI/models/loras) ---
    # trigger: ss_tag_frequency에서 학습 캡션 기준으로 확인 (2026-08-28)
    "lora_daioo_v2":       ("daioo v2.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r"@daioo, "),  # @daioo 750/1400 (학습 750장, reg 제외) — 기존 daioo v2
    "lora_softmanhua":     ("softmanhuastyle_000006000.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r"softmanhuastyle, "),  # 1/1 캡션 trigger 확인 — [2026-09-08] civitai 권장 프레이즈: 'softmanhuastyle, 2d adult manhua illustration style, clean ink lineart, soft cel shading, pastel skin tones, glossy skin highlights, high detail illustration'
    "lora_kurorin":        ("kurorin_AnimaB_v01-000011.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r"@kur0r1n, "),  # 의심 (34/190, AnimaB v01 패턴)
    "lora_ratatatat74":    ("ratatatat74_AnimaB_v02.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r"@r4t4t4t4t, "),  # [2026-09-08] 확인: 31/31(100%) 등장 + civitai trainedWords=['@r4t4t4t4t,']
    "lora_shufflesong":    ("shufflesongdatiankongstyle_animaBasev1.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r"@shufflesongdatiankongstyle, "),  # 의심 (43/657, 512+1024 2그룹)
    "lora_kemuri_haku":    ("Kemuri_Haku___Artist_Style___Anima.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r"kmurihku, "),  # [2026-09-08] trigger 수정: ai-toolkit 학습본은 ss_tag_frequency가 빈 캡션 1개({"1_":{"":1}})라 무정보 → modelspec.title='kmurihku' + civitai trainedWords=['kmurihku'] 확인 (기존 'trigger 없음'은 오독)
    "lora_lambton":        ("Lambton.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r""),  # trigger 없음 (빈 캡션 1개)
    "lora_takeani_takeda": ("_takeani__Takeda_Hiromitsu__epoch_10.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r""),  # trigger 없음 (빈 캡션 1개, Takeda Hiromitsu epoch_10)
    "lora_xiaoluonion":    ("xiaoluonionmix_v2.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r""),  # 미확인 (메타데이터 없음)
    # --- 2026-08-28 추가 (기존 미등록 파일 보충) ---
    "lora_berserker":      ("Berserker00R.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r""),  # 미확인 (TF 비어있음, base=anima-base-v1.0)
    "lora_glossy1":        ("glossy1_epoch17.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r""),  # 미확인 (LyCORIS kohya, trigger 정보 없음)
    "lora_niji_sweet":     ("ANIMA_Niji_Sweet_Spot_v4.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r""),  # 미확인 (메타데이터 전무)
    "lora_realism":        ("realism-anima-v2.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r"@callmemaybe, "),  # 의심 (28/1179, base=realism-anima-v10)
    # --- 2026-09-08 추가 (신규 다운로드 6종 중 미등록 3종) ---
    # 분석: util/analyze_new_loras_260908.py (safetensors 헤더 + tensor shape + civitai hash API)
    # 나머지 3종(softmanhua/ratatat74/kemuri_haku)은 이미 등록본과 sha256 동일(재다운로드만) → 위 항목 trigger/코멘트 보정
    # ★ unet 필드는 위 헤더 주석대로 전 항목 anima_aestheticV11.safetensors로 통일 (anima_baseV10 폐기)
    # Cirima Style (civitai model 1751396 / version 'Anima'): ai-toolkit(Civitai Spine) 학습,
    #   base=Anima-Base-v1.0-Diffusers, dim16/alpha16, 177장×13repeats, epoch1 저장본, noise_offset 0.03.
    #   캡션 학습 없음(ss_tag_frequency={'1_':{'':1}}) + trainedWords=[] → trigger 없음 (Illustrious 판의 C1r1m4계열은 다른 버전용)
    "lora_cirima":          ("Cirima_Anima.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r""),
    # BuAnime NSFW Style Pack Anima — BuAnime V2 (civitai 2645819/3301514): 기존 lora_buanime 개선판
    #   (학습 160장/12ep, dim32/alpha16, "less body lighting, more vibrant colors"). trainedWords=[] → trigger 없음.
    #   제작자 권장 강도 0.8(밸런스)~1.0(화풍 강조) — 여기서는 다른 항목과 동일하게 1.0
    "lora_buanime_v2":      ("BuAnime_NSFW_Style_Anima_BuAnimeV2.safetensors", 1.0, "", 0.0, "anima_aestheticV11.safetensors", r""),
}

# [2026-09-07] 고정 화풍(ANIMA_LORA_CONFIG 고정 키)의 unet 필드는 histórica하게
#   anima_baseV10.safetensors를 가리키는데 **이 파일은 어느 ComfyUI 모델 디렉터리에도 없다**
#   (lora_random만 UNET 풀로 덮어써서 살아남았다). 고정 스타일 선택 시 ComfyUI가 400을 뱉던 원인.
#   → 실제 존재하는 파일로 폴백한다. plot.json의 "anima_unet"으로 직접 지정도 가능하다.
ANIMA_BASE_UNET_DEFAULT = "anima_aestheticV11.safetensors"
_UNET_SUBDIRS = ("models/diffusion_models", "models/unet", "models/checkpoints")
_LORA_SUBDIRS = ("models/loras",)
_lora_file_cache = {}              # (이름 → 실존 여부) — 렌더마다 디스크를 다시 쪼하지 않는다


def _comfyui_roots():
    """ComfyUI 설치 루트 후보 — plot.json `comfyuidir`(경로 지시의 유일한 지점) > ~/AI/ComfyUI 폴백.
    코드에 개인/기기 절대경로는 넣지 않는다(env COMFYUI_DIR 로 덮어쓰기 가능)."""
    roots = []
    try:
        cfgd = str((config.get_json_value() or {}).get("comfyuidir") or "").strip()
    except Exception:
        cfgd = ""
    for r in (os.environ.get("COMFYUI_DIR", "").strip(), cfgd,
              os.path.expanduser("~/AI/ComfyUI")):
        if r and r not in roots:
            roots.append(r)
    return roots


def _unet_exists(name: str) -> bool:
    """unet/체크포인트 파일이 ComfyUI 모델 디렉터리에 실제로 있는지."""
    if not name:
        return False
    for root in _comfyui_roots():
        for sub in _UNET_SUBDIRS:
            if os.path.isfile(os.path.join(root, sub, name)):
                return True
    return False


def _lora_exists(name: str) -> bool:
    """LoRA 파일이 ComfyUI models/loras에 실제로 있는지 (결과 캐시)."""
    if not name:
        return False
    if name in _lora_file_cache:
        return _lora_file_cache[name]
    hit = False
    for root in _comfyui_roots():
        for sub in _LORA_SUBDIRS:
            if os.path.isfile(os.path.join(root, sub, name)):
                hit = True
                break
        if hit:
            break
    _lora_file_cache[name] = hit
    return hit


def _apply_unet(resolved, unet_override: str = ""):
    """resolve 결과 (lora1,str1,lora2,str2,unet,trigger)의 unet를 검증/교체.

    - plot.json anima_unet 지정 → 무조건 그 값(잘못되면 ComfyUI 400 body에 사유가 찍힌다).
    - 미지정이고 tuple의 unet이 디스크에 없으면 → 기본 모델(존재하는 것)로 폴백 + 경고 로그.
    """
    if not resolved:
        return resolved
    lora1, str1, lora2, str2, unet, trigger = resolved
    if unet_override:
        if unet_override != unet:
            log(f"[ANIMA unet] plot.json anima_unet 지정: {unet!r} → {unet_override!r}")
        return (lora1, str1, lora2, str2, unet_override, trigger)
    if unet and _unet_exists(unet):
        return resolved
    fallback = ANIMA_BASE_UNET_DEFAULT if _unet_exists(ANIMA_BASE_UNET_DEFAULT) else \
        next((u for u in ANIMA_RANDOM_UNET_POOL if _unet_exists(u)), "")
    if fallback and fallback != unet:
        log(f"[ANIMA unet] 메인 모델 없음: {unet!r} → {fallback!r} 사용 "
            f"(plot.json anima_unet으로 직접 지정 가능)")
    return (lora1, str1, lora2, str2, fallback or unet, trigger)


# lora_random에서 베이스 모델로 랜덤 선택할 후보 풀
# [2026-08-30] LoRA 사용 시 메인 모델 풀 (animaBreaker_v10 제외 → ANIMA_SOLE_UNET_POOL로 이동)
ANIMA_RANDOM_UNET_POOL = [
    "anima_aestheticV11.safetensors",
    "waiANIMA_v10Base10.safetensors",
]

# [2026-08-28] -real 모드: 모든 LoRA 무시 + 아래 리얼 메인 모델(UNet) 중 1개 랜덤 선택
ANIMA_REAL_UNET_POOL = [
    "realDream_animaV4.safetensors",
    "beretMixAnimaReal28D_v10.safetensors",
    "uwazumimixAnima_uwazumimixAnimaV30.safetensors",
]

# [2026-08-30] -sole 모드: LoRA 없이 메인 모델(UNet)만 아래 풀에서 1개 랜덤 선택 (real 아님, 애니메이션 화풍)
#    "animaBreaker_v10.safetensors",
ANIMA_SOLE_UNET_POOL = [
    "pornmasterAnima_baseV1.safetensors",
    "fnMomentAnimaTurbo_v40NoTurbo.safetensors",
    "illustrijGEN_a1.safetensors",
    "riMixIllustriousAnima_riMixAnima.safetensors",
    "miaomiaoHarem_anima16.safetensors",
    "terraRisingUnity_v30Unity.safetensors",
]

# [2026-08-30] step(기-승-전-결)별 최대 pose 수 — LLM이 1~N개 생성 (상황에 맞게, 최대 N)
# 독자들이 '여러장'을 요청하므로 step당 포즈를 최대 4개까지 뽑아 모두 이미지로 출력
MAX_POSES_PER_STEP = 3

# [2026-08-30] -real 모드 prompt_header 추가 태그 (리얼 화풍 강화, 기존 헤더에 추가)
ANIMA_REAL_HEADER_TAGS = (
    "realistic, photoreal, stable high quality, high densification, HDR, analog film, "
    "SLR Nikon-lens, source_photo, ultra sharp focus, detailed depiction, "
    "delicate and beautiful, A photorealistic"
)

# 캐릭터 LoRA 키 (상호배제: 랜덤 페어에 최대 1개만 포함)
# - key1이 캐릭터면 key2 후보에서 다른 캐릭터 LoRA 제외 (역도 동일)
# - CLI -lora1/-lora2 명시 선택에는 적용되지 않음 (사용자 의도 우선)
ANIMA_CHARACTER_LORA_KEYS = {
    "lora_evie", "lora_hyeonbomi", "lora_imdana", "lora_jeongsooah",
    "lora_parkSORIM", "lora_seoheeju", "lora_woojiyoung", "lora_lasihyeon",
}


def _pick_random_lora_pair():
    """
    단독 LoRA(lora2=="") 2개를 무작위 선택 → (key1, key2)
    - mix 제외 (보조 LoRA 버려짐 방지)
    - key2: key1과 lora1 파일이 다른 것 중 선택 (같은 파일 중복 방지 → 다양성)
    - 캐릭터 LoRA(ANIMA_CHARACTER_LORA_KEYS) 상호배제: key1이 캐릭터면
      key2 후보에서 다른 캐릭터 LoRA 제외
    """
    keys = [k for k, v in ANIMA_LORA_CONFIG.items() if v[2] == "" and k != "lora_random"]
    key1 = rand.choice(keys)
    lora1_file = ANIMA_LORA_CONFIG[key1][0]
    candidates2 = [k for k in keys if k != key1 and ANIMA_LORA_CONFIG[k][0] != lora1_file]
    if key1 in ANIMA_CHARACTER_LORA_KEYS:
        candidates2 = [k for k in candidates2 if k not in ANIMA_CHARACTER_LORA_KEYS]
    fallback = [k for k in keys if k != key1]
    if key1 in ANIMA_CHARACTER_LORA_KEYS:
        fallback = [k for k in fallback if k not in ANIMA_CHARACTER_LORA_KEYS] or fallback
    key2 = rand.choice(candidates2) if candidates2 else rand.choice(fallback)
    return key1, key2

# lora_random 결과 캐시 (세션당 1회 생성 → trigger/파일 일관성 유지)
anima_random_lora_resolved = None


# ============================================================================
# CLI LoRA 인자 처리 (run_comic.py --lora1/--lora2/--lora-chg/--str1/--str2 → config.lora*_cli)
# ============================================================================
# --lora1/--lora2  : ANIMA_LORA_CONFIG 키 (plot.json의 anima_style보다 우선)
# --lora-chg episode : EP 변경 시마다 단독 LoRA 쌍을 랜덤 재선택 (lora_random 규칙 동일)
# --str1/--str2    : 강도 오버라이드 (해석된 모든 쌍에 적용 — anima_gen._apply_lora_strengths)
# [2026-09-09] --lora-chg increment는 **폐지**했다 — _norm_lora_chg()에서 차단한다.
#   사유: 총 회차(config.total_episodes)가 현재 --ep보다 작으면 ratio>1 → lora2 강도가
#   1.0을 넘어간다(실측 EP12/total=1 → str2=10.0). 설정 자체가 불가능하다.
# - unet : ANIMA_RANDOM_UNET_POOL 랜덤 (세션당 1회 캐시)
# ============================================================================
anima_cli_lora_unet = None       # CLI 모드 UNet (세션 캐시)
anima_episode_lora_cache = {}    # lora_chg=episode 모드: EP별 resolved 캐시
anima_real_unet = None           # [2026-08-28] -real 모드 메인 모델 (세션 캐시)
anima_sole_unet = None           # [2026-08-30] -sole 모드 메인 모델 (세션 캐시)


def _real_mode_active():
    """[2026-08-28] -real 모드: 모든 LoRA 무시 + ANIMA_REAL_UNET_POOL 메인 모델 랜덤"""
    return bool(getattr(config, "real_cli", False))


def _resolve_real_unet():
    """[2026-08-28] -real 모드 메인 모델 선택 (세션당 1회 캐시 → setup/실행 간 일관성)"""
    global anima_real_unet
    if anima_real_unet is None:
        anima_real_unet = rand.choice(ANIMA_REAL_UNET_POOL)
        log(f"[ANIMA REAL] 모든 LoRA 무시, 메인 모델: {anima_real_unet} (ANIMA_REAL_UNET_POOL 랜덤)")
    return anima_real_unet


def _sole_mode_active():
    """[2026-08-30] -sole 모드: LoRA 없이 ANIMA_SOLE_UNET_POOL 메인 모델 랜덤 (real 아님)
    real 모드가 우선 (real_cli가 True면 sole 무시)."""
    if _real_mode_active():
        return False
    return bool(getattr(config, "sole_cli", False))


def _resolve_sole_unet():
    """[2026-08-30] -sole 모드 메인 모델 선택 (세션당 1회 캐시 → setup/실행 간 일관성)"""
    global anima_sole_unet
    if anima_sole_unet is None:
        anima_sole_unet = rand.choice(ANIMA_SOLE_UNET_POOL)
        log(f"[ANIMA SOLE] LoRA 없이 메인 모델: {anima_sole_unet} (ANIMA_SOLE_UNET_POOL 랜덤)")
    return anima_sole_unet


anima_lora_chg_warned = set()       # increment 폐지 로그는 값당 1회만 (렌더마다 중복 방지)


def _norm_lora_chg(value):
    """lora_chg 정규화 — "episode"만 허용하고 나머지는 None(무시)으로 되돌린다.

    "increment"는 폐지: 강도 선형 증가식이 총 회차를 기준하는데 --total-episodes를
    실제 회차보다 작게 주면 ratio>1 → lora2 강도가 1.0을 초과한다(실측 EP12/total=1 → 10.0).
    """
    v = str(value or "").strip().lower()
    if v == "episode":
        return "episode"
    if v and v not in anima_lora_chg_warned:
        anima_lora_chg_warned.add(v)
        log(f"[ANIMA CLI-LoRA] lora_chg '{v}'는 지원하지 않습니다(episode만 허용, increment는 폐지) → 무시")
    return None


def _cli_lora_active():
    """CLI LoRA 인자 중 하나라도 유효하게 전달됐으면 True (increment는 폐지 → False)"""
    return bool(getattr(config, "lora1_cli", None)
                or getattr(config, "lora2_cli", None)
                or _norm_lora_chg(getattr(config, "lora_chg_cli", None)))


# ============================================================================
# [2026-09-09] LoRA 강도 오버라이드 (--str1/--str2) + 파일 실존 검사 + 트리거 주입
# ============================================================================
anima_str_warned = set()           # "--strN이 무시되었습니다" 로그는 사유당 1회
anima_lora_logged = None           # 실제로 ComfyUI에 들어간 LoRA 조합(변경 시에만 로그)


def _norm_lora_strength(value):
    """--str1/--str2 정규화 → 0.0~2.0 float 또는 None(미지정 = 튜플 값 사용).

    범위를 넘는 값은 클램프한다 (예전에 폐지된 increment가 10.0을 만들어 LoRA가 터졌다).
    """
    if value is None or value == "":
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    clamped = max(0.0, min(2.0, v))
    if v != clamped and f"clamp:{v}" not in anima_str_warned:     # 렌더마다 같은 경고를 찍지 않는다
        anima_str_warned.add(f"clamp:{v}")
        log(f"[ANIMA LoRA-STR] 강도 {v}은(는) 허용 범위(0.0~2.0) 밖 → {clamped}로 클램프")
    return round(clamped, 3)


def _apply_lora_strengths(resolved):
    """config.lora_str{1,2}_cli(--str1/--str2)를 resolved 튜플의 강도 자리에 덮어쓴다.

    모든 해석 경로(plot.json anima_style / lora_random / --lora1·--lora2 / --lora-chg)를
    통과하는 유일한 지점이라 여기서 한 번만 적용하면 된다. 슬롯에 파일이 없으면 켤 수 없다.
    """
    if not resolved:
        return resolved
    s1 = _norm_lora_strength(getattr(config, "lora_str1_cli", None))
    s2 = _norm_lora_strength(getattr(config, "lora_str2_cli", None))
    if s1 is None and s2 is None:
        return resolved
    lora1, str1, lora2, str2, unet, trigger = resolved
    for slot, s in ((1, s1), (2, s2)):
        if s is None:
            continue
        have = lora1 if slot == 1 else lora2
        if not have:
            reason = f"slot{slot}"
            if reason not in anima_str_warned:
                anima_str_warned.add(reason)
                log(f"[ANIMA LoRA-STR] --str{slot}={s}는 무시됩니다: 슬롯 {slot}에 LoRA 파일이 "
                    f"없습니다 (--real/--sole이면 LoRA가 전부 OFF입니다)")
            continue
        if slot == 1:
            str1 = s
        else:
            str2 = s
    return (lora1, str1, lora2, str2, unet, trigger)


def _apply_lora_files_exist(resolved):
    """디스크에 없는 LoRA 파일은 슬롯을 비운다(강도 0) — ComfyUI 400보다 로그가 먼저."""
    if not resolved:
        return resolved
    lora1, str1, lora2, str2, unet, trigger = resolved
    for slot in (1, 2):
        f = lora1 if slot == 1 else lora2
        if f and not _lora_exists(f):
            log(f"[ANIMA LoRA] {f} 파일이 없습니다(models/loras) → 슬롯 {slot} OFF "
                f"(ANIMA_LORA_CONFIG 경로 확인)")
            if slot == 1:
                lora1, str1 = "", 0.0
            else:
                lora2, str2 = "", 0.0
    return (lora1, str1, lora2, str2, unet, trigger)


def _sync_artist_trigger(resolved):
    """resolved의 trigger를 프롬프트 헤더 앞단(artist_anima)에 반영한다.

    comic 경로는 렌더 전에 컷 프롬프트를 미리 조립하므로, 트리거가 헤더에 들어가려면
    조립보다 앞서 resolve가 한 번 돌아야 한다 (init_anima_tags가 그 시점을 담당한다).
    """
    global artist_anima
    artist_anima = ANIMA_ARTIST_BASE + ((resolved[5] if resolved and len(resolved) > 5 else "") or "")
    return resolved


def _apply_lora_nodes(prompt, resolved):
    """resolved → ComfyUI 워크플로우 반영 (노드 122 슬롯 + 노드 46 UNet).

    ★ [2026-09-09] "--lora1이 안 먹힌다"의 1순위 원인: 템플릿의 lora_1/lora_2 기본값이
      on:false인데 기존 코드는 lora/strength만 넣고 on을 건드리지 않았다 → LoRA는 항상 꺼져 있었다.
      강도>0 이면 on=True를 여기서 반드시 켠다.
    """
    if not resolved:
        return
    lora1, str1, lora2, str2, unet, _trigger = resolved
    for slot, fname, strength in (("lora_1", lora1, str1), ("lora_2", lora2, str2)):
        node = prompt["122"]["inputs"][slot]
        if not fname or not strength:
            node["on"] = False
            node["strength"] = 0
            continue
        node["lora"] = fname
        node["strength"] = float(strength)
        node["on"] = True
    if unet:
        prompt["46"]["inputs"]["unet_name"] = unet
    global anima_lora_logged
    now = (lora1, str1, lora2, str2, unet)
    if now != anima_lora_logged:
        anima_lora_logged = now
        log(f"[ComfyUI LoRA] lora_1={lora1 or '-'}({str1}) lora_2={lora2 or '-'}({str2}) "
            f"unet={unet or '-'} | trigger={_trigger!r}")


def _resolve_cli_lora(episode):
    """
    CLI -lora1/-lora2/-lora_chg → resolved 튜플 해석
    반환: (lora1파일, str1, lora2파일, str2, unet모델, trigger) 또는 None(기본 동작 폴백)
    """
    global anima_cli_lora_unet
    lora1_key = getattr(config, "lora1_cli", None)
    lora2_key = getattr(config, "lora2_cli", None)
    lora_chg = _norm_lora_chg(getattr(config, "lora_chg_cli", None))   # increment는 여기서 차단된다

    # 키 검증: ANIMA_LORA_CONFIG에 없으면 에러 로그 + 기본 설정 폴백
    for key, label in ((lora1_key, "lora1"), (lora2_key, "lora2")):
        if key and key not in ANIMA_LORA_CONFIG:
            log(f"[ANIMA CLI-LoRA] ERROR: -{label} '{key}'가 ANIMA_LORA_CONFIG에 없음 → 기본 설정 사용")
            return None

    # UNet: ANIMA_RANDOM_UNET_POOL 랜덤 (세션당 1회 캐시 → setup/실행 간 일관성)
    if anima_cli_lora_unet is None:
        anima_cli_lora_unet = rand.choice(ANIMA_RANDOM_UNET_POOL)
        log(f"[ANIMA CLI-LoRA] unet={anima_cli_lora_unet} (ANIMA_RANDOM_UNET_POOL 랜덤)")

    # lora_chg=episode: EP 변경 시마다 단독 LoRA 쌍 랜덤 재선택 (lora_random 규칙 동일)
    if lora_chg == "episode":
        ep_idx = 0 if episode is None else max(0, episode)
        if ep_idx not in anima_episode_lora_cache:
            key1, key2 = _pick_random_lora_pair()
            cfg1, cfg2 = ANIMA_LORA_CONFIG[key1], ANIMA_LORA_CONFIG[key2]
            resolved = (cfg1[0], 1.0, cfg2[0], round(rand.uniform(0.5, 1.0), 2),
                        anima_cli_lora_unet, cfg1[5] + cfg2[5])
            anima_episode_lora_cache[ep_idx] = resolved
            log(f"[ANIMA CLI-LoRA] EP{ep_idx+1} episode 모드: {key1}(1.0) + {key2}({resolved[3]}) | trigger={resolved[5]!r}")
        return anima_episode_lora_cache[ep_idx]

    # lora1/lora2 키 모두 없으면 (lora_chg 단독 지정) → 의미 없음, 기본 동작
    if not lora1_key and not lora2_key:
        return None

    # lora1/lora2 키 모드
    lora1, str1 = "", 0.0
    if lora1_key:
        lora1, str1 = ANIMA_LORA_CONFIG[lora1_key][0], ANIMA_LORA_CONFIG[lora1_key][1]
    lora2, str2 = "", 0.0
    if lora2_key:
        lora2, str2 = ANIMA_LORA_CONFIG[lora2_key][0], ANIMA_LORA_CONFIG[lora2_key][1]

    trigger = (ANIMA_LORA_CONFIG[lora1_key][5] if lora1_key else "") \
            + (ANIMA_LORA_CONFIG[lora2_key][5] if lora2_key else "")
    return (lora1, str1, lora2, str2, anima_cli_lora_unet, trigger)


def resolve_anima_lora(json_value, episode=None):
    """LoRA 설정 해석의 단일 진입점 — 내부 해석 후 CLI 오버라이드/실존 검사/트리거를 반영한다.

    순서: (1) 모드 해석(--real > --sole > --lora1·2 > plot.json) →
          (2) --str1/--str2 강도 오버라이드 → (3) 파일 실존 검사 → (4) 트리거 → artist_anima
    """
    resolved = _resolve_anima_lora_core(json_value, episode)
    resolved = _apply_lora_strengths(resolved)
    resolved = _apply_lora_files_exist(resolved)
    return _sync_artist_trigger(resolved)


def _resolve_anima_lora_core(json_value, episode=None):
    """
    anima_style을 실제 LoRA 설정 튜플로 해석.
    반환: (lora1파일, str1, lora2파일, str2, unet모델, trigger) 또는 None

    - CLI LoRA 인자 (config.lora*_cli) : 최우선 (plot.json의 anima_style/anima_lora 덮어쓰기)
    - 일반 스타일        : ANIMA_LORA_CONFIG에서 직접 조회
    - 'lora_random'(anima_lora==1) :
        a) 단독 LoRA(lora2=="") 2개를 무작위 선택 (mix 제외 → 보조 LoRA 버려짐 방지)
        b) trigger = 두 LoRA trigger 합치기
        c) lora1 = 1.0, lora2 = 0.5~1.0
        d) unet = ANIMA_RANDOM_UNET_POOL 중 무작위
        결과를 anima_random_lora_resolved에 캐시해 anima_setup(트리거)과
        comfyui_run_anima(파일)가 동일한 쌍을 사용하도록 보장.

    Args:
        json_value: 설정 JSON
        episode: 현재 에피소드 인덱스 (0-based, CLI lora_chg 처리용, None 허용)
    """
    global anima_random_lora_resolved
    # [2026-09-07] unet 직접 지정(모든 모드 공통): plot.json "anima_unet" (빈 값 = 튜플 값 + 존재 폴백)
    unet_ovr = str(json_value.get("anima_unet", "") or "").strip()
    # [2026-08-28] -real 모드 최우선: 모든 LoRA 무시 (CLI -lora1/-lora2 포함) + 리얼 메인 모델
    if _real_mode_active():
        return _apply_unet(("", 0.0, "", 0.0, _resolve_real_unet(), ""), unet_ovr)

    # [2026-08-30] -sole 모드: LoRA 없이 ANIMA_SOLE_UNET_POOL 메인 모델 (real 아님, CLI lora보다 우선)
    if _sole_mode_active():
        return _apply_unet(("", 0.0, "", 0.0, _resolve_sole_unet(), ""), unet_ovr)

    # CLI LoRA 인자 우선 (Q4: CLI가 plot.json보다 우선)
    if _cli_lora_active():
        return _apply_unet(_resolve_cli_lora(episode), unet_ovr)

    style = json_value.get("anima_style", "")
    anima_lora = json_value.get("anima_lora", 0)

    if style == "lora_random" and anima_lora == 1:
        if anima_random_lora_resolved is None:
            # 단독 LoRA 무작위 페어 (mix 제외 + 캐릭터 LoRA 상호배제)
            key1, key2 = _pick_random_lora_pair()
            cfg1, cfg2 = ANIMA_LORA_CONFIG[key1], ANIMA_LORA_CONFIG[key2]
            lora1, str1 = cfg1[0], 1.0
            lora2, str2 = cfg2[0], round(rand.uniform(0.5, 1.0), 2)
            unet = rand.choice(ANIMA_RANDOM_UNET_POOL)
            trigger = cfg1[5] + cfg2[5]
            anima_random_lora_resolved = (lora1, str1, lora2, str2, unet, trigger)
            log(f"[ANIMA Random] {key1}({str1}) + {key2}({str2}) | unet={unet} | trigger={trigger!r}")
        return _apply_unet(anima_random_lora_resolved, unet_ovr)

    return _apply_unet(ANIMA_LORA_CONFIG.get(style), unet_ovr)


def explicit_allowed() -> bool:
    """[local 전용] 수위 상한을 explicit까지 열어두는지 (run_comic --allow-explicit / --safety explicit).

    이 repo의 기본 정책은 **청년향**(상한 nsfw)이고, explicit는 로컬에서 스위치를 켠다.
    켜지면: ① explicit→nsfw 강등 중단 ② 성기 태그 파기 중단 ③ 컷 대본 프롬프트도 성인 정책으로.
    """
    return bool(getattr(config, "explicit_cli", False))


def _cap_safety(tag: str) -> str:
    """수위 정규화의 단일 관문 — explicit 허용이 아니면 nsfw로 낮춘다."""
    t = str(tag or "").strip().lower()
    if t == "explicit" and not explicit_allowed():
        return "nsfw"
    return t if t in ("safe", "sensitive", "nsfw", "explicit") else ""


def get_safety_tag(plot_type: str, extended: str, json_value: dict, clothes: str, name: str, sex: str) -> str:
    """플롯 타입에 따라 안전 태그 결정"""
    if plot_type == "novelpia":
        return "safe, "
    elif extended == "yes":
        return config.clothes_update(json_value, clothes, name, sex)
    else:
        return "safe, "


# =====================================================================
# [2026-09-07] LLM 태그 생성 — rp_visual_tags(결정론 풀) 폐지로의 대체물
#   - 기존: get_visual_tags()/get_body_anatomy_tags()가 만든 태그를
#     _verify_and_adjust_tags()가 LLM으로 고쳤다 (호출 2회 + 고정 풀).
#   - 현재: gemma-4-31B 1회 호출이 캐릭터 설정 + 회차 요약(기승전결 가이드)에서
#     이 EP의 태그 전 세트를 생성한다. (에피소드 본문 원문은 LLM에 보내지 않는다)
#     실패 시에만 결정론 fallback.
#   - 해부학 태그(nipples/vagina/penis 등)는 사용자 정책으로 금지:
#     LLM 출력과 최종 이미지 프롬프트(comic_gen.build_panel_prompt) 양쪽에서 제거.
# =====================================================================

# [2026-09-07] 사용자 정책 변경: 노출은 nipples(유두)까지 허용, cameltoe까지 허용.
#   → nipples/areolae는 제거 대상에서 제외. 성기 직접 묘사 계열만 제거한다.
#   (clothes-상태 태그 cameltoe, bulge, cleavage, topless 등은 그대로 유지)
_ANATOMY_WORDS = (r"vagin\w*|vulvas?|puss(?:y|ies)|labia(?:l|s)?|penises|penis|"
                  r"glans|clitor\w*|clits?|foreskin|testicles?|scrotums?|futanari|perineums?")
_ANATOMY_RE = re.compile(r"(?:\w+_)*(?:" + _ANATOMY_WORDS + r")(?:_\w+)*\b", re.I)
_KO_TAG_RE = re.compile(r"[가-힣]")

_FALLBACK_LOCATIONS = [
    "a classroom", "a bedroom", "a hallway", "a park", "a cafe", "a library",
    "a rooftop", "a gym", "a bathroom", "a living room", "a garden", "a shrine",
]
_FALLBACK_TIMES = [
    "in the morning", "in the afternoon", "in the evening", "at night",
    "at dawn", "at noon", "at sunset",
]
_BASE_STATS_BY_PROGRESS = {
    0: [4, 0, 3, 2, 4, 4, 3], 1: [3, 1, 3, 2, 3, 3, 3], 2: [2, 2, 4, 3, 3, 3, 2],
    3: [1, 3, 4, 3, 2, 2, 2], 4: [0, 4, 5, 4, 2, 1, 2], 5: [0, 5, 5, 5, 1, 1, 1],
}


def strip_anatomy_tags(text: str) -> tuple:
    """성기 직접 묘사 태그(vagina/pussy/penis/clitoris 등) 제거 → (클린 문자열, 제거 수)

    [2026-09-07] 정책: nipples/areolae/cameltoe/topless 노출은 허용 — 성기 계열만 걷어낸다.
    쉼표 단위 태그(erect_penis_through_clothes, pink pussy …)와
    산문 문장("His penis is in her mouth.") 양쪽에서 동작한다.
    밑줄 태그는 통째로, 문장 안에서는 해당 어구만 걷어내 어조를 남긴다.
    """
    if not text:
        return text or "", 0
    # [2026-09-09] local explicit 모드: 성기 태그를 걷어내면 explicit가 결국 청년향과 같은 그림이 된다
    #   → 스위치가 켜져 있으면 파기 자체를 하지 않는다.
    if explicit_allowed():
        return text, 0
    removed, kept = 0, []
    for part in text.split(","):
        p = part.strip()
        if not p:
            continue
        if _ANATOMY_RE.search(p):
            removed += 1
            c = _ANATOMY_RE.sub(" ", p)
            c = re.sub(r"^\s*(?:his|her|the|a|an|with|and|has)\b\s*", "", c, flags=re.I)
            c = re.sub(r"\s*(?:and|with|has)\b\s*$", "", c, flags=re.I)
            c = re.sub(r"\s{2,}", " ", c).strip(" ,.;:")
            if not c or re.fullmatch(r"[^\w]*", c):
                continue
            kept.append(c)
        else:
            kept.append(p)
    return ", ".join(kept), removed


def _llm_tag_str(v) -> str:
    """LLM 태그 값 → 소문자 영문 태깅 문자열 (한글 조각 파기 + 해부학 태그 제거)"""
    s = str(v or "").strip().replace("\n", " ")
    parts = []
    for p in s.split(","):
        p = p.strip().strip('"').lower()
        if not p or _KO_TAG_RE.search(p):
            continue
        parts.append(p)
    out, _ = strip_anatomy_tags(", ".join(parts))
    return out


def _guideline_lines(ep_num: int) -> list:
    """이 회차의 기승전결 요약( comic_input이 ①에서 만들어 config에 넣은 가이드).
    에피소드 본문 자체는 LLM에 보내지 않으므로 태그 생성의 줄거리 입력은 이것이 전부다."""
    m = getattr(config, "ep_corruption_guides_map", {}) or {}
    entry = m.get(ep_num) or m.get(str(ep_num)) or {}
    lines = []
    for who in ("protagonist", "partner", "sub"):
        for g in entry.get(who, []) or []:
            g = str(g).strip()
            if g:
                lines.append(f"- [{who}] {g}")
    return lines


def _generate_tags_via_llm(episode: int, client=None) -> dict:
    """LLM 1회 → 이 EP의 태그 세트. client 없음/실패/파싱 실패 → 결정론 fallback (예외 미발생).

    [2026-09-07] 에피소드 본문 원문은 LLM에 보내지 않는다: 줄거리 입력은 ① 추출 단계가 만든
    기승전결 가이드(config.ep_corruption_guides_map) + $행동 키워드만 사용한다.

    key: stats[7], face, makeup, marks, exposure, parts_exposure, body, bodystyle,
         background, expressions(list), partner_exposure, partner_expression,
         location, time_of_day, safety(""|safe|sensitive|nsfw|explicit)
    """
    ep_num = episode + 1
    total = max(1, int(getattr(config, "total_episodes", 1) or 1))
    fb_idx = min(int((episode / max(total - 1, 1)) * 5), 5)
    try:
        rating = config.review_safety[episode] or ""
    except Exception:
        rating = ""
    try:
        dollar = list((getattr(config, "special_writing_req", {}) or {}).get(ep_num, []) or [])
    except Exception:
        dollar = []

    fb = {
        "stats": _BASE_STATS_BY_PROGRESS[fb_idx],
        "face": "blushing", "makeup": "natural makeup", "marks": "",
        "exposure": _llm_tag_str(config.clothes) or "casual clothes",
        "parts_exposure": "",
        "body": _llm_tag_str(config.body_shape) or "slim body",
        "bodystyle": "", "background": "",
        "expressions": ["blushing", "closed eyes", "open mouth", "shy smile", "looking at viewer"],
        "partner_exposure": "", "partner_expression": "",
        "location": _FALLBACK_LOCATIONS[episode % len(_FALLBACK_LOCATIONS)],
        "time_of_day": _FALLBACK_TIMES[episode % len(_FALLBACK_TIMES)],
        "safety": "",
    }
    if not client:
        log(f"[TAGS_LLM] EP{ep_num} client 없음 → fallback 태그 사용")
        return fb

    guides = "\n".join(_guideline_lines(ep_num)) or "(가이드 없음 — 캐릭터 설정과 수위로 판단)"
    # [2026-09-09] --allow-explicit(local)이면 LLM이 explicit를 고를 수 있게 정책을 풀어준다
    #   (기본은 청년향 — 아래 세 줄이 그 상한을 문자열로 못 박은 곳이므로 여기서 분기한다).
    if explicit_allowed():
        role_line = "당신은 **성인(explicit) 만화**의 태그 디렉터입니다."
        safety_line = "- safety: safe|sensitive|nsfw|explicit 중 하나 (local explicit 모드 — explicit 허용)"
        anatomy_line = ("성기 태그(vagina, pussy, vulva, penis, clitoris 등)도 사용할 수 있습니다 — explicit 모드.\n"
                        "노출은 topless, visible nipples, cameltoe, cleavage를 지나 성기 노출까지 허용합니다.")
    else:
        role_line = "당신은 청년향 만화(노출 최대 상체까지, 성기 직접 묘사 없음)의 태그 디렉터입니다."
        safety_line = "- safety: safe|sensitive|nsfw 중 하나 (청년향 — explicit 쓰지 않음)"
        anatomy_line = ("금지 태그: vagina, pussy, vulva, labia, penis, glans, futanari, clitoris, testicles/scrotum 계열과 그것들을 품은 태그 전부.\n"
                        "노출은 topless, visible nipples, cameltoe, cleavage까지 허용 — 성기는 절대 보이지 않게.")
    prompt = f"""{role_line} 아래 [캐릭터 설정]과 [이 회차의 줄거리 요약]만으로 이 회차 이미지 프롬프트에 넣을 Danbooru 태그를 생성하세요.

[주인공] {config.name} ({config.sex}) — 머리: {config.hair_color}, {config.hair_style} / 눈: {config.eye_color} / 피부: {config.skin_color} / 복장: {config.clothes} / 몸: {config.body_shape}
[상대방] {config.name2} ({config.sex2}) — 복장: {config.outfit2}
[본문에 등장하는 행동(참고)] {", ".join(dollar) if dollar else "(없음)"}
[수위 참고] {rating or "(자동 판단)"}

[이 회차의 줄거리 요약 (기승전결)]
{guides}

출력 필드 (모두 소문자 영문 danbooru 태그만, 쉼표 구분. 한국어/설명문/산문 금지):
- face: 이 회차의 **기본** 표정·얼굴 상태 — 모든 컷에 그대로 붙는다. 그래서 평범하게 쓴다
  (soft smile, thoughtful, calm 등). 눈이 뒤집히거나 혀가 나오는 **극단 표정은 쓰지 않는다**
  (그렇게 하면 일상 컷까지 그 표정으로 고정된다). 극단 표정은 expressions 풀에 최대 1개만
- makeup: 화장 (없으면 natural makeup)
- marks: 몸에 남은 자국·장신구 (예: choker, sweat, tear trail)
- exposure: 이 회차의 복장 상태 (예: school uniform, open shirt, wet clothes, topless)
- parts_exposure: 복장 밖으로 드러나는 부위 (예: cleavage, cameltoe, see-through clothes, navel)
- body: 체형·질감 (예: slim body, large breasts, pale skin, oily skin)
- bodystyle: 자세·습관 (예: standing, kneeling, shy posture) + 복장 추가 아이템 (예: police cap)
- background: 배경 효과 태그 (예: dim light, floating hearts, rain)
- expressions: 얼굴 클로즈업 패널용 표정 문구 정확히 5개 (배열) — **서로 다른 감정**으로 다양하게
  (기쁨/당황/쓸쓸/불안/평온… 같은 결). 같은 결의 변주(blushing×5)나 극단 표정 몰아주기는 금지
- partner_exposure: 상대방의 옷 상태 / partner_expression: 상대방 표정 1개
- location: 장소 (영문 3~5단어, 예: a bedroom), time_of_day: 시간대 (영문 3단어 이내, 예: at night)
- stats: 주인공 심리 7수치 0~5 정수. M=도덕성, L=두근거림(설렘), A=호감도, O=복종도, I=지성, S=수치심, D=주도권. 줄거리 기준 판단 (초반 M 높고 L·S 낮음 / 후반 반대).
{safety_line}

{anatomy_line}
인물은 기본 주인공 혼자(solo). 상대방은 pov hands, 손, 어깨 등 부분 묘사만.
주인공의 머리색/눈색/이름/직업은 태그에 다시 넣지 마세요(프롬프트 헤더에 이미 들어갑니다).

JSON 하나만 출력 (설명·코드펜스 금지):
{{"stats":{{"M":4,"L":1,"A":3,"O":2,"I":4,"S":4,"D":2}},"face":"...","makeup":"...","marks":"...","exposure":"...","parts_exposure":"...","body":"...","bodystyle":"...","background":"...","expressions":["...","...","...","...","..."],"partner_exposure":"...","partner_expression":"...","location":"a bedroom","time_of_day":"at night","safety":"nsfw"}}
"""
    try:
        messages = [{"role": "system", "content": config.system_prompt_anima +
                                " Your only job: output danbooru image tags as strict JSON."},
                    {"role": "user", "content": prompt}]
        data = None
        for _try in (1, 2):
            try:
                _, result = openAI_response(config.get_json_value(), client, messages, "", 1, False,
                                            call_label=f"TAGS_LLM_EP{ep_num}" + ("" if _try == 1 else "_retry"))
                data = _extract_json(result)
            except Exception as ex:
                log(f"[TAGS_LLM] EP{ep_num} {_try}회 호출 실패: {ex}")
                data = None
            if isinstance(data, dict) and data:
                break
            if _try == 1:
                log(f"[TAGS_LLM] EP{ep_num} 1회 응답을 읽지 못해 재시도합니다(결정론 fallback보다 싸다)")
                data = None
    except Exception as e:
        log(f"[TAGS_LLM] EP{ep_num} LLM 호출 실패 → fallback 태그: {e}")
        return fb
    if not isinstance(data, dict) or not data:
        log(f"[TAGS_LLM] EP{ep_num} JSON 파싱 실패(2회 시도) → fallback 태그")
        return fb

    out = dict(fb)
    try:
        sd = data.get("stats") or {}
        keys = ("M", "L", "A", "O", "I", "S", "D")
        out["stats"] = [max(0, min(5, int(sd.get(k, fb["stats"][i])))) for i, k in enumerate(keys)]
    except Exception:
        pass
    for k in ("face", "makeup", "marks", "exposure", "parts_exposure", "body", "bodystyle",
              "background", "partner_exposure", "partner_expression"):
        v = _llm_tag_str(data.get(k))
        if v:
            out[k] = v
    exprs = []
    for x in (data.get("expressions") or []):
        e2 = _llm_tag_str(x)
        if e2 and e2 not in exprs:
            exprs.append(e2)
    if exprs:
        out["expressions"] = exprs[:5]
    loc = str(data.get("location") or "").strip().strip('\"\'.')
    if loc and not _KO_TAG_RE.search(loc) and len(loc) <= 60:
        out["location"] = loc
    tod = str(data.get("time_of_day") or "").strip().strip('\"\'.')
    if tod and not _KO_TAG_RE.search(tod) and len(tod) <= 30:
        out["time_of_day"] = tod
    safety = _cap_safety(data.get("safety"))
    if safety:
        out["safety"] = safety
    log(f"[TAGS_LLM] EP{ep_num} 생성 완료: face={out['face']!r} exposure={out['exposure']!r} "
        f"safety={out['safety'] or '(auto)'}")
    return out


def init_anima_tags(episode: int, client=None, json_value: dict = None) -> dict:
    """
    [2026-09-07] rp_visual_tags 결정론 풀 폐지 → LLM(gemma-4-31B) 1회 호출로
    이 EP의 태그 일체(face/makeup/marks/exposure/parts/body/bodystyle/background/
    expressions/partner/location/stats/safety)를 생성한다.
    입력은 캐릭터 설정 + 회차 요약(가이드)까지이며 에피소드 본문 원문은 LLM에 들어가지 않는다.
    config.*_tag 배열과 config.body_shape, config.current_level을 채운다.
    해부학 정책([2026-09-07] 갱신): 성기 계열(vagina/penis 등)은 LLM 출력·최종 프롬프트에서
    제거, nipples/cameltoe 노출은 허용.
    양쪽에서 strip_anatomy_tags()로 제거한다.

    Args:
        episode: 에피소드 번호 (0-indexed)
        client: OpenAI 클라이언트 (태그 생성용, None이면 결정론 fallback)
        json_value: 설정 JSON (None이면 config.get_json_value() 사용)

    Returns:
        {"status": "ok"|"missing"|"no_episode", "fields": {...}, ...}
    """

    # Init eye shape
    if config.eye_shape == "":
        config.eye_shape = _det_choice(["tareme", "jitome", "tsurime", "sanpaku"],
                                       f"eye|{episode}|{getattr(config, 'name', '')}")
    if zlib.crc32(f"eyenarrow|{episode}".encode("utf-8")) % 2 == 0:
        config.eye_shape += ", narrow eyes"

    _ensure_log_files()
    if json_value is None:
        json_value = config.get_json_value()

    # [2026-09-09] LoRA 트리거를 헤더 앞단(artist_anima)에 먼저 심는다.
    #   comic_gen은 렌더 전에 컷 프롬프트를 미리 조립하므로, 이 시점을 놓치면 트리거가 없는
    #   그림이 나온다(LoRA 파일은 올라가는데 화풍은 안 변한다).
    try:
        resolve_anima_lora(json_value, episode)
    except Exception as e:
        log(f"[ANIMA trigger] EP{episode + 1} 트리거 동기화 실패(무시하고 계속): {e}")

    # === 0. 필수 config 값 검증 (character_sheet에서 설정된 값 사용) ===
    required_fields = [
        ("name", config.name),
        ("sex", config.sex),
        ("hair_color", config.hair_color),
        ("hair_style", config.hair_style),
        ("eye_color", config.eye_color),
        ("skin_color", config.skin_color),
        ("face_style", config.face_style),
        ("clothes", config.clothes),
        ("body_shape", config.body_shape),
        ("job", config.job),
    ]
    missing = [name for name, val in required_fields if not val or not str(val).strip()]
    if missing:
        log(f"[init_anima_tags] 필수 값 누락: {missing}")
        return {"status": "missing", "fields": missing}

    # 에피소드 번호 검증
    if episode < 0 or episode >= config.total_episodes:
        log(f"[init_anima_tags] 유효하지 않은 에피소드 번호: {episode}")
        return {"status": "no_episode", "episode": episode}
    
    # 에피소드 내용 확인
    episode_content = get_episode_content(episode)
    if not episode_content or not episode_content.strip():
        log(f"[init_anima_tags] EP{episode + 1} 내용이 비어있습니다.")
        return {"status": "no_episode", "episode": episode}
    
    # === 1. 에피소드 정보 로깅 ===
    log(f"\n{'='*60}")
    log(f"[init_anima_tags] EP{episode + 1} 처리 시작")
    log(f"  총 에피소드 수: {config.total_episodes}")
    log(f"  에피소드 내용 길이: {len(episode_content)}자 (원문은 LLM에 보내지 않음, 앞 120자만 로그)")
    log(f"  미리보기: {episode_content.strip()[:120]}...")
    log(f"{'='*60}")
    
    # === 2. LLM 1회 호출 → EP 태그 일체 생성 (rp_visual_tags 풀 폐지)
    # 본문 원문은 보내지 않고, ①에서 만든 가이드/키워드/수위만 보낸다. ===
    sex = config.sex
    tagset = _generate_tags_via_llm(episode, client)
    stats = tagset["stats"]
    face_tag = tagset["face"]
    makeup_tag = tagset["makeup"]
    marks_tag = tagset["marks"]
    exposure_tag = tagset["exposure"]
    p_exposure_tag = tagset["parts_exposure"]
    body_tag = tagset["body"]
    bodystyle_tag = tagset["bodystyle"]
    background_tag = tagset["background"]
    face_tag_array = tagset["expressions"]
    config.review_stats[episode] = stats
    log(f"[init_anima_tags] EP{episode + 1} LLM 태그 생성: stats={stats}")

    # ★ config 배열 저장 → comic_gen.build_panel_prompt → anima_gen._build_tag_block이 읽는다
    config.face_tag[episode] = face_tag
    config.makeup_tag[episode] = makeup_tag
    config.marks_tag[episode] = marks_tag
    config.body_tag[episode] = body_tag
    config.bodystyle_tag[episode] = bodystyle_tag
    config.exposure_tag[episode] = exposure_tag
    config.p_exposure_tag[episode] = p_exposure_tag
    config.background_tag[episode] = background_tag
    # [2026-08-30] -real 전용: stats[1] 두근거림(L) 레벨별 체모 태그 (_build_tag_block이 real 모드일 때만 주입 — comic 기본 경로 아님)
    _l_level = min(5, max(0, int(stats[1])))
    _pubic_map = {0: "hairless_pubic_region", 1: "pubic_stubble", 2: "pubic_hair",
                  3: "bushy_pubic_hair", 4: "bushy_pubic_hair", 5: "bushy_pubic_hair"}
    config.pubic_hair_tag[episode] = _pubic_map.get(_l_level, "bushy_pubic_hair")

    # === 3. 상대방(PARTNER) 태그: LLM 우선, 없으면 결정론 풀 폴백 (주인공 태그와 물리 분리) ===
    p_level = min(max(stats[1], 0), 5)
    p_gender_key = "male" if sex in ("male", "남자", "남성") else "female"
    config.partner_exposure_tag[episode] = tagset.get("partner_exposure") or \
        _det_choice(_PARTNER_EXPOSURE_POOLS[p_gender_key][p_level],
                    f"pexp|{episode}|{p_level}|{p_gender_key}")
    config.partner_expression_tag[episode] = tagset.get("partner_expression") or \
        _det_choice(_PARTNER_EXPRESSION_POOL, f"pexpr|{episode}")

    # === 4. 표정 배열/레벨/수위/장소 ===
    config.expression_arr[episode] = ", ".join(face_tag_array)
    config.expression = ", ".join(face_tag_array)
    config.current_level = stats[1]

    if not config.review_safety[episode]:
        safety_tag = _cap_safety(tagset.get("safety") or get_safety_tag(
            json_value.get("plot", ""), json_value.get("extended", "no"),
            json_value, config.clothes, config.name, config.sex
        ))
        config.review_safety[episode] = safety_tag
        log(f"[init_anima_tags] EP{episode + 1} safety_tag 결정: {safety_tag}")
    else:
        config.review_safety[episode] = _cap_safety(config.review_safety[episode])
        log(f"[init_anima_tags] EP{episode + 1} 기존 review_safety 사용: {config.review_safety[episode]}")

    config.location = tagset.get("location") or ""
    config.time_of_day = tagset.get("time_of_day") or ""   # [2026-09-07] 생성만 되던 필드 → 배경 사용
    config.episode_num = episode

    # === 5. body_shape 보완 (comic_input이 영문 태그로 주입한 값 우선) ===
    if not str(config.body_shape or "").strip():
        body_only = [t.strip() for t in body_tag.split(",") if t.strip()
                     and not any(k in t for k in ("breast", "hip", "skin"))]
        config.body_shape = ", ".join(body_only) if body_only else "slim body"

    log(f"\n[init_anima_tags] EP{episode + 1} 태그 초기화 완료 (LLM 생성)")
    log(f"  face_tag   : {face_tag}")
    log(f"  exposure   : {exposure_tag} / parts: {p_exposure_tag}")
    log(f"  body       : {body_tag}")
    log(f"  bodystyle  : {bodystyle_tag}")
    log(f"  marks      : {marks_tag} / makeup: {makeup_tag}")
    log(f"  background : {background_tag} / location: {config.location} ({tagset.get('time_of_day')})")
    log(f"  expressions: {face_tag_array}")
    log(f"  body_shape : {config.body_shape} / current_level: {config.current_level}")

    return {"status": "ok", "fields": {name: str(val).strip() for name, val in required_fields}}

def _comfyui_output_dirs(json_value: dict = None) -> list:
    """ComfyUI 출력 후보 디렉토리 (실존하는 것만, 우선순위 순서)

    1) 환경변수 COMFYUI_OUTPUT_DIR
    2) plot.json comfyuidir/output  ← 경로를 지시하는 유일한 설정(플레이스홀더가 아닌 실존 경로일 때만)
    3) ~/AI/ComfyUI/output (이 프로젝트 관행 설치 위치로 폴백 — 코드에 개인/기기 절대경로는 없다)
    """
    cands = []
    env = os.environ.get("COMFYUI_OUTPUT_DIR", "").strip()
    if env:
        cands.append(env)
    if json_value is None:
        try:
            json_value = config.get_json_value()
        except Exception:
            json_value = {}
    cfgd = str((json_value or {}).get("comfyuidir", "") or "").strip()
    if os.path.isabs(cfgd):
        # endswith("/output")만 보면 Windows 경로("D:\\ComfyUI\\output")에서 output이 중복 붙는다
        cands.append(cfgd if os.path.basename(cfgd.rstrip("\\/")) == "output"
                     else os.path.join(cfgd, "output"))
    # plot.json에 경로가 없거나 잘못된 경우의 폴백 — OS별 흔한 설치 위치(linux/Windows 아키라/macOS)
    cands.append(os.path.expanduser("~/AI/ComfyUI/output"))
    cands.append(os.path.expanduser("~/ComfyUI/output"))
    sysd = str(os.environ.get("SYSTEMDRIVE", "C:")).rstrip("\\/") + "\\"
    cands.append(sysd + "ComfyUI-aki-v3\\ComfyUI\\output")   # 아키라(Aki) 설치형 기본 위치
    cands.append(sysd + "ComfyUI\\output")
    out, seen = [], set()
    for d in cands:
        if d and d not in seen and os.path.isdir(d):
            out.append(d)
            seen.add(d)
    return out


def _wait_and_copy_image(prefix: str, json_value: dict, min_mtime: float = 0.0, wait_seconds: int = 120):
    """ComfyUI 이미지 생성 대기 후 ./image로 복사

    [2026-09-07] 출력 디렉토리를 하드코딩 하나(`~/AI/ComfyUI/output`)로만 보면
    현재 정본 설치본(*/AIDATA/AI/ComfyUI)에서 생긴 파일을 영영 못 찾아서 컷당 120초를 낭비한다
    (실측: /home 쪽은 9-01 이후 안 쓰이는 과거 백업, AIDATA 쪽이 최신 출력).
    → 후보 디렉토리 전부를 순회하며 찾는다 (`_comfyui_output_dirs`).

    min_mtime: 큐 시각보다 이전 파일은 '이전 실행의 결과'다. SaveImage는 같은 prefix에서
    _00001_ → _00002_ 로 번호만 올릴 뿐 옛 파일을 남기므로, 이름만 보면 옛 이미지를 집어와버린다
    (실측: 재렌더가 4.6초 만에 '완료'). → mtime으로 새 생성분만 받는다.
    """
    target_dir = "./image"
    os.makedirs(target_dir, exist_ok=True)
    dirs = _comfyui_output_dirs(json_value)
    if not dirs:
        log(f"[ComfyUI] 출력 후보 디렉토리가 없음 — 복사 생략 (prefix={prefix})")
        return
    # [2026-09-07] 파일명이 되는 prefix는 SaveImage에서 50자로 잘린다(comfyui_run_anima의 prefix[:50]).
    # comic 접두어(episode_N_comic_eN_pNN_…_anima_)는 50자를 넘어 잘린 형태로 저장되므로
    # 원본 prefix와 50자 절단형 둘 다 매치한다. (이것 때문에 이미지가 있는데도 '실패'로 보였다.)
    keys = [prefix]
    if len(prefix) > 50:
        keys.append(prefix[:50])

    # [2026-09-07] ./image에 이미 같은 prefix 사본이 있으면 대기 루프가 0초 만에 끝나서
    # '이전 실행의 이미지'를 그대로 쓴다(실측: 8컷 재렌더가 4.8초 만에 '완료').
    # → 먼저 옛 사본을 지우고, 이번 큐에서 새로 만들어진 파일을 받아온다
    #   (ComfyUI SaveImage는 output에서 이름을 _00001_ → _00002_로 증가시키므로 덮어쓰지 않는다).
    for f in os.listdir(target_dir):
        if f.endswith(".png") and any(k and f.startswith(k) for k in keys):
            try:
                os.remove(os.path.join(target_dir, f))
                log(f"[ComfyUI] 옛 사본 제거: {f}")
            except OSError:
                pass

    # 최대 wait_seconds 대기
    for i in range(max(1, int(wait_seconds))):
        found = False
        for comfyui_output in dirs:
            try:
                names = os.listdir(comfyui_output)
            except OSError:
                continue
            for f in names:
                if f.endswith(".png") and any(f.startswith(k) and k for k in keys):
                    src = os.path.join(comfyui_output, f)
                    try:
                        size = os.path.getsize(src)
                    except OSError:
                        continue
                    if size > 0:
                        try:
                            mtime = os.path.getmtime(src)
                        except OSError:
                            continue
                        if min_mtime and mtime < min_mtime:
                            continue          # 이전 실행이 만든 파일 → 이번 결과가 아니다
                        shutil.copy2(src, os.path.join(target_dir, f))
                        log(f"[ComfyUI] Copied: {f} <- {comfyui_output} -> ./image/")
                        found = True
                        break
            if found:
                break
        if found:
            break
        time_mod.sleep(1)

    return


def queue_prompt(prompt):
    """ComfyUI에 프롬프트 큐에 추가

    [2026-09-07] 400(검증 실패 — 예: 없는 unet/lora 파일)의 원문이 버려지고 있었다.
    body를 로그에 남긴다 (모델 파일명 오타/미다운로드 진단용).
    """
    p = {"prompt": prompt}
    data = json.dumps(p).encode('utf-8')
    req = request.Request("http://localhost:8188/prompt", data=data)
    try:
        return request.urlopen(req)
    except request.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "ignore")[:2000]
        except Exception:
            detail = ""
        log(f"[ComfyUI] /prompt HTTP {e.code}: {detail}")
        raise


def comfyui_run_anima(json_value, episode, full_prompt, res, client=None,
                       seed=None, queue_count: int = 2) -> str:
    """[2026-09-08⑥] 만화(comic)용 확장: seed/queue_count 지정 가능, 생성 prefix 반환

    seed=None, queue_count=2 → 기존 동작과 100% 동일(랜덤 시드 2장 큐). 만화는 (seed, 1)을 쓴다.
    (본래: ComfyUI 실행 함수 — llm_def.py에서 가져옴)
    """
    global anima_nametag
    
    # [2026-09-04] 최종 프롬프트 가중치 태그 중복 제거 (tag_out.txt 6245행 사례: qwen 커레이션
    # 퇴행 루프로 같은 태그 2~3회 반복). 여기 단일 지점에서 제거하면 ComfyUI 전달분과
    # tag_out.txt/json 기록이 항상 일치한다.
    _deduped = _dedupe_weighted_tags(full_prompt)
    if _deduped != full_prompt:
        log(f"[TAG DEDUP] 가중치 태그 중복 제거: {len(full_prompt)} → {len(_deduped)}자")
        full_prompt = _deduped
    
    json_file = "data_comfyui/anima_spectrum_July11.json"
    with open(json_file) as f:
        prompt = json.load(f)
    
    # 랜덤 시드 ([2026-09-08⑥] seed 지정 시에는 고정 — 만화 컷 재현성 확보)
    a = int(seed) % (18446744073709551615 + 1) if seed is not None else rand.randint(0, 18446744073709551615)
    prompt["1016"]["inputs"]["seed"] = a
    
    # LoRA 설정
    # [2026-08-28] -real 모드: 모든 LoRA OFF (템플릿 기본 lora_3/4 detailer 포함) + 리얼 메인 모델
    if _real_mode_active():
        for _lk in ("lora_1", "lora_2", "lora_3", "lora_4"):
            prompt["122"]["inputs"][_lk]["on"] = False
            prompt["122"]["inputs"][_lk]["strength"] = 0
        prompt["46"]["inputs"]["unet_name"] = _resolve_real_unet()
    # [2026-08-30] -sole 모드: 모든 LoRA OFF (템플릿 기본 lora_3/4 detailer 포함) + SOLE 메인 모델
    elif _sole_mode_active():
        for _lk in ("lora_1", "lora_2", "lora_3", "lora_4"):
            prompt["122"]["inputs"][_lk]["on"] = False
            prompt["122"]["inputs"][_lk]["strength"] = 0
        prompt["46"]["inputs"]["unet_name"] = _resolve_sole_unet()
    # CLI LoRA 인자 전달 시 anima_lora=0 강제 OFF 무시 (CLI 우선)
    elif json_value.get("anima_lora", 0) == 0 and not _cli_lora_active():
        prompt["122"]["inputs"]["lora_1"]["strength"] = 0
        prompt["122"]["inputs"]["lora_2"]["strength"] = 0
        prompt["122"]["inputs"]["lora_3"]["strength"] = 0
        prompt["122"]["inputs"]["lora_4"]["strength"] = 0
    
    # LoRA 설정: 통합 config(ANIMA_LORA_CONFIG)에서 해석 (lora_random/CLI LoRA/--str1·--str2 포함)
    _apply_lora_nodes(prompt, resolve_anima_lora(json_value, episode))
    
    # [2026-08-30] 최종 prompt '_' → 공백 (score_N만 '_' 유지): anima 모델이 danbooru tag의 '_'를 인식 못 하는 경우 방지
    prompt["86"]["inputs"]["text"] = _sanitize_prompt_underscores(full_prompt)
    prompt["123"]["inputs"]["width"] = resol[int(res)][0]
    prompt["123"]["inputs"]["height"] = resol[int(res)][1]

    # Add negative nsfw, explicit if necessary 
    if anima_nametag.find("_pov_") > -1:
        # [2026-09-05] 2girls 추가: POV는 1인 피사체(헤더의 1girl/1boy + solo)가 정상이므로
        #   두 번째 소녀(관찰자 여성/주인공 복제)도 음수 처리한다.
        prompt["87"]["inputs"]["text"] = f"""
2girls, 2boys, 3girls, 3boys, score_1, score_2, score_3, blurry, worst quality, low quality, jpeg artifacts, signature, watermark, username, deformed hands, bad anatomy, extra limbs, poorly drawn hands, poorly drawn face, mutation, deformed, extra eyes, extra arms, extra legs, malformed limbs, fused fingers, too many fingers, long neck, cross-eyed, bad proportions, missing arms, missing legs, extra digit, fewer digits, cropped, normal quality, (multiple views:2.0), (split view:2.0), (collage:2.0), (grid view:2.0), (clones:2.0), smudged makeup, running makeup, smeared eyeliner
""".strip()
    else:        
        prompt["87"]["inputs"]["text"] = f"""
3girls, 3boys, score_1, score_2, score_3, blurry, worst quality, low quality, jpeg artifacts, signature, watermark, username, deformed hands, bad anatomy, extra limbs, poorly drawn hands, poorly drawn face, mutation, deformed, extra eyes, extra arms, extra legs, malformed limbs, fused fingers, too many fingers, long neck, cross-eyed, bad proportions, missing arms, missing legs, extra digit, fewer digits, cropped, normal quality, (multiple views:2.0), (split view:2.0), (collage:2.0), (grid view:2.0), (clones:2.0), smudged makeup, running makeup, smeared eyeliner
""".strip()

    # 파일명 (./image 디렉토리로 저장)
    # event{숫자} 제거, 특수 기호 제거, 중복 언더스코어 정리
    #clean_nametag = re.sub(r"event\d+", "", anima_nametag)
    clean_nametag = re.sub(r"[^a-zA-Z0-9_]", "", anima_nametag)
    clean_nametag = re.sub(r"_+", "_", clean_nametag).strip("_")
    prefix = f"episode_{config.episode_num+1}_{clean_nametag}_anima_"
    prompt["91"]["inputs"]["filename_prefix"] = prefix[:50]
    
    # 태그 저장 (log 디렉토리에 JSON + 텍스트 2개 출력)
    os.makedirs("./log", exist_ok=True)
    os.makedirs("./image", exist_ok=True)
    
    # 1) JSON 포맷 출력
    tag_data = {
        "prefix": prefix,
        "anima_nametag": anima_nametag,
        "episode": config.episode_num + 1,
        "mode": res,
        "full_prompt": full_prompt,
        "seed": prompt["1016"]["inputs"]["seed"]
    }
    with open("./log/tag_out.json", "a", encoding="utf-8") as tag_file:
        tag_file.write(json.dumps(tag_data, ensure_ascii=False) + "\n")
    
    # 2) 일반 텍스트 포맷 출력 (디버깅 목적)
    with open("./log/tag_out.txt", "a", encoding="utf-8") as txt_file:
        txt_file.write(f"--- {prefix} ---\n")
        txt_file.write(f"Episode : {config.episode_num + 1}\n")
        txt_file.write(f"Mode    : {res}\n")
        txt_file.write(f"Nametag : {anima_nametag}\n")
        txt_file.write(f"Seed    : {prompt['1016']['inputs']['seed']}\n")
        txt_file.write(f"Prompt  : \n{full_prompt}\n")
        txt_file.write("\n")
    
    # 기존 호환성 유지
    my_dict = {}
    my_dict[prefix] = prefix
    with open("./image/tag_out.json", "a", encoding="utf-8") as tag_file:
        tag_file.write(json.dumps(my_dict, ensure_ascii=False) + "\n")
    
    # ComfyUI 큐에 추가 (queue_count장; 기본 2 = 기존 동작)
    queue_prompt(prompt)
    if int(queue_count) > 1:
        a = rand.randint(0, 18446744073709551615)
        prompt["1016"]["inputs"]["seed"] = a

        # 두 번째 이미지 (다른 시드)
        queue_prompt(prompt)

    log(f"[ComfyUI] Queue sent! mode={res}, episode={config.episode_num+1}, nametag={anima_nametag}")

    # 이미지 생성 대기 후 ./image로 복사 (아카이브에서 일괄 복사하므로 스킵)
    # _wait_and_copy_image(prefix, json_value)
    return prefix

# 해상도 매핑 (원본 llm_def.py:43-47)
resol = [""] * 10
resol[3] = [1344, 1024]
resol[4] = [1280, 1280]
resol[5] = [1024, 1344]
resol[6] = [1024, 1344]
resol[7] = [1280, 1344]  # [2026-08-27] 2인물 사이드뷰 와이드-포트레이트 (1280x1344)
resol[8] = [1024, 1366]  # [2026-08-30] SIMPLE 전용 tall (LLM aspect_ratio=tall)
resol[9] = [1366, 1024]  # [2026-08-30] SIMPLE 전용 wide (LLM aspect_ratio=wide)


def _split_bodystyle_clothes(bodystyle_tag: str) -> tuple:
    """bodystyle_tag를 (clothes_tokens, style_tokens)로 분리 (init_anima_tags 10-3-2와 동일한 키워드)"""
    clothes_keywords = ["uniform", "cap", "hat", "dress", "shirt", "blouse", "skirt", "pants", "trousers",
                        "sock", "stocking", "pantyhose", "shoe", "boots", "coat", "jacket", "sweater",
                        "apron", "vest", "tie", "ribbon", "bow", "scarf", "glove", "bracelet",
                        "fully_clothed", "partially_undressed", "clothed"]
    if not bodystyle_tag:
        return "", ""
    tokens = [t.strip() for t in bodystyle_tag.split(',') if t.strip()]
    clothes_tokens = [t for t in tokens if any(kw in t.lower() for kw in clothes_keywords)]
    style_tokens = [t for t in tokens if not any(kw in t.lower() for kw in clothes_keywords)]
    return ", ".join(clothes_tokens), ", ".join(style_tokens)


def _protagonist_has_glasses() -> bool:
    """[2026-08-27] 주인공 안경 유무: face_style에 eyewear/glasses 태그가 있으면 True
    (character_setup에서 json_value['glasses'] 확률로 Eyewear.txt 태그를 face_style에 추가)"""
    fs = (getattr(config, 'face_style', '') or '').lower()
    return ("eyewear" in fs) or ("glasses" in fs)


# ============================================================================
# [2026-08-28] 상대방(PARTNER) 태그 세트 — 주인공 태그 오염 방지
# ============================================================================
# 문제: 2인물/POV에서 상대방 태그가 불완전하면(예: makeup 언급 없음) LLM/이미지모델이
#       주인공의 makeup/skin/eye 태그를 상대방에 전이시킨다.
#       또한 구 _build_partner_observer가 주인공의 p_exposure_tag(parts exposure)
#       를 상대방 노출로 오용했다 (치명적 오염).
# 해결: 1) 상대방 기본 태그를 스토리 설정(한글)에서 결정론적으로 danbooru 영문 변환
#            + 명시적 부정 태그(no makeup, bare face, ...)로 완전 정의
#       2) EP별 노출/표정은 주인공 stats(두근거림 L) level 기반 결정론 풀에서 선택
#       3) 태그 블록을 [AAA ...]/[BBB ...]로 물리 분리 (캐릭터 귀속 라벨)
# ============================================================================

# 한글 스토리 설정 → danbooru 영문 변환 맵 (theme_gen_auto 3-1에서 생성되는 값)
_PARTNER_HAIR_COLOR_MAP = {
    "검정": "black hair", "갈색": "brown hair", "회색": "grey hair",
    "흰색": "white hair", "금발": "blonde hair", "밤색": "dark brown hair",
}
_PARTNER_HAIR_LENGTH_MAP = {
    "짧은 머리": "short hair", "중간 머리": "medium hair",
    "긴 머리": "long hair", "대머리": "bald, no hair",
}
_PARTNER_BODY_MAP = {
    "뚱뚱함": "chubby body, fat", "마름": "skinny body, thin", "보통": "average build",
}
_PARTNER_LOOK_MAP = {
    "추남": "ugly face", "평범": "average face", "잘생김": "handsome face",
}

# 상대방 EP별 노출 풀 — 주인공 stats[1] '두근거림(L)' level(0~5) 기반
# [2026-09-07] 청년향: 상체는 topless(bare chest)까지, 성기는 노출 풀에 넣지 않는다(프롬프트 필터가 보장)
_PARTNER_EXPOSURE_POOLS = {
    "male": {
        0: ["fully clothed", "intact clothes"],
        1: ["fully clothed", "loosened collar"],
        2: ["unbuttoned shirt", "loosened tie", "rolled-up sleeves"],
        3: ["open shirt, bare chest", "unbuttoned pants", "messy clothes"],
        4: ["bare chest", "pants unbuttoned", "partially undressed"],
        5: ["naked", "bare body"],
    },
    "female": {
        0: ["fully clothed", "intact clothes"],
        1: ["fully clothed", "loose blouse"],
        2: ["unbuttoned blouse", "cleavage visible", "loose skirt"],
        3: ["open blouse, bare midriff", "lifted skirt", "messy clothes"],
        4: ["bare chest, torn blouse", "partially undressed", "falling clothes"],
        5: ["naked", "bare body"],
    },
}

# 상대방 EP별 표정 풀 — [2026-09-07] 청년향: 음란 계열(grin/lustful)은 장난기/온화 계열로 격하
_PARTNER_EXPRESSION_POOL = [
    "smug smile", "teasing grin", "satisfied smirk", "serious expression",
    "intense gaze", "confident smile", "cold stare", "gentle smile",
    "soft eyes", "shy glance",
]


def _build_partner_base_tags() -> dict:
    """[2026-08-28] 상대방 기본 태그 세트 (결정론적, 매 호출 동일).

    스토리 설정(config.appearance2/hair_color2/hair_length2/glasses2/eye_color2/
    skin_color2/outfit2, 한글)을 danbooru 영문 태그로 변환하고, 주인공 태그 오염 방지를
    위한 명시적 부정 태그(makeup 등)를 포함한다.

    Returns:
        {"hair", "face", "makeup", "body", "clothes", "has_info"} (모두 str, has_info=bool)
        - makeup: 항상 명시적 부정 태그 (상대방은 무화장 고정 — 주인공 makeup 전이 차단)
        - has_info: 스토리 설정이 하나라도 있으면 True (없으면 faceless fallthrough)
    """
    sex2 = getattr(config, 'sex2', '남자')
    is_male = sex2 in ("male", "남자", "남성")
    appearance2 = (getattr(config, 'appearance2', '') or '')

    # hair: hair_color2 + hair_length2 (대머리 우선)
    hair = []
    hc = (getattr(config, 'hair_color2', '') or '').strip()
    hl = (getattr(config, 'hair_length2', '') or '').strip()
    if hl == "대머리":
        hair.append("bald, no hair")
    else:
        if hc:
            hair.append(_PARTNER_HAIR_COLOR_MAP.get(hc, hc))
        if hl:
            hair.append(_PARTNER_HAIR_LENGTH_MAP.get(hl, hl))

    # face: 인상(look) + 눈 + 피부 + 안경 (명시적)
    face = []
    for k, v in _PARTNER_LOOK_MAP.items():
        if k in appearance2:
            face.append(v)
    ec = (getattr(config, 'eye_color2', '') or '').strip()
    if ec:
        face.append(ec)
    sc = (getattr(config, 'skin_color2', '') or '').strip()
    if sc:
        face.append(sc)
    g = (getattr(config, 'glasses2', '') or '').strip()
    if g == "안경":
        face.append("glasses")
    elif g == "안경없음":
        face.append("no glasses")
    elif _protagonist_has_glasses():
        # glasses2 미설정(기존 프로젝트) + 주인공만 안경 → 상대방 명시적 no glasses
        face.append("no glasses")

    # body: 체형 + 수염 (명시적)
    body = []
    for k, v in _PARTNER_BODY_MAP.items():
        if k in appearance2:
            body.append(v)
    if "수염있음" in appearance2:
        body.append("beard")
    elif "수염없음" in appearance2:
        body.append("clean shaven")

    # makeup: 명시적 부정 태그 (오염 방지 핵심 — 주인공 makeup이 상대방에 전이되지 않도록)
    if is_male:
        makeup = "no makeup, bare face, no eyeliner, no lipstick, no eyeshadow, no false lashes, masculine face"
    else:
        makeup = "no makeup, bare face, no eyeliner, no lipstick, natural skin"

    clothes = (getattr(config, 'outfit2', '') or getattr(config, 'opponent_outfit', '') or '').strip()
    has_info = bool(hair or face or body or clothes)
    return {"hair": ", ".join(hair), "face": ", ".join(face), "makeup": makeup,
            "body": ", ".join(body), "clothes": clothes, "has_info": has_info}


# ────────────────────────────── 시트 #…# 캐릭터 공식 태그 (comic_input.extract_char_tags → config)
def _char_tag_list(attr: str) -> list:
    """config.char_tags / config.partner_char_tags → 정규화 목록 (str·list 모두 허용, 중복 제거, ≤6)"""
    raw = getattr(config, attr, "") or ""
    items = raw.split(",") if isinstance(raw, str) else list(raw)
    out = []
    for t in items:
        t = re.sub(r"\s+", " ", str(t).replace("_", " ")).strip(" .,;:")
        if t and not any(t.lower() == o.lower() for o in out):
            out.append(t)
    return out[:6]


def protagonist_char_tag_line() -> str:
    """[AAA TRIGGER]에 들어갈 주인공 캐릭터 태그 문자열 (없으면 "")"""
    return ", ".join(_char_tag_list("char_tags"))


def partner_char_tag_line() -> str:
    """[BBB TRIGGER]에 들어갈 상대방 캐릭터 태그 문자열 (없으면 "")"""
    return ", ".join(_char_tag_list("partner_char_tags"))


def ensure_char_tags(body: str, include_partner: bool = True) -> tuple:
    """#…# 태그가 최종 프롬프트에 살아남았는지 확인하고 빠진 것을 채운다 → (문자열, 추가된 태그 목록)

    [2026-09-08] POV/multi 컷 본문은 LLM이 가이드(prompt_pov.md/prompt_multi.md)로 다시 쓰기 때문에
    캐릭터 태그가 빠질 수 있습니다. 그래서 정제(dedupe/sanitize/anatomy)를 **모두 통과한 뒤** 여기서
    보장 주입합니다. 이미 들어가 있으면(대소문자·'_' 차이 무시) 중복을 만들지 않습니다.
    주인공 태그는 맨 앞(모델이 먼저 읽는 자리), 상대방 태그는 맨 뒤(POV 컷만 해당)에 붙입니다.
    """
    text = str(body or "")
    low = re.sub(r"\s+", " ", text.lower()).replace("_", " ")

    def present(tag: str) -> bool:
        n = re.sub(r"\s+", " ", tag.lower()).replace("_", " ")
        return bool(re.search(rf"(?<![0-9a-z]){re.escape(n)}(?![0-9a-z])", low))

    head_add = [t for t in _char_tag_list("char_tags") if not present(t)]
    tail_add = [t for t in _char_tag_list("partner_char_tags") if not present(t)] \
        if include_partner else []
    out = text.strip().strip(", ")
    if head_add:
        out = ", ".join(head_add) + (", " + out if out else "")
    if tail_add:
        out = (out + ", " if out else "") + ", ".join(tail_add)
    return out, head_add + tail_add


def _build_partner_block(episode: int, name_b: str, cut_state: dict = None) -> list:
    """[2026-08-28] 상대방(BBB) 태그 블록 — 주인공(AAA)과 물리 분리.

    [BBB ...] 라인의 태그는 상대방에게만 적용됨을 명시 (LLM 프롬프트 생성용).
    EP별 노출(partner_exposure_tag)/표정(partner_expression_tag) 포함.

    Returns:
        ["[BBB] ...", "[BBB HAIR] ...", ...] 라인 리스트
    """
    pbase = _build_partner_base_tags()
    lines = [f"[BBB] {name_b} (partner) - tags in [BBB ...] lines apply ONLY to {name_b}, NEVER to "
             f"the protagonist, and [BBB TRIGGER] tags must be kept verbatim"]
    pct = partner_char_tag_line()
    if pct:
        lines.append(f"[BBB TRIGGER] {pct}")
    if not pbase["has_info"]:
        # 스토리 설정 없음(기존 프로젝트) → 기존 faceless 실루엣 fallthrough
        lines.append("[BBB LOOK] faceless_male, silhouette, black_body, no clothes")
        return lines
    # [2026-09-09] 컷 상태 시트에 상대방 항목이 있으면 회차 설정보다 앞선다 — 주인공만 '지금'을
    #   가지고 상대는 회차 평균 태그였던 탓에, 두 사람이 한 화면인 컷에서상이 회차 중간의
    #   표정·복장으로 그려졌다. (빈 항목만 회차 값 사용)
    _cs = cut_state or {}

    def _own(_k):
        _v = str(_cs.get(_k) or "").strip()
        _v = _dedupe_csv(_v)
        # 한글 값은 최종 프롬프트에서 파기되니 지금 버린다(재시도 여지 확보)
        return ",".join(t for t in _v.split(",") if t and not re.search(r"[\u3131-\u318e\uac00-\ud7af\u4e00-\u9fff]", t))

    def _pick(_k, _base):
        return _own(_k) or (_base or "")

    if _pick("p_hair", pbase["hair"]):
        lines.append(f"[BBB HAIR] {_pick('p_hair', pbase['hair'])}")
    if _pick("p_face", pbase["face"]):
        lines.append(f"[BBB FACE] {_pick('p_face', pbase['face'])}")
    if _pick("p_makeup", pbase["makeup"]):
        lines.append(f"[BBB MAKEUP] {_pick('p_makeup', pbase['makeup'])}")
    p_expr = config.partner_expression_tag[episode] if 0 <= episode < len(config.partner_expression_tag) else ""
    p_expr = _dedupe_csv(",".join([x for x in (p_expr, _own("p_face")) if x]))
    if p_expr:
        lines.append(f"[BBB EXPRESSION] {p_expr}")
    if _pick("p_body", pbase["body"]):
        lines.append(f"[BBB BODY] {_pick('p_body', pbase['body'])}")
    if _own("p_clothes"):
        lines.append(f"[BBB CLOTHES] {_own('p_clothes')}")
    elif pbase["clothes"]:
        lines.append(f"[BBB CLOTHES] {pbase['clothes']} (Korean - translate to English)")
    p_exposure = config.partner_exposure_tag[episode] if 0 <= episode < len(config.partner_exposure_tag) else ""
    if p_exposure:
        lines.append(f"[BBB EXPOSURE] {p_exposure}")
    for _lab, _k in (("[BBB ACCESSORIES]", "p_accessories"), ("[BBB MARKS]", "p_marks"),
                     ("[BBB PROPS]", "p_props"), ("[BBB POSTURE]", "p_posture")):
        if _own(_k):
            lines.append(f"{_lab} {_own(_k)}")
    return lines


def _observer_pronoun() -> str:
    """[2026-09-05] POV 관찰자(상대방) 소유 대명사 — sex2에서 파생."""
    sex2 = getattr(config, 'sex2', '남자')
    return "her" if sex2 in ("female", "여자", "여성") else "his"


def _det_choice(pool, key):
    """[2026-09-07] 재현성: seed 고정인데 rand.choice(미시드)가 프롬프트를 매 실행 바꿔
    같은 seed라도 이미지가 달라졌다. (에피소드/pose) 키 해시로 항상 같은 선택을 고른다."""
    pool = list(pool)
    if not pool:
        return ""
    return pool[zlib.crc32(str(key).encode("utf-8")) % len(pool)]


def _pick_observer_visible(pronoun: str = None, key: str = None) -> tuple:
    """[2026-09-05] POV [OBSERVER] 가시 부분 선택 → (프롬프트 문구, 모드).

    모드: "none" | "hands" | "forearms"
    _build_tag_block과 _filter_observer_section이 **같은 선택**을 봐야 하므로
    랜덤抽取을 이 함수 하나로 모은다 (예전에는 tag_block 안에서만 rand.choice → 후처리와 기준 불일치).
    """
    pronoun = pronoun or _observer_pronoun()
    mode = _det_choice(["none", "hands", "forearms"], key) if key else rand.choice(["none", "hands", "forearms"])
    if mode == "none":
        return "not visible in the frame", "none"
    if mode == "hands":
        return f"{pronoun} bare hands", "hands"
    return f"{pronoun} bare hands, {pronoun} forearms", "forearms"


# 공개 코드에는 순한 것만 남기고 나머지는 local_settings의 extreme_face로 옮겼다(이유: 어휘 노출).
_EXTREME_FACE_BASE = ("heart-shaped pupils", "heart pupils", "rolling eyes", "tongue out",
                      "crossed eyes", "empty eyes", "drooling", "boredom")


def _extreme_face():
    """걷을 극단 표정 태그 = 공개 기본 + 로컬(local_settings: extreme_face)."""
    return _EXTREME_FACE_BASE + tuple(str(x).strip().lower() for x in
                                      (getattr(config, 'extreme_face', []) or []) if str(x).strip())


def _dedupe_csv(s: str) -> str:
    """쉼표 태그 목록을 순서 유지로 중복 제거(회차 시작/후반 태그를 합칠 때 중복이 생긴다)."""
    out = []
    for p in str(s or "").split(","):
        p = p.strip()
        if p and p.lower() not in {o.lower() for o in out}:
            out.append(p)
    return ", ".join(out)


def _calm_face(tags: str) -> str:
    """극단 표정 태그를 거른다 — 회차 톤이 일상 컷까지 물들면 모든 표정이 아헤가오가 된다."""
    s = str(tags or "")
    low = s.lower()
    hits = [t for t in _extreme_face() if t in low]
    if not hits:
        return s
    out = ", ".join(p.strip() for p in s.split(",")
                    if p.strip() and not any(t in p.lower() for t in _extreme_face()))
    return out or "soft smile"


# 옷을 '입고 있다'고 말해주는 품목 어휘 — 이게 없으면 모델은 노출 부분 태그만 보고 벌거벗긴다.
_GARMENTS = ("uniform", "skirt", "dress", "gown", "shirt", "blouse", "bodysuit", "pants", "trouser",
             "jean", "shorts", "bikini", "swimsuit", "swim", "lingerie", "bra", "panties", "kimono",
             "suit", "jacket", "coat", "hoodie", "sweater", "cardigan", "overall", "veil", "armor",
             "cheongsam", "sailor", "scrub", "apron", "uniforms")
# 본문 근거 없이 들어오면 'nudity'로 읽히는 어구 — 컷이 스스로 옷을 해치지 못하게 막는다.
_TATTER = ("tattered", "torn", "ripped", "shredded", "destroyed clothes", "broken clothes",
           "dirty clothes", "wet clothes", "transparent")
# 같은 이유로 이름이 직관적인 어휘는 local_settings의 nude_words로 — 공개 코드에는 완곡한 것만 둔다.
_NUDE_WORDS_BASE = ("no clothes", "undressed", "stripped", "bare chest")


def _nude_words():
    """걷을 과노출 복장 태그 = 공개 기본 + 로컬(local_settings: nude_words)."""
    return _NUDE_WORDS_BASE + tuple(str(x).strip().lower() for x in
                                    (getattr(config, 'nude_words', []) or []) if str(x).strip())


def _merge_clothes(base: str, override: str) -> str:
    """컷 복장은 회차 의상을 **덮어쓰지 않는다**(실측: 'tattered school uniform, dirty clothes' 만
    남고 회차 의류가 사라져 클라이맥스도 아닌 컷이 누드로 찍혔다).

      · override에 품목이 없거나 회차와 같은 품목 → 회차 의류를 유지한 채 변화만 더한다(교복이 더러워짐)
      · override에 다른 품목이 있다 → 정말 갈아입은 컷이므로 override만 쓴다(수영복으로 changed)
    """
    base = re.sub(r"\s+", " ", str(base or "")).strip()
    override = re.sub(r"\s+", " ", str(override or "")).strip()
    if not override:
        return base
    bg = [g for g in _GARMENTS if g in base.lower()]
    og = [g for g in _GARMENTS if g in override.lower()]
    if not (og and not bg and set(og) & set(bg) == set() and og != bg):
        # 품목이 없거나 겹치면 합친다 (같은 옷의 상태 변화)
        if not og or set(og) & set(bg) or not bg:
            kept = [p.strip() for p in override.split(",") if p.strip()]
            out = [p.strip() for p in base.split(",") if p.strip()]
            out += [k for k in kept if k.lower() not in {o.lower() for o in out}]
            return ", ".join(out)
    return override


def _undress_guard(clothes: str, base: str = "") -> str:
    """본문 근거 없는 옷 훼손/전라 어구를 걷는다 — explicit 상한이 열려 있을 때만 통과시킨다."""
    s = re.sub(r"\s+", " ", str(clothes or "")).strip()
    if not s or explicit_allowed():
        return s
    parts = [p.strip() for p in s.split(",") if p.strip()]
    nw = _nude_words()
    kept = [p for p in parts if not any(w in p.lower() for w in nw)]
    if not any(any(g in p.lower() for g in _GARMENTS) for p in kept):
        # 어휘를 이름으로 세지 않는 **구조의 방어**: 입고 있다는 표시가 없으면 회차 의상을 그대로 쓴다.
        #   (로컬 어휘가 없는 clone에서도 누드화가 안 벌어진다 — '품목 소실'이 실제 원인이었으므로)
        kept = [p for p in parts if not any(w in p.lower() for w in _TATTER + nw)]
        if not any(any(g in p.lower() for g in _GARMENTS) for p in kept):
            return re.sub(r"\s+", " ", str(base or "")).strip()
    return ", ".join(kept or parts)


def _build_tag_block(episode: int, pose_text: str, camera_view: str, aspect_ratio: str,
                     position_sentence: str, step_expression: str, is_side: bool,
                     name_a: str = None, name_b: str = None, observer_text: str = None,
                     climax_tag: str = "", clothes_override: str = "",
                     partner_block: bool = None, observer_block: bool = None,
                     cut_state: dict = None) -> str:
    """자연어 문장 대신 원본 danbooru 태그를 카테고리별로 나열 (LLM 프롬프트 생성용)

    [2026-08-28] [AAA ...]/[BBB ...] 블록으로 캐릭터별 물리 분리 (오염 방지):
    - [AAA ...]: 주인공 태그 (hair/face/makeup/expression/body/clothes/exposure)
    - [BBB ...]: 상대방 태그 (기본 세트 + EP별 노출/표정, 명시적 부정 태그 포함)
    - [OBSERVER]: POV 전용 (프레임 내 상대방 가시 부분)

    Args:
        episode: 에피소드 인덱스
        pose_text: step별 pose
        camera_view: 카메라 앵글
        aspect_ratio: wide/tall
        position_sentence: 상대방 위치 문장
        step_expression: step별 표정
        is_side: True=2인물(사이드뷰, prompt2.md), False=POV(prompt1.md)
        name_a: 주인공 이름 (None이면 config.name)
        name_b: 상대방 이름 (None이면 config.name2)
        observer_text: [2026-09-05] POV [OBSERVER] 문구 고정값 (None이면 내부 랜덤).
            호출부가 _pick_observer_visible()로 고른 값을 넘기면 LLM 응답 후처리와 기준이 일치한다.
        climax_tag: [2026-09-08] actions.yaml 4번째 슬롯 climax 이벤트 (creampie 등).
            비어있으면 [CLIMAX] 라인을 쓰지 않는다 (LLM 생성 pose는 항상 빈 값).
    """
    sex2 = getattr(config, 'sex2', '남자')
    observer_pronoun = "her" if sex2 in ("female", "여자", "여성") else "his"

    # [2026-08-28] 캐릭터 이름 (AAA/BBB 라벨용)
    name_a = name_a or getattr(config, 'name', 'AAA')
    name_b = name_b or getattr(config, 'name2', 'BBB')

    clothes_tokens, style_tokens = _split_bodystyle_clothes(config.bodystyle_tag[episode])
    # [2026-09-09] 회차 의상 기준도(config.clothes)를 컷 프롬프트에도 넣는다 — 예전은 태그셋이 만든
    #   exposure 조각만 쓰여, 컷이 "tattered school uniform" 같은 어구로 갈음하면 의류가 통째로
    #   사라지고 노출 부분 태그(cleavage/navel/midriff/thighs)만 이겨 누드가 됐다.
    # 회차 의상 기준도는 **컷이 옷을 바꿀 때만** 받는다(없을 때는 [AAA EXPOSURE]가 이미 그 의상을 쓴다)
    # [2026-09-09] 시간 분리 — 회차 의상 목록은 '시작 + 후반'이 한 줄로 섞여 있다(태그셋 LLM은
    #   회차 전체를 본다). 그래서 컷이 옷을 명시하면 **컷 것만** 쓰고, 컷이 침묵하면 회차
    #   시작 상태(config.clothes)를 기준으로 삼는다. 후반 옷/회차 의상 목록은 클라이맥스 컷에서만 더한다.
    # [2026-09-09] 컷 **연속 상태 시트**(comic_gen.fold_cut_state)가 들어오면 그것이 이 컷의 정답
    #   이다. 표정/화장/몸/옷/악세사리/머리/흔적/소지품/자세를 회차 상수 대신 쓴다.
    _st = cut_state if isinstance(cut_state, dict) else {}
    _ovr = str(_st.get("clothes") or "").strip() or str(clothes_override or "").strip()
    _start = str(getattr(config, "clothes", "") or "").strip()
    _late_c = str(getattr(config, "clothes_late", "") or "").strip()
    if _ovr:
        base_clothes = _ovr
    else:
        bits = [_start or clothes_tokens]
        if climax_tag:
            bits += [clothes_tokens if _start else "", _late_c]
        base_clothes = _dedupe_csv(", ".join([p for p in bits if p]))
    # 컷별 복장 변화는 '덮어쓰기'가 아니라 '덧쓰기'(정말 갈아입은 컷만 덮어쓴다)
    clothes_tokens = _undress_guard(_merge_clothes(base_clothes, _ovr), _start or base_clothes)
    # 시작 복장을 그대로 쓰는 컷까지 [AAA EXPOSURE]를 잃지 않게, 노출 태그는 '옷이 실제로
    #   바뀐 컷'에서만 뺀다(예전은 clothes를 명시한 모든 컷에서 빠져 노출 태그가 사라졌다).
    _wardrobe_changed = bool(_ovr) and _ovr.lower() != _start.lower()

    # expression(주인공): face_tag + step_expression + expression_arr에서 랜덤 1개
    expr_pick = ""
    expr_arr = config.expression_arr[episode]
    if expr_arr:
        candidates = [e.strip() for e in expr_arr.split(",") if e.strip()]
        if candidates:
            # [2026-09-07] 결정론: (에피소드|pose) 해시 → 재실행 재현 (rand.choice는 seed와 무관)
            expr_pick = _det_choice(candidates, f"expr|{episode}|{pose_text}")
    # [2026-09-09] 표정 고정 버그: face_tag는 **회차당 1개**를 태그셋 LLM이 정해 모든 컷에 붙는다.
    #   실측(face_tag = "ahegao, wide eyes, tongue out, rolling eyes, flushed face…") → 일상 컷까지 아헤가오.
    #   그래서 컷별 표정(step_expression = 컷의 화면 감정)이 있으면 회차 톤은 물러난다.
    cut_face = (step_expression or str(_st.get("face") or "")).strip()
    ep_face = config.face_tag[episode] or ""
    if cut_face:
        expression_parts = [cut_face]
        face_line = ""
    elif climax_tag:
        # 클라이맥스 컷만 회차 표정을 쓴다 — 어휘를 들지 않고 '때에 맞는 자리'로 제한하는 구조 규칙이다.
        expression_parts = [p for p in [ep_face, expr_pick] if p]
        face_line = ep_face
    else:
        expression_parts = [p for p in [expr_pick] if p]
        face_line = str(getattr(config, 'face_style', '') or '')       # 회차 시작 표정만基準

    lines_block = [
        f"[ACTION] {pose_text}",
        f"[CAMERA] {_canon_camera(camera_view)}, {aspect_ratio}",
    ]
    if not _is_no_position(position_sentence):
        lines_block.append(f"[POSITION] {position_sentence}")
    # [2026-09-08] climax 이벤트 (actions.yaml 4번째 슬롯)
    if not _is_no_climax(climax_tag):
        lines_block.append(f"[CLIMAX] {climax_tag}")

    # ============ AAA (주인공) 블록 — [AAA ...] 태그는 주인공에게만 적용 ============
    lines_block.append(f"[AAA] {name_a} (protagonist) - tags in [AAA ...] lines apply ONLY to {name_a}, NEVER to "
                       f"the partner, and [AAA TRIGGER] tags must be kept verbatim")
    # [2026-09-08] 시트 #…# 캐릭터 공식 태그(트리거) — 모델이 캐릭터를 지정하는 태그라 이름 바로 아래 둔다
    _ctags = protagonist_char_tag_line()
    if _ctags:
        lines_block.append(f"[AAA TRIGGER] {_ctags}")
    # [2026-08-27] 안경 혼입 방지: 2인물(사이드뷰)에서 상대방만 안경이면 주인공은 명시적 no glasses
    hair_line = str(_st.get("hair") or "").strip() or f"{config.hair_color}, {config.hair_style}"
    if is_side and (getattr(config, 'glasses2', '') or '').strip() == '안경' and not _protagonist_has_glasses():
        hair_line += ", no glasses"
    lines_block.append(f"[AAA HAIR] {hair_line}")
    # [2026-09-09] 조건이 거꾸로였다(calm을 클라이맥스에만 적용) — 일상 컷에 극단 표정(실측
    #   [AAA FACE] crying, blushing, ahegao)이 그대로 들어가 모든 컷이 같은 표정으로 찍혔다.
    #   회차 후반 표정(*_late)은 **클라이맥스 컷에만** 붙인다(회차가 시작하는 표정은 face_style).
    if climax_tag:
        _late_f = str(getattr(config, "face_style_late", "") or "").strip()
        if _late_f:
            face_line = _dedupe_csv((face_line + ", " + _late_f) if face_line else _late_f)
    if face_line:
        _fl = face_line if climax_tag else _calm_face(face_line)
        if _fl:
            lines_block.append(f"[AAA FACE] {_fl}")
    _mk = str(_st.get("makeup") or "").strip() or (config.makeup_tag[episode] or "")
    if _mk:
        lines_block.append(f"[AAA MAKEUP] {_mk}")
    lines_block.append("[AAA EXPRESSION] " + ", ".join(
        _calm_face(p) if not climax_tag else p for p in expression_parts))
    _bd = str(_st.get("body") or "").strip() or ", ".join(
        [p for p in [config.body_shape, config.body_tag[episode]] if p])
    body_parts = [p for p in [_bd, style_tokens] if p]
    lines_block.append(f"[AAA BODY] {', '.join(body_parts)}")
    if clothes_tokens:
        lines_block.append(f"[AAA CLOTHES] {clothes_tokens}")
    # [2026-09-09] 상태 시트로만 표현 가능했던 항목들 — 회차 상수엔 없는 컷 단위 정보다.
    if str(_st.get("accessories") or "").strip():
        lines_block.append(f"[AAA ACCESSORIES] {str(_st['accessories']).strip()}")
    if str(_st.get("marks") or "").strip():
        lines_block.append(f"[AAA MARKS] {str(_st['marks']).strip()}")
    if str(_st.get("props") or "").strip():
        lines_block.append(f"[PROPS] {str(_st['props']).strip()}")
    if str(_st.get("posture") or "").strip():
        lines_block.append(f"[POSTURE] {str(_st['posture']).strip()}")
    # [2026-08-28] 주인공 노출: exposure + p_exposure(parts exposure) + marks (모두 주인공)
    # makeup은 [AAA MAKEUP]으로 분리 (이전에는 [EXPOSURE]에 섞여 상대방 전이 원인)
    exposure_parts = [p for p in ["" if _wardrobe_changed else config.exposure_tag[episode],
                                  config.p_exposure_tag[episode],
                                  config.marks_tag[episode]] if p]
    # [2026-08-30] -real 모드: 두근거림(L) 레벨별 체모 태그 강제 주입 (신체 속성 → [AAA EXPOSURE], real 모드만)
    if _real_mode_active():
        _pubic = getattr(config, 'pubic_hair_tag', None)
        if _pubic and 0 <= episode < len(_pubic) and _pubic[episode]:
            if _pubic[episode] not in exposure_parts:
                exposure_parts.append(_pubic[episode])
    if exposure_parts:
        lines_block.append(f"[AAA EXPOSURE] {', '.join(exposure_parts)}")

    # ============ BBB (상대방) 블록 — [BBB ...] 태그는 상대방에게만 적용 (오염 방지) ============
    # [2026-09-07] 청년향 1인 화면: comic은 상대방이 실제로 프레임에 있는 컷(POV)만 True로 켠다.
    #   (예전엔 무조건 주입 — "혼자 있는" 회차에서도 상대방 태그가 프레임을 오염시켰다.)
    if True if partner_block is None else partner_block:
        lines_block.extend(_build_partner_block(episode, name_b, cut_state))

    # [2026-09-07] observer도 동일: 기본은 옛 동작(not is_side)이나 comic은 POV 컷만 켠다.
    if (not is_side) if observer_block is None else observer_block:
        # POV: 관찰자(상대방) 프레임 내 가시 부분 랜덤 (없을 수도 있음)
        # [BBB] 블록의 속성(피부색/무화장 등)으로 이 부분만 묘사
        # [2026-08-29] chest 제거: 주인공 breast 태그가 observer 가슴에 혼입되는 문제 해결
        # (공유 부위 "chest"가 이미지 모델에서 protagonist breast 특징으로 렌더링됨)
        # 손만 남기고 "bare" 명시 → 주인공 액세서리(장갑 등) 전이 방지
        # [2026-09-05] Patch D: observer_text를 호출부에서 받는다 (후처리와 선택 기준 공유)
        observer = observer_text or _pick_observer_visible(
            observer_pronoun, key=f"obs|{episode}|{pose_text}")[0]
        lines_block.append(f"[OBSERVER] {observer}")

    # [2026-09-07] time_of_day: 생성만 되고 미사용이던 필드 → 배경에 합류(조명/시간 재현)
    # [2026-09-09] 장소·시간·배경도 컷 연속 상태의 일부다. 회차 배경 태그는 회차 전체 목록이라
    #   컷이 상태를 주면 그것을 쓰고, 비어 있을 때만 회차 값으로 돌아간다.
    _bg = _dedupe_csv(", ".join([p for p in [
        str(_st.get("place") or "").strip() or str(getattr(config, "location", "") or "").strip(),
        str(_st.get("time") or "").strip() or str(getattr(config, "time_of_day", "") or "").strip(),
        str(_st.get("background") or "").strip() or (config.background_tag[episode] or "")] if p]))
    if _bg:
        lines_block.append(f"[BACKGROUND] {_bg}")
    lines_block.append(f"[SAFETY] {config.review_safety[episode]}")
    return "\n".join(lines_block)


# [2026-09-05] Patch D — observer 섹션에서 제거할 '머리/얼굴/화장/표정' 계열 어휘 (ban list).
#   ban list 방식(허용 목록 아님)을 쓴 이유: 손/팔/소매/신발 등은 [OBSERVER]/[ACTION]에 따라
#   정당하게 프레임에 들어올 수 있으므로, 문서화된 오염(머리카락·수염·얼굴·화장)만 정확히 잘라낸다.
_OBSERVER_HEAD_BAN_RE = re.compile(
    r"\b(hair|hairstyle|hairstyles|bangs|bang|ponytail|braid|braids|ahoge|odango|bun|buns|"
    r"beard|bearded|stubble|mustache|sideburns|"
    r"face|facial|jaw|jawline|chin|cheek|cheeks|forehead|temple|temples|nose|nostril|"
    r"eye|eyes|eyebrow|eyebrows|eyelash|eyelashes|eyeliner|eyeshadow|eye\s+shadow|"
    r"makeup|make-up|make-up|lipstick|blush|blushing|blushed|foundation|contour|highlighter|"
    r"glasses|sunglasses|spectacles|monocle|goggle|goggles|"
    r"smile|smiling|grin|grinning|smirk|smirking|mouth|lips|lip|teeth|tooth|tongue|dimples|"
    r"frown|frowning|scowl|glare|glaring|expression|expressions|gaze|stare|staring|look|"
    r"head|heads|ear|ears|earring|earrings|headband|hairband|hairpin|hairclip|hat|cap|helmet)\b", re.I)


def _sanitize_prompt_underscores(prompt: str) -> str:
    """[2026-08-30] 최종 prompt의 '_'를 공백으로 치환 (score_N만 '_' 유지).

    anima 언어모델이 danbooru tag의 '_'를 인식하지 못하는 경우가 있어 안전장치로 추가.
    score_9/score_8 같은 품질 태그는 '_'가 필요하므로 보호한다.
    """
    # score_N 보호 (placeholder sentinel — prompt에 등장하지 않는 토큰)
    prompt = re.sub(r'score_(\d)', r'@@SCORE\1@@', prompt)
    prompt = prompt.replace('_', ' ')
    prompt = re.sub(r'@@SCORE(\d)@@', r'score_\1', prompt)
    return prompt


def _build_simple_prompt_header(sex: str, safety_tag: str, is_side: bool = False, position_sentence: str = "",
                                pose_text: str = "") -> str:
    """ANIMA SIMPLE용 프롬프트 헤더 생성
    
    Args:
        sex: 주인공 성별
        safety_tag: 안전 태그
        is_side: 사이드뷰 여부
        position_sentence: 상대방 위치 문장 (예: "He is standing.", "He is sitting.", "#NONE")
    """
    safety_tag = safety_tag.strip(", ")
    # [2026-08-30] -real 모드: 리얼 화풍 태그 (교체가 아닌 추가)
    real_suffix = f", {ANIMA_REAL_HEADER_TAGS}" if _real_mode_active() else ""
    if is_side:
        header = artist_anima + f"score_9, score_8, masterpiece, best quality, very aesthetic, amazing quality, newest, highres, absurdres, colorful, perfect eyes, detailed, {safety_tag}{real_suffix}, [ANGLE]"
        sex2 = getattr(config, 'sex2', '남자')
        
        # position_sentence가 있으면 좌우 랜덤 할당 (standing/sitting/walking만)
        he_position_desc = ""
        if not _is_no_position(position_sentence):
            pos_lower = position_sentence.lower()
            if "standing" in pos_lower or "sitting" in pos_lower or "walking" in pos_lower:
                side = _det_choice(["left", "right"], f"side|{pose_text}|{position_sentence}")
                if "standing" in pos_lower:
                    he_position_desc = f"He is standing at the {side}."
                elif "sitting" in pos_lower:
                    he_position_desc = f"He is sitting at the {side}."
                elif "walking" in pos_lower:
                    he_position_desc = f"He is walking at the {side}."
            else:
                # lying, behind 등은 그대로 사용
                he_position_desc = position_sentence
        
        if sex in ("male", "남자", "남성") and sex2 in ("male", "남자", "남성"):
            header += ",2boy. A detailed anime illustration of two boys. They are looking at each other."
            if he_position_desc:
                header += f" {he_position_desc}"
            header += "\n"
        elif sex in ("female", "여자", "여성") and sex2 in ("female", "여자", "여성"):
            header += ",2girl. A detailed anime illustration of two girls. They are looking at each other."
            if he_position_desc:
                header += f" {he_position_desc}"
            header += "\n"
        else:
            pronoun = "a girl" if sex in ("female", "여자", "여성") else "a boy"
            pronoun2 = "a boy" if sex2 in ("male", "남자", "남성") else "a girl"
            # [2026-09-08⑤] 군중 포즈(난교/군무 등)는 기본 카운터 '1 boy'와 충돌한다.
            # pose_text에 군중 태그가 detection되면 카운터를 승격시켜 1boy 강제를 푼다.
            crowd = bool(pose_text and re.search(
                r'multiple_boys|multiple_men|group_sex|gangbang|extra_penis|more_than_one_guy', pose_text))
            if crowd:
                header += (f",1girl,multiple boys.\nA detailed anime illustration of {pronoun} "
                           f"at the center surrounded by men, with {pronoun2} ")
            else:
                header += f",1girl,1 boy.\nA detailed anime illustration of {pronoun} at the center and {pronoun2} "
            # [2026-09-08] 문장 이어붙이기 정리: 미리 "…and a boy  He is standing at the right.." —
            #   꼬리 문장이 이미 마침표를 가지고 있는데 다시 붙여 이중 마침표, 앞 공백+뒤 공백으로 이중 공백
            tail = re.sub(r"\.{2,}", ".", str(he_position_desc or "at the right").strip())
            if not tail.endswith("."):
                tail += "."
            header = header.rstrip() + " " + tail
        return header
    else:
        header = artist_anima + f"score_9, score_8, masterpiece, best quality, very aesthetic, amazing quality, newest, highres, absurdres, colorful, perfect eyes, detailed, {safety_tag}{real_suffix}, [ANGLE]"
        # [2026-09-07] [ANGLE] 앞에 쉼표 누락 수정: safety_tag는 strip(", ")당해 쉼표가 사라지므로
        # "explicit" + "close_up"이 'explicitclose_up'으로 붙어 두 태그가 모두 죽었다(comic face 컷 실측).
        if sex in ("male", "남자", "남성"):
            header += ",1boy,solo.\nA detailed anime illustration of a boy at the center."
        else:
            header += ",1girl,solo.\nA detailed anime illustration of a girl at the center."
        return header


if __name__ == "__main__":
    log("ANIMA GEN STANDING/SIMPLE 모듈 로드 완료")

# 모듈 로드 시 log 파일 초기화
_ensure_log_files()
