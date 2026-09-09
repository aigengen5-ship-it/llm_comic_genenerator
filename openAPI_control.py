"""
OpenAI API 호출 공통 모듈 — [llm_comic_gen 독립 환경] 원본 repo(novel/GUI)용 함수들을
뺀 최소화 버전 (2026-09-07).

[2026-09-07] 단일 LLM 정책: plot.json의 text_/main/agent/anima가 전부 같은 서버
            (localhost:8081, gemma-4-31B)를 가리킨다. 라우터 모델 스위칭/비전 평가/
            에피소드 작성 경로(call_openai_for_client/episode/image 등)는 이 파이프라인에
            없으므로 삭제했다.

함수 목록:
- get_openai_client()          : Main LLM 클라이언트 (plot.json ip_main:port_main)
- get_openai_client_anima()    : ANIMA LLM 클라이언트 (plot.json ip_anima:port_anima)
- get_openai_client_text()     : 텍스트 전용 클라이언트 (plot.json text_ip:text_port)
- call_openai_for_text()       : 텍스트 생성 (컷 스크립트·필드 추출)
- call_openai_for_plot()       : 비스트리밍 (한글 glossary 등)
- openAI_response()            : ANIMA용 호출 (init_anima_tags의 태그 생성)
- unload_all_router_models()   : ComfyUI 기동 전 llama 라우터 모델 unload
"""

import os
import json
import re
import time
import threading
import urllib.request
import random as rand
from openai import OpenAI, APITimeoutError, APIStatusError

import config


# =====================================================================
# 클라이언트 생성
# =====================================================================

def get_openai_client() -> OpenAI:
    log_fn = None
    if _ollama_enabled():
        return get_ollama_client(log_fn=log_fn)
    """Main LLM용 OpenAI 클라이언트를 생성하여 반환합니다."""
    api_key = os.environ.get("OPENAI_API_KEY", "gemma-4-31b")
    jv = config.get_json_value()
    return OpenAI(
        base_url=f"http://{jv.get('ip_main', '192.168.1.162')}:{jv['port_main']}/v1",
        api_key=api_key
    )


def get_openai_client_anima(log_fn=None) -> OpenAI:
    if _ollama_enabled():
        return get_ollama_client(log_fn=log_fn)
    """[2026-09-04] ANIMA LLM 전용 클라이언트 — plot.json ip_anima:port_anima.

    하드코딩 금지 원칙: ANIMA 호출의 엔드포인트/모델은 모두 plot.json에서진다.
      엔드포인트: ip_anima/port_anima  (모델: agent_anima — _resolve_anima_model)
      serve 서브타입(animaserve: vllm/llama)은 unload 대상 선정에만 사용된다
      (vLLM은 router식 unload 미지원 — unload_all_router_models 참고).
    키 누락 시 main 키(ip_main/port_main)로 fallback하되 ERROR를 로그에 출력한다.
    """
    jv = config.get_json_value()
    ip = jv.get("ip_anima") or jv.get("ip_main", "127.0.0.1")
    port = jv.get("port_anima") or jv.get("port_main", "8000")
    if not jv.get("ip_anima") or not jv.get("port_anima"):
        msg = (f"[ERROR][ANIMA_CLIENT] plot.json에 ip_anima/port_anima 없음 "
               f"→ main 키 fallback (ip={ip}, port={port})")
        if log_fn:
            log_fn(msg)
        else:
            print(msg)
    api_key = os.environ.get("OPENAI_API_KEY", "gemma-4-31b")
    if _ollama_enabled():
        return get_ollama_client(log_fn=log_fn)
    return OpenAI(base_url=f"http://{ip}:{port}/v1", api_key=api_key)


# =====================================================================
# [2026-09-07] ollama 백엔드 (plot.json "ollama_enb"=="yes")
#   - `pip install ollama`는 **클라이언트**다. 서버 바이너리는 별도로 떠야 한다
#     (ollama-linux-amd64.tgz → bin/ollama serve).
#   - 첫 호출 시 ollama를 import하고 `ollama_gguf` 경로로 gguf를 읽어들여
#     (Modelfile `FROM <gguf>` → 서버 blob에 해싱/저장) 모델을 만들고,
#     OpenAI와 같은 모양(.chat.completions.create)의 shim으로 감싼다.
#   - 렌더 직전 release_llm_for_gpu()가 keep_alive=0으로 메모리에서 완전히 내린다.
#
# 메모리 정책(타깃: VRAM 16GB + RAM 32GB / gemma-4-26B-A4B Q4_K_M ≈16GB 파일):
#   ollama는 num_ctx를 안 보내면 서버 기본값을 쓴다(구버전 2048=잘림, 신버전 262144=KV 폭주).
#   32GB RAM에서는 KV 캐시가 RAM을 잡아먹으므로 **매 요청마다 num_ctx를 명시**한다(기본 8192).
#   offload 비율은 ollama 자동(빈 값) — 레이어 수를 고정하려면 plot.json ollama_num_gpu_layers.
# =====================================================================

_ollama_client_cache = {}

OLLAMA_DEFAULT_NUM_CTX = 8192          # 16GB VRAM + 32GB RAM 기준(가중치 16.8GB + KV 약 1.5GB)
OLLAMA_DEFAULT_KEEP_ALIVE = "10m"      # LLM 단계 동안 상주, 렌더 직전 keep_alive=0으로 반납


def _ollama_run_options(jv=None) -> dict:
    """plot.json ollama_* → ollama options(매 요청에 붙인다). 미설정 키는 넣지 않는다(=ollama 자동)."""
    jv = jv if jv is not None else (config.get_json_value() or {})

    def _int(k):
        v = str(jv.get(k, "") or "").strip()
        return int(v) if v.lstrip("-").isdigit() else None

    opts = {}
    nc = _int("ollama_num_ctx")
    opts["num_ctx"] = nc if nc and nc > 0 else OLLAMA_DEFAULT_NUM_CTX
    for k, oname in (("ollama_num_gpu_layers", "num_gpu_layers"),
                     ("ollama_num_batch", "num_batch"),
                     ("ollama_top_k", "top_k")):
        v = _int(k)
        if v is not None:
            opts[oname] = v
    return opts


def _ollama_enabled(jv=None) -> bool:
    """ollama 백엔드 사용 여부. env `COMIC_OLLAMA_ENB=yes|no`가 plot.json을 오버라이드한다.

    런처가 plot.json을 임시로 바꿔놓고 원복하는 방식은 프로세스가 죽는 순간
    `ollama_enb=yes`가 파일에 고착된다(창을 ✕로 닫으면 trap이 안 돈다). 그래서 env로 옮겼다.
    """
    e = os.environ.get("COMIC_OLLAMA_ENB", "").strip().lower()
    if e in ("yes", "y", "true", "1"):
        return True
    if e in ("no", "n", "false", "0"):
        return False
    jv = jv if jv is not None else (config.get_json_value() or {})
    return str(jv.get("ollama_enb", "")).strip().lower() in ("yes", "y", "true", "1")


def ollama_host(jv=None) -> str:
    """ollama 베이스 URL. env `COMIC_OLLAMA_HOST`가 있으면 plot.json보다 앞선다.

    런처가 독자 포트(예: 127.0.0.1:11455)로 띄우면 사용자가 설치한 ollama(11434)와
    충돌하지 않는다 — 이 함수 한 곳만 봐서 클라이언트/언로드/프리플라이트가 같은 포트를 본다.
    """
    e = os.environ.get("COMIC_OLLAMA_HOST", "").strip()
    if e:
        return e if "://" in e else "http://" + e
    jv = jv if jv is not None else (config.get_json_value() or {})
    return str(jv.get("ollama_host") or "http://127.0.0.1:11434").strip()


class _OllamaCompletions:
    """client.chat.completions.create(**kwargs) 최속 shim — ollama python client 채팅."""

    def __init__(self, cli, model_name, run_opts=None, keep_alive=None):
        self._cli = cli
        self._model = model_name
        self._run = dict(run_opts or {})
        self._keep_alive = keep_alive or OLLAMA_DEFAULT_KEEP_ALIVE

    def create(self, model=None, messages=None, temperature=None, top_p=None,
               max_tokens=None, timeout=None, extra_body=None, reasoning_effort=None, **_ignored):
        import types
        options = dict(self._run)                          # num_ctx 등 메모리 상한 우선
        if temperature is not None:
            options["temperature"] = float(temperature)
        if top_p is not None:
            options["top_p"] = float(top_p)
        if max_tokens:
            options["num_predict"] = int(max_tokens)
        kwargs = dict(model=self._model, messages=messages or [], options=options or None,
                      stream=False, keep_alive=self._keep_alive)   # 파이프라인 LLM 단계 동안 상주
        try:
            # thinking 모델(gguf에 reasoning 채널)은 content가 비고 thinking에 답이 갇힌다 → think=False
            r = self._cli.chat(think=False, **kwargs)
        except TypeError:
            r = self._cli.chat(**kwargs)                      # 구버전 client: think kw 미지원
        msg_d = r.get("message") or {}
        content = (msg_d.get("content") or msg_d.get("thinking") or "") or ""
        p_tok = int(r.get("prompt_eval_count") or 0)
        c_tok = int(r.get("eval_count") or 0)
        # done_reason=length 이면 컨텍스트/num_predict 에 절단된 것 — OpenAI 처럼 알려준다
        done = str(r.get("done_reason") or "stop").strip().lower()
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(
                message=types.SimpleNamespace(content=content, role="assistant"),
                finish_reason="length" if done == "length" else "stop")],
            usage=types.SimpleNamespace(prompt_tokens=p_tok, completion_tokens=c_tok,
                                        total_tokens=p_tok + c_tok),
            model=self._model)


def _ollama_create_from_gguf(host: str, name: str, gguf_path: str, log_fn=None):
    """gguf로 ollama 모델을 만든다 (버전별 3단 폴백, 이미 있으면 호출부에서 스킵).

    v0.33 테스트 로그: 신서버는 API 'modelfile' 필드를 무시하고(from/files 필수로 본다)
    CLI는 파일을 blob으로 해싱해 {from: sha256:...}로 보낸다. 구버전 서버는 modelfile을
    parsed다. 그래서 순서: ① client create(from_=경로) ② raw POST modelfile(구서버)
    ③ ollama CLI(설치돼 있으면) — 어느 조합에서도 동작한다.
    """
    import json as _json
    import shutil as _sh
    msgs = []
    # ① python client (구/신 모두 정석 인자는 from_)
    try:
        import ollama
        ollama.Client(host=host).create(model=name, from_=gguf_path)
        msgs.append("client create(from_)")
    except Exception as e1:
        # ② raw POST — 구버전 서버는 modelfile 문자열 파싱
        try:
            body = _json.dumps({"model": name, "modelfile": f"FROM {gguf_path}\n"}).encode("utf-8")
            req = urllib.request.Request(host.rstrip("/") + "/api/create", data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=3600) as resp:
                for raw in resp:
                    try:
                        j = _json.loads(raw)
                    except Exception:
                        continue
                    if j.get("error"):
                        raise RuntimeError(j["error"])
            msgs.append("raw modelfile POST")
        except Exception as e2:
            # ③ CLI 폴백
            binp = _sh.which("ollama") or os.environ.get("OLLAMA_BIN") \
                or os.path.expanduser("~/AI/ollama/bin/ollama")
            if not _sh.which("ollama") and not os.path.isfile(binp):
                raise RuntimeError(f"ollama create 불가 (client:{e1} / api:{e2})")
            import subprocess, tempfile
            with tempfile.NamedTemporaryFile("w", suffix=".Modelfile", delete=False) as mf:
                mf.write(f"FROM {gguf_path}\n")
                mf_path = mf.name
            r = subprocess.run([binp, "create", name, "-f", mf_path],
                               capture_output=True, text=True, timeout=3600, env={**os.environ, "OLLAMA_HOST": host})
            if r.returncode != 0:
                raise RuntimeError(f"ollama create 실패(client:{e1} / api:{e2} / cli:{r.stderr[-200:]})")
            msgs.append("ollama CLI")
    msg = f"[ollama] gguf import → 모델 '{name}' 생성 ({msgs[0]}): {gguf_path}"
    print(msg)
    if log_fn:
        log_fn(msg)


def get_ollama_client(log_fn=None):
    """ollama 클라이언트(캐시). ollama_gguf가 있으면 그 gguf로 모델을 만든다(세션 1회)."""
    import types
    jv = config.get_json_value() or {}
    name = str(jv.get("ollama_model") or "comic-llm").strip()
    host = ollama_host(jv)
    # 상대경로("./gguf/x.gguf") 허용 — ollama 서버는 절대경로로 읽으므로 여기서 절대화한다
    gguf = os.path.abspath(str(jv.get("ollama_gguf") or "").strip()) if str(jv.get("ollama_gguf") or "").strip() else ""
    key = (host, name, gguf)
    if key in _ollama_client_cache:
        return _ollama_client_cache[key]
    try:
        import ollama
    except ImportError as e:
        raise RuntimeError("ollama_enb=yes 이지만 pip install ollama 가 없습니다 (requirements 참고)") from e
    cli = ollama.Client(host=host)
    if gguf:
        if not os.path.isfile(gguf):
            raise RuntimeError(f"ollama_gguf 파일 없음: {gguf}")
        try:
            ms = (cli.list().get("models") or [])

            def _mname(m):   # client 버전에 따라 name / model 키를 다 씀
                if isinstance(m, dict):
                    return str(m.get("name") or m.get("model") or "")
                return str(getattr(m, "name", None) or getattr(m, "model", None) or "")
            existing = {_mname(m) for m in ms}
        except Exception:
            existing = set()
        if name not in existing and (name + ":latest") not in existing:
            _ollama_create_from_gguf(host, name, gguf, log_fn=log_fn)
    shim = types.SimpleNamespace(chat=types.SimpleNamespace(completions=_OllamaCompletions(
        cli, name, run_opts=_ollama_run_options(jv),
        keep_alive=str(jv.get("ollama_keep_alive") or "").strip() or OLLAMA_DEFAULT_KEEP_ALIVE)))
    _ollama_client_cache[key] = shim
    return shim


# =====================================================================
# 텍스트(대사/캡션) 생성 전용 엔드포인트  [2026-09-07]
# =====================================================================
def get_openai_client_text(log_fn=None):
    if _ollama_enabled():
        return get_ollama_client(log_fn=log_fn)
    """텍스트 생성 전용 클라이언트 — plot.json text_ip:text_port.

    사용자 지시: "텍스트는 gemma-4-31B로 추출". main이 gx10(vllm/qwen)으로 넘어가도
    만화 캡션·대사 같은 결과물 텍스트는 gemma(llama router)로 뽑기 위한 역할 분리.
    키 누락 시 main 키로 폴백하되 침묵하지 않는다(anima 클라이언트와 동일 정책).
    """
    jv = config.get_json_value()
    ip = jv.get("text_ip") or jv.get("ip_main", "127.0.0.1")
    port = jv.get("text_port") or jv.get("port_main", "8000")
    if not jv.get("text_ip") or not jv.get("text_port"):
        msg = (f"[ERROR][TEXT_CLIENT] plot.json에 text_ip/text_port 없음 "
               f"→ main 키 fallback (ip={ip}, port={port})")
        if log_fn:
            log_fn(msg)
        else:
            print(msg)
    api_key = os.environ.get("OPENAI_API_KEY", "gemma-4-31b")
    return OpenAI(base_url=f"http://{ip}:{port}/v1", api_key=api_key)


def resolve_text_model(log_fn=None) -> str:
    """plot.json textLLM → model 필드 값 (alias는 MAIN_MODEL_MAP 변환, 없으면 mainLLM 폴백)"""
    jv = config.get_json_value()
    key = (jv.get("textLLM") or "").strip()
    if not key:
        key = (jv.get("mainLLM") or "gemma").strip()
        msg = f"[ERROR][TEXT_MODEL] plot.json에 textLLM 없음 → mainLLM fallback (model={key})"
        if log_fn:
            log_fn(msg)
        else:
            print(msg)
    return _resolve_main_model(key)


def call_openai_for_text(prompt_text: str, system_prompt: str = None, messages: list = None,
                         log_fn=None, **kw) -> tuple:
    """call_openai_for_plot과 동일 구현, 엔드포인트/모델만 text_*로 교체.

    사용처: 만화 컷 텍스트(상황 묘사 + 대사) 생성. 파라미터/재시도/타임아웃 정책은 plot와 같다.
    kw: temperature, repeat_penalty, reasoning_effort, enable_thinking, max_retries 등 그대로 전달.
    """
    kw.setdefault("temperature", 0.8)
    return call_openai_for_plot(prompt_text, system_prompt=system_prompt, messages=messages,
                                log_fn=log_fn, model=resolve_text_model(log_fn=log_fn),
                                client=get_openai_client_text(log_fn=log_fn), **kw)


def _log_token_usage(usage, log_fn=None, tag=""):
    """[2026-08-27] 토큰 사용량 로깅 공용 헬퍼 (usage가 None인 서버도 대응)."""
    if usage is None:
        msg = f"[TOKEN] {tag}토큰 사용량 미반환 (usage=None)"
        print(msg)
        if log_fn:
            log_fn(msg)
        return
    msg = (f"[TOKEN] {tag}입력(프롬프트) 토큰 수: {usage.prompt_tokens}, "
           f"출력(생성된) 토큰 수: {usage.completion_tokens}, "
           f"총 사용된 context: {usage.total_tokens}")
    print(msg)
    if log_fn:
        log_fn(msg)


def _fmt_elapsed(seconds: float) -> str:
    """[2026-09-04] 걸린 시간을 읽기 좋은 문자열로 변환 (예: 9.8초 / 2분 14.5초)."""
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{seconds:.1f}초"
    m, s = divmod(seconds, 60)
    return f"{int(m)}분 {s:.1f}초"


# =====================================================================
# call_openai_for_plot (비스트리밍 - plot_gen / 에피소드 리뷰 용)
# =====================================================================

# [2026-09-01] plot.json mainLLM 값 → main 서버로 보낼 model 필드
#   값이 아래 표에 없으면(예: "qwen3.8-flash-next") 서버가 받는 모델명 그대로 통과시킨다.
MAIN_MODEL_MAP = {
    "gemma": "gemma-4-31B-it",
    # [2026-09-05] 이미지 판독 전용 프리셋 alias (models_router.ini의 [gemma-4-31B-img], mmproj 장착).
    #   텍스트 생성용 gemma-4-31B는 mmproj가 없어 이미지를 받지 않는다 → 평가는 이 alias/실 ID로.
    "gemma-img": "gemma-4-31B-img",
    "qwen": "qwen3.6-27b_mtp",
    "qwen3.8": "qwen3.8-27b_mtp",
}


def _is_qwen_model(model_name: str) -> bool:
    """모델명/alias에서 qwen 계열 여부를 판정한다 (thinking/reasoning 파라미터 분기용)."""
    return "qwen" in (model_name or "").strip().lower()


def _resolve_main_model(main_llm: str = None) -> str:
    """plot.json mainLLM 값을 main 서버 model 필드 값으로 변환한다.

    기존에는 "qwen" 완전일치만 qwen으로 인식하고 나머지는 무조건 gemma-4-31B-it으로 보내,
    mainLLM="qwen3.8-flash-next"처럼 값을 바꿔도 gemma로 호출되는 문제가 있었다.

    Args:
        main_llm: 지정 시 그 값을 사용하고, None이면 plot.json의 mainLLM을 읽는다

    Returns:
        chat.completions.create의 model 필드 값
    """
    if main_llm is None:
        main_llm = config.get_json_value().get("mainLLM", "gemma")
    key = (main_llm or "gemma").strip()
    return MAIN_MODEL_MAP.get(key.lower(), key)


def _resolve_anima_model(log_fn=None) -> str:
    """[2026-09-04] ANIMA 호출의 model 필드 값 = plot.json agent_anima.

    키 누락 시 mainLLM으로 fallback하되 ERROR를 로그에 출력한다 (폴백 정책 허용,
    단 침묵하지 않음). 값 해석은 _resolve_main_model과 동일 규칙(MAIN_MODEL_MAP
    등록 값은 변환, 미등록 값은 그대로 통과).
    """
    key = (config.get_json_value().get("agent_anima") or "").strip()
    if not key:
        key = (config.get_json_value().get("mainLLM") or "gemma").strip()
        msg = f"[ERROR][ANIMA_MODEL] plot.json에 agent_anima 없음 → mainLLM fallback (model={key})"
        if log_fn:
            log_fn(msg)
        else:
            print(msg)
    return _resolve_main_model(key)

def call_openai_for_plot(prompt_text: str, system_prompt: str = None, messages: list = None, log_fn=None,
                         temperature: float = None, timeout: float = None,
                         repeat_penalty: float = None, max_retries: int = None,
                         retry_delay: float = None, model: str = None,
                         enable_thinking: bool = True, reasoning_effort: str = "medium",
                         client=None) -> tuple:
    """OpenAI API를 호출하여 플롯 생성 응답을 반환합니다.
    messages가 제공되면 대화 이력을 유지합니다 (원본 리스트는 변형되지 않음).
    log_fn: (msg: str) -> None 형태로 로깅 함수를 전달합니다.
    temperature: 지정 시 해당 값 사용 (기본: 0.9 + rand)
    timeout: 지정 시 해당 값 사용 (기본: 400.0)
    repeat_penalty: 지정 시 해당 값 사용 (기본: 1.15)
    max_retries: 재시도 횟수 (기본: 3)
    retry_delay: 재시도 대기 시간 (기본: 10)
    model: [2026-09-01] main 서버 model 필드 직접 지정 (기본 None = plot.json mainLLM 해석)
    enable_thinking: [2026-09-01] qwen 계열 thinking 활성화 (기본 True)
    reasoning_effort: [2026-09-01] qwen 계열 reasoning 강도 (None = 필드 미전달)
    client: [2026-09-07] 클라이언트 직접 지정 (기본 None = get_openai_client() = main).
            텍스트 전용 엔드포인트(call_openai_for_text)처럼 서버만 바꿔 쓰려는 용도.

    [2026-09-01] 사용처 확장: full_episode_gen의 기-승-전-결 **리뷰 4회**가 이 함수로 이동했다
    (본문 작성/재작성은 agent 쪽 call_openai_for_episode).

    Returns:
        (result: str, messages: list) — 업데이트된 messages 리스트를 함께 반환
    """
    if client is None:
        client = get_openai_client()

    # [2026-09-01] mainLLM 설정 확인 (plot.json) — 미등록 값은 그대로 통과
    if model is None:
        model = _resolve_main_model()

    if system_prompt is None:
        system_prompt = config.system_prompt

    if messages is None:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt_text}
        ]
    else:
        messages = list(messages)  # 원본 리스트 변형 방지
        messages.append({"role": "user", "content": prompt_text})

    if temperature is None:
        temperature = 0.9 + rand.randint(0, 1) / 10.0
    if timeout is None:
        timeout = 800.0
    if max_retries is None:
        max_retries = 3
    if retry_delay is None:
        retry_delay = 10

    top_p = 0.95

    # 모델별 파라미터 설정
    if _is_qwen_model(model):
        extra_body = {
            "chat_template_kwargs": {
                "enable_thinking": enable_thinking,
                "preserve_thinking": True,
            },
        }
        effort = reasoning_effort
        if log_fn:
            log_fn(f"[PLOT_PROMPT] model={model}, reasoning={effort}, thinking={enable_thinking}, temp={temperature:.2f}\n{prompt_text}")
    else:
        if repeat_penalty is None:
            repeat_penalty = 1.15
        top_k = 64
        extra_body = {"repeat_penalty": repeat_penalty, "top_k": top_k}
        effort = None
        if log_fn:
            log_fn(f"[PLOT_PROMPT] model={model}, temp={temperature:.2f}\n{prompt_text}")

    max_try = 0
    timeout_check = 0
    # [2026-09-04] LLM 호출 걸린 시간 측정 (theme_gen_auto / plot_gen / full_episode_gen 로그에 출력)
    t_call_start = time.time()
    elapsed_api = 0.0

    while timeout_check == 0:
        try:
            create_kwargs = {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "top_p": top_p,
                "stream": False,
                "timeout": timeout,
                "extra_body": extra_body,
            }
            if effort:
                create_kwargs["reasoning_effort"] = effort

            t_attempt = time.time()
            response = client.chat.completions.create(**create_kwargs)
            elapsed_api = time.time() - t_attempt
            timeout_check = 1
        except APITimeoutError:
            print(f"서버 응답 시간이 초과되었습니다. 다시 시도합니다 ({max_try + 1}/{max_retries}, {_fmt_elapsed(time.time() - t_call_start)} 경과)")
            if log_fn:
                log_fn(f"[TIMEOUT] 서버 응답 시간 초과. 재시도 ({max_try + 1}/{max_retries}, {_fmt_elapsed(time.time() - t_call_start)} 경과)")
            timeout_check = 0
            max_try += 1
            time.sleep(retry_delay)
            if max_try > max_retries:
                if log_fn:
                    log_fn(f"[ERROR] 서버 응답 실패 ({max_retries}회 재시도 후, 총 {_fmt_elapsed(time.time() - t_call_start)} 경과)")
                return "서버 응답 실패", messages
        except APIStatusError as e:
            if e.status_code < 500:
                # 4xx: 클라이언트 에러 → 재시도 무의미
                print(f"API 에러 ({e.status_code}): {e.message}")
                if log_fn:
                    log_fn(f"[API_ERROR] {e.status_code}: {e.message} (4xx - 재시도 불가, {time.time() - t_call_start:.1f}초 경과)")
                return "서버 응답 실패", messages
            print(f"API 에러 ({e.status_code}): {e.message}. 재시도 ({max_try + 1}/{max_retries}, {_fmt_elapsed(time.time() - t_call_start)} 경과)")
            if log_fn:
                log_fn(f"[API_ERROR] {e.status_code}: {e.message}. 재시도 ({max_try + 1}/{max_retries}, {_fmt_elapsed(time.time() - t_call_start)} 경과)")
            timeout_check = 0
            max_try += 1
            time.sleep(retry_delay)
            if max_try > max_retries:
                if log_fn:
                    log_fn(f"[ERROR] 서버 응답 실패 ({max_retries}회 재시도 후, 총 {_fmt_elapsed(time.time() - t_call_start)} 경과)")
                return "서버 응답 실패", messages

    result = response.choices[0].message.content.strip()
    messages.append({"role": "assistant", "content": result})

    # [2026-09-04] LLM 호출 걸린 시간 로그
    msg = (f"[ELAPSED] PLOT({model}) API 응답 {_fmt_elapsed(elapsed_api)} / "
           f"총 경과 {_fmt_elapsed(time.time() - t_call_start)} (시도 {max_try + 1}회)")
    print(msg)
    if log_fn:
        log_fn(msg)

    # [2026-08-27] 토큰 사용량 확인
    _log_token_usage(getattr(response, "usage", None), log_fn, tag=f"PLOT({model}) ")

    if log_fn:
        log_fn(f"[PLOT_RESULT]\n{result}")

    return result, messages


# =====================================================================
# router 헬퍼 (unload_all_router_models가 사용)
# =====================================================================

def _router_base_url() -> str:
    jv = config.get_json_value()
    return f"http://{jv.get('ip_agent', '127.0.0.1')}:{jv.get('port_agent', '8081')}"


def _router_get_models(log_fn=None, base: str = None) -> dict | None:
    """GET /models → {alias: status_value} (loaded/loading/unloaded).

    router가 죽었거나 응답 없으면 None 반환.
    [2026-09-04] base 지정 시 그 엔드포인트 조회 (llama serve 공통 — unload 게이트용).
    """
    try:
        with urllib.request.urlopen((base or _router_base_url()) + "/models", timeout=5) as resp:
            if resp.status != 200:
                return None
            data = json.loads(resp.read().decode())
        return {m["id"]: m.get("status", {}).get("value") for m in data.get("data", [])}
    except Exception:
        return None


def _router_post(endpoint: str, model: str, base: str = None) -> bool:
    """POST /models/load | /models/unload (body: {"model": alias})"""
    try:
        req = urllib.request.Request(
            (base or _router_base_url()) + endpoint,
            data=json.dumps({"model": model}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception:
        return False


def _wait_router_model(alias: str, want_status: str, timeout: int, log_fn=None, base: str = None) -> bool:
    """모델 상태가 want_status가 될 때까지 대기 (최대 timeout 초)."""
    def _log(msg):
        if log_fn:
            log_fn(msg)
    for i in range(timeout):
        time.sleep(1)
        statuses = _router_get_models(base=base)
        if statuses is None:
            _log(f"[ROUTER] '{alias}' 대기 중 router 응답 없음 ({i+1}초)")
            return False
        if statuses.get(alias) == want_status:
            return True
    return False


# [2026-08-27] parallel qwen 리뷰 스레드(4개)의 동시 router 모델 스위칭 race 방지
# (스위칭만 직렬화, 실제 추론(chat completions)은 parallel 유지)
_router_switch_lock = threading.Lock()


def _post_ok(url: str, payload: dict, timeout: float = 5.0) -> bool:
    """best-effort POST JSON → 2xx 여부만."""
    import json as _json
    try:
        req = urllib.request.Request(url, data=_json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def _ollama_unload(jv, _log) -> bool:
    """[2026-09-08] ollama 백엔드 언로드 단일 지점: keep_alive=0 → 'ollama ps'가 비워질 때까지 대기.

    release_llm_for_gpu와 unload_all_router_models가 함께 쓴다 — ollama에는 llama 라우터식
    /models·/unload API가 없어서 라우터 쪽으로 보내면 시간만 낭비된다.
    """
    try:
        import ollama
        import time as _t
        _host = ollama_host(jv)
        _name = str((jv or {}).get("ollama_model") or "comic-llm").strip()
        _cli = ollama.Client(host=_host)
        _cli.generate(model=_name, keep_alive=0)
        _log(f"[LLM release] ollama 모델 '{_name}' keep_alive=0 → 메모리에서 내림")
        # unload는 비동기 — 목록에서 실제로 사라질 때까지(최대 20s) 기다린다.
        #   안 기다리면 ComfyUI가 모델 로딩을 시작하는 순간에도 가중치가 VRAM/RAM에 남아
        #   NaN(검은 이미지)을 만든다(16GB VRAM+32GB RAM 실측).
        _stem = _name.split(":")[0]
        for _ in range(20):
            try:
                ps = (_cli.ps() or {}).get("models") or []
            except Exception:
                break
            if not any(_stem in str((m or {}).get("name") or "") for m in ps):
                _log("[LLM release] ollama ps 비움 확인 → 메모리 반납 완료")
                break
            _t.sleep(1)
        return True
    except Exception as e:
        _log(f"[LLM release] ollama unload 실패({e}) → 다른 경로 계속")
        return False


def release_llm_for_gpu(json_value=None, log_fn=None) -> bool:
    """[2026-09-07] 사용자 지시: "LLM 동작 완료하면 무조건 정지시켜 메모리를 회복할 것
    (ComfyUI가 써야 되니까)". 컷 스크립트 생성이 끝난 직후(렌더 직전) 호출된다.

    엔드포인트별 전략 (모두 best-effort, 실패해도 파이프라인은 계속):
      ① llm_server.py(우리 전용 서버) : POST /v1/unload → 프로세스 종료로 VRAM 완전 회수
      ② ollama 호환                   : POST /api/chat {model, keep_alive:0} → 모델 언로드
      ③ llama router                  : unload_all_router_models() (plot의 *serve 키 기준)
    """
    jv = json_value or config.get_json_value() or {}
    def _log(m):
        if log_fn:
            log_fn(m)
        else:
            print(m)
    eps, seen = [], set()
    for ipk, portk in (("text_ip", "text_port"), ("ip_main", "port_main"),
                       ("ip_agent", "port_agent"), ("ip_anima", "port_anima")):
        ip = str(jv.get(ipk) or jv.get("ip_main") or "127.0.0.1")
        port = str(jv.get(portk) or jv.get("port_main") or "8081")
        if (ip, port) not in seen:
            seen.add((ip, port))
            eps.append((ip, port))
    # ⓪ [2026-09-07] ollama 백엔드: keep_alive=0 generate로 모델 완전 언로드(VRAM 반환)
    if _ollama_enabled(jv):
        # ollama 모드에서는 llama 라우터/전용서버 API를 두드릴 필요가 없다(없다). 성공/실패와 무관하게 여기서 끝.
        return _ollama_unload(jv, _log)
    models = sorted({str(jv.get(k) or "").strip() for k in ("textLLM", "mainLLM")} - {""})
    released = False
    for ip, port in eps:
        base = f"http://{ip}:{port}"
        if _post_ok(base + "/v1/unload", {}, timeout=4):
            _log(f"[LLM release] {ip}:{port} 전용 서버 종료 → VRAM 반환")
            released = True
            continue
        ok_ep = False
        for m in models:
            if _post_ok(base + "/api/chat", {"model": m, "stream": False, "keep_alive": 0}, timeout=6):
                _log(f"[LLM release] {ip}:{port} ollama 모델 '{m}' 언로드(keep_alive=0)")
                ok_ep = released = True
        if ok_ep:
            continue
    if not released:
        try:
            if unload_all_router_models(log_fn=log_fn):
                released = True
        except Exception as e:
            _log(f"[LLM release] llama router unload 실패: {e}")
    if not released:
        _log("[LLM release] unload 엔드포인트 없음 — GPU 메모리가 회수되지 않았을 수 있다 "
             "(ComfyUI VRAM 경합 시 LLM 서버를 수동 종료하라)")
    return released


def unload_all_router_models(log_fn=None) -> bool:
    """llama 서빙 엔드포인트만 loaded 모델 전부 unload (GPU 메모리 확보용).

    [2026-09-04] "router(agent) 전용" → "llama 서빙 전체"로 확장.
    plot.json의 서빙 타입 키가 unload 대상을 결정한다 — vLLM은 router식 unload
    미지원이므로 건너뛴다 (로그만 남기고 실패로 취급하지 않음):
      - mainserve   == "llama" 인 경우만 → ip_main:port_main unload
      - agentserve  == "llama" 인 경우만 → ip_agent:port_agent unload
        (라우터는 llama.cpp가 기본 — 키 없으면 llama로 간주, 옛 plot.json 하위호환)
      - animaserve  == "llama" 인 경우만 → ip_anima:port_anima unload
        (키 없으면 클라이언트와 동일하게 main 키 사용)
      - textserve   == "llama" 인 경우만 → text_ip:text_port unload   [2026-09-07]
        (텍스트 전용 엔드포인트. text_ip 키가 없으면 main과 같은 서버이므로 별도 대상 아님)
    같은 ip:port 엔드포인트는 중복 제거하여 1회만 수행한다.
    unload된 모델은 이후 요청 시 router autoload로 다시 로드된다.

    Args:
        log_fn: 로깅 함수

    Returns:
        True: llama 대상 전체 unload 성공 (또는 llama 대상 없음)
        False: 일부 llama 엔드포인트 미응답 또는 unload 실패
    """
    def _log(msg):
        if log_fn:
            log_fn(msg)
        else:
            print(msg)

    # [2026-09-08] ollama 모드면 llama 라우터 API(/models·/unload)는 존재하지 않는다.
    #   ComfyUI 기동 전(run_comic.start_comfyui)에도 이 쪽이 호출되므로 ollama 언로드로 대체한다.
    if _ollama_enabled():
        return _ollama_unload(config.get_json_value() or {}, _log)

    jv = config.get_json_value()
    targets = []  # [(label, base_url)] — 같은 endpoint 중복 제거
    serve_specs = [
        ("main", str(jv.get("mainserve", "")).strip().lower(),
         jv.get("ip_main", "127.0.0.1"), jv.get("port_main", "8000")),
        ("agent", str(jv.get("agentserve", "llama")).strip().lower(),
         jv.get("ip_agent", "127.0.0.1"), jv.get("port_agent", "8081")),
        ("anima", str(jv.get("animaserve", "")).strip().lower(),
         jv.get("ip_anima") or jv.get("ip_main", "127.0.0.1"),
         jv.get("port_anima") or jv.get("port_main", "8000")),
    ]
    if jv.get("text_ip"):        # [2026-09-07] 텍스트 전용(gemma)도 llama 서빙이면 unload 대상
        serve_specs.append(("text", str(jv.get("textserve", "llama")).strip().lower(),
                            jv.get("text_ip"), jv.get("text_port") or jv.get("port_main", "8000")))
    for label, serve, ip, port in serve_specs:
        if serve != "llama":
            _log(f"[UNLOAD_ALL] '{label}' serve='{serve}' → llama 미서빙, unload skip (vLLM 미지원)")
            continue
        base = f"http://{ip}:{port}"
        if base not in [b for _, b in targets]:
            targets.append((label, base))

    if not targets:
        _log("[UNLOAD_ALL] llama serve 대상 없음 - unload 스킵")
        return True

    all_ok = True
    for label, base in targets:
        if not _unload_endpoint_models(label, base, _log):
            all_ok = False
    return all_ok


def _unload_endpoint_models(label: str, base: str, _log) -> bool:
    """llama 엔드포인트 1곳의 loaded(로딩중 포함) 모델을 순차 unload한다."""
    statuses = _router_get_models(base=base)
    if statuses is None:
        _log(f"[UNLOAD_ALL] '{label}' 미응답 ({base}) - unload 스킵")
        return False

    loaded = {alias: st for alias, st in statuses.items() if st in ("loaded", "loading")}
    if not loaded:
        _log(f"[UNLOAD_ALL] '{label}' ({base}) - loaded 모델 없음")
        return True

    _log(f"[UNLOAD_ALL] '{label}' ({base}) {len(loaded)}개 모델 unload: {', '.join(sorted(loaded))}")
    all_ok = True
    for alias in sorted(loaded):
        if _router_post("/models/unload", alias, base=base):
            if _wait_router_model(alias, "unloaded", 60, log_fn=_log, base=base):
                _log(f"[UNLOAD_ALL] '{label}' '{alias}' unload 완료 ✓")
            else:
                _log(f"[UNLOAD_ALL] ⚠ '{label}' '{alias}' unload 대기 시간초과(60초)")
                all_ok = False
        else:
            _log(f"[UNLOAD_ALL] ✗ '{label}' '{alias}' unload 요청 실패")
            all_ok = False
    return all_ok


# =====================================================================
# openAI_response (ANIMA용 간단한 호출)
# =====================================================================

# [2026-09-05] ANIMA 태그 큐레이션 퇴행 루프 억제용 상수 (openAI_response 전용).
#   log/anima_gen.log(260905) 실측: 같은 섹션 내 동일 태그 반복 18%, [TAG DEDUP] 발동 40%,
#   background 섹션 13,166자 폭주. qwen 분기에는 반복 패널티가 아예 없었고(repeat_penalty 1.15는
#   비-qwen 전용) max_tokens도 없어서 루프가 무제한으로 길어졌다 → 두개 모두 상한을 건다.
#   (openAI_response는 ANIMA 전용 경로 — 정상 생성은 ~1,100 token이라 2048는 충분한 여유값)
ANIMA_PRESENCE_PENALTY = 0.15
ANIMA_MAX_TOKENS = 2048


def openAI_response(json_value, client, messages_history, user_input, op_mode, chat1, call_label=""):
    """OpenAI API 호출 함수 (ANIMA용, anima_gen.py의 모든 LLM 호출 지점).

    [2026-09-02] plot(call_openai_for_plot)과 동일한 방식으로 호출한다.
      기존: plot.json의 anima_agent("gemma") → "gemma-4-31B-it" 고정 + temperature만 전달.
      변경: 모델 = _resolve_main_model() → plot.json mainLLM(현재 "qwen3.8-flash-next")을
            plot과 완전히 같은 경로로 사용. 모델별 파라미터도 call_openai_for_plot과 동일 규칙:
            - qwen 계열 : chat_template_kwargs(enable_thinking=False, preserve_thinking=False)
                          — ANIMA는 마커/JSON 조립 작업이라 thinking은 순수 오버헤드.
                          (2026-09-02: thinking+preserve_on 시 RP 히스토리에 thinking본이
                           누적되고 포맷 준수가 떨어져 재시도 폭증 → OFF로 확정)
            - 그 외     : repeat_penalty 1.15 + top_k 64
            + 800s timeout, timeout/5xx 재시도(3회, 10초 간격), 4xx 즉시 예외.
      재시도 후에도 실패하면 기존 계약대로 예외를 raise한다(anima_gen.py 호출부 try/except 유지).

    Args:
        json_value: 설정 JSON (호환성 유지용 — 모델 해석은 plot.json mainLLM을 사용하므로 미사용)
        client: OpenAI 클라이언트 (get_openai_client_anima() = plot.json ip_anima:port_anima)
        messages_history: 메시지 히스토리
        user_input: 사용자 입력
        op_mode: 운영 모드 (미사용 유지)
        chat1: 채팅 모드 (미사용 유지)
        call_label: API 호출 라벨 (로깅용)

    Returns:
        (messages_history, full_response)
    """
    # [2026-09-04] ANIMA 전용 해석: 모델 = plot.json agent_anima (없으면 mainLLM 폴백+ERROR 로그).
    #   엔드포인트는 호출부가 전달한 get_openai_client_anima() 클라이언트(ip_anima:port_anima) 사용.
    model = _resolve_anima_model()

    messages = messages_history + [{"role": "user", "content": user_input}]

    timeout = 800.0          # plot과 동일 (qwen thinking 모드는 생성 시간이 길다)
    max_retries = 3
    retry_delay = 10
    top_p = 0.95

    # 모델별 파라미터 설정 (call_openai_for_plot과 동일한 규칙)
    # [2026-09-04] qwen 계열 thinking은 plot.json anima_effort가 결정 (기본 "off" = 기존처럼 무thinking).
    #   "low"/"medium"/"high" 설정 시에만 reasoning 켬. 포맷팅 작업이라 기본 OFF가 권장.
    _ae = str(config.get_json_value().get("anima_effort", "off")).strip().lower()
    if _is_qwen_model(model):
        if _ae in ("low", "medium", "high"):
            extra_body = {
                "chat_template_kwargs": {
                    "enable_thinking": True,
                    "preserve_thinking": False,
                },
            }
            effort = _ae
        else:
            extra_body = {
                "chat_template_kwargs": {
                    "enable_thinking": False,
                    "preserve_thinking": False,
                },
            }
            effort = None
    else:
        extra_body = {"repeat_penalty": 1.15, "top_k": 64}
        effort = None

    create_kwargs = {
        "model": model,
        "messages": messages,
        "temperature": 0.7,   # ANIMA는 프롬프트/JSON 조립 위주라 plot(0.9~1.0)보다 낮은 값 유지
        "top_p": top_p,
        "stream": False,
        "timeout": timeout,
        "extra_body": extra_body,
        # [2026-09-05] 퇴행 루프 억제/비용 상한 (위 상수 주석 참고)
        "presence_penalty": ANIMA_PRESENCE_PENALTY,
        "max_tokens": ANIMA_MAX_TOKENS,
    }
    if effort:
        create_kwargs["reasoning_effort"] = effort

    max_try = 0
    while True:
        try:
            response = client.chat.completions.create(**create_kwargs)
            break
        except APITimeoutError as e:
            max_try += 1
            if max_try > max_retries:
                if call_label:
                    print(f"  [에러] {call_label}: 응답 시간 초과 ({max_retries}회 재시도 후) {e}")
                raise
            if call_label:
                print(f"  [타임아웃] {call_label}: 응답 시간 초과, 재시도 ({max_try}/{max_retries})")
            time.sleep(retry_delay)
        except APIStatusError as e:
            # 4xx: 클라이언트 에러 → 재시도 무의미, 즉시 예외 (plot과 동일 판정)
            if e.status_code < 500 or max_try >= max_retries:
                if call_label:
                    print(f"  [에러] {call_label}: {e}")
                raise
            max_try += 1
            if call_label:
                print(f"  [에러] {call_label}: API 에러 ({e.status_code}), 재시도 ({max_try}/{max_retries})")
            time.sleep(retry_delay)
        except Exception as e:
            if call_label:
                print(f"  [에러] {call_label}: {e}")
            raise

    full_response = response.choices[0].message.content.strip()

    # [2026-09-02] 토큰 사용량 확인 (plot과 동일) — ANIMA(model) 태그
    _log_token_usage(getattr(response, "usage", None), tag=f"ANIMA({model}) ")

    messages_history = messages + [{"role": "assistant", "content": full_response}]
    return messages_history, full_response
