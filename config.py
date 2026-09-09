# config.py — 전역 상태 (llm_comic_gen 독립 환경)
#
# [2026-09-07] 원본 repo(llm_shortnovel_generator_gui)의 novel/theme/persona/flow/GUI 전용
# 상태(prologue_text, persona_text, flow_stats, progression_array, theme_*, trigger,
# character C, lora/wildcard CLI 스위치, sex_count 등)는 이 파이프라인 어디에서도 읽지
# 않아 삭제했다. 남은 항목은 오직:
#   comic_input.apply_to_config()가 채우는 값 +
#   init_anima_tags/comic_gen/comic_page_merge/_build_tag_block이 읽는 값 +
#   plot.json/Lora 경로 설정.
import json

# Setup - plot.json 매번 새로 읽기 (cache 금지)
def get_json_value():
    with open('plot.json') as f:
        return json.load(f)

# (json_value 모듈 변수는 사용처 없음 — get_json_value()를 매번 호출한다)

# 총 에피소드 수 (data/episode_setup.json)
try:
    with open('data/episode_setup.json', 'r', encoding='utf-8') as ef:
        episode_setup = json.load(ef)
    total_episodes = episode_setup.get("total_episodes", 12)
except Exception:
    total_episodes = 12

# 에피소드 본문 (index 0 = EP1). comic_input이 평문 입력에서 채운다 (progress/ 미사용).
episode_content = ["" for _ in range(total_episodes)]

# ------------------------------------------------------------------ 주인공 (Character A)
name = ""
sex = ""
job = ""
hair_color = ""
hair_style = ""
eye_color = ""
eye_shape = ""          # init_anima_tags가 랜덤 초기화(tareme/jitome/...)
skin_color = ""
face_style = ""
acc = ""
clothes = ""
breasts_size = -1
hip_size = -1
# [2026-09-08] 시트에서 #…# 로 감싼 '캐릭터 공식 태그(트리거)'. comic_input.extract_char_tags가 LLM 없이
# 그대로 뽑고, anima_gen._build_tag_block / comic_gen.build_panel_prompt가 프롬프트에 무조건 넣는다.
# 예) sheet: "#Usagi Tsukino from Sailor Moon#" → char_tags = ["Usagi Tsukino from Sailor Moon"]
char_tags = []                    # 주인공 태그 목록(list[str])
partner_char_tags = []            # 상대방 태그 목록 — [BBB]쪽(POV 컷만 프레임에 들어간다)

# ------------------------------------------------------------------ 상대방 (Character B)
name2 = ""
sex2 = "남자"
outfit2 = "일상복"
appearance2 = "평범한 일상복"
hair_color2 = ""        # 상대방 머리색 (캐릭터 시트 + anima prompt 혼입 방지)
hair_length2 = ""       # "짧은 머리/중간 머리/긴 머리" 또는 "대머리"
glasses2 = ""           # "안경" / "안경없음"
eye_color2 = ""         # danbooru English (예: "brown eyes")
skin_color2 = ""        # 예: "fair skin"

# 서브 캐릭터 사용 여부 (comic 컷 스크립트에서 3인모드 여부 판단에만 사용)
chr_num3 = 0

# ------------------------------------------------------------------ EP별 입력 (comic_input이 주입)
episode_protagonist_sheets = []     # EP별 주인공 시트 평문
episode_partner_sheets = []         # EP별 상대방 시트 평문
episode_sub_sheets = []             # EP별 서브 캐릭터 시트
ep_corruption_guides_map = {}       # EP 번호 → {"protagonist":[기,승,전,결], "partner":[...], "sub":[...]}
special_writing_req = {}            # EP 번호 → ["터치", ...] ('$' 행동 키워드)
plot_hash = ""                      # progress/ 파일명용 (독립 환경에서는 대부분 없음 → fallback)

# ------------------------------------------------------------------ ANIMA 태그 (init_anima_tags가 LLM으로 생성)
face_tag = ["" for _ in range(total_episodes)]
makeup_tag = ["" for _ in range(total_episodes)]
marks_tag = ["" for _ in range(total_episodes)]
body_tag = ["" for _ in range(total_episodes)]
bodystyle_tag = ["" for _ in range(total_episodes)]
exposure_tag = ["" for _ in range(total_episodes)]
p_exposure_tag = ["" for _ in range(total_episodes)]      # 주인공 parts exposure (cameltoe 등)
pubic_hair_tag = ["" for _ in range(total_episodes)]      # -real 전용
background_tag = ["" for _ in range(total_episodes)]
partner_exposure_tag = ["" for _ in range(total_episodes)]   # 상대방 옷 상태 (주인공 태그와 물리 분리)
partner_expression_tag = ["" for _ in range(total_episodes)]
expression_arr = ["" for _ in range(total_episodes)]     # 얼굴 클로즈업 컷용 표정 5개(쉼표 문자열)
expression = ""

review_stats = [[0] * 7 for _ in range(total_episodes)]  # [M, L, A, O, I, S, D] (LLM 판정)
review_safety = ["" for _ in range(total_episodes)]      # safe | sensitive | nsfw | explicit

body_shape = ""
current_level = 0
episode_num = 0
location = ""
time_of_day = ""      # [2026-09-07] EP별 시간대 태그(LLM 생성) → [BACKGROUND]에 사용

# ------------------------------------------------------------------ 만화(comic) 설정 (run_comic/comic_gen)
# [2026-09-08⑥] -comic: 본문 생성 전 에피소드별 만화 먼저 만든다 (기본 disable)
comic_enable = False
comic_panels_per_page = 5         # 페이지당 컷 수 (cut.yaml OFF일 때만)
# [2026-09-08⑦] 컷 수·페이지 수를 본문 길이에서 역산한다 (예전: 4페이지/24컷 고정 → 본문 잘림).
comic_pages = 0                   # 0=본문 길이로 자동 / N>0=N페이지 고정 / -1=cut.yaml OFF(자동 문법)
comic_max_pages = 24              # auto 모드 페이지 상한 (페이지당 2~8컷 → 최대 ~190컷)
comic_chars_per_panel = 600       # 본문 이 정도 분량 = 컷 1컷 (컷 수 역산 기준)
comic_beat_chars = 1800           # 장면(컷 스크립트 LLM 1호출) 본문 상한 (num_ctx 보호)
comic_max_panels = 0              # 컷 상한 (0=무제한)
comic_font = ""                   # 한글 폰트 경로 지정(비우면 comic_page_merge.FONT_CANDIDATES가 OS별로 찾는다)
# [2026-09-09] 화면 문법(설명/풍선/의성어) 용도별 폰트 — --font-narration 등. 비우면 data/fonts → OS 순
comic_font_narration = ""         # 설명(지문)
comic_font_dialog = ""            # 대사(말풍선)
comic_font_thought = ""           # 속마음(풍선)
comic_font_sfx = ""               # 의성어/의태어
comic_summary_cuts = True         # ★서두 요약 컷: 각 기승전결 첫 컷 = 배경만 + 큰 지문(컷 70%)
comic_epilogue = True               # ★에필로그 컷: 결 끝에 반투명 이벤트신 2컷 + 큰 지문
comic_emo_marks = True              # 감정 이모티콘(분노/놀람/땀/하트/음영/반짝/물음) → --no-emo-marks
comic_book_num = 0                # 0 = comic/bookNNN 자동
angle_llm_cli = False             # -angle: action 컷에 angle.txt 구도 적용
camera_canon_cli = False          # -camera_canon: 카메라 뷰 태그 정석화 A/B
# [2026-09-09] 이 8개는 run_comic.py CLI로 채워진다
#   (--real / --sole / --lora1 / --lora2 / --lora-chg / --str1 / --str2 / --allow-explicit).
# 여기 값을 직접 적으면 CLI 인자가 없을 때의 기본값이 된다(anima_gen이 렌더 직전에 읽는다).
# 우선순위: real > sole > lora*_cli > plot.json(anima_style/anima_lora) — anima_gen.resolve_anima_lora
real_cli = False                  # --real: LoRA 전부 OFF + 리얼 UNET(ANIMA_REAL_UNET_POOL)
sole_cli = False                  # --sole: LoRA 없이 SOLE UNET만(ANIMA_SOLE_UNET_POOL)
lora1_cli = None                  # ANIMA_LORA_CONFIG 키 (--lora1)
lora2_cli = None                  # ANIMA_LORA_CONFIG 키 (--lora2)
lora_chg_cli = None               # "episode"만 허용 (--lora-chg). "increment"는 폐지 — anima_gen._norm_lora_chg에서 차단
lora_str1_cli = None              # --str1: lora1 강도 오버라이드 (None = 튜플 값 사용) — anima_gen._apply_lora_strengths
lora_str2_cli = None              # --str2: lora2 강도 오버라이드 (0.0이면 그 슬롯 OFF)
# [2026-09-09] local 전용 수위 스위치: 기본 정책은 청년향(상한 nsfw)이고, --allow-explicit
#   (또는 --safety explicit / env COMIC_ALLOW_EXPLICIT=yes)로만 explicit까지 연다 — anima_gen.explicit_allowed
explicit_cli = False

# ------------------------------------------------------------------ LLM 시스템 프롬프트
system_prompt = ""
system_prompt_anima = ""

# ================================================================== [2026-09-09] 로컬 전용 설정 파일
# 커밋에 넣고 싶지 않은 값은 local_settings.yaml(repo 루트, .gitignore 대상)에 둔다.
#   키 목록·형식·예시는 README_local.md(여기 역시 .gitignore 대상)에 정리해 두었습니다.
#   우선순위: run_comic CLI 인자 > 환경변수 > local_settings.yaml > 위 기본값
#   파일이 없거나 yaml이 없어도 파이프라인은 그냥 기본값으로 돕는다(중단하지 않는다).
LOCAL_SETTINGS_FILE = "local_settings.yaml"
local_settings = {}             # 파싱 결과(파일 없으면 {})
ko_map_explicit = {}            # local_settings['ko_map'] — comic_gen._ko_map_explicit가 쓴다
ko_map_safe = {}                # local_settings['ko_map_safe'] — comic_gen._ko_map_safe가 쓴다
                                #   (청년향 강등 사전: 공개 repo에 올리지 않는 체위·성행위어 키)
counter_alias = {}              # local_settings['counter_alias'] — novel_progress 집계 항목 이름
climax_vocab_local = []         # local_settings['climax_vocab'] — climax 어휘 증보
safety_local = ""               # local_settings['safety'] — --safety를 안 주셨을 때만 적용


def _truthy(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v or "").strip().lower() in ("1", "yes", "y", "true", "on")


def load_local_settings(path: str = "") -> dict:
    """local_settings.yaml → dict. 파일 없음/파싱 실패/yaml 없음 → {} (예외를 올리지 않는다)."""
    try:
        import yaml
        with open(path or LOCAL_SETTINGS_FILE, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        # cp949 콘솔(Windows)에서 한글 print가 터질 수 있어 조용히 넘긴다 — 설정 파일은 치명 오류가 아니다
        try:
            print(f"  ! {path or LOCAL_SETTINGS_FILE}을 읽지 못했습니다(무시하고 기본값으로 계속): {e}")
        except Exception:
            pass
        return {}


def _str_map(v) -> dict:
    """yaml dict → {str: str} (빈 항목 버림). 리스트 값도 문자열로 엮어 보관한다."""
    if not isinstance(v, dict):
        return {}
    out = {}
    for k, val in v.items():
        k = str(k).strip()
        if not k:
            continue
        if isinstance(val, (list, tuple)):
            out[k] = [str(x).strip() for x in val if str(x).strip()]
        else:
            out[k] = str(val).strip()
    return {k: v for k, v in out.items() if v}


def apply_local_settings(data: dict = None) -> dict:
    """yaml 값을 위 스위치들의 기본값 자리로 심는다.

    run_comic.py의 CLI 주입이 이보다 **나중에** 실행되므로 인자를 주시면 그쪽이 이깁니다.
    """
    global local_settings, ko_map_explicit, ko_map_safe, counter_alias, climax_vocab_local, safety_local
    global explicit_cli, lora1_cli, lora2_cli, lora_chg_cli, lora_str1_cli, lora_str2_cli
    d = local_settings if data is None else (data or {})
    if _truthy(d.get("allow_explicit")):
        explicit_cli = True
    s = str(d.get("safety") or "").strip().lower()
    if s in ("", "safe", "sensitive", "nsfw", "explicit"):
        safety_local = s
    for key, attr in (("lora1", "lora1_cli"), ("lora2", "lora2_cli"), ("lora_chg", "lora_chg_cli")):
        v = str(d.get(key) or "").strip()
        if v:
            globals()[attr] = v
    for key, attr in (("str1", "lora_str1_cli"), ("str2", "lora_str2_cli")):
        if d.get(key) not in (None, ""):
            try:
                globals()[attr] = float(d[key])
            except (TypeError, ValueError):
                pass
    km = d.get("ko_map")
    ko_map_explicit = {str(k).strip(): str(v).strip() for k, v in km.items()
                       if str(k).strip() and str(v).strip()} if isinstance(km, dict) else {}
    ko_map_safe = _str_map(d.get("ko_map_safe"))       # 청년향 강등 사전(민감 키만 로컬로)
    counter_alias = _str_map(d.get("counter_alias"))   # 집계 항목 이름(로컬 산출물 이름)
    counter_alias = {k: ([str(x).strip() for x in v if str(x).strip()] if isinstance(v, (list, tuple))
                         else str(v).strip()) for k, v in counter_alias.items()}
    cv = d.get("climax_vocab")
    climax_vocab_local = [str(x).strip().lower() for x in cv if str(x).strip()] \
        if isinstance(cv, (list, tuple)) else []
    return d


local_settings = load_local_settings()
apply_local_settings(local_settings)


def clothes_update(json_value, clothes, name, sex):
    """현재 레벨(current_level)에 따라 안전 태그 반환 (get_safety_tag 폴백 경로)"""
    if current_level <= 1:
        return "safe, "
    elif current_level == 2:
        return "sensitive, "
    elif current_level == 3:
        return "nsfw, "
    return "explicit, "
