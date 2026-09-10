#!/usr/bin/env python3
"""run_comic.py — 독립 실행 만화 생성기 (llm_comic_gen)

입력  : ① 에피소드 평문 파일 ② 캐릭터 시트 평문 파일 ③ 페이지당 컷 수
출력  : comic/bookNNN/episode_NN_pageXX.png (+ episode_NN_comic.json)
동작  : 평문 →(gemma 추출)→ config 주입 → EP 태그 생성(LLM 1회) → 컷 스크립트(LLM)
        → 컷별 ComfyUI 렌더(seed 고정) → 흰 프레임/검은 선 페이지 합성
레이아웃: data/cut.yaml 페이지 템플릿(기승전결 대응). 페이지 수는 **본문 길이에서 역산**한다
        (--pages 0 = 자동 기본, N>0 = 고정, --no-cut-yaml = 자동 2열 문법).
[2026-09-08] 에피소드 전체 반영: 본문이 컷 수를 정하고(1컷≈--chars-per-panel 자) 컷 수가 페이지 수를
        정한다. 긴 본문은 추출/컷 스크립트 모두 장면(창) 단위로 나눠 LLM을 호출한다(num_ctx 제약).

  python3 run_comic.py --episode inputs/ep01.txt --sheet inputs/sheet01.txt --panels-per-page 5
  python3 run_comic.py --episode inputs/ep01.txt --sheet inputs/sheet01.txt --dry-run --preview 2
  python3 run_comic.py --episode inputs/ep01.txt --sheet inputs/sheet01.txt --start-comfy
  python3 run_comic.py --episode inputs/ep01.txt --sheet inputs/sheet01.txt --chars-per-panel 400  # 컷 늘리기

[2026-09-08] 로컬 전용 입력 포맷 (--special) — 단편 생성기 GUI의 progress/ 산출물을 그대로 받습니다:
  # 회차 한 개 (시트는 같은 디렉터리의 character_sheet_epNN_해시.json을 자동 연결)
  python3 run_comic.py --special --episode ~/progress --ep 3 --dry-run --preview 2
  # 전 회차(10화)를 comic/book001 에 담는다
  python3 run_comic.py --special --all-eps --episode ~/progress --plot-hash 2218f2f3797744fe \\
                       --book 1 --safety safe --start-llm
  어댑터: novel_progress.py (본문 평문화 + 기승전결 앵커 확보 + 시트 JSON → config 우선주입)

[2026-09-09] 화풍(LoRA) 강도는 명령줄로 조정합니다 (ANIMA_LORA_CONFIG를 편집하지 않고):
  # LoRA 두 장을 직접 지명하고 강도까지 잡는다 (강도를 안 주면 ANIMA_LORA_CONFIG 값)
  python3 run_comic.py --episode inputs/ep01.txt --sheet inputs/sheet01.txt \\
                       --lora1 lora_vassago --str1 0.8 --lora2 lora_sex --str2 0.3
  # 강도 0 = 그 슬롯 OFF (--real/--sole은 LoRA를 전부 끄는 모드)
  python3 run_comic.py ... --lora1 lora_mi1k --str2 0
  실전 로그 확인: log/anima_gen.log의 "[ComfyUI LoRA] lora_1=…(강도) … | trigger=…" 한 줄

[2026-09-09] 화면 문법(만화 규약) — 컷마다 설명/대사/속마음 중 하나를 고릅니다:
  # 설명=하단 왼쪽 흰 박스(글자 수에 맞춰 작게) / 대사=말풍선 / 속마음=속마음 풍선 / 의성어=큰 글씨 (풍선 최대 2개)
  # 풍선 자리: 주인공=왼쪽 위(2개면 아래까지) · 상대방=오른쪽 위(2개면 아래까지) (꼬리·물방울은 폐지)
  # 감정 표시: lines[].emo → 분노/놀람/땀/하트/음영/반짝/물음 이모티콘을 감정마다 다른 색으로    → --no-emo-marks
  # ★서두 요약: 각 기승전결의 첫 컷은 인물 없이 배경만 + 요약 지문(컷의 70%)   → --no-summary-cuts
  # ★에필로그 : 결 뒤에 반투명 이벤트신 2컷 + 큰 지문 1페이지가 자동 추가된다   → --no-epilogue
  python3 run_comic.py --get-fonts                    # 만화체 폰트 4종(전부 OFL)을 data/fonts/로 받습니다
  python3 run_comic.py ... --font-dialog data/fonts/Jua-Regular.ttf   # 용도별 지정도 됩니다

[local] 로컬 전용 스위치/설정 파일은 README_local.md(.gitignore 대상)에 정리해 두었습니다.
  (실행 시 로컬 설정이 읽히면 콘솔에 "local 설정 : …" 라인이 찍힙니다)

필요 환경: LLM 1대(gemma-4-26B — plot.json의 text_/main/agent/anima 전부 같은 서버를 가리킨다) + ComfyUI(8188) + Anima 워크플로.
[2026-09-07] 단일 LLM 지시: 모든 텍스트/태그/가이드/리뷰 생성을 gemma-4-26B가 담당한다.
           (rp_visual_tags 결정론 태그 풀과 태그 후처리 LLM 리뷰 단계는 폐지)
엔드포인트/모델/LoRA는 전부 plot.json에서 읽는다(하드코딩 없음). repo root에서 실행할 것(상대경로 다수).

[2026-09-08] 크로스 플랫폼: 셸 트윈(run_ollama_win.sh) 없이 **OS 분기를 이 파일이 다 가진다**.
  Linux : ./run_ollama.sh <인자...>            (= python3 run_comic.py --start-llm <인자...>)
  Windows: run_ollama_win.bat <인자...>         (= py -3 run_comic.py --start-llm <인자...>)
  --start-llm  : ollama 없으면 이 프로그램이 기동(env COMIC_OLLAMA_ENB=yes로 배선 전환, plot.json 미수정)
  --stop-llm   : 서버/모델만 내리기  --keep-llm : 연속 실행 위해 서버 유지  --llm-plan : 분기 결과만 출력
  OS는 os.name으로 자동 감지, 필요할 때만 --os win|linux 로 덮는다.
"""
import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time

from PIL import Image

import config
import runlog
import anima_gen
import comic_gen as CG
import comic_input as CI
from openAPI_control import get_openai_client_anima, unload_all_router_models, release_llm_for_gpu, \
    ollama_host, _ollama_enabled

COMFY_URL = ("localhost", 8188)

# =====================================================================
# OS 분기는 이 파일 안의 함수들에서만 일어난다 (run_ollama_win.sh 트윈이 없다).
# 감지는 런타임(os.name)이 기본, --win/--linux(--os)는 오버라이드 용도다.
# =====================================================================
IS_WIN = (os.name == "nt")
_OS_OVERRIDE = "auto"              # main()이 --os 값으로 바꾼다
_LLM_STARTED_BY_US = False         # 우리가 띄운 서버만 종료한다(사용자 기동분은 건드리지 않음)
_KEEP_LLM = False                  # --keep-llm


def is_win() -> bool:
    """Windows에서 도는지 — 경로/명령/프로세스 분기의 단일 지점."""
    o = (_OS_OVERRIDE or "auto").strip().lower()
    if o in ("win", "windows"):
        return True
    if o in ("linux", "posix", "unix"):
        return False
    return IS_WIN


def _detach_kwargs() -> dict:
    """서버를 터미널에서 떼어 기동하기 위한 OS별 Popen kwargs.

    POSIX는 start_new_session(=setsid), Windows는 그 플래그가 **조용히 무시**되므로
    creationflags로 대체한다(안 주면 콘솔 창이 뜨고 창을 ✕로 닫으면 서버가 같이 죽는다).
    """
    if is_win():
        flags = 0
        for name in ("CREATE_NEW_PROCESS_GROUP", "CREATE_NO_WINDOW", "DETACHED_PROCESS"):
            flags |= int(getattr(subprocess, name, 0) or 0)
        return {"creationflags": flags}
    return {"start_new_session": True}


def _repo_path(name: str) -> str:
    """로그/PID 파일 위치 — repo `log/`. `/tmp`도 `%TEMP%`도 아니라 어느 OS에서 같은 자리다."""
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "log")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, name)


def _comfy_dir() -> str:
    """ComfyUI 설치 경로 — **plot.json 의 `comfyuidir` 가 경로를 지시하는 유일한 지점**이다.

    우선순위: env COMFYUI_DIR > plot.json comfyuidir(실존할 때만) > OS별 폴백 probe.
    코드에 개인/기기 절대경로를 박지 않는다(배포 시 경로 정보는 plot.json 하나만 고치면 된다).
    """
    d = os.environ.get("COMFYUI_DIR", "").strip()
    if not d:
        try:
            d = str((config.get_json_value() or {}).get("comfyuidir") or "").strip()
        except Exception:
            d = ""
    if d and os.path.isdir(d):
        return d
    if is_win():
        cands = [os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "ComfyUI"),
                 r"C:\ComfyUI", os.path.expanduser("~/ComfyUI")]
    else:
        cands = [os.path.expanduser("~/AI/ComfyUI"), os.path.expanduser("~/ComfyUI")]
    for c in cands:
        if c and os.path.isdir(c):
            return c
    return cands[0]


def _comfy_python(comfy_dir: str) -> str:
    r"""ComfyUI venv의 python — Windows는 `venv\Scripts\python.exe`, POSIX는 `venv/bin/python`."""
    cands = [os.path.join(comfy_dir, "venv", "Scripts", "python.exe"),
             os.path.join(comfy_dir, "venv", "bin", "python"),
             os.path.join(comfy_dir, "venv", "bin", "python3")]
    for c in cands:
        if os.path.isfile(c):
            return c
    return ""


def _comfy_args() -> list:
    """ComfyUI 기동 인수. `--use-ck-attention`은 ROCm/Linux 계열 옵션이라 Windows에서는 넘기지 않는다."""
    extra = [a for a in os.environ.get("COMFY_EXTRA_ARGS", "").split() if a]
    return ["main.py"] + (["--use-ck-attention"] if not is_win() else []) + extra


def _comfy_env() -> dict:
    """ComfyUI 기동 환경변수(가속기 벤더별) — 값은 env로 덮어쓸 수 있다.

    MIOPEN/HIP 계열은 **Linux ROCm 전용**이라 Windows에서는 넘기지 않는다(Windows torch는
    그 변수를 읽지 않는다). Windows에서는 UTF-8만 강제한다(한글 경로/로그 사고 방지).
    """
    if is_win():
        return {"PYTHONUTF8": "1"}
    return {
        "COMFYUI_ENABLE_MIOPEN": os.environ.get("COMFYUI_ENABLE_MIOPEN", "1"),
        "MIOPEN_FIND_MODE": os.environ.get("MIOPEN_FIND_MODE", "2"),
        "MIOPEN_USER_DB_PATH": os.environ.get("MIOPEN_USER_DB_PATH",
                                              os.path.expanduser("~/.cache/miopen")),
        "PYTORCH_HIP_ALLOC_CONF": os.environ.get("PYTORCH_HIP_ALLOC_CONF", "expandable_segments:True"),
        "AITER_TRITON_ONLY": os.environ.get("AITER_TRITON_ONLY", "1"),
        "FLASH_ATTENTION_TRITON_AMD_ENABLE": os.environ.get("FLASH_ATTENTION_TRITON_AMD_ENABLE", "TRUE"),
        "HIP_VISIBLE_DEVICES": os.environ.get("HIP_VISIBLE_DEVICES", "0"),
    }


def p(msg: str = ""):
    print(msg, flush=True)


def perr(msg: str = ""):
    """콘솔 + **log/error.log** 둘 다 남긴다.

    run_comic 자기가 판단한 실패(추출 실패·엄격 게이트 중단·회차 스킵)는 예전에 stdout에만
    남았습니다. 실측: EP09는 컷 스크립트 게이트로 죽었는데 로그에는 13:39:52 → 13:40:19로
    그냥 시간이 건너뛰었고, 산출물에는 episode_09가 없어서 '면책'인지 '미실행'인지 읽을 수 없었다.
    """
    try:
        runlog.note(msg, "RUN")
    except Exception:
        pass
    p(msg)


# 실패 코드 → 사람이 읽는 사유 (산출물 옆 스킵 메모의 내용)
_RC_WHY = {1: "페이지 0장(렌더는 했는데 합성된 페이지가 없음)",
           2: "추출 실패 또는 컷 스크립트가 필수 상태를 채우지 못해 렌더 전에 중단",
           3: "프리플레이트(ollama/ComfyUI/폰트 등) 실패",
           4: "dry-run 태그 초기화 실패"}


def _skip_note(ep_num: int, ep_path: str, rc: int) -> str:
    """스킵한 회차는 산출물 디렉터리에 사유를 남긴다(파일 없는 회차는 원인 찾기가 제일 어려웠다)"""
    try:
        d = CG.comic_out_dir()
        os.makedirs(d, exist_ok=True)
        f = os.path.join(d, f"episode_{ep_num:02d}_SKIPPED.txt")
        with open(f, "w", encoding="utf-8") as fh:
            fh.write("\n".join([
                f"EP{ep_num:02d} 스킵 — rc={rc} · " + _RC_WHY.get(rc, "알 수 없는 사유"),
                "시각: " + time.strftime("%Y-%m-%d %H:%M:%S"),
                "원고: " + str(ep_path),
                "이 회차는 페이지를 만들지 못했습니다. 사유는 log/error.log(pid 포함 줄)와 "
                "log/comic_input.log·log/comic_gen.log에 있습니다.",
                "재시도: 같은 원고를 다시 돌리면 추출 체크포인트(state/extract_cache.yaml)의 빈 칸만 채웁니다.",
            ]) + "\n")
        return f
    except Exception:
        return ""


def _tcp(host, port, timeout=2.0) -> bool:
    try:
        with socket.create_connection((str(host), int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def start_comfyui(wait_seconds: int = 300) -> bool:
    """ComfyUI가 꺼져 있으면 기동하고 포트를 기다린다(경로는 _comfy_dir() = plot.json)."""
    if _tcp(*COMFY_URL, timeout=1.0):
        p("  ComfyUI 이미 실행 중")
        return True
    comfy_dir = _comfy_dir()
    comfy_py = _comfy_python(comfy_dir)
    if not comfy_py:
        p(f"  ComfyUI venv python 없음: {comfy_dir}\\venv\\Scripts|bin → 직접 실행 필요 "
          f"(plot.json comfyuidir 확인)")
        return False
    try:                                     # VRAM 확보 (텍스트 LLM이 로컬 llama면 unload)
        unload_all_router_models(log_fn=lambda m: p(f"  {m}"))
    except Exception as e:
        p(f"  unload 실패(계속 진행): {e}")
    env = os.environ.copy()
    env.update(_comfy_env())
    log_path = _repo_path("comfyui.log")
    with open(log_path, "ab") as logf:
        subprocess.Popen(_comfy_args(), cwd=comfy_dir, env=env,
                         stdout=logf, stderr=subprocess.STDOUT, **_detach_kwargs())
    p(f"  ComfyUI 기동 시도… {comfy_dir} (로그 {log_path})")
    t0 = time.time()
    while time.time() - t0 < wait_seconds:
        if _tcp(*COMFY_URL, timeout=1.0):
            p(f"  ComfyUI 포트 개방 ({time.time() - t0:.0f}s)")
            return True
        time.sleep(3)
    p(f"  ComfyUI {wait_seconds}s 안에 포트 미개방")
    return False


# =====================================================================
# ollama 서버 생명주기 — 예전은 run_ollama.sh(POSIX 전용)가 소유한 일이엇다.
# 셸 트윈(run_ollama_win.sh)을 뜨면 규칙 4개(모델명/포트/num_ctx/kill)가 복제되어
# 반드시 어긋난다 → Python으로 옮기고 OS 분기는 이 블록 안에만 둔다.
# =====================================================================
def _ollama_bin() -> str:
    """ollama 바이너리 위치: env OLLAMA_BIN > PATH > OS별 설치 위치."""
    b = os.environ.get("OLLAMA_BIN", "").strip()
    if b and os.path.isfile(b):
        return b
    w = shutil.which("ollama") or shutil.which("ollama.exe")
    if w:
        return w
    cands = ([os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Ollama", "ollama.exe"),
              r"C:\Program Files\Ollama\ollama.exe", os.path.expanduser("~/ollama/ollama.exe")] if is_win()
             else [os.path.expanduser("~/AI/ollama/bin/ollama"), "/usr/local/bin/ollama"])
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return ""


def _ollama_host_port():
    """plot.json/env(`COMIC_OLLAMA_HOST`)가 지시하는 ollama 주소 → (host, port) 문자열."""
    hp = ollama_host().split("://", 1)[-1].rstrip("/")
    host, _, port = hp.partition(":")
    return (host or "127.0.0.1"), (port or "11434")


def _proc_alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def start_ollama(wait_seconds: int = 240) -> bool:
    """ollama 서버가 없으면 이 프로그램이 띄운다 (--start-llm). 이미 뜨면 아무것도 안 한다.

    하는 일: 바이너리 탐색 → 전용 HOST/OLLAMA_MODELS(포터블) 지정 → detached 기동 →
    포트 대기 → `log/ollama_serve.pid`에 PID 기록(종료 시 우리가 띄운 것만 내린다).
    plot.json은 **건드리지 않는다**(env COMIC_OLLAMA_ENB로 배선 전환 → 크래시해도 잔류 없음).
    """
    global _LLM_STARTED_BY_US
    host, port = _ollama_host_port()
    if _tcp(host, port, 1.0):
        # [2026-09-08] 직전 실행이 내리는 중(killpg 유예 구간)에 포트가 열려 있으면 "이미 실행 중"으로
        #   건너뛰고 → 추출에서 Connection refused(2026-09-08 16:22 재현). 연속 두 번 열려야 진짜다.
        alive = True
        for _ in range(2):
            time.sleep(1.0)
            if not _tcp(host, port, 1.0):
                alive = False
                break
        if alive:
            p(f"  ollama 이미 실행 중 ({host}:{port})")
            return True
        p(f"  ollama 포트가 깜빡인다(직전 실행이 반납 중) — 새로 기동합니다 ({host}:{port})")
        t_close = time.time()                     # 반납이 끝나기 전에 spawn하면 'address already in use'로 죽는다
        while time.time() - t_close < 30 and _tcp(host, port, 0.5):
            time.sleep(1.0)
    b = _ollama_bin()
    if not b:
        p("  ollama 바이너리 없음 → OLLAMA_BIN=<ollama 바이너리 경로> 지정 또는 공식 릴리즈 설치 후 재실행")
        return False
    env = os.environ.copy()
    env["OLLAMA_HOST"] = f"{host}:{port}"
    env.setdefault("OLLAMA_MAX_LOADED_MODELS", "1")      # 모델/컨텍스트 중복 적재 금지
    env.setdefault("OLLAMA_NUM_PARALLEL", "1")
    env.setdefault("PYTHONUTF8", "1")
    models_dir = (os.environ.get("OLLAMA_MODELS_DIR") or
                  str((config.get_json_value() or {}).get("ollama_models_dir") or "")).strip()
    if models_dir:                                       # 포터블: 앱 폴더 안으로(C:\Users 오염 방지)
        os.makedirs(models_dir, exist_ok=True)
        env["OLLAMA_MODELS"] = os.path.abspath(models_dir)
    log_path = _repo_path("ollama_serve.log")
    with open(log_path, "ab") as logf:
        proc = subprocess.Popen([b, "serve"], env=env, stdout=logf, stderr=subprocess.STDOUT,
                                **_detach_kwargs())
    with open(_repo_path("ollama_serve.pid"), "w", encoding="utf-8") as f:
        f.write(str(proc.pid))
    _LLM_STARTED_BY_US = True
    p(f"  ollama 기동 시도… {b} ({host}:{port}, 로그 {log_path})")
    t0 = time.time()
    while time.time() - t0 < wait_seconds:
        if _tcp(host, port, 1.0):
            p(f"  ollama 포트 개방 ({time.time() - t0:.0f}s)")
            return True
        if not _proc_alive(proc.pid):
            p(f"  ollama 프로세스가 바로 죽었다 — {log_path} 확인")
            return False
        time.sleep(2)
    p(f"  ollama {wait_seconds}s 안에 포트 미개방 — {log_path} 확인")
    return False


def stop_ollama() -> bool:
    """우리가 띄운 ollama 서버(와 자식 llama-server)를 내린다.

    Windows는 `taskkill /T`로 프로세스 트리까지 잡는다(서버만 죽이면 llama-server가 16GB를
    물고 남은 채로 남는다 — Linux에서 pkill -x llama-server를 따로 때리는 것과 같은 이유).
    """
    global _LLM_STARTED_BY_US
    pidf = _repo_path("ollama_serve.pid")
    try:
        pid = int((open(pidf, encoding="utf-8").read() or "0").strip())
    except Exception:
        pid = 0
    if not pid:
        return False
    ok = False
    try:
        if is_win():
            r = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, text=True, timeout=60)
            ok = (r.returncode == 0)
        else:
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except ProcessLookupError:
                ok = True
            for _ in range(20):
                time.sleep(0.5)
                if not _proc_alive(pid):
                    break
            else:
                try:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                except Exception:
                    pass
            ok = True
        p(f"  ollama 서버 종료(PID {pid}) {'완료' if ok else '실패 — 수동 종료 필요'}")
    except Exception as e:
        p(f"  ollama 종료 실패({e}) — 수동 종료 필요")
    try:
        os.remove(pidf)
    except OSError:
        pass
    _LLM_STARTED_BY_US = False
    return ok


def preflight(need_llm: bool, need_comfy: bool, need_pages: bool = True) -> list:
    """사전 점검 → 문제 목록 (있으면 실행 중지)

    점검 순서에 이유가 있다: LLM 백엔드 분기(ollama / 전용 서버)가途中で `return`하기 때문에
    ComfyUI·폰트 점검을 그 **앞에** 둔다. 안 그러면 ollama 경로에서 두 항목이 조용히 건너뛴다.
    """
    problems = []
    p("\n== 프리플라이트 ==")
    pj = config.get_json_value()
    miss = [k for k in ("name", "sex", "hair_color", "hair_style", "eye_color", "skin_color",
                        "face_style", "clothes", "body_shape", "job")
            if not str(getattr(config, k, "") or "").strip()]
    p(f"  config 필수 10필드 : {'OK' if not miss else '누락 ' + str(miss)}")
    if miss:
        problems.append(f"필수 필드 누락 {miss}")
    # 한글 폰트: 페이지 합성(compose)이 일어나면 반드시 쓰인다. OS별로 후보가 다르며
    # 하나도 없어도 실행은 되지만 캡션/대사가 전부 □(tofu)로 찍힌다 → 경고만 남긴다(중단하지 않는다).
    if need_pages:
        CG._apply_font_roles_from_config()
        fp = str(getattr(config, "comic_font", "") or "").strip()
        font_ok = bool(fp and os.path.isfile(fp)) or CG.CPM.has_cjk_font()
        p(f"  한글 폰트   : {'OK' if font_ok else '없음 → 캡션/대사가 □로 찍힌다 (--font <ttf/ttc> 또는 data/fonts/NotoSansKR-Bold.ttf)'}"
          + (f" ({fp})" if fp else ""))
        _miss = CG.CPM.missing_font_roles()
        p(f"  화면 문법 폰트: 설명/대사/속마음/의성어 중 미설치 "
          f"{('없음(전부 OS 폰트로 렌더 — ' + ', '.join(_miss) + ')') if _miss else '없음(4종 모두 data/fonts)'}"
          + (" · scripts/get_fonts.sh 한 번 돌려주시면 만화체가 들어갑니다" if _miss else ""))
        _pn, _pn2 = str(getattr(config, "pin_name", "") or "").strip(), str(getattr(config, "pin_name2", "") or "").strip()
        p(f"  이름 고정    : 주인공 '{_pn or '(추출이 정한 이름)'}' · 상대방 '{_pn2 or '(추출이 정한 이름)'}'"
          + ("  (--name/--name2 · local_settings name/partner_name · COMIC_PIN_NAME)"
             if (_pn or _pn2) else "  — 시트의 #캐릭터 태그#에서 이름이 새어들면 --name으로 고정하세요"))
        p(f"  ★화면 문법 : 서두 요약 컷 {'ON' if getattr(config, 'comic_summary_cuts', True) else 'off'} · "
          f"에필로그 컷 {'ON' if getattr(config, 'comic_epilogue', True) else 'off'} · "
          f"감정 표시 {'ON' if getattr(config, 'comic_emo_marks', True) else 'off'} · "
          f"★프롤로그 {'ON(첫 회차에만)' if getattr(config, 'comic_prologue_cut', True) else 'off'} · "
          f"★에필로그는 마지막 회차(전 {getattr(config, 'total_episodes', 1)}회)에만 붙습니다")

    if need_comfy:
        ok = _tcp(*COMFY_URL)
        dirs = anima_gen._comfyui_output_dirs(pj)
        p(f"  ComfyUI  {COMFY_URL[0]}:{COMFY_URL[1]} : {'연결됨' if ok else '연결 실패'}")
        p(f"  ComfyUI 출력 후보: {dirs}")
        if not ok:
            problems.append(f"ComfyUI({COMFY_URL[0]}:{COMFY_URL[1]}) 접속 불가 → --start-comfy 사용")
        if not dirs:
            problems.append("ComfyUI 출력 디렉토리 없음(plot.json comfyuidir / 설치 경로 확인)")

    # [2026-09-07] ollama 백엔드(plot ollama_enb=yes 또는 env COMIC_OLLAMA_ENB=yes)면
    #   8081이 아니라 ollama_host를 점검한다 (env 오버라이드는 --start-llm이 쓴다)
    ollama_on = _ollama_enabled(pj)
    if ollama_on:
        ohost, oport = _ollama_host_port()
        ok = _tcp(ohost or "127.0.0.1", oport or "11434")
        p(f"  ollama {'(env COMIC_OLLAMA_ENB=yes)' if os.environ.get('COMIC_OLLAMA_ENB') else '(plot ollama_enb=yes)'} "
          f"{ohost}:{oport or '11434'} : {'연결됨' if ok else '연결 실패'}")
        if need_llm and not ok:
            problems.append(f"ollama({ohost}:{oport or '11434'}) 접속 불가 → --start-llm 사용(자동 기동) 또는 ollama serve")
        return problems
    # [2026-09-07] 단일 LLM(gemma-4-26B) — text/main/agent/anima가 같은 서버면 한 번만 점검한다
    labels = (("textLLM", pj.get("text_ip") or pj.get("ip_main"), pj.get("text_port") or pj.get("port_main")),
              ("mainLLM", pj.get("ip_main"), pj.get("port_main")),
              ("agent", pj.get("ip_agent"), pj.get("port_agent")),
              ("animaLLM", pj.get("ip_anima"), pj.get("port_anima")))
    groups = {}
    for label, host, port in labels:
        groups.setdefault((str(host), str(port)), []).append(label)
    for (host, port), names in groups.items():
        ok = _tcp(host, port)
        p(f"  LLM {'/'.join(names):24s} {host}:{port} : {'연결됨' if ok else '연결 실패'}")
        if need_llm and not ok:
            problems.append(f"{'+'.join(names)}({host}:{port}) 접속 불가")
            p("    → 전용 서버로 띄우기: python llm_server.py --model <model.gguf> --port "
              f"{port}  (pip install llama-cpp-python fastapi uvicorn, README '전용 LLM 서빙')")
    return problems


def make_thumbs(paths, divisor=4):
    """결과 확인용 1/divisor 크기 jpg (모델 없이 눈으로 보려면 이 파일부터)"""
    out = []
    for f in paths:
        try:
            im = Image.open(f)
            w, h = im.size
            t = f.rsplit(".", 1)[0] + "_thumb.jpg"
            im.convert("RGB").resize((max(1, w // divisor), max(1, h // divisor))).save(t, quality=75)
            out.append(t)
        except Exception as e:
            p(f"  썸네일 실패 {f}: {e}")
    return out


def _run_episode(args, ep_num: int, total_eps: int, ep_path: str, sheet_path: str, t0: float) -> int:
    """한 회차를 끝까지 돈다 (추출 → config 주입 → 태그 생성 → 컷 스크립트 → 렌더 → 페이지 합성)

    --special이면 ep_path/sheet_path가 progress/ 원본 파일이어도 됩니다(CI.load_inputs가 평문화).
    반환 코드: 0=성공 / 2=추출 실패 / 3=프리플라이트 / 4=dry-run 태그 실패 / 1=페이지 0장
    """
    # 1) 평문 입력 → (2) LLM 추출 → (3) config 주입
    inp = CI.load_inputs(ep_path, sheet_path, special=(True if args.special else None))
    ep_text, sheet_text = inp["episode_text"], inp["sheet_text"]
    if inp["format"] != "plain" and inp["ep_num"] > 1 and ep_num <= 1:
        ep_num = inp["ep_num"]                          # 파일명의 회차 번호 존중 (--ep 2처럼 명시하면 이긴다)
    # 회차 시작 전에 book 번호를 박는다(이 회차가 죽어도 스킵 메모가 올바른 book에 떨어지도록)
    config.comic_book_num = max(0, int(args.book or 0))
    _xkey = CI.extract_key(ep_text, sheet_text, ep_num=ep_num, mode=inp["format"])
    _xsrc = CI._src_note(ep_path, sheet_path)          # 체크포인트에 어느 파일이었는지 적어 둔다
    _prev_fail = (CI.load_extract_record(_xkey) or {}).get("failed") or []
    if _prev_fail and not getattr(args, "fresh_extract", False):
        p(f"  ○ 지난 실행에서 이 원고의 추출이 실패했습니다: {_prev_fail[-1][:90]}")
    data = CI.extract(ep_text, sheet_text, ep_num=ep_num,
                      need_segments=not inp["segments"])   # 막 앵커를 파서가 확보했으면 LLM에게 시키지 않는다
    if not data and args.start_llm:
        host, port = _ollama_host_port()     # 기동 확인 후 죽은 경우(직전 실행 반납 경합) 한 번만 재시도
        if not _tcp(host, port, 1.0):
            p("  추출 중 ollama 사망 — 재기동 후 추출 1회 재시도")
            if start_ollama():
                data = CI.extract(ep_text, sheet_text, ep_num=ep_num,
                                  need_segments=not inp["segments"])
    if not data:
        perr("✗ 필드 추출 실패 — LLM(textLLM) 상태와 plot.json을 확인하세요. 상세: log/comic_input.log")
        # [2026-09-10] 예전은 여기서 그냥 나가 체크포인트가 없었습니다(재실행이 0부터 시작).
        CI.save_extract_failure(_xkey, "추출 JSON 파싱 실패(2회 시도) — 항목 원고를 더 잘게 나눠 주세요",
                                source=_xsrc)
        return 2
    # [2026-09-10] 초기 추출이 반쯤 깨졌을 때 회차 전체를 다시 물어먹지 않는다 —
    #   ① 같은 원고의 지난 체크포인트에서 빈 칸만 이어받고 ② 빈 항목만 다시 묻고
    #   ③ 그래도 모자라면 캐릭터 설정(공식 태그·직업)으로 추론해 메운다(로그에 '추론' 명시).
    _cached = {} if getattr(args, "fresh_extract", False) else CI.load_extract_checkpoint(_xkey)
    if _cached:
        data, _cf = CI.merge_extract_cached(data, _cached)
        if _cf:
            p(f"  ○ 지난 실행의 추출 체크포인트에서 {len(_cf)}항목을 이어받았습니다 "
              f"(빈 칸만): {', '.join(_cf[:6])}" + (" 외" if len(_cf) > 6 else ""))
    _miss = CI.missing_extract_fields(data)
    if _miss:
        p(f"  추출에서 빈 핵심 항목 {len(_miss)}개 → 그 항목만 다시 묻습니다: {', '.join(_miss[:6])}"
          + (" 외" if len(_miss) > 6 else ""))
        data, _miss = CI.fill_missing_extract(data, _miss, ep_text, sheet_text, ep_num=ep_num)
    if _miss:
        data, _inf = CI.infer_missing_from_profile(data, _miss)
        if _inf:
            p("  ○ 캐릭터 설정(공식 태그·직업)으로 추론해 메운 항목: " + ", ".join(_inf) + " — 본문 근거 아님")
        _miss = CI.missing_extract_fields(data)
    if _miss:
        p(f"  ✗ 여전히 빈 핵심 항목 {len(_miss)}개: {', '.join(_miss)}")
        p(f"     체크포인트를 남깁니다({CI.EXTRACT_CACHE}) — 같은 원고를 다시 돌리면 이 항목만 채웁니다."
          + (f" (파일: {_xsrc.get('episode')})" if _xsrc.get("episode") else ""))
    CI.save_extract_checkpoint(_xkey, data, _miss, source=_xsrc)
    CI.apply_to_config(data, ep_text, sheet_text, ep_num=ep_num,
                       panels_per_page=args.panels_per_page, book_num=args.book,
                       total_episodes=total_eps, overrides=inp["overrides"],
                       segments=inp["segments"], safety=args.safety)
    if args.no_wide:
        CG.WIDE_ENABLE = False
    # 페이지 수: -1=cut.yaml OFF / 0=본문 길이 자동 / N>0=고정 (예전 min(4) 클램프는 본문 잘림의 원인)
    config.comic_pages = -1 if args.no_cut_yaml else max(0, int(args.pages or 0))
    config.comic_max_pages = max(1, int(args.max_pages or 0)) if args.max_pages else \
        max(1, int(getattr(config, "comic_max_pages", 24)))
    if args.chars_per_panel:
        config.comic_chars_per_panel = max(80, int(args.chars_per_panel))
    if args.max_panels:
        config.comic_max_panels = max(1, int(args.max_panels))
    if args.beat_chars:
        config.comic_beat_chars = max(300, int(args.beat_chars))
    if args.angle:
        config.angle_llm_cli = True
    tgt = CI.target_panels(ep_text, config.comic_chars_per_panel, CG.MIN_PANELS,
                           config.comic_max_panels)
    page_mode = (f"auto≤{config.comic_max_pages}" if config.comic_pages == 0
                 else ("cut.yaml OFF" if config.comic_pages < 0 else str(config.comic_pages)))
    _u0 = ((getattr(config, "ep_action_units", {}) or {}).get(ep_num)
           or (getattr(config, "ep_action_units", {}) or {}).get(str(ep_num)) or [])
    if _u0:                                   # 저울은 항목 수 — 글자 수 예산을 여기서 고친다
        tgt = max(tgt, sum(int(u.get("cuts") or 1) for u in _u0))
    p(f"\n본문 {len(ep_text)}자 → 만들 컷 {tgt} ({('본문 항목 ' + str(len(_u0)) + '개(항목 1 = 컷 1)') if _u0 else ('1컷=' + str(config.comic_chars_per_panel) + '자')}, "
      f"상한 {config.comic_max_panels or '무제한'}, 페이지 {page_mode})")

    idx = max(0, ep_num - 1)
    jv = config.get_json_value()
    client = None
    try:
        client = get_openai_client_anima(log_fn=CG._clog)
    except Exception as e:
        p(f"  anima 클라이언트 생성 실패: {e}")

    if args.start_comfy and not args.dry_run:
        p("\n== ComfyUI 기동 ==")
        start_comfyui()
    problems = preflight(need_llm=True, need_comfy=not args.dry_run, need_pages=not args.dry_run)
    for pr in problems:
        p(f"  ! {pr}")
    if problems:
        return 3

    # 4) dry-run이면 태그 초기화 + 컷/프롬프트 확인 후 종료
    if args.dry_run:
        init = anima_gen.init_anima_tags(idx, client, jv)
        p(f"\n  init_anima_tags: {init.get('status') if isinstance(init, dict) else init}")
        if isinstance(init, dict) and init.get("status") != "ok":
            p(f"  ✗ 태그 초기화 실패: {str(init)[:200]}")
            return 4
        try:
            script = CG.request_panel_script(ep_num, max(1, int(config.total_episodes)), client=client)
        except CG.PanelScriptError as e:
            perr(f"[오류] {e}")
            p("  컷 스크립트가 화면에 필요한 상태를 채우지 못해 여기서 멈춥니다. 원고는 그대로 두고, ") 
            p("  --no-strict-state로 경고만 켜고 진행하거나 장면을 조금 더 잘게 나눠 주세요.")
            return 2
        panels = script.get("panels", [])
        plans = script.get("page_plans") or []
        if plans:
            p("  cut.yaml 레이아웃: " + " | ".join(
                f"P{pl['page']} {pl['template_id']}[{pl['situation']}] {len(pl['slots'])}컷" for pl in plans))
        p(f"  본문 {len(ep_text)}자 · 목표 {script.get('target_panels')}컷 · "
          f"장면 {script.get('beats')}개(LLM 호출 그 만큼) · 레이아웃 {len(plans)}페이지")
        p(f"  컷 {len(panels)}개 (face {sum(1 for x in panels if x['type']=='face')} / "
          f"action {sum(1 for x in panels if x['type']=='action')}, "
          f"wide {sum(1 for x in panels if x.get('wide'))})  base_seed={CG._base_seed(ep_num, panels) if panels else '-'}")
        for n in script.get("notes", []):
            p(f"    보정: {n}")
        for pnl in panels:
            pg = f"p{pnl['page']}t{pnl['tier']} {int(round(float(pnl.get('share', 1)) * 100)):3d}% " if "page" in pnl else ""
            role = {"summary": " ★서두요약(배경만)", "epilogue": " ★에필로그(반투명)"}.get(
                str(pnl.get("text_role") or ""), "")
            if role.startswith(" ★서두요약") and pnl.get("prologue"):
                role = " ★프롤로그(배경만)"
            tp = CG.panel_text_payload(pnl)
            mode = ("설명+대사(이벤트)" if tp["narration"] and tp["balloons"] else
                    "설명만" if tp["narration"] else "대사/속마음만" if tp["balloons"] else "텍스트 없음")
            p(f"    [{pnl['no']:02d}] {pg}{pnl['type']:6s} cam={pnl['camera']:10s} "
              f"facing={pnl.get('facing', 'front'):5s} "
              f"res={'wide' if pnl.get('wide') else 'tall':4s} | [{mode}]{role}")
            if tp["narration"]:
                p(f"         설명{'(컷 70%)' if tp['narr_large'] else ''}: {tp['narration']}")
            for b in tp["balloons"]:
                spot = {"me": "주인공·왼쪽", "other": "상대방·오른쪽"}.get(b.get("speaker") or "", "")
                p(f"         {'속마음풍선' if b['kind'] == 'thought' else '말풍선'}"
                  f"{('[' + spot + ']') if spot else ''}"
                  f"{('[감정:' + b['emo'] + ']') if b.get('emo') else ''}: {b['text']}")
            if tp["sfx"]:
                p(f"         의성어: {tp['sfx']}")
        if args.preview:
            gloss = CG.request_ko_glossary(CG._ko_fragments(idx))
            safety = ""
            try:
                safety = config.review_safety[idx] or ""
            except Exception:
                pass
            for pnl in panels[:args.preview]:
                p(f"\n  --- 컷{pnl['no']} 프롬프트 ---")
                p(CG.build_panel_prompt(idx, pnl, safety, gloss=gloss,
                                        angle_preset=CG._pick_panel_angle(pnl)))
        out_dir = CG.comic_out_dir()
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, f"episode_{ep_num:02d}_script.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"ep": ep_num, "panels": panels, "notes": script.get("notes", []),
                       "beats": script.get("beats"), "target_panels": script.get("target_panels"),
                       "page_plans": script.get("page_plans")},
                      f, ensure_ascii=False, indent=2)
        p(f"  스크립트 저장: {os.path.join(out_dir, f'episode_{ep_num:02d}_script.json')}")
        p(f"\n===== 결과 ({time.time() - t0:.1f}s) — 렌더 없음 =====")
        return 0

    # 5) 본 실행: 태그 초기화 + 컷 생성 + 렌더 + 페이지 합성 (comic_gen_episode가 전 과정 담당)
    try:
        meta = CG.comic_gen_episode(idx, client=client, json_value=jv, do_render=True)
    except CG.PanelScriptError as e:
        perr(f"[오류] {e}")
        p("  컷 스크립트가 화면에 필요한 상태를 채우지 못해 여기서 멈춥니다(렌더는 시작되지 않았습니다).")
        p("  --no-strict-state로 경고만 켜고 진행하거나, 장면을 조금 더 잘게 나눠 주세요.")
        return 2
    pages = meta.get("pages", [])
    if not args.no_thumb:
        make_thumbs(pages)
    p("\n===== 결과 =====")
    p(f"  회차      : EP{meta.get('ep')}  컷 {len(meta.get('files', []))}장 / 페이지 {len(pages)}장")
    p(f"  base_seed : {meta.get('base_seed')}  seeds: {meta.get('seeds')}")
    for pg in pages:
        p(f"  페이지    : {pg}")
    for n in meta.get("notes", []):
        p(f"  보정      : {n}")
    p(f"  소요      : {time.time() - t0:.1f}s")
    return 0 if pages else 1
    return 0 if pages else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="평문 에피소드 + 평문 캐릭터 시트 → 만화 페이지")
    ap.add_argument("--episode", default="", help="에피소드 평문 파일(.md도 허용)")
    ap.add_argument("--sheet", default="", help="캐릭터 시트 평문 파일(비우면 본문에서만 추출)")
    # [2026-09-08] local 전용 포맷(--special): 코어의 입력 계약(두 평문)은 그대로 두고 경계에서만 번역한다
    ap.add_argument("--special", action="store_true",
                    help="[local 전용] llm_shortnovel_generator_gui의 progress/ 포맷으로 받는다 "
                         "(epNN_해시.txt + character_sheet_epNN_해시.json). --episode에 파일이나 디렉터리 가능")
    ap.add_argument("--plot-hash", default="",
                    help="--special로 디렉터리를 탈 때 작품 해시 필터(예: 2218f2f3797744fe)")
    ap.add_argument("--all-eps", action="store_true",
                    help="--special 디렉터리 디스커버리로 발견된 전 회차를 같은 --book에 연속 생성")
    ap.add_argument("--safety", default="", choices=("", "safe", "sensitive", "nsfw", "explicit"),
                    help="수위 강제(비우면 LLM 판단). 예: --safety safe")
    ap.add_argument("--panels-per-page", type=int, default=5,
                    help="한 페이지 컷 수 (cut.yaml 레이아웃 off일 때만 적용)")
    ap.add_argument("--pages", type=int, default=0,
                    help="cut.yaml 페이지 수 (기본 0=본문 길이로 자동 산출, N>0=N페이지 고정)")
    ap.add_argument("--max-pages", type=int, default=0, help="자동 모드 페이지 상한 (기본 24)")
    ap.add_argument("--chars-per-panel", type=int, default=0,
                    help="본문 몇 자로 컷 1컷으로 볼지 (기본 600 — 작을수록 컷/페이지가 는다)")
    ap.add_argument("--max-panels", type=int, default=0, help="컷 상한 (기본 0=무제한)")
    ap.add_argument("--beat-chars", type=int, default=0,
                    help="장면(컷 스크립트 LLM 1호출) 본문 상한 (기본 1800)")
    ap.add_argument("--no-cut-yaml", action="store_true",
                    help="cut.yaml 레이아웃 끔 (자동 2열/wide 문법으로 회귀)")
    ap.add_argument("--ep", type=int, default=1, help="에피소드 번호(1기준)")
    ap.add_argument("--total-episodes", type=int, default=1, help="전체 회차 수(기본 1)")
    ap.add_argument("--book", type=int, default=1, help="out comic/bookNNN 번호 (기본 01)")
    ap.add_argument("--dry-run", action="store_true", help="컷 스크립트까지 (렌더 없음)")
    ap.add_argument("--preview", type=int, default=0, help="컷 프롬프트 N개 출력(dry-run과 함께)")
    ap.add_argument("--no-wide", action="store_true", help="wide(1366x1024) 컷 금지")
    ap.add_argument("--font", default="", help="한글 폰트 ttf/ttc 경로 (비우면 OS별 자동probe; 예: C:\\Windows\\Fonts\\malgunbd.ttf)")
    # [2026-09-09] 화면 문법(설명/대사/속마음/의성어) 용도별 폰트 — 우선순위: 여기 > data/fonts/ > OS
    ap.add_argument("--font-narration", dest="font_narration", default="",
                    help="설명(지문) 폰트 경로 — 비우면 data/fonts/(scripts/get_fonts.sh) → OS 순")
    ap.add_argument("--font-dialog", dest="font_dialog", default="", help="대사(말풍선) 폰트 경로")
    ap.add_argument("--font-thought", dest="font_thought", default="", help="속마음 풍선 폰트 경로")
    ap.add_argument("--font-sfx", dest="font_sfx", default="", help="의성어/의태어 폰트 경로")
    ap.add_argument("--no-epilogue", action="store_true", dest="no_epilogue",
                    help="★에필로그(마지막 회차 끝의 반투명 이벤트신 1칸 + 큰 여운 지문)를 붙이지 않는다")
    ap.add_argument("--no-summary-cuts", action="store_true", dest="no_summary_cuts",
                    help="★회차 도입 요약 컷(각 회차의 첫 컷 = 배경만 + 큰 지문)을 끈다")
    ap.add_argument("--wide-share", type=float, default=0.5, dest="wide_share",
                    help="전폭(가로 넓이) 컷 비중 상한 (기본 0.5 = 컷의 절반까지, 1.0 = 제한 없음)")
    ap.add_argument("--template", default="", dest="template",
                    help="페이지 템플릿을 고정합니다 (id 또는 이름 일부, 쉼표로 여러 개 → 페이지마다 회전). 예: --template romcom_banter_6panels")
    ap.add_argument("--list-templates", action="store_true",
                    help="사용 가능한 페이지 템플릿(id / 이름 / 페이지당 컷 수 / 상황)을 보이고 끝냅니다")
    ap.add_argument("--fresh-extract", action="store_true", dest="fresh_extract",
                    help="이전 실행의 추출 체크포인트(state/extract_cache.yaml)를 무시하고 처음부터 추출한다")
    ap.add_argument("--balloon-style", choices=("vector", "image"), dest="balloon_style",
                    default=None, help="말풍선·속마음 그림 방식 (기본: config.comic_balloon_style='vector'; "
                                        "image = data/balloons/ 자산 9슬라이스 합성, 없으면 벡터 폴백)")
    ap.add_argument("--get-balloons", action="store_true", dest="get_balloons",
                    help="말풍선·속마음 자리표시 자산 9종과 manifest.json을 만들고 끝난다")
    ap.add_argument("--force-balloons", action="store_true", dest="force_balloons",
                    help="--get-balloons가 기존 자산도 다시 그린다")
    ap.add_argument("--keep-logs", action="store_true", dest="keep_logs",
                    help="log/*.log를 실행 시작에 초기화하지 않고 이어서 쓴다 (기본: 초기화)")
    ap.add_argument("--no-strict-state", action="store_true",
                    help="컷 스크립트 JSON이 필수 항목(상태 시트 12종·pose·첫 컷의 시작 상태)을 못 채울 때 기본은 에러로 종료합니다 — 이 플래그는 경고만 하고 진행")
    ap.add_argument("--item-cuts", action="store_true", dest="item_cuts", default=None,
                    help="본문을 시간 순 '행동/대사/속마음' 항목으로 나눠 항목 하나를 컷 하나로 씁니다(기본 켬)")
    ap.add_argument("--no-item-cuts", action="store_false", dest="item_cuts",
                    help="예전처럼 사건 단위로 배분합니다(한 사건에 컷 1~2개를 LLM이 고름)")
    ap.add_argument("--variation", type=int, default=0,
                    help="컷 배분에 변동을 섞는다 (0=완전 재현, N>0=그 값마다 다른 레이아웃·장면당 컷 수)")
    ap.add_argument("--vary", action="store_true",
                    help="변동 값을 이번 실행에서 뽑고 로그에 남긴다(마음에 들면 그 값으로 재실행)")
    ap.add_argument("--face-crop", action="store_true", dest="face_crop",
                    help="컷에 넣을 때 얼굴 위치로 자릅니다(기본 켬). OpenCV가 없어도 '위에서 8%%' 추정치로 동작합니다")
    ap.add_argument("--no-face-crop", action="store_false", dest="face_crop", default=True,
                    help="얼굴 중심 크롭을 끄고 세로 가운데로 자릅니다(옛 동작)")
    ap.add_argument("--get-face-model", action="store_true",
                    help="얼굴 검출 모델(YuNet ONNX 227KB)을 받아 둡니다 — OpenCV가 있을 때만 쓰입니다")
    ap.add_argument("--chatty", action="store_true",
                    help="수다장이 모드: **모든 컷** 아래에 설명(지문)을 붙인다 — 서술할 내용이 없으면 그녀의 행동·표정을 짧게 묘사한다")
    ap.add_argument("--name", default="", help="주인공 이름을 고정한다 (추출 LLM이 시트의 #캐릭터 태그#에서 이름을 주워오는 것을 막는다)")
    ap.add_argument("--name2", default="", help="상대방 이름을 고정한다")
    ap.add_argument("--no-action-cuts", action="store_true",
                    help="컷 배분을 본문 '글자 수'로 되돌린다 (기본: LLM이 나눈 사건(액션) 단위로 배분)")
    ap.add_argument("--strong-cut-weight", type=int, default=0,
                    help="사건 유닛이 컷 수를 안 줬을 때 강한 사건으로 보는 컷 수 (기본 2)")
    ap.add_argument("--no-prologue", action="store_true", dest="no_prologue",
                    help="★프롤로그(회차집 첫 회차 맨 앞의 도입 1컷 = 배경만 + 큰 지문)를 붙이지 않는다")
    ap.add_argument("--no-emo-marks", action="store_true", dest="no_emo_marks",
                    help="감정 이모티콘(분노/놀람/땀/하트/음영/반짝/물음) 표시를 끄는다")
    ap.add_argument("--get-fonts", dest="get_fonts", action="store_true",
                    help="만화 화면 문법 폰트(설명/대사/속마음/의성어, 전부 OFL)를 data/fonts/로 받고 종료")
    ap.add_argument("--angle", action="store_true", help="action 컷에 angle.txt 구도 적용")
    ap.add_argument("--start-comfy", action="store_true", help="ComfyUI 꺼져 있으면 기동")
    ap.add_argument("--start-llm", action="store_true",
                    help="ollama 서버가 없으면 이 프로그램이 기동(구 run_ollama.sh의 일)")
    ap.add_argument("--keep-llm", action="store_true",
                    help="--start-llm으로 띄운 ollama 서버를 종료하지 않음(연속 실행용)")
    ap.add_argument("--stop-llm", action="store_true", help="ollama 모델/서버를 내리고 종료(실행 안 함)")
    ap.add_argument("--os", dest="os_override", choices=("auto", "win", "linux"), default="auto",
                    help="OS 자동 감지(os.name) 오버라이드 — 기본 auto")
    ap.add_argument("--llm-plan", action="store_true",
                    help="OS 분기 결과(바이너리/경로/기동 명령/env)만 출력하고 종료")
    ap.add_argument("--no-thumb", action="store_true", help="썸네일 미생성")
    # [2026-09-09] 화풍/LoRA 스위치 — config.real_cli / sole_cli / lora*_cli에 주입된다
    # 우선순위: real > sole > CLI LoRA > plot.json(anima_style/anima_lora) (anima_gen.resolve_anima_lora)
    ap.add_argument("--real", action="store_true",
                    help="모든 LoRA OFF + 리얼 메인 모델(ANIMA_REAL_UNET_POOL에서 랜덤 1개)")
    ap.add_argument("--sole", action="store_true",
                    help="LoRA 없이 SOLE 메인 모델(ANIMA_SOLE_UNET_POOL에서 랜덤 1개) — --real이 있으면 무시")
    ap.add_argument("--lora1", default="", metavar="KEY",
                    help="ANIMA_LORA_CONFIG 키 (예: lora_mi1k) — plot.json anima_style보다 우선")
    ap.add_argument("--lora2", default="", metavar="KEY",
                    help="ANIMA_LORA_CONFIG 키 — 보조 슬롯 (예: lora_sex)")
    ap.add_argument("--lora-chg", default="", choices=("", "episode"), dest="lora_chg",
                    help="episode: 에피소드가 바뀔 때마다 단독 LoRA 쌍을 랜덤 재선택 (increment는 폐지 — lora2 강도가 1.0을 넘습니다)")
    # [2026-09-09] LoRA 강도 오버라이드 — --lora1/--lora2는 물론 plot.json anima_style/lora_random/--lora-chg에도 걸린다
    ap.add_argument("--str1", type=float, default=None, metavar="0.0~2.0", dest="str1",
                    help="lora1 강도 오버라이드 (미지정 시 ANIMA_LORA_CONFIG의 값) — 0이면 그 슬롯 OFF")
    ap.add_argument("--str2", type=float, default=None, metavar="0.0~2.0", dest="str2",
                    help="lora2 강도 오버라이드 (예: --lora2 lora_sex --str2 0.3)")
    # [2026-09-09] [local] 수위 상한 스위치 — 상세는 README_local.md (.gitignore 대상)
    ap.add_argument("--allow-explicit", action="store_true", dest="allow_explicit",
                    help="[local] 수위 상한을 한 등급 해제합니다 (README_local.md 참고)")
    ap.add_argument("--no-explicit", action="store_true", dest="no_explicit",
                    help="[local] 로컬 설정/환경변수로 켜진 위 스위치를 이번 실행만 끕니다")
    args = ap.parse_args()

    # [2026-09-10] 실행 시작에 본 로그를 비웁니다(전부 append라 어제 실패와 섞였습니다).
    #   에러·경고는 지우지 않는 log/error.log에 따로 남깁니다.
    runlog.start_run(keep=bool(getattr(args, "keep_logs", False)))

    global _OS_OVERRIDE, _KEEP_LLM
    _OS_OVERRIDE = args.os_override
    _KEEP_LLM = bool(args.keep_llm)
    for s in (sys.stdout, sys.stderr):      # Windows cp949 콘솔(한글/→/✗) UnicodeEncodeError 안전판
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    if args.llm_plan:
        p(f"OS            : {'win' if is_win() else 'linux'} (--os {args.os_override}, 실측 os.name={os.name})")
        p(f"ollama 바이너리 : {_ollama_bin() or '(없음 — OLLAMA_BIN 지정/설치 필요)'}")
        host, port = _ollama_host_port()
        p(f"ollama 주소    : {host}:{port}  (env COMIC_OLLAMA_HOST / plot ollama_host)")
        p(f"OLLAMA_MODELS  : {os.environ.get('OLLAMA_MODELS_DIR') or '(plot ollama_models_dir 없으면 서버 기본)'}")
        p(f"ComfyUI dir    : {_comfy_dir()}")
        p(f"ComfyUI python : {_comfy_python(_comfy_dir()) or '(venv 없음)'}")
        p(f"ComfyUI 명령   : {' '.join(_comfy_args())} + venv python")
        p(f"ComfyUI env    : {sorted(_comfy_env())}")
        p(f"detach         : {_detach_kwargs()}")
        p(f"로그/PID        : {_repo_path('ollama_serve.log')} / {_repo_path('ollama_serve.pid')}")
        return 0

    if args.stop_llm:
        try:
            release_llm_for_gpu(log_fn=lambda m: p(f"  {m}"))
        except Exception as e:
            p(f"  모델 언로드 실패(계속): {e}")
        stop_ollama()
        return 0

    if args.get_fonts:
        p("== 화면 문법 폰트 다운로드 (전부 OFL — 설명 MaruBuri / 대사 Jua / 속마음 Do Hyeon / 의성어 Black Han Sans)")
        CG.CPM.download_fonts(log=lambda *a: p(" ".join(str(x) for x in a)))
        _missed = CG.CPM.missing_font_roles()
        p("  용도별 상태: " + ("설명/대사/속마음/의성어 모두 준비됨 ✓" if not _missed
                          else "미준비 " + ", ".join(_missed) + " → OS 폰트로 렌더됩니다"))
        return 0

    if getattr(args, "get_face_model", False):
        import comic_page_merge as _CPM
        p("  얼굴 검출 모델 받음 : " + ("완료" if _CPM.download_face_model(log=lambda s: p(s)) else "실패(추정치로 계속)"))
        p(f"    OpenCV : {'있음' if _CPM.face_model_available() else '없음'} — cv2가 없으면 추정치(원본 위에서 8%)를 씁니다")
    if getattr(args, "get_balloons", False):
        # [2026-09-10] 말풍선·속마음 자산 9종을 data/balloons/에 만든다(자리는 코드로 그림).
        #   실제 작화 자산을 같은 파일명·스펙으로 덮어쓰면 코드 수정이 필요 없다.
        import comic_page_merge as _CPM
        _bi = _CPM.generate_balloon_set(force=bool(args.force_balloons))
        p(f"  말풍선 자산 {_bi['made']}장면 → {_bi['dir']} (manifest: {_bi['manifest']})")
        p("  쓰실 때: --balloon-style image (자산이 없으면 자동으로 벡터로 돌아갑니다)")
        return 0
    if getattr(args, "list_templates", False):
        import comic_gen as _CG
        _tm = _CG.load_cut_templates()
        p(f"사용 가능한 페이지 템플릿 {len(_tm)}종 (data/cut.yaml) — --template 에 id나 이름 일부를 넣으세요")
        for _tid in sorted(_tm):
            _t = _tm[_tid]
            _n = sum(len(x["shares"]) for x in _t["tiers"])
            p(f"  {_tid:<32} {len(_t['tiers'])}단 {_n:>2}컷/페이지 [{'/'.join(_t['situations']) or '—'}]"
              f"{' ★에필로그 전용' if _t.get('epilogue') else ''}  {_t['name']}")
        return 0
    if not args.episode:
        ap.error("--episode 필수 (--stop-llm/--llm-plan/--get-fonts/--list-templates/--get-face-model 모드에서는 생략 가능)")
    if args.ep < 1:                      # 1기준. --ep 0(0기준 습관)을 1로 흡수한다 —
        args.ep = 1                      # 0으로 두면 가이드 map은 key 0, 조회는 1기준이라 기승전결이 증발한다
    if getattr(args, "balloon_style", None):
        config.comic_balloon_style = args.balloon_style
    import comic_page_merge as _CPMB
    _CPMB.set_balloon_style(getattr(config, "comic_balloon_style", "vector"),
                            getattr(config, "comic_balloon_dir", "data/balloons"))
    if args.font:
        config.comic_font = args.font
    # [2026-09-09] 화면 문법 용도별 폰트 / ★요약·에필로그 컷 스위치
    for _arg, _cfg in (("font_narration", "comic_font_narration"), ("font_dialog", "comic_font_dialog"),
                       ("font_thought", "comic_font_thought"), ("font_sfx", "comic_font_sfx")):
        if getattr(args, _arg, ""):
            setattr(config, _cfg, getattr(args, _arg))
    if args.no_epilogue:
        config.comic_epilogue = False
    if args.no_summary_cuts:
        config.comic_summary_cuts = False
    if args.no_emo_marks:
        config.comic_emo_marks = False
    if args.no_prologue:
        config.comic_prologue_cut = False
    if getattr(args, "no_strict_state", False):
        config.comic_strict_state = False
    # [2026-09-09] 아래 스위치들도 이 자리에서 배선한다 — 예전에 이 위치에 붙이지 않아
    #   --no-action-cuts / --name 이 장식품이었던 적(플래그만 있고 안 씀)이 있다.
    if args.no_action_cuts:
        config.comic_action_cuts = False
    if int(getattr(args, "strong_cut_weight", 2) or 2) >= 1:
        config.comic_cut_strong_weight = max(1, int(args.strong_cut_weight))
    if getattr(args, "vary", False):
        import time as _t
        config.comic_variation = (int(_t.time()) % 99999) + 1
    if str(getattr(args, "template", "") or "").strip():
        _want = [x.strip() for x in str(args.template).split(",") if x.strip()]
        import comic_gen as _CG
        _tm = _CG.load_cut_templates()
        _hit, _miss = [], []
        for _w in _want:
            _wl = _w.lower()
            _m = [k for k in sorted(_tm) if k.lower() == _wl or _wl in k.lower() or _wl in (_tm[k].get("name") or "").lower()]
            (_hit if _m else _miss).extend(_m or [_w])
        if _miss:
            p(f"[오류] 템플릿을 찾을 수 없습니다: {_miss} — `--list-templates`로 34종을 확인하세요")
            return 2
        config.comic_templates_pin = sorted(set(_hit))
    if getattr(args, "item_cuts", None) is not None:
        config.comic_item_cuts = bool(args.item_cuts)
    p(f"  컷 템플릿      : {('고정 ' + ', '.join(config.comic_templates_pin)) if getattr(config, 'comic_templates_pin', []) else '34종 자동 (회차 안 재사용)'}")
    p(f"  컷 배분 단위   : {'본문 항목 1 = 컷 1 (행동/대사/속마음)' if config.comic_item_cuts else '사건 단위(LLM이 컷 1~2개 지정)'}")
    if getattr(args, "wide_share", None) is not None:
        config.comic_wide_share_max = min(1.0, max(0.0, float(args.wide_share)))
    if int(getattr(args, "variation", 0) or 0) > 0:
        config.comic_variation = int(args.variation)        # --variation은 --vary보다 뒤에 적용(강함)
    config.comic_face_crop = bool(getattr(args, "face_crop", True))
    if getattr(args, "chatty", False):
        config.comic_chatty = True
    if str(getattr(args, "name", "") or "").strip():
        config.pin_name = str(args.name).strip()
    if str(getattr(args, "name2", "") or "").strip():
        config.pin_name2 = str(args.name2).strip()
    _var = int(getattr(config, "comic_variation", 0) or 0)
    p(f"  컷 배분 변동   : {_var if _var else '0 (같은 입력 → 같은 배분)'}"
      + (f" — 같은 배분을 고정이면 --variation {_var}" if _var else " — 매번 다르게 원하면 --vary"))
    p(f"  수다장이 모드  : {'ON (모든 컷 하단에 설명)' if config.comic_chatty else 'off (지문이 있는 컷만 설명)'}")
    # [2026-09-09] local_settings.yaml(로컬 전용 · gitignore)이 심어둔 기본값을 먼저 알린다.
    #   우선순위: CLI 인자 > env(COMIC_ALLOW_EXPLICIT) > local_settings.yaml > 기본 — CLI 주입은 아래에서 된다.
    if config.local_settings:
        p(f"  local 설정       : {config.LOCAL_SETTINGS_FILE} 사용 "
          f"(keys: {', '.join(sorted(config.local_settings))})")
        if config.ko_map_explicit or config.climax_vocab_local:
            p(f"                   민감 어휘 사전 {len(config.ko_map_explicit)}건 · "
              f"climax 어휘 +{len(config.climax_vocab_local)}건")
    if not args.safety and config.safety_local:
        args.safety = config.safety_local       # --safety를 안 주셨을 때만 로컬 값이 대신한다
    # [2026-09-09] 화풍/LoRA 스위치 → config 주입 (anima_gen이 렌더 직전에 getattr로 읽는다)
    if args.real:
        config.real_cli = True
    if args.sole:
        config.sole_cli = True
    if args.lora1:
        config.lora1_cli = args.lora1
    if args.lora2:
        config.lora2_cli = args.lora2
    if args.lora_chg:
        config.lora_chg_cli = args.lora_chg
    # [2026-09-09] LoRA 강도 오버라이드 (--str1/--str2) — anima_gen._apply_lora_strengths가 적용한다
    if args.str1 is not None:
        config.lora_str1_cli = args.str1
    if args.str2 is not None:
        config.lora_str2_cli = args.str2
    # [2026-09-09] [local 전용] explicit 허용 스위치: --allow-explicit / --safety explicit(암시) /
    #   env(COMIC_ALLOW_EXPLICIT) / local_settings.yaml 중 하나로 켜진다 (yaml은 import 시 이미 반영됨)
    if args.no_explicit:
        if args.safety == "explicit":
            p("  ! --no-explicit와 --safety explicit는 모순입니다 — --no-explicit가 이깁니다(explicit는 nsfw로 낮아집니다)")
        config.explicit_cli = False
    elif args.allow_explicit or args.safety == "explicit" or \
            os.environ.get("COMIC_ALLOW_EXPLICIT", "").strip().lower() in ("1", "yes", "y", "true"):
        config.explicit_cli = True
    if config.real_cli or config.sole_cli or config.lora1_cli or config.lora2_cli \
            or config.lora_str1_cli is not None or config.lora_str2_cli is not None:
        p(f"  화풍 override  : real={bool(config.real_cli)} sole={bool(config.sole_cli)} "
          f"lora1={config.lora1_cli or '-'}({config.lora_str1_cli if config.lora_str1_cli is not None else '기본'}) "
          f"lora2={config.lora2_cli or '-'}({config.lora_str2_cli if config.lora_str2_cli is not None else '기본'}) "
          f"lora_chg={config.lora_chg_cli or '-'}")
        if config.real_cli or config.sole_cli:
            p("  ! plot.json의 anima_unet이 비어 있어야 UNET 풀 선택이 적용됩니다 (지정하시면 그 모델이 우선)")
        if (config.lora_str1_cli is not None or config.lora_str2_cli is not None) \
                and (config.real_cli or config.sole_cli):
            p("  ! --real/--sole은 LoRA를 전부 끄는 모드입니다 — --str1/--str2는 무시됩니다")
    if config.explicit_cli:
        p("  수위            : explicit 허용 [local 모드] (기본은 청년향 = nsfw 상한)")
    elif args.no_explicit and config.local_settings:
        p("  수위            : 청년향 (--no-explicit로 local_settings의 스위치를 껐습니다)")
    if args.start_llm:
        os.environ["COMIC_OLLAMA_ENB"] = "yes"   # plot.json을 안 건드리고 ollama 배선으로 전환

    t0 = time.time()
    os.makedirs("log", exist_ok=True)
    os.makedirs("image", exist_ok=True)

    if args.start_llm:                       # 추출(첫 LLM 호출) **전에** 떠 있어야 한다
        p("\n== ollama 기동 (--start-llm) ==")
        if not start_ollama():
            # [2026-09-08] 미기동 상태로 추출을 태우면 "Connection refused"만 뱉고 만다 — 여기서中止
            p("  ! ollama 미기동 — 추출을 시도하지 않고 종료합니다 (log/ollama_serve.log 확인)")
            return 2

    # ============================================================
    # 1) 회차 작업 목록 (평문 파일 1건 = 회차 1개, progress/ 디렉터리 = 디스커버리)
    # ============================================================
    jobs, discovered = _resolve_jobs(args, ap)
    total_eps = max(1, int(args.total_episodes or 0), discovered, args.ep)
    # 발견한 회차 수를 총 회차로 쓴다(한 화만 뽑아도 컷 스크립트의 "EP3 / 총 10화"가 정확해야 한다) —
    # init_anima_tags의 'no_episode' 방지까지 이것으로 함께 해결된다.
    p(f"\n입력 {len(jobs)}건(회차 {', '.join(str(e) for e, _, _ in jobs)}) · 총 회차 {total_eps}"
      + (" · --special(local progress/ 포맷)" if args.special else ""))

    rc_all = 0
    done, skipped = [], []            # 전 회차 요약용 (완성/스킵)
    for k, (ep_num, ep_path, sheet_path) in enumerate(jobs, 1):
        if len(jobs) > 1:
            p(f"\n{'#' * 68}\n#  EP{ep_num:02d}  ({k}/{len(jobs)})  {os.path.basename(ep_path)}\n{'#' * 68}")
        try:
            rc = _run_episode(args, ep_num, total_eps, ep_path, sheet_path, t0)
        except KeyboardInterrupt:
            p("  중단(Ctrl-C) — 남은 회차를 접습니다")
            return 130
        # 회차가 끝난 뒤에 경로를 계산한다 — book 번호는 회차 안에서 확정되므로 그 전엔 모른다
        _skip_f = os.path.join(CG.comic_out_dir(), f"episode_{ep_num:02d}_SKIPPED.txt")
        if rc:
            rc_all = rc_all or rc
            perr(f"  ✗ EP{ep_num:02d} 실패(rc={rc} · {_RC_WHY.get(rc, '알 수 없음')}) — 다음 회차는 계속 시도합니다")
            _sn = _skip_note(ep_num, ep_path, rc)
            if _sn:
                p(f"  스킵 메모: {os.path.relpath(_sn)}")
            skipped.append((ep_num, rc))
        else:
            done.append(ep_num)
            if os.path.exists(_skip_f):                   # 이번 회차는 성공 — 지난 스킵 메모는 치웁니다
                try:
                    os.remove(_skip_f)
                except Exception:
                    pass
    if len(jobs) > 1:
        p(f"\n===== 전 회차 종료 ({time.time() - t0:.1f}s) · {len(jobs)}건 · "
          f"{'전부 성공' if not rc_all else f'실패 rc={rc_all}'} =====")
        p(f"  완성 {len(done)}회차 {('· ' + ', '.join('EP%02d' % e for e in done)) if done else ''}"
          + (f" / 스킵 {len(skipped)}회차 " + ", ".join("EP%02d(rc=%d)" % (e, r) for e, r in skipped)
             if skipped else ""))
        if skipped:
            perr("  ✗ 스킵된 회차: " + ", ".join("EP%02d(%s)" % (e, _RC_WHY.get(r, r)) for e, r in skipped)
                 + f" — 사유는 {runlog.ERROR_LOG}와 각 episode_NN_SKIPPED.txt")
    return rc_all


def _resolve_jobs(args, ap):
    """회차 작업 목록 → ([(ep_num, 에피소드 파일, 시트 파일), …], 발견된 총 회차 수)

    --episode가 디렉터리면 progress/ 산출물을 디스커버리합니다(--plot-hash로 작품 필터).
    파일 하나면 그 회차만 씁니다. --all-eps로 발견된 전 회차를 같은 --book에 담습니다.
    """
    if os.path.isdir(args.episode):
        import novel_progress as NP
        found = NP.discover(args.episode, args.plot_hash)
        if not found:
            ap.error(f"epNN_해시.txt 형식 에피소드를 찾지 못했습니다: {args.episode}"
                     + (f" (해시 {args.plot_hash})" if args.plot_hash else ""))
        pick = found if args.all_eps else ([f for f in found if f["ep"] == args.ep] or found[:1])
        return [(f["ep"], f["episode"], f["sheet"]) for f in pick], len(found)

    sheet = args.sheet
    if args.special and not sheet:
        import novel_progress as NP
        m = NP.EP_FILE_RE.match(os.path.basename(args.episode))
        if m:
            cand = NP.discover(os.path.dirname(os.path.abspath(args.episode)) or ".", m.group(2))
            hit = [c for c in cand if c["ep"] == int(m.group(1)) and c["sheet"]]
            sheet = hit[0]["sheet"] if hit else ""
            p(f"  --special 시트 자동 연결: {os.path.basename(sheet)}" if sheet else
              "  --special: 시트 JSON이 없어 본문 꼬리의 '--- 캐릭터 시트 ---'를 씁니다")
    return [(max(1, args.ep), args.episode, sheet)], 1


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        # [2026-09-08] 성공/실패/dry-run 어느 경로로 끝나도 LLM은 확실히 내린다.
        #   컷 스크립트가 0컷으로 early return되면comic_gen_episode의 반납 코드를 타지 않아
        #   17GB 모델이 keep_alive 동안 그대로 올라 있어 ComfyUI가 메모리를 못 쓴다(실측).
        try:
            release_llm_for_gpu(log_fn=lambda m: print(m, flush=True))
        except Exception as e:
            p(f"  LLM 메모리 반납 실패: {e}")
        # [2026-09-08] --start-llm으로 **우리가** 띄운 서버까지 내린다(Windows는 /T로 작업 트리).
        #   --keep-llm이면 서버를 살려둔 채 회차만 연속 실행한다(예전 OLLAMA_KEEP_SERVER=1).
        if _LLM_STARTED_BY_US and not _KEEP_LLM:
            try:
                stop_ollama()
            except Exception as e:
                p(f"  ollama 서버 종료 실패: {e}")
    try:
        print(runlog.summary(), flush=True)
    except Exception:
        pass
    sys.exit(rc)
