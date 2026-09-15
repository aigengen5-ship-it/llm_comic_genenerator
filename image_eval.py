"""이미지 LLM 평가 — 그림 한 장에 점수를 매겨 '쓸까, 다시 그릴까'를 가릅니다 (2026-09-15).

원작: llm_shortnovel_generator_gui/util/merge_episode_images.py 의 이미지 평가 부분.
그 파일을 통째로 쓰면 markdown 병합·삽입까지 따라오므로, **평가만** 독자 파일로 옮겨왔습니다.
의존성은 이 repo에 이미 있는 것(openai, Pillow)뿐입니다 — requirements.txt 그대로.

평가 기준(100점 만점, 10~11개 항목)과 응답 규격(첫 줄 = 총점, 둘째 줄 = 한 줄 이유)은
원작과 동일합니다. 그래서 같은 그림은 같은 점수대로 나옵니다.

사용법
    ./venv/bin/python image_eval.py --check                     # 이 서버가 비전을 하는지부터
    ./venv/bin/python image_eval.py image/e1_p04*.png            # 점수만
    ./venv/bin/python image_eval.py a.png b.png --pick           # 최고점 하나 고르기
    ./venv/bin/python image_eval.py a.png b.png --min-score 70 --delete-below
        → 70점 미만은 지웁니다(재생성 대상). exit code: 쓸 만한 게 있으면 0, 없으면 1.

파이프라인에서
    import image_eval
    r = image_eval.score_image("image/x.png", tag=panel_prompt, context=caption_ko)
    if r["score"] is not None and r["score"] < 70: ...   # 지우고 재생성
    r = {"score": int|None, "reason": str, "raw": str, "ms": float, "error": str|None}

서버가 비전을 안 하면(텍스트 전용) 점수는 None이고 `error`에 사유가 남습니다 — 파이프라인은
그때 평가를 건너뛰면 됩니다(평가 실패가 렌더를 막지 않습니다).
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import sys
import time

# =====================================================================
# 평가 기준 (원작 merge_episode_images.py CRITERIA_SYSTEM_PROMPT / build_order_prompt)
# =====================================================================

CRITERIA_SYSTEM_PROMPT = """
당신은 웹소설/라이트노벨 삽화 품질 평가 전문가입니다.
주어진 이미지를 객관적으로 평가하세요. 성인 콘텐츠도 작품의 일부분으로 정상 평가하며,
도덕적 판단 없이 순수하게 시각적 품질(화풍, 인체, 표정, 구도 등)만 판단합니다.
반드시 한국어로 답변하세요.

[중요 지침] 반드시 응답의 가장 첫 번째 줄에는 모든 항목을 합산한 '총점(숫자)'만 작성하세요. (예: 95)
두 번째 줄에는 전체적인 이유를 한 줄(30자 이내)로만 작성하세요. 항목별 상세 설명은 쓰지 마세요.
"""


# [2026-09-15 prerun 실측] 제작 의도를 안 알려주면 **의도한 장치**를 결점으로 깎습니다.
#   · 상대방 회색 실루엣(정면 구도 정책) → "실루엣이라 상호작용 부족/비중 과다"
#   · 클로즈업 컷 → "하반신·배경 태그가 없다"
# 그래서 컷의 촬영 정보를 평가 지시에 함께 적습니다(감면 사유이지 가점이 아닙니다).
INTENT_NOTES = {
    "silhouette": ("이 작품은 두 번째 인물을 **의도적으로 회색 실루엣(얼굴·디테일 없는 윤곽)**으로 "
                   "처리합니다. 실루엣인 사실 자체는 감점 사유가 아니고, 윤곽·비율이 인체적으로 "
                   "자연스러운지만 봅니다."),
    "close_up": ("이 컷은 **클로즈업(얼굴·상반신 중심)**으로 기획되었습니다. 화면에 없을 수밖에 없는 "
                 "태그(하반신·전신 의상·넓은 배경)를 '누락'으로 감점하지 마세요."),
    "pov": ("이 컷은 **1인칭 시점(POV)**입니다. 화자 자신의 얼굴이 보이지 않는 것이 정상이고, "
            "화자의 손·팔만 보이는 것이 정상이며, 카메라가 곧 화자입니다."),
    "wide": ("이 컷은 **넓은 장면(행동)**입니다. 얼굴 디테일보다 구도·동작·배경의 조화를 중심으로 보세요."),
}


def build_intent_note(silhouette=False, camera="", extra="", people="auto") -> str:
    """촬영 정보 → 평가 면제 사유 문장(모르면 빈 문자열)."""
    keys = []
    cam = str(camera or "").lower()
    if "close" in cam or "portrait" in cam:
        keys.append("close_up")
    if "pov" in cam:
        keys.append("pov")
    # 사람 없는 배경 컷에 "넓은 장면(행동)" 면제 문장을 붙이면 오히려 사람이 없다고 깎습니다(실측).
    if ("wide" in cam or "front_view" in cam) and str(people).lower() != "none":
        keys.append("wide")
    if silhouette or "silhouette" in (extra or "").lower():
        keys.append("silhouette")
    seen, out = set(), []
    for k in keys:
        if k not in seen and k in INTENT_NOTES:
            seen.add(k)
            out.append(INTENT_NOTES[k])
    if extra and extra.lower() not in ("silhouette",):
        out.append(str(extra))
    return " ".join(out)


def build_order_prompt(tag: str, filename: str = "", context: str = "",
                       single: bool = True, people: str = "auto", intent: str = "") -> str:
    """평가 지시 문장 — 100점 만점 10개 항목 + 인원수 항목 2개(1명/2명 이상).

    Args:
        tag: 이 그림을 그린 프롬프트(TAG). 태그 재현성·소품 평가의 근거입니다.
        filename: 파일명(동작·상황 힌트로만 씁니다).
        context: 이 그림이 들어갈 장면 본문(스토리 맥락).
        single: True면 1인 화면 기준(2명 이상 나오면 감점), False면 2인 이상 기준.
    """
    people = (people or "auto").strip().lower()
    if people == "auto":
        people = "one" if single else "multi"
    if people == "none":
        # 만화의 배경·분위기 컷(사람이 안 나오는 것이 정상) — 원작엔 없던 변형입니다.
        rule_9 = ("10) 인원수 조건 (10점): **사람이 등장하지 않는 배경(분위기) 컷**입니다. 화면에 사람이 "
                  "보이면 큰 감점입니다. (인물 항목은 채점하지 말고 배경·조명·구도만 보세요)")
        rule_10 = ("11) 배경 밀도와 분위기 (10점): 배경이 텅 비지 않고 다채로우며 원근·조명·소품이 정교해 "
                   "그 자체로 장면을 설명하는 만화 컷으로서 밀도가 있는가?")
    elif people == "one":
        rule_9 = ("10) 인원수 조건 (10점): **단일 캐릭터(1명)** 전용 삽화입니다. 화면에 오직 1명의 "
                  "캐릭터만 등장하는지 확인하세요. (2명 이상 등장 시 큰 감점)")
        rule_10 = ("11) 화면 장악력 및 구도 (10점): 단일 캐릭터가 화면을 매력적으로 채우고 있으며, "
                   "캐릭터에 포커스가 잘 맞추어져 일러스트로서의 밀도감이 훌륭한가?")
    else:
        rule_9 = ("10) 인원수 조건 (10점): **다중 캐릭터(2명 이상)** 전용 삽화입니다. 화면에 2명 이상 "
                  "캐릭터가 등장하는지 확인하세요. (1명만 등장 시 큰 감점)")
        rule_10 = ("11) 캐릭터 간 균형 및 상호작용 (10점): 2명 이상의 캐릭터가 등장할 때, 원근법에 따른 "
                   "크기 비율(투시)이 자연스럽고 캐릭터 간의 시선 교환이나 상호작용이 조화로우며 균형이 "
                   "맞는가? 특히 2명인 경우 캐릭터들의 그려진 크기가 너무 차이가 나면 안 됨")

    if people == "none":
        # 사람 없는 배경 컷: 인물 항목(화풍/인체/표정/의상/동작 = 50점)이 전부 N/A라, 그것을
        # 남기면 모델이 절반을 임의로 점缀해 같은 그림이 42↔82점으로 흔들렸습니다(prerun 실측).
        # 항목을 배경 기준으로 다시 배분해 100점으로 맞춥니다.
        return f"""
이 이미지는 만화의 **배경(분위기) 컷**입니다 — 사람이 등장하지 않는 것이 정상입니다.
아래 5개 항목에 점수를 매겨 모두 합산한 총점을 계산해주세요. 각 항목 최대 점수는 적혀 있는 값이고, 총점은 100점 만점입니다.
실사가 아닌 **웹소설/라이트노벨 삽화(애니메이션풍)** 기준입니다. 반드시 한국어로 답하세요.

TAG: {tag or "(TAG 없음 — 화면 quality만 평가하세요)"}

동작 및 상황은 아래 파일 제목을 참고할 것:
filename: {filename or "(없음)"}

해당 장면 본문 (스토리 맥락):
{context or "(맥락 없음)"}

[평가 항목 - 총 100점 만점]
1) 태그 재현성 (30점): TAG가 말하는 장소·시간대·소품(책상, 서류, 네온사인 등)이 화면에 실제로 보이는가?
2) 배경 밀도·원근 (25점): 배경이 텅 비지 않고 다채로우며 원근·레이어가 살아 있는가?
3) 조명 및 색감 (20점: 장면 분위기에 맞는 극적인 조명과 아름다운 색채를 사용했는가?
4) 구도 (15점): 시선 유도·여백·비율이 만화 컷으로서 안정적이고 다음 컷과 이어지는가?
5) 붕괴·노이즈 (10점: 텍스처가 뭉개지거나 반복 무늬가 깨지거나 AI 아티팩트가 눈에 띄지 않는가?

{("[제작 의도 — 다음 사유로는 감점하지 마세요] " + intent) if intent else ""}

[응답 형식] 첫 줄: 총점(숫자)만. 두 번째 줄: 한 줄 이유만. (이유는 30자 이내)
"""

    return f"""
이 이미지가 아래 태그 조건을 만족하는지 파악한 후, 총 10가지 평가 항목별로 점수를 매기고 이를 모두 합산한 총점을 계산해주세요.
이 이미지는 실사가 아닌 **한국 웹소설 또는 일본 라이트노벨 삽화(애니메이션풍)** 기준입니다.
각 항목의 최대 점수는 10점이며, 총점은 100점 만점입니다.

TAG: {tag or "(TAG 없음 — 화면 quality만 평가하세요)"}

동작 및 상황은 아래 파일 제목을 참고할 것:
filename: {filename or "(없음)"}

해당 장면 본문 (스토리 맥락, 이 이미지가 삽입될 장면의 내용):
{context or "(맥락 없음)"}

[평가 항목 - 총 100점 만점]
1) 화풍 (5점): 웹소설/라이트노벨 삽화 특유의 매력적인 미형(애니풍) 화풍이 잘 나타나는가?
2) 인체 구조 (15점): 실사 비율은 아니더라도, AI 특유의 인체 붕괴(손가락 융합, 기형적인 관절, 목/팔다리 굵기 오류 등)가 없이 안정적인가?
3) 에피소드 내용 반영 (10점): 에피소드의 상황에 얼마나 이 이미지가 맞는지 꼼꼼하게 확인.
4) 표정 및 감정 (10점): 상황에 맞는 캐릭터의 표정이 어색하지 않고 생동감 있게 묘사되었는가?
5) 의상 및 소품 (10점): 캐릭터의 복장, 장신구, 무기 등의 디테일이 뭉개지지 않고 정교하게 그려졌는가?
6) 동작과 포즈 (10점): 캐릭터의 자세가 뻣뻣하지 않고 자연스러우며, 지시된 동작을 정확히 수행하는가?
7) 태그 재현성 (10점): 주어진 TAG의 요소들이 화면에 누락 없이 모두 구현되었는가? 참고로 한국어 출력이 되지 않는 것은 excuse할 것.
8) 조명 및 색감 (5점): 삽화에 어울리는 화사하거나 분위기 있는 극적인 조명 처리와 아름다운 색채를 사용했는가?
9) 배경의 조화 (5점): 배경이 텅 비지 않고 다채로우며, 캐릭터와 공간적으로 이질감 없이 융화되는가?
{rule_9}
{rule_10}

{("[제작 의도 — 다음 사유로는 감점하지 마세요] " + intent) if intent else ""}

[응답 형식] 첫 줄: 총점(숫자)만. 두 번째 줄: 한 줄 이유만. (이유는 30자 이내)
"""


def extract_score(llm_response: str):
    """응답 **첫 줄**에서 총점(숫자)을 뽑습니다. 없으면 None. (원작과 같은 규칙)"""
    if not llm_response:
        return None
    first = str(llm_response).strip().split("\n")[0]
    m = re.search(r"\d+", first)
    return int(m.group()) if m else None


def extract_reason(llm_response: str) -> str:
    """둘째 줄(한 줄 이유). 없으면 빈 문자열."""
    if not llm_response:
        return ""
    lines = [l.strip() for l in str(llm_response).strip().split("\n") if l.strip()]
    return lines[1] if len(lines) > 1 else ""


# =====================================================================
# 서버 (plot.json / plot.local.json — 엔드포인트·모델을 하드코딩하지 않습니다)
# =====================================================================

def _client(endpoint: str = "main", log_fn=None):
    """평가할 채팅 서버 클라이언트 — plot.json의 main/agent/anima/text를 그대로 씁니다."""
    import openAPI_control as OC
    ep = (endpoint or "main").strip().lower()
    if ep == "anima":
        return OC.get_openai_client_anima(log_fn=log_fn)
    if ep == "text":
        return OC.get_openai_client_text(log_fn=log_fn)
    if ep == "agent":
        return getattr(OC, "get_openai_client_agent", OC.get_openai_client)()
    return OC.get_openai_client()


def _model(endpoint: str = "main", model: str = None) -> str:
    """모델 ID — 지정값이 있으면 그것, 없으면 plot.json의 해당 엔드포인트 모델값."""
    if model:
        return model
    import config
    jv = config.get_json_value()
    ep = (endpoint or "main").strip().lower()
    key = {"anima": "agent_anima", "agent": "agent", "text": "textLLM"}.get(ep, "mainLLM")
    return (jv.get(key) or jv.get("mainLLM") or "").strip()


def server_models(endpoint: str = "main", log_fn=None) -> list:
    """/v1/models 목록 → [{id, modalities, arch}] (서버가 안 주면 빈 필드)."""
    try:
        raw = _client(endpoint, log_fn).models.with_raw_response.list()
        data = raw.parse().data
    except Exception as e:                                  # 목록 API가 없는 서버도 있다
        if log_fn:
            log_fn(f"[MODELS] 목록 조회 실패: {type(e).__name__}: {e}")
        return []
    out = []
    for m in data:
        d = getattr(m, "model_dump", lambda: {})()
        out.append({"id": d.get("id"),
                    "modalities": (d.get("input_modalities") or d.get("supported_modalities")
                                   or (d.get("meta") or {}).get("input_modalities")),
                    "arch": d.get("architecture")})
    return out


def vision_supported(model: str, endpoint: str = "main", log_fn=None):
    """서버 advertised로 본 비전 지원 여부 → True / False / None(정보 없음)."""
    for m in server_models(endpoint, log_fn):
        if str(m.get("id", "")).lower() != str(model or "").lower():
            continue
        mods = [str(x).lower() for x in (m.get("modalities") or [])]
        arch = m.get("arch") or {}
        if mods:
            return "image" in mods
        if isinstance(arch, dict) and arch.get("image_token_id") is not None:
            return True
        return None
    return None


# =====================================================================
# 호출
# =====================================================================

def image_data_url(path: str, max_side: int = 768, jpeg_quality: int = 85) -> str:
    """이미지를 data URL로 — 긴 변을 max_side로 줄여 JPEG로 쏩니다(컨텍스트 토큰 절감)."""
    from PIL import Image
    with Image.open(path) as im:
        im = im.convert("RGB")
        if max_side and max(im.size) > max_side:
            im.thumbnail((max_side, max_side), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=int(jpeg_quality))
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("utf-8")


def ask_image(image_path: str, prompt_text: str, system_prompt: str = None,
              model: str = None, endpoint: str = "main", max_side: int = 768,
              jpeg_quality: int = 85, max_tokens: int = 400, temperature: float = 0.2,
              timeout: float = 180.0, max_retries: int = 3, retry_delay: float = 5.0,
              log_fn=None) -> str:
    """그림 1장 + 지시문 → LLM 응답 텍스트(전부). 실패 시 None.

    [원작 실측을 그대로 옮긴 것]
      · vLLM(qwen 계열)은 `enable_thinking: false`를 안 보내면 max_tokens 예산을 reasoning이
        다 써버리고 본문(content)을 비워 반환합니다 → 항상 보냅니다.
      · 긴 변 768px JPEG면 평가에 충분한 대신 토큰은 훨씬 적습니다.
    """
    if not image_path or not os.path.isfile(image_path):
        if log_fn:
            log_fn(f"[IMAGE_SKIP] 파일 없음: {image_path}")
        return None
    client = _client(endpoint, log_fn)
    mdl = _model(endpoint, model)
    sys_p = system_prompt or CRITERIA_SYSTEM_PROMPT
    messages = [
        {"role": "system", "content": sys_p},
        {"role": "user", "content": [
            {"type": "text", "text": prompt_text},
            {"type": "image_url", "image_url": {"url": image_data_url(
                image_path, max_side=max_side, jpeg_quality=jpeg_quality)}},
        ]},
    ]
    kwargs = {"model": mdl, "messages": messages, "max_tokens": int(max_tokens),
              "timeout": float(timeout), "temperature": float(temperature),
              "extra_body": {"chat_template_kwargs": {"enable_thinking": False,
                                                      "preserve_thinking": False}}}
    if log_fn:
        log_fn(f"[IMAGE_PROMPT] model={mdl} endpoint={endpoint} image={os.path.basename(image_path)}")
    last = None
    for attempt in range(1, max(1, int(max_retries)) + 1):
        try:
            resp = client.chat.completions.create(**kwargs)
            msg = resp.choices[0].message
            txt = (msg.content or "").strip()
            if txt:
                if log_fn:
                    log_fn(f"[IMAGE_RESULT] {txt[:200]}")
                return txt
            rc = getattr(msg, "reasoning_content", None) or getattr(msg, "reasoning", None)
            last = (f"본문 없음(finish_reason={getattr(resp.choices[0], 'finish_reason', None)}, "
                    f"reasoning {len(str(rc)) if rc else 0}자) — thinking OFF/vision 지원/max_tokens 확인")
            break                                   # 빈 본문은 재시도로 안 고쳐집니다
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
            if log_fn:
                log_fn(f"[IMAGE_FAIL {attempt}/{max_retries}] {last}")
            if attempt < max_retries:
                time.sleep(float(retry_delay))
    if log_fn and last:
        log_fn(f"[IMAGE_FAIL] {mdl}: {last}")
    return None


def detect_people(tag: str) -> str:
    """TAG에서 인원 기대치를 뽑습니다 — 'none'(배경 컷) | 'multi' | 'one'.

    [2026-09-15 prerun 실측] 인원수 항목을 잘못 주면 실루엣·배경 컷이systematically 깎입니다.
    헤더의 인원 태그(1girl, 1boy / no humans)가 정답이므로 그것을 우선 봅니다.
    """
    t = (tag or "").lower()
    if re.search(r"\b(no humans?|nobody|no people|empty scene|wide establishing)\b", t):
        return "none"
    heads = re.findall(r"\b(1girl|1boy|2girls?|2boys?|3girls?|3boys?|solo)\b", t)
    people = sum(2 if h[0] in "23" else 1 for h in heads if h != "solo")
    return "multi" if people >= 2 else "one"


def ask_image_raw(messages: list, model: str = None, endpoint: str = "main",
                  max_tokens: int = 200, temperature: float = 0.0, timeout: float = 180.0,
                  log_fn=None) -> str:
    """이미지 n개를 첨부한 messages로 한 번 호출합니다(compare_images용)."""
    client = _client(endpoint, log_fn)
    try:
        resp = client.chat.completions.create(
            model=_model(endpoint, model), messages=messages, max_tokens=int(max_tokens),
            timeout=float(timeout), temperature=float(temperature),
            extra_body={"chat_template_kwargs": {"enable_thinking": False, "preserve_thinking": False}})
        return (resp.choices[0].message.content or "").strip() or None
    except Exception as e:
        if log_fn:
            log_fn(f"[IMAGE_FAIL] {type(e).__name__}: {e}")
        return None


def score_image(image_path: str, tag: str = "", context: str = "", single=None, people="auto",
                intent: str = "", camera: str = "", silhouette=None,
                filename: str = "", model: str = None, endpoint: str = "main",
                max_side: int = 768, max_tokens: int = 400, temperature: float = 0.2,
                max_retries: int = 3, system_prompt: str = None,
                log_fn=None) -> dict:
    """그림 1장 평가 → {"score","reason","raw","ms","error","model","path"}

    score가 None이면 평가를 못 했습니다(서버 비전 미지원/응답 파싱 실패). 그때 `error`에 사유가
    남고, 파이프라인은 평가를 건너뛰면 됩니다 — 평가 실패가 렌더를 막아선 안 됩니다.
    """
    t0 = time.time()
    ppl = (people or "auto")
    if str(ppl).lower() == "auto":
        ppl = detect_people(tag)
    is_single = ppl == "one" if single is None else bool(single)
    note = intent or build_intent_note(
        silhouette=(("silhouette" in (tag or "").lower()) if silhouette is None else bool(silhouette)),
        camera=camera, people=ppl)
    raw = ask_image(image_path,
                    build_order_prompt(tag=tag, filename=filename or os.path.basename(image_path),
                                       context=context, single=is_single, people=ppl, intent=note),
                    system_prompt=system_prompt, model=model, endpoint=endpoint,
                    max_side=max_side, max_tokens=max_tokens, temperature=temperature,
                    max_retries=max_retries, log_fn=log_fn)
    sc = extract_score(raw)
    err = None if sc is not None else ("응답 없음" if raw is None else "응답에서 점수를 찾지 못함")
    return {"path": image_path, "score": sc, "reason": extract_reason(raw), "raw": raw,
            "ms": round((time.time() - t0) * 1000, 1), "error": err,
            "model": _model(endpoint, model), "single": is_single, "people": ppl,
            "intent": note}


def score_images(paths, tags=None, contexts=None, workers: int = 2, **kw) -> list:
    """여러 장 평가(병렬) → score_image 결과 목록(입력 순서)."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    paths = list(paths)
    tags = list(tags) if tags else [None] * len(paths)
    contexts = list(contexts) if contexts else [None] * len(paths)
    while len(tags) < len(paths):
        tags.append(None)
    while len(contexts) < len(paths):
        contexts.append(None)
    out = [None] * len(paths)

    def one(i):
        kw2 = dict(kw)
        if tags[i]:                       # 컷마다 다른 TAG/맥락을 쓸 수 있다
            kw2["tag"] = tags[i]
        if contexts[i]:
            kw2["context"] = contexts[i]
        return i, score_image(paths[i], **kw2)

    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as ex:
        futs = [ex.submit(one, i) for i in range(len(paths))]
        for f in as_completed(futs):
            i, r = f.result()
            out[i] = r
    return [r for r in out if r is not None]


def pick_best(results: list) -> dict:
    """평가 결과 목록에서 최고점 1장(점수 없는 건 무시). 전부 없으면 None."""
    ok = [r for r in results if r and r.get("score") is not None]
    return max(ok, key=lambda r: r["score"]) if ok else None


def compare_images(path_a: str, path_b: str, tag: str = "", context: str = "", people="auto",
                   intent: str = "", model: str = None, endpoint: str = "main",
                   max_side: int = 768, max_tokens: int = 200, temperature: float = 0.0,
                   twice: bool = True, log_fn=None) -> dict:
    """두 장을 **한 번에** 보여주고 나은 쪽을 고르게 합니다(절대 점수보다 안정적).

    [2026-09-15 prerun] 같은 그림을 두 번 채점하면 점수가 흔들립니다(정확히 같은 점수 19/43).
    절대 점수는 '문턱 넘는지'로만 쓰고, 컷을 고를 때는 이 상대 비교를 쓰면emplacement가 덜 합니다.
    twice=True면 A/B 순서를 뒤바꿔 두 번 물어서 **순서에도 뒤집히는 답**이면 무승부로 봅니다.

    Returns: {"winner": "A"|"B"|None(무승부/판정불가), "reason": str, "agreed": bool, "raw": ...}
    """
    note = intent or build_intent_note(silhouette="silhouette" in (tag or "").lower(), people=people)
    q = (build_order_prompt(tag=tag, context=context, people=people, intent=note)
         .replace("이 이미지", "A 이미지(B도 같은 조건으로 그린 다른 버전입니다)").split("[응답 형식]")[0])
    fmt = ("\n[응답 형식] 첫 줄: 좋은 쪽의 문자자 A 또는 B만(예: A). "
           "둘 다 쓸 만하면 SAME, 둘 다 형편없으면 BOTH. 두 번째 줄: 한 줄 이유만.\n"
           "이미지는 A가 먼저, 그 다음에 B 순서로 첨부되었습니다.")
    url_a, url_b = (image_data_url(x, max_side=max_side) for x in (path_a, path_b))

    def ask(first, second, fu, su):
        msgs = [{"role": "system", "content": CRITERIA_SYSTEM_PROMPT},
                {"role": "user", "content": [
                    {"type": "text", "text": q + fmt.replace("A가 먼저, 그 다음에 B", f"{first}가 먼저, 그 다음에 {second}")},
                    {"type": "image_url", "image_url": {"url": fu}},
                    {"type": "image_url", "image_url": {"url": su}}]}]
        return ask_image_raw(msgs, model=model, endpoint=endpoint, max_tokens=max_tokens,
                             temperature=temperature, log_fn=log_fn) or ""

    r1 = ask("A", "B", url_a, url_b)
    v1 = re.search(r"\b(A|B|SAME|BOTH)\b", r1.strip().split("\n")[0].upper())
    w1 = v1.group(1) if v1 else None
    agreed, w2 = True, None
    if twice:
        r2 = ask("B", "A", url_b, url_a)
        v2 = re.search(r"\b(A|B|SAME|BOTH)\b", r2.strip().split("\n")[0].upper())
        w2 = v2.group(1) if v2 else None
        # 두 번째 요청에서는 A/B가 뒤집혀 있으므로, B/A 순서에서 'A'는 path_b를 뜻한다.
        flip = {"A": "B", "B": "A", "SAME": "SAME", "BOTH": "BOTH", None: None}[w2]
        agreed = (w1 == flip)
    win = w1 if agreed and w1 in ("A", "B") else None
    reason = extract_reason(r1) or extract_reason(r2 if twice else "")
    return {"winner": win, "first": path_a, "second": path_b, "agreed": agreed,
            "votes": [w1, w2], "reason": reason, "raw": r1}


def evaluate_and_filter(paths, min_score: int = 70, delete: bool = True, tags=None,
                        contexts=None, workers: int = 2, log_fn=None, **kw) -> dict:
    """생성 → 평가 → (기준 미만은 지우고) 사용/재생성 판정 한 번에.

    렌더 루프에서 쓸 수 있게 만든 얇은 래퍼입니다. 평가 자체가 실패하면(비전 미지원 등)
    **어느 것도 지우지 않고** keep에 전부 남깁니다 — 평가 실패가 렌더를 막지 않습니다.

    Returns: {"keep": [path…], "regen": [{"path","score","error"}…], "results": [...], "best": {...}}
    """
    rs = score_images(paths, tags=tags, contexts=contexts, workers=workers, **kw)
    scored = [r for r in rs if r.get("score") is not None]
    if not scored:                        # 평가 불능 → 판정 보류(지우지 않는다)
        return {"keep": [r["path"] for r in rs], "regen": [], "results": rs, "best": None,
                "note": "평가 불능 — 전수 사용"}
    keep, regen = [], []
    for r in rs:
        if r.get("score") is None:
            # 이 장만 평가를 못 했습니다(서버 일시 오류 등) → 지우지 않고 사용으로 둡니다.
            keep.append(r["path"])
            if log_fn:
                log_fn(f"[EVAL] 평가 불능 → 사용으로 둔다: {r['path']}")
            continue
        if r["score"] >= int(min_score):
            keep.append(r["path"])
            continue
        regen.append({"path": r["path"], "score": r.get("score"), "error": r.get("error")})
        if delete:
            try:
                os.remove(r["path"])
                if log_fn:
                    log_fn(f"[EVAL] 삭제({r.get('score')}점 < {min_score}): {r['path']}")
            except OSError as e:
                if log_fn:
                    log_fn(f"[EVAL] 삭제 실패: {r['path']} {e}")
    return {"keep": keep, "regen": regen, "results": rs, "best": pick_best(rs)}


def vision_probe(model: str = None, endpoint: str = "main", log=print) -> bool:
    """이 서버/모델로 평가할 수 있는지 한 번 확인합니다(--check)."""
    mdl = _model(endpoint, model)
    log(f"[check] endpoint={endpoint} model={mdl}")
    adv = vision_supported(mdl, endpoint, log_fn=log)
    if adv is False:
        log(f"[check] ❌ {mdl} 은(는) 텍스트 전용 advertised(input_modalities=[text]) — 이미지 평가 불가")
    elif adv is True:
        log(f"[check] ✅ {mdl} vision advertised")
    else:
        log(f"[check] ℹ️  {mdl} 은(는) 서버가 지원 여부를 안 알려줍니다(vLLM 등) — 실제 호출로 확인")
    # 실제 왕복: 글자 3개가 있는 tiny PNG를 만들어 점수를 매겨봅니다(1초 안쪽).
    from PIL import Image, ImageDraw
    buf = io.BytesIO()
    im = Image.new("RGB", (256, 256), (250, 250, 250))
    d = ImageDraw.Draw(im)
    d.ellipse([48, 48, 208, 208], fill=(255, 210, 200), outline=(40, 40, 40), width=6)
    d.ellipse([96, 104, 116, 124], fill=(30, 30, 30))
    d.ellipse([140, 104, 160, 124], fill=(30, 30, 30))
    d.arc([96, 140, 160, 176], 0, 180, fill=(30, 30, 30), width=6)
    im.save(buf, format="PNG")
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
        fh.write(buf.getvalue())
        probe_path = fh.name
    try:
        r = score_image(probe_path, tag="1girl, solo, simple face, white background",
                        context="테스트", single=True, model=model, endpoint=endpoint,
                        max_side=256, max_tokens=80, max_retries=1)
    finally:
        try:
            os.remove(probe_path)
        except OSError:
            pass
    if r.get("score") is None:
        log(f"[check] ❌ 이미지 평가 실패: {r.get('error')} ({r.get('ms')}ms)")
        return False
    log(f"[check] ✅ 이미지 평가 동작 — {r['score']}점 / {r['ms']}ms / 이유: {r['reason'][:40]}")
    return True


# =====================================================================
# CLI
# =====================================================================

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="이미지 LLM 평가(점수 0~100) — 사용/재생성 판정용")
    ap.add_argument("images", nargs="*", help="평가할 이미지 파일")
    ap.add_argument("--tag", default="", help="이 그림을 그린 프롬프트(TAG)")
    ap.add_argument("--context", default="", help="장면 본문(스토리 맥락)")
    ap.add_argument("--context-file", default="", help="맥락을 파일에서 읽기")
    ap.add_argument("--camera", default="", help="close_up/pov/wide 등 — 촬영 의도를 평가에 알려줍니다")
    ap.add_argument("--no-intent", action="store_true", help="제작 의도 면제 문장을 빼고 채점(비교용)")
    ap.add_argument("--people", default="auto", choices=["auto", "one", "multi", "none"],
                    help="인원 기대치(기본 auto: TAG의 인원 태그/`no humans`를 봅니다) — "
                         "배경 컷을 'multi'로 착각하면 배경 점수만 남고, 실루엣 컷을 'one'으로 착각하면 "
                         "상대방 때문에 무조건 깎입니다(2026-09-15 prerun 실측)")
    ap.add_argument("--single", default="auto", choices=["auto", "yes", "no"],
                    help="1인 화면 기준评估(기본 auto: TAG에서 인원 태그를 봅니다)")
    ap.add_argument("--model", default=None, help="평가 모델(기본 plot.json 값)")
    ap.add_argument("--endpoint", default="main", choices=["main", "agent", "anima", "text"])
    ap.add_argument("--max-side", type=int, default=768, help="전송 전 긴 변 리사이즈(0=원본)")
    ap.add_argument("--max-tokens", type=int, default=400)
    ap.add_argument("--workers", type=int, default=2, help="병렬 평가 수")
    ap.add_argument("--min-score", type=int, default=70, help="이 점수 미만 = 재생성 대상")
    ap.add_argument("--delete-below", action="store_true",
                    help="--min-score 미만인 파일을 지웁니다(재생성 대상 표시)")
    ap.add_argument("--pick", action="store_true", help="최고점 하나만 골라 경로를 찍습니다")
    ap.add_argument("--json", action="store_true", help="결과를 JSON으로")
    ap.add_argument("--check", action="store_true", help="비전 지원 확인만 (이미지 없음)")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)

    log = (lambda m: None) if a.quiet else print
    if a.check or not a.images:
        return 0 if vision_probe(model=a.model, endpoint=a.endpoint, log=log) else 2

    ctx = a.context
    if a.context_file:
        ctx = open(a.context_file, encoding="utf-8").read()
    single = None if a.single == "auto" else (a.single == "yes")
    kw = dict(tag=a.tag, context=ctx, single=single, people=a.people,
              camera=a.camera, model=a.model, endpoint=a.endpoint,
              max_side=(a.max_side or None), max_tokens=a.max_tokens, log_fn=log)
    if a.no_intent:
        kw["intent"] = " "
    rs = score_images(a.images, workers=a.workers, **kw)

    if a.json:
        print(json.dumps(rs, ensure_ascii=False))
    for r in rs:
        sc = "실패" if r["score"] is None else f"{r['score']}점"
        flag = "" if r["score"] is not None and r["score"] >= a.min_score else "   [재생성 대상]"
        print(f"  {sc:>5}  {os.path.basename(r['path'])}  ({r['ms']}ms) {r['reason']}{flag}"
              + (f"  <- {r['error']}" if r.get("error") else ""))
    if a.delete_below:
        for r in rs:
            if r["score"] is not None and r["score"] < a.min_score:
                try:
                    os.remove(r["path"])
                    print(f"  [삭제] {r['path']} ({r['score']}점 < {a.min_score})")
                except OSError as e:
                    print(f"  [삭제 실패] {r['path']}: {e}")
    if a.pick:
        b = pick_best(rs)
        print(("PICK " + b["path"] + f" ({b['score']}점)") if b else "PICK (없음)")
    keep = [r for r in rs if r["score"] is not None and r["score"] >= a.min_score]
    return 0 if keep else 1


if __name__ == "__main__":
    sys.exit(main())
