"""
llm_server.py — llm_comic_gen 전달용 전용 LLM 서빙 (ollama/llama-server 대체물, pip-only)

타깃 사양(2026-09-07 사용자 지시): **VRAM 16GB + gemma-4-26B-A4B Q4_K_M(≈16GB 파일)** —
파일이 VRAM과 거의 같으므로 계층 일부 CPU 오프로드가 필수. 기본값이 그것에 맞춰져 있다:
  - 모델 파일 크기와 --vram-gb(기본 16)로 남길 GPU 레이어 수를 자동 계산한다(gguf 메타 있으면).
  - 사용법: python llm_server.py --model gemma-4-26B-A4B-it-Q4_K_M.gguf
  - 렌더 단계 전에 파이프라인이 POST /v1/unload를 보내면 **프로세스를 종료**시켜 VRAM을
    ComfyUI에 되돌린다. 크래시로 unload를 못 맞아도 --idle-exit(기본 900초 무활동)로 자체 종료.

파이프라인은 OpenAI 호환 /v1만 쓰므로 plot.json(text_ip/text_port)이 여기만 가리키면 된다.
GPU offload 빌드:
    CPU(기본)   : pip install llama-cpp-python fastapi uvicorn
    CUDA        : CMAKE_ARGS="-DGGML_CUDA=on" pip install llama-cpp-python fastapi uvicorn
    ROCm(AMD)   : CMAKE_ARGS="-DGGML_HIP=on"  pip install llama-cpp-python fastapi uvicorn
엔드포인트: /health, /v1/models, /v1/chat/completions(non-stream+stream, usage), /v1/unload
의존성 지연 import: 이 파일 자체는 llama_cpp/fastapi 없이 import 가능(selftest용).
"""
import argparse
import json
import os
import threading
import time
import uuid

DEFAULT_PORT = 8081        # plot.json text_port 기본값과 동일
DEFAULT_CTX = 16384        # 16GB에서 kv cache 자리 확보용(원문은 대체로 10k 토큰 이내)
DEFAULT_VRAM_GB = 16.0     # 타깃 사양
RESERVE_GB = 3.0           # kv cache/활성화 예약(오프로드 계산에서 제외)
DEFAULT_IDLE_EXIT = 900    # 무활동 이초간 요청 없으면 자체 종료(VRAM 회수)


def default_name_for(model_path: str) -> str:
    """--name 미지정 시 모델 파일명에서 접미시를 뺀 id (예: gemma-4-26B-A4B-it-Q4_K_M.gguf → …-Q4_K_M)."""
    base = os.path.basename(model_path or "local-llm")
    for suffix in (".gguf", ".safetensors", ".bin"):
        if base.lower().endswith(suffix):
            base = base[: -len(suffix)]
    return base or "local-llm"


def suggest_gpu_layers(model_path: str, vram_gb: float = DEFAULT_VRAM_GB,
                       reserve_gb: float = RESERVE_GB):
    """16GB 기준 자동 오프로드: GPU에 남릴 레이어 수 (None = 모름 → -1 전적재 시도).

    gguf 메타(pygguf)로 레이어 수를 읽고, 파일 크기 대비 (vram - 예약)/파일_gb 비율로
    계층을 잘른다. 예: 15.8GB 파일 @16GB → (16-3)/15.8 ≈ 0.82 → 레이어의 82%만 GPU.
    """
    try:
        from gguf import GGUFReader
        total_gb = os.path.getsize(model_path) / 1e9
        reader = GGUFReader(model_path, "r")
        fields = getattr(reader, "fields", None) or getattr(reader, "metadata", {}) or {}
        n_layers = 0
        for k, v in fields.items():
            if str(k).endswith("layer_count"):
                try:
                    n_layers = int(getattr(v, "contents", v))
                except Exception:
                    n_layers = 0
                if n_layers > 0:
                    break
        if total_gb <= 0 or n_layers <= 0:
            return None
        if total_gb + reserve_gb <= vram_gb:
            return -1                                  # 다 들어간다
        keep = int(n_layers * max(0.05, (vram_gb - reserve_gb) / total_gb))
        return max(2, min(keep, n_layers - 1))         # 전부 CPU만은 피한다
    except Exception:
        return None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="llm_comic_gen dedicated GGUF server (OpenAI-compatible, 16GB target)")
    p.add_argument("--model", required=True, help="GGUF 모델 파일 경로 (예: gemma-4-26B-A4B-it-Q4_K_M.gguf)")
    p.add_argument("--name", default=None, help="서빙 model id (미지정=파일명, 호출 측 모델명과 무관하게 동작)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--gpu-layers", type=int, default=None,
                   help="GPU 적재 레이어 수 (미지정=16GB 기준 자동 오프로드 계산)")
    p.add_argument("--vram-gb", type=float, default=DEFAULT_VRAM_GB, help="자동 계산에 쓸 VRAM 용량")
    p.add_argument("--ctx", type=int, default=DEFAULT_CTX)
    p.add_argument("--threads", type=int, default=0, help="0 = 자동")
    p.add_argument("--flash-attn", action="store_true")
    p.add_argument("--idle-exit", type=int, default=DEFAULT_IDLE_EXIT,
                   help="이 초 동안 요청 없으면 프로세스 종료(VRAM 회수). 0=비활성화")
    p.add_argument("--api-key", default="", help="지정 시 Authorization Bearer 검증 (비우면 무검증)")
    return p


class Engine:
    """llama_cpp.Llama 래퍼 — 생성 직렬화 락."""

    def __init__(self, args, gpu_layers: int):
        from llama_cpp import Llama   # 지연 import
        kwargs = dict(model_path=args.model, n_ctx=args.ctx, n_gpu_layers=gpu_layers, verbose=False)
        if args.threads:
            kwargs["n_threads"] = args.threads
        if args.flash_attn:
            kwargs["flash_attn"] = True
        self.llm = Llama(**kwargs)
        self.name = args.name or default_name_for(args.model)
        self._lock = threading.Lock()

    def chat(self, body: dict):
        kwargs = dict(
            messages=body.get("messages") or [],
            temperature=float(body.get("temperature", 0.8)),
            top_p=float(body.get("top_p", 0.95)),
            max_tokens=body.get("max_tokens") or body.get("max_completion_tokens") or None,
            stop=body.get("stop") or None,
        )
        if body.get("seed") is not None:
            try:
                kwargs["seed"] = int(body["seed"])
            except (TypeError, ValueError):
                pass
        with self._lock:
            return self.llm.create_chat_completion(stream=bool(body.get("stream")), **kwargs)


def openai_chat_response(raw: dict, model_name: str) -> dict:
    """llama_cpp 응답을 OpenAI chat.completions 스키마로 재포장(model id는 우리 걸로 통일)."""
    raw = dict(raw or {})
    raw["object"] = raw.get("object") or "chat.completion"
    raw["model"] = model_name
    raw.setdefault("id", "chatcmpl-" + uuid.uuid4().hex[:24])
    raw.setdefault("created", int(time.time()))
    raw.setdefault("usage", {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
    raw.setdefault("choices", [])
    return raw


def create_app(engine, api_key: str = "", on_unload=None, touch=None):
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, StreamingResponse

    app = FastAPI(title="llm_comic_gen llm_server", version="1.1")

    def _auth(request: Request) -> bool:
        if not api_key:
            return True
        return request.headers.get("authorization", "").removeprefix("Bearer ").strip() == api_key

    @app.get("/health")
    def health():
        return {"status": "ok", "model": engine.name}

    @app.get("/v1/models")
    def models():
        return {"object": "list", "data": [{
            "id": engine.name, "aliases": [engine.name], "object": "model",
            "created": int(time.time()), "owned_by": "llm_comic_gen",
        }]}

    @app.post("/v1/unload")
    def unload():
        """파이프라인 렌더 단계 진입 신호 — 응답 후 프로세스를 종료해 VRAM를 되돌린다."""
        if on_unload:
            on_unload()
        return {"status": "bye", "model": engine.name}

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        if not _auth(request):
            return JSONResponse({"error": {"message": "invalid api key", "type": "auth"}}, status_code=401)
        if touch:
            touch()
        body = await request.json()
        if body.get("stream"):
            def sse():
                for chunk in engine.chat(body):
                    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")
                yield b"data: [DONE]\n\n"
            return StreamingResponse(sse(), media_type="text/event-stream")
        try:
            return openai_chat_response(engine.chat(body), engine.name)
        except Exception as e:   # noqa: BLE001 — HTTP 500 body로 원인 노출
            return JSONResponse({"error": {"message": str(e), "type": "server_error"}}, status_code=500)

    return app


def _quit_later(delay: float = 0.6):
    """응답이 먼저 나가도록 조금 뒤 종료 (GPU 컨텍스트 포함 프로세스째 종료 → VRAM 완전 회수)."""
    def _kill():
        print("[llm_server] unload/timeout → 종료(VRAM 반환)")
        os._exit(0)
    threading.Timer(delay, _kill).start()


def main(argv=None):
    args = build_parser().parse_args(argv)
    import uvicorn

    gpu_layers = args.gpu_layers
    if gpu_layers is None:
        gpu_layers = suggest_gpu_layers(args.model, args.vram_gb)
        if gpu_layers is None:
            gpu_layers = -1
            print("[llm_server] 레이어 수 측정 실패 — 전적재(-1) 시도. OOM이면 --gpu-layers 를 낮추세요 "
                  f"(16GB/26B-A4B Q4_K_M 권장: 30~40)")
        else:
            print(f"[llm_server] 16GB 자동 오프로드: GPU 레이어 {gpu_layers} (vram={args.vram_gb}GB, 예약 {RESERVE_GB}GB)")

    engine = Engine(args, gpu_layers)
    state = {"last": time.monotonic()}

    def touch():
        state["last"] = time.monotonic()

    if args.idle_exit > 0:
        def watchdog():
            while True:
                time.sleep(10)
                if time.monotonic() - state["last"] > args.idle_exit:
                    print(f"[llm_server] {args.idle_exit}s 무활동 → 종료(VRAM 반환)")
                    os._exit(0)
        threading.Thread(target=watchdog, daemon=True).start()

    print(f"[llm_server] model={engine.name} ctx={args.ctx} gpu_layers={gpu_layers} "
          f"@ http://{args.host}:{args.port}/v1  (unload: POST /v1/unload)")
    app = create_app(engine, args.api_key,
                     on_unload=lambda: _quit_later(0.6), touch=touch)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
