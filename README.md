# llm_comic_gen — 평문 에피소드 → 만화 페이지 생성기

안녕하세요. 한국어 산문(에피소드 + 캐릭터 시트)을 넣어 주시면 LLM이 컷·대사·태그를 만들고,
ComfyUI(Anima)가 컷을 렌더하며, 흰 프레임과 검은 선의 일본식 만화 페이지(1024×1454)로 합성해 드리는
프로그램입니다.

GUI·novel 파이프라인 의존성이 없는 **self-contained** 디렉토리이며, LLM은 **ollama 한 대**에만 연결됩니다.
수위는 **safe** 기준으로 동작합니다(포크에서 수정하시면 다른 등급도 사용하실 수 있습니다).

```
inputs/epNN.txt + inputs/sheetNN.txt
  → (LLM) 필드 추출 — 본문이 컨텍스트 예산을 넘으면 장면(창)으로 나눠 창당 1회 → 병합
  → (LLM) EP 태그 → 본문 길이에서 컷 수 역산 → 컷 수로 페이지/슬롯 확정(cut.yaml)
  → (LLM) 컷 스크립트 — 장면마다 1회, 직전 컷을 연속성으로 넘긴다
  → LLM 언로드 → (ComfyUI) 컷 렌더(seed 고정) → 페이지 합성
  → comic/bookNNN/episode_NN_pageXX.png
```

> **중요 — 에피소드 중단 문제 해결 [2026-09-08]**
> 본문 길이가 컷 수를 정하고, 컷 수가 페이지 수를 정합니다.
> (이전에는 4페이지/24컷이 먼저 고정되어 본문 9천 자가 4문장 가이드로 압축되었습니다.) 3-7장을 참고해 주세요.

---

## 1. 설치 (venv)

### 1-1) Python 환경

이 디렉토리에서 실행해 주세요(`plot.json`, `log/`, `image/`는 상대경로입니다).

```
python3 -m venv venv                  # python 3.11+ (확인: 3.14)
source venv/bin/activate
pip install -r requirements.txt       # openai / ollama(클라이언트) / PyYAML / Pillow
python3 -c "import openai, ollama, yaml, PIL; print('deps ok')"
```

### 1-2) LLM 서버 바이너리 (ollama) — **pip 클라이언트와 별개**

`pip install ollama`는 클라이언트일 뿐입니다. 서버 바이너리는 별도로 설치해 주세요(sudo는 필요 없습니다).

# Linux
```
mkdir -p ~/AI/ollama && cd ~/AI/ollama
curl -L -o ollama.tgz https://ollama.com/download/ollama-linux-amd64.tgz
tar -xzf ollama.tgz                   # → ~/AI/ollama/bin/ollama
~/AI/ollama/bin/ollama --version
```

# Windows
```
1) 설치형 Ollama.exe(트레이 앱), 또는 LMStudio/llama등을 사용하신다면 port 정보만 맞추면 됩니다.
2) 포터블로 쓰시려면 아래 zip을 특정 위치(C:\tools\ollama)에 풀고 OLLAMA_BIN 으로 디렉토리 위치를 알려주세요.
  a) 다운로드 위치
https://github.com/ollama/ollama/releases/latest/download/ollama-windows-amd64.zip
  b) 설치 위치 설정
set OLLAMA_BIN=C:\tools\ollama\ollama.exe
Note) 만일 그 경로가 없으면 run_comic.py가 PATH → %LOCALAPPDATA%\Programs\Ollama 순으로 찾습니다.
바이너리 위치는 런처가 직접 탐색하므로 **경로를 코드에 하드코딩하지 않습니다**.
무엇이 선택되었는지 확인하시려면 아래 명령을 사용해 주세요.

- Linux: `python run_comic.py --llm-plan`
- Windows: `run_ollama_win.bat plan`
```

### 1-3) LLM 모델 (GGUF)
```
mkdir -p gguf
# 16GB VRAM + 32GB RAM 기준 타깃 모델 (≈16.8GB, Q4_K_M)
# https://huggingface.co/TrevorJS/gemma-4-26B-A4B-it-uncensored-GGUF/tree/main

다운로드 받은 gguf 파일을 gguf/ 디렉토리에 넣어주세요.
```

### 1-4) ComfyUI (렌더)
Anima 체크포인트/LoRA/VAE가 설치된 ComfyUI가 필요합니다(기본 포트 `localhost:8188`).
설치 경로는 **두 곳**에 알려줍니다(하드코딩된 위치가 다르다면 여기서만 고치시면 됩니다).

| 위치 | 키/변수 |
|---|---|
| `plot.json` | `"comfyuidir": "/path/to/ComfyUI"` (Windows면 `"D:\\ComfyUI_aki"`) |
| env | `COMFYUI_DIR` (plot.json보다 우선), `COMFY_EXTRA_ARGS`(기동 인수 추가) |

`--start-comfy`는 OS별로 venv python을 찾습니다(Windows `venv\Scripts\python.exe`, Linux `venv/bin/python`).
`--use-ck-attention`은 Linux에서만 넘깁니다(Windows torch는 지원하지 않습니다).

Anima용 워크플로 JSON 파일은 `data_comfyui/anima_spectrum_July11.json`입니다. 필요에 따라 수정하시면 됩니다.

### 1-5) (선택) ollama 없이 전용 서버로 돌리기(비추천)

`llm_server.py`는 GGUF 하나로 OpenAI 호환 `/v1`을 띄우는 대체 수단입니다(ollama 불필요).
```
pip install llama-cpp-python fastapi uvicorn
# GPU offload는 빌드가 필요합니다:
#   CUDA: CMAKE_ARGS="-DGGML_CUDA=on" pip install llama-cpp-python
#   ROCm: CMAKE_ARGS="-DGGML_HIP=on"  pip install llama-cpp-python
python3 llm_server.py --model gguf/gemma-4-26B-A4B-it-uncensored-Q4_K_M.gguf   # 127.0.0.1:8081
# 이 경우 plot.json 을 "ollama_enb": "no" + text_ip/text_port=127.0.0.1/8081 로 두세요.

만일 이미 깔려 있는 LMSTUDIO, llama가 있고 메모리 제약이 적다면 그대로 사용하시면 됩니다.
```

### 1-6) 실행 환경변수

[2026-09-08] 이전 버전에서 `run_ollama.sh`가 들고 있던 값들
(`OLLAMA_GGUF`/`OLLAMA_MODEL`/`OLLAMA_NUM_CTX`/`OLLAMA_KEEP_SERVER`)은
**plot.json 키 또는 run_comic 플래그로 이관**되었습니다.
셸이 값을 가지고 있으면 Linux/Windows 두 트윈에서 반드시 어긋나기 때문입니다.

| 변수 | 기본값 | 설명 |
|---|---|---|
| `OLLAMA_BIN` | PATH → `~/AI/ollama/bin/ollama` / Win `%LOCALAPPDATA%\Programs\Ollama\ollama.exe` | 서버 바이너리 위치 |
| `COMIC_OLLAMA_ENB` | plot.json `ollama_enb` | `yes/no`로 백엔드를 강제합니다 — **plot.json을 수정하지 않게 되었습니다** |
| `COMIC_OLLAMA_HOST` | plot.json `ollama_host` | `127.0.0.1:11455`처럼 지정하시면 설치형 ollama(11434)와 충돌하지 않습니다 |
| `OLLAMA_MODELS_DIR` | ollama 서버 기본 | blob/모델을 앱 폴더로 격리합니다(`C:\Users` 오염 방지) |
| `OLLAMA_HOST_URL` | — | 셸 래퍼 전용(내부적으로 `COMIC_OLLAMA_HOST`로 변환합니다) |
| `COMFYUI_DIR` / `COMFY_EXTRA_ARGS` | plot.json `comfyuidir` / 없음 | ComfyUI 경로·기동 인수 |
| `num_ctx` | plot.json `ollama_num_ctx`(8192) | 요청마다 보내는 컨텍스트 — 넘기면 프롬프트 앞부분이 잘립니다 |

서버 유지/종료는 플래그로 지정해 주세요: `--keep-llm`(유지) / `--stop-llm`(수동 종료) —
이전 `OLLAMA_KEEP_SERVER=1`을 대체합니다.

---

## 2. 설정 (`plot.json`)

LLM 호출은 **`ollama_enb == "yes"` 일 때 예외 없이 아래 ollama 설정**으로 갑니다
(추출·태그 생성·컷 스크립트·POV/multi 가이드·한글 glossary 전부 해당합니다. `selftest.py`로 동작 테스트를 진행할 수 있습니다.).

```json
{
  "ollama_enb": "yes",
  "ollama_gguf": "./gguf/gemma-4-26B-A4B-it-uncensored-Q4_K_M.gguf",
  "ollama_model": "comic-gemma-4-26B-A4B-it-uncensored-Q4_K_M",
  "ollama_host": "http://127.0.0.1:11434",
  "ollama_num_ctx": 8192,
  "ollama_num_gpu_layers": "",
  "ollama_keep_alive": "10m",

  "anima_style": "lora_lambton",
  "anima_unet": "anima_aestheticV11.safetensors",
  "comfyuidir": "/path/to/ComfyUI"
}
```

| 키 | 설명 |
|---|---|
| `ollama_enb` | `yes` = 모든 LLM 트래픽을 ollama로 보냅니다. `no` = `text_ip:text_port`(전용 서버/OpenAI 호환)를 사용합니다 |
| `ollama_gguf` | 비우시면 이미 등록된 `ollama_model`만 사용합니다. 값이 있으면 첫 호출 때 자동으로 등록합니다(이미 있으면 건너뜁니다) |
| `ollama_num_ctx` | **반드시 지정해 주세요.** 8192 ≈ KV 캐시 1~2GB. 16GB VRAM/32GB RAM 기준값입니다 |
| `ollama_num_gpu_layers` | 비우시면 ollama가 자동으로 offload합니다(Q4 16.8GB 파일은 일부 레이어가 CPU로 올라갑니다). 고정하고 싶으시면 수치를 지정해 주세요 |
| `ollama_keep_alive` | LLM 단계 동안의 상주 시간입니다. 렌더 직전 `keep_alive=0`으로 강제 반납합니다 |
| `anima_style` / `anima_unet` / `anima_lora` | 화풍(LoRA)/메인 체크포인트입니다. 키 목록은 `anima_gen.py`의 `ANIMA_LORA_CONFIG`를 참고해 주세요 — **고를 수 있는 키와 설명은 `LORA.md`에 표로 정리해 두었습니다** |
| ↳ 우선순위 | `--real` > `--sole` > `--lora1/--lora2` > `plot.json`의 `anima_style`/`anima_lora` 입니다 (3-5절). |
| ↳ `anima_unet` 주의 | `anima_unet`을 **지정하시면 항상 그 모델이 우선**합니다. `--real`/`--sole`의 UNET 풀 선택을 쓰고 싶으시면 `"anima_unet": ""`로 비워 주세요 |
| `text_*`/`ip_main`/`ip_agent`/`ip_anima` | `ollama_enb=no` 일 때만 의미가 있습니다(단일 서버로 통일하셔도 됩니다) |

---

## 3. 사용법

### 3-1) 자가 점검 (LLM·ComfyUI 없이)

먼저 아래 명령부터 실행해 주시면 문제를 미리 막을 수 있습니다.

```bash
python3 selftest.py        # 전부 돌려 PASS/FAIL을 چاپ니다. 파일 자가보유/추출 파싱/합성 픽셀(1024x1454 고정)/
                           # cut.yaml 플래너(4:6 포함)/성기 필터/재현성/ollama 배선 감사/
                           # 본문→컷 수→페이지 수 + 장면 분할 호출 + 추출 map-reduce 병합/
                           # 시트의 `#캐릭터 태그#` 인식·보장 주입/OS 분기(셸 래퍼 두께·폰트 후보·env 오버라이드) 감사/
                           # 화풍 CLI(--real/--sole/--lora*)와 LORA.md 동기화
                           # 입력은 배송되는 inputs/만 읽습니다(로컬 전용 검증 입력은 selftest가 쓰지 않습니다)
```

### 3-2) 미리보기 (렌더 없음, LLM만 사용)

```bash
python3 run_comic.py --episode inputs/ep01.txt --sheet inputs/sheet01.txt \
                     --dry-run --preview 2
```

컷 구성·대사·최종 프롬프트를 화면에 출력하고 `comic/bookNNN/episode_NN_script.json`을 남깁니다.

### 3-3) 본 실행 — 권장 (ollama 런너)

```bash
# Linux / macOS
./run_ollama.sh --episode inputs/ep01.txt --sheet inputs/sheet01.txt \
                --ep 1 --total-episodes 1 --book 1          # 페이지는 본문 길이로 자동

# [local 전용] 단편 생성기 progress/ 포맷을 전 회차 한 권으로(3-9절)
./run_ollama.sh --special --all-eps --episode ~/progress \
                --plot-hash 2218f2f3797744fe --book 1 --safety safe
```

```bat
rem Windows (더블클릭 가능, cmd/powershell 둘 다 됩니다)
run_ollama_win.bat --episode inputs\ep01.txt --sheet inputs\sheet01.txt --ep 1 --book 1 --start-comfy
```

두 래퍼의 **실행 라인은 한 줄**입니다(`run_comic.py --start-llm …`, 나머지는 주석).
서버 생명주기는 `run_comic.py`가 소유합니다.

1. ollama 바이너리 탐색 → 없으면 기동(`OLLAMA_MAX_LOADED_MODELS=1`, `log/ollama_serve.log`/`.pid`)
2. gguf 자동 등록
3. 추출/태그/컷 스크립트
4. 렌더 직전 `keep_alive=0` + `ollama ps`가 비워진 것 확인
5. 종료 시 **자기가 띄운 서버만** 종료(Linux: process group TERM→KILL, Windows: `taskkill /T /F`)

plot.json은 고치지 않습니다(env `COMIC_OLLAMA_ENB=yes`로 배선을 전환합니다 → 창을 ✕로 닫으셔도 잔류가 없습니다).

```bash
./run_ollama.sh stop                        # ollama 모델+서버 종료
python3 run_comic.py --keep-llm …           # 회차 연속 실행: 서버 유지
run_ollama_win.bat stop / plan              # Windows: 종료 / 분기 결과 확인
```

### 3-4) 직접 실행 (ollama를 이미 띄워 둔 경우, 또는 다른 LLM serving 프로그램이 이미 있는 경우)

```bash
export OLLAMA_HOST=127.0.0.1:11434
~/AI/ollama/bin/ollama serve &              # 서버
python3 run_comic.py --episode inputs/ep01.txt --sheet inputs/sheet01.txt --start-comfy
```

### 3-5) 자주 쓰는 플래그 (`run_comic.py`)

| 플래그 | 의미 |
|---|---|
| `--episode FILE` (필수) | 에피소드 평문(`.md`도 허용됩니다). `--special`이면 **디렉터리**도 가능합니다 |
| `--sheet FILE` | 캐릭터 시트 평문(생략하시면 본문에서만 추출합니다) |
| `--special` | [local 전용] 단편 생성기 GUI의 `progress/` 포맷으로 받습니다 — 3-9절 |
| `--plot-hash HASH` | `--special`로 디렉터리를 탈 때 작품 해시 필터(여러 작품이 섞여 있을 때) |
| `--all-eps` | `--special` 디렉터리에서 발견한 **전 회차**를 같은 `--book`에 연속 생성합니다 |
| `--safety safe` | 수위를 강제합니다(비우면 LLM 판단). 이 repo의 기본 정책은 safe입니다 |
| `--pages N` | cut.yaml 페이지 수 (기본 0 = **본문 길이로 자동 산출**, N>0 = N페이지 고정) |
| `--max-pages N` | 자동 모드 페이지 상한(기본 24 ≈ 최대 ~190컷) |
| `--chars-per-panel N` | 본문 몇 자로 컷 1컷으로 볼지(기본 600 — 작을수록 컷/페이지가 늘어납니다) |
| `--max-panels N` | 컷 상한(기본 0 = 무제한) |
| `--beat-chars N` | 장면(컷 스크립트 LLM 1호출) 본문 상한(기본 1800) |
| `--name NAME` | 주인공 이름을 고정합니다(시트의 `#캐릭터 태그#`에서 이름이 새어들 때) |
| `--name2 NAME` | 상대방 이름을 고정합니다 |
| `--variation N` | 컷 배분에 변동을 섞습니다 (0=같은 입력 → 같은 배분, N>0=값마다 다른 페이지 구성·장면당 컷 수) |
| `--vary` | 변동 값을 이번 실행에서 뽑고 화면에 찍습니다. 마음에 드는 배분이 나오면 그 값으로 `--variation N` 재실행 |
| `--chatty` | **수다장이 모드**: 모든 컷 아래에 설명을 붙입니다. 서술할 사건이 없는 컷은 그녀의 행동·표정을 한 문장으로 LLM이 작문합니다 |
| `--no-action-cuts` | 컷 배분을 본문 '글자 수'로 되돌립니다(기본은 LLM이 나눈 사건 단위) |
| `--strong-cut-weight N` | 유닛이 컷 수를 안 줬을 때 강한 사건으로 보는 컷 수(기본 2, 2~3) |
| `--panels-per-page N` | cut.yaml을 끌 때 페이지당 컷 수(기본 5) |
| `--ep N` / `--total-episodes N` | 회차 번호 / 총 회차 수 |
| `--book N` | 출력 `comic/bookNNN` 번호 |
| `--dry-run` / `--preview N` | 렌더 없이 스크립트/프롬프트를 확인합니다 |
| `--start-comfy` | ComfyUI가 꺼져 있으면 기동합니다 |
| `--start-llm` | ollama 서버가 없으면 이 프로그램이 기동합니다(래퍼가 자동 지정) |
| `--keep-llm` | `--start-llm`으로 띄운 서버를 종료하지 않습니다(회차 연속 실행용) |
| `--stop-llm` | ollama 모델/서버만 내리고 종료합니다 |
| `--os auto\|win\|linux` | OS 자동 감지(`os.name`) 오버라이드 — 기본 auto |
| `--llm-plan` | OS 분기 결과(바이너리/경로/기동 명령/env)만 출력합니다 |
| `--font PATH` | 한글 폰트(`C:\Windows\Fonts\malgunbd.ttf` 등), 비우면 OS별 자동 선택 |
| `--get-fonts` | 화면 문법 폰트 4종(설명/대사/속마음/의성어, 전부 OFL)을 `data/fonts/`로 받고 종료 |
| `--font-narration` `--font-dialog` `--font-thought` `--font-sfx` | 용도별 폰트 지정 — 비우면 `data/fonts/` → OS 순서 |
| `--no-epilogue` | ★에필로그 페이지(반투명 이벤트신 1칸 + 큰 지문)를 붙이지 않습니다 — **마지막 회차에만** 붙습니다 |
| `--no-prologue` | ★프롤로그(회차집 첫 회차 맨 앞의 도입 1컷)를 붙이지 않습니다 |
| `--no-summary-cuts` | ★회차 도입 요약 컷(각 회차의 첫 컷 = 배경만 + 큰 지문)을 끕니다 |
| `--no-emo-marks` | 감정 이모티콘(분노/놀람/땀/하트/음영/반짝/물음) 표시를 끕니다 |
| `--template ID[,ID…]` | 페이지 템플릿을 **고정**합니다 (1종 = 회차 전체 같은 구성, 여러 종 = 페이지마다 회전) |
| `--list-templates` | 사용 가능한 템플릿(id / 단수 / 페이지당 컷 수 / 상황)을 보이고 끝냅니다 |
| `--no-strict-state` | 컷 스크립트 JSON이 필수 항목(상태 시트·pose·첫 컷의 시작 상태)을 못 채워도 **경고만** 하고 진행합니다 (기본은 에러로 종료) |
| `--no-face-crop` | 컷 크롭을 세로 가운데 자르기로 되돌립니다(기본은 얼굴 중심)
| `--get-face-model` | 얼굴 검출 모델(YuNet 227KB)을 받습니다 — OpenCV가 있을 때만 쓰입니다 |
| `--no-cut-yaml` | 레이아웃 자동 문법으로 회귀합니다 |
| `--no-wide` | wide(1366×1024) 컷을 금지합니다 |
| `--angle` | action 컷에 `data_comfyui/angle.txt` 구도를 적용합니다 |
| `--no-thumb` | `_thumb.jpg`를 만들지 않습니다 |
| `--real` | LoRA를 전부 OFF 하고 리얼 메인 모델(`ANIMA_REAL_UNET_POOL`에서 랜덤 1개)로 그립니다 |
| `--sole` | LoRA 없이 SOLE 메인 모델(`ANIMA_SOLE_UNET_POOL`에서 랜덤 1개)로 그립니다 (`--real`이 있으면 무시됩니다) |
| `--lora1 KEY` | `ANIMA_LORA_CONFIG`의 키로 lora1 슬롯을 고정합니다 (`plot.json`의 `anima_style`보다 우선) |
| `--lora2 KEY` | lora2(보조) 슬롯입니다. 예: `--lora1 lora_mi1k --lora2 lora_sex` |
| `--lora-chg episode` | 에피소드가 바뀔 때마다 단독 LoRA 쌍을 랜덤으로 다시 고릅니다 (`increment`는 폐지되었습니다) |
| `--str1 0.8` | lora1 **강도** 오버라이드(0.0~2.0, 생략하면 `ANIMA_LORA_CONFIG`의 값). `--lora1`뿐 아니라 `plot.json` 화풍/`lora_random`에도 적용됩니다 |
| `--str2 0.3` | lora2 강도 오버라이드. `0`이면 그 슬롯이 꺼집니다 |

화풍/LoRA 플래그를 쓰실 때 참고해 주세요.

- **어느 키가 어떤 화풍인지**는 `LORA.md`(선택 가능한 LoRA·UNet 안내)를 열어 주세요. README를 두껍게 하지 않으려고 분리했습니다.
```bash
# 쓸 수 있는 LoRA 키 79개 확인
python -c "import anima_gen as A; print('\n'.join(sorted(A.ANIMA_LORA_CONFIG)))"

# 리얼 화풍 (--real/--sole은 plot.json의 "anima_unet": "" 가 함께 필요합니다)
python run_comic.py --episode inputs/ep01.txt --sheet inputs/sheet01.txt --real

# 화풍 고정 (LoRA 2장 조합)
python run_comic.py --episode inputs/ep01.txt --lora1 lora_mi1k --lora2 lora_sex
```

- 우선순위는 `--real` > `--sole` > `--lora1/--lora2` > `plot.json`입니다.
- `--lora-chg increment`는 **폐지**되었습니다. 총 회차(`--total-episodes`)보다 `--ep`가 크면 lora2 강도가 1.0을 넘어버려서(실측 EP12/총 1화 → `10.0`) 아예 설정할 수 없게 막았습니다 — `anima_gen._norm_lora_chg()`에서 차단됩니다.
- 잘못된 KEY를 넣으시면 에러 로그를 남기고 `plot.json` 기본 설정으로 돌아갑니다(렌더는 계속됩니다).
- 파일이 `models/loras`에 없으면 그 슬롯을 끄고 로그에 알립니다(ComfyUI가 400으로 죽지 않습니다).

**강도(스트렝스)를 고치실 때 — `--str1` / `--str2`**

`ANIMA_LORA_CONFIG`를 편집하지 않고 명령줄에서 강도를 덮어씁니다. 두 플래그는 LoRA 키를 직접 지명하지 않아도(`plot.json`의 `anima_style`, `lora_random`, `--lora-chg episode` 포함) 적용됩니다.

```bash
# vassago를 0.8로, sex_style을 0.3만 섞어서 그리기
python run_comic.py --episode inputs/ep01.txt --lora1 lora_vassago --str1 0.8 \\
                    --lora2 lora_sex --str2 0.3

# 강도 0 = 그 슬롯 OFF (보조 LoRA만 빼고 그리고 싶을 때)
python run_comic.py --episode inputs/ep01.txt --lora1 lora_mi1k --lora2 lora_sex --str2 0
```

- 범위는 0.0~2.0이며 넘는 값은 클램프됩니다(`--str2 9` → 2.0).
- `--real`/`--sole`은 LoRA를 전부 끄는 모드이므로 `--str1/--str2`도 함께 무시됩니다.
- 실제로 ComfyUI에 들어간 값은 `log/anima_gen.log`의 `[ComfyUI LoRA] lora_1=…(강도) … | trigger=…` 한 줄에서 확인됩니다.

### 3-6) 캐릭터 시트 작성법 — `#캐릭터 태그#`로 캐릭터를 지정해 주세요

시트는 평문이시면 됩니다(형식 제약 없음). 다만 그 캐릭터를 **anima가 알고 있는 경우**에는
`#…#` 로 공식 태그를 감싸서 그대로 알려 주시는 편이 가장 정확합니다.

```
#Usagi Tsukino from Sailor Moon#
with blonde hair, long twin odango pigtails, blue eyes, slender figure, wearing a classic sailor fuku uniform.
처음 표정은 의지에 차 있다.
```

| 시트의 문법 | 인식 결과 |
|---|---|
| `#Usagi Tsukino from Sailor Moon#` | **주인공 캐릭터 태그(트리거)** — 모든 컷에 들어갑니다 |
| `상대방은 #Tuxedo Mask#` (상대/파트너 섹션 안의 `#…#`) | **상대방 태그** — 상대방이 프레임에 있는 POV 컷에만 들어갑니다 |
| 한 줄에 여러 개 `#a# #b#`, 여러 줄 사용 | 전부 쓰기 순서대로 인식합니다(캐릭터당 최대 6개) |

- `#…#` 속 문자열은 **그대로** 프롬프트에 들어갑니다. 줄바꿈·연속 공백만 정리되고, anima가 읽지
  못하는 `_`는 공백으로 바뀝니다(`twin_odango → twin odango`). 대소문자는 그대로 유지됩니다.
- 주입 시점은 **정제·LLM 재작성이 끝난 마지막 단계**(`anima_gen.ensure_char_tags`)입니다. 그래서
  컷 프롬프트를 쓰는 LLM이 태그를 빼먹어도 무조건 되살아나고, 이미 들어가 있으면 중복을 만들지
  않습니다(대소문자·`_` 차이는 무시하고 같은 태그로 봅니다).
- 로그에서 확인하실 수 있습니다: `log/comic_input.log`의 `#캐릭터 태그 인식: …`,
  `log/tag_out.txt`의 `#캐릭터 태그 보장 주입: …`.
- 마크다운 헤딩(`# 제목` — `#` 다음 공백)과 `#태그`는 구분됩니다. 닫는 `#`이 없는 줄은 태그로
  보지 않고 경고만 남깁니다. 태그는 **영문**으로 적어 주세요(한글 `#유즈키#`도 넣지만 anima는 모릅니다).

```bash
# 시트(예: inputs/sheet01.txt) 첫 줄에 `#캐릭터 공식 태그#`를 넣어 보신 뒤, 렌더 없이 확인만 해 보시려면
python3 run_comic.py --episode inputs/ep01.txt --sheet inputs/sheet01.txt --dry-run --preview 2
# 확인하실 곳: log/comic_input.log 의 `#캐릭터 태그 인식: …`, log/tag_out.txt 의 `#캐릭터 태그 보장 주입: …`
```

### 3-7) 에피소드 전체 반영 — 컷 수/페이지 수가 본문에서 나옵니다

[2026-09-08] 이전 배선은 ① 본문을 기승전결 **가이드 4문장**으로 압축 ② 레이아웃이 컷 수(≤4페이지/24컷)를
먼저 확정 ③ 컷 스크립트 프롬프트가 본문 원문을 **한 번도 보지 못하고** 가이드만 봤습니다.
그래서 회차 본문 9천 자 중 장면·소품·대사가 컷으로 내려가지 못했습니다. 지금은 아래 순서로 동작합니다.

| 단계 | 결정자 | 근거 |
|---|---|---|
| ① 본문 → 컷 수 | `comic_input.target_panels_from_weights()` (2026-09-09) | **사건(액션) 단위** 합산 — 평범한 사건 1컷, 신체 접촉·관계 변화·클라이맥스는 2컷. 하한 `max(6, 막당 2컷)`, 상한 `--max-panels` |
| ①-예 글자 수 배분 | `comic_input.target_panels()` | `본문 글자 수 ÷ --chars-per-panel(600)` — 유닛을 못 받았거나 `--no-action-cuts`일 때의 폴백 |
| ② 컷 수 → 페이지/슬롯 | `comic_gen.plan_pages_layout()` | 템플릿 평균 슬롯(≈5.2컷/페이지)으로 어림한 페이지 수 ±2를 훑어 목표에 가장 가까운 구성을 고릅니다 |
| ③ 본문 → 장면(창) | `comic_input.split_beats()` | 컷 6개마다 장면 1개, 빈 줄(장면전환)→문장 순으로 자릅니다, **서사 순서는 보존**됩니다 |
| ④ 장면 → 컷 | `request_panel_script()` | 장면마다 LLM 1회. 이 장면 본문 + 이 장면 슬롯 + 직전 컷(캡션·복장)을 넘깁니다 |

장면으로 나누는 이유는 품질이 아니라 **물리 제약** 때문입니다.
`plot.json ollama_num_ctx`(기본 8192)를 넘으면 ollama는 에러 없이 프롬프트 **앞부분(규칙 블록)을 잘라냅니다**.
한국어는 실측 ≈1.8자/토큰이므로, 1회 호출에 넣는 본문 상한을 `episode_char_budget()`이 num_ctx에서 계산합니다
(기본 ≈11천 자).

- **추출도 같은 창을 사용합니다**: 본문을 창으로 나눠 창당 1회 호출 → `가이드`는 창 수만큼 늘어납니다(≤24줄),
  이름·외모 태그는 첫 창 값(캐릭터 일관성), `rating`은 창 중 최대값, `$행동`은 합집합(≤6)으로 병합합니다.
- **[2026-09-08] 자수가 적으면 기승전결 '막(幕)'이 장면 골격이 됩니다**: 추출 LLM은 가이드와 함께
  각 막(기/승/전/결)의 **첫 문장을 본문에서 그대로 사본**(`segments`)도 돌려보냅니다. 컷 스크립트는
  이 앵커를 원문에서 찾아(공백 무관) 장면을 막 단위로 나누고, **막당 최소 2컷**을 하한으로 삼아
  컷 예산이 모자라면 레이아웃을 다시 계획합니다(4막 → 최소 8컷). 각 장면 프롬프트에는 그 막의
  가이드 줄만 실립니다(`기장면 1/4`처럼 라벨로 표시). 앵커가 하나라도 어긋나면 기존 글자수
  분할로 조용히 돌아갑니다(`split_by_segments → []`, 로그에 `(기승전결 막 분할)`이 안 뜨면 폴백입니다).
  예: 706자짜리 짧은 회차도 `--pages 2`와 함께 쓰면 4막 × 막당 2~3컷으로 펼쳐집니다(본문이 먼저니까요).
- **비용(실측: gemma-4-26B-A4B Q4 + Anima, 16GB GPU)**: 본문 9,034자 → 16컷/3페이지 **249초(4분 9초)**,
  27,121자 → 43컷/9페이지 **643초(10분 43초)** — 컷 43/43 렌더 성공, 검은 컷 0장.
  LLM은 렌더 전에 모두 내려갑니다(dry-run은 ep01 ≈56초 / 27k ≈141초), 렌더 시간만 컷 수에 선형으로 늘어납니다.
- `--chars-per-panel 600`은 실측으로 고정한 기본값입니다(컷 1개 ≈ 본문 565자 = 행동 1개 — ep01 16컷이
  본문의 알약·화장실·속삭임 같은 세부까지 담았습니다). 촘촘하게 원하시면 `400`(ep01 → 23컷/4페이지, 렌더 1.4배),
  요약만 원하시면 `900`(11컷/2페이지)을 사용해 주세요. 짧은 회차(≲1,500자)는 값과 무관하게 하한 6컷/1페이지입니다.
- 컷 수가 아깝하시면 `--chars-per-panel 400`(컷/페이지 증가), 본문이 잘리는 것 같으시면 `--max-pages`를 올려 주세요.
  컷 스크립트 단계에서 장면이 통째로 미응답이면 `log/comic_gen.log`에 `침묵 컷 채움`으로 남습니다.
  실제 배선은 실행 첫 줄(`본문 N자 → 컷 예산 N컷`)과 `EP1 컷 스크립트 완성: … N장면 LLM N회` 두 줄로 확인하실 수 있습니다.

**[2026-09-09] 컷 배분의 저울을 '글자 수'에서 '일어난 사건'으로 옮겼습니다**

실측 사례가 발단이었습니다 — 본문 2,666자 → 목표 6컷 → 레이아웃 8컷 → *4막 × 막당 최소 2컷*이 8을 모두
써 버려 `[2,2,2,2]`. 기(起)가 2컷인 이유는 본문이 짧아서가 아니라 **배분 저울이 글자 수**였기 때문입니다.
같은 600자라도 "대화만 600자"와 "손이 겹침 + 카메라 + 고백 600자"가 같은 예산을 받았죠.

| 무엇 | 어떻게 |
|---|---|
| 사건 나누기 | 추출 LLM이 본문을 **사건(액션) 단위**로 나눕니다 — `{"at": "사건이 시작하는 문장을 본문에서 그대로 복사", "cuts": 1\|2}` (4~12개). 이동·호칭·대사만 있는 장면은 앞 유닛에 합칩니다 |
| 위치 확정 | 글자 좌표는 모델이 세지 못하므로 **문장 사본을 원문에서 찾아** 경계를 확정합니다(공백 무관, 다른 막의 유닛은 조용히 건너뜁니다) |
| 컷 예산 | `Σ cuts` (하한 `max(6, 막 수 × 2)`, 상한 `--max-panels`) → 예산이 바뀌면 페이지/슬롯을 **다시 계획**합니다 |
| 컷 배분 | 장면별 `Σ cuts`에 비례 배분 (예전: 막 글자수에 비례) |
| 폴백 | 유닛이 없거나 JSON이 망가지면 → 강약 큐(신체 접촉·관계 변화 어휘)로 컷 수를 메우고, 그것도 없으면 **본문 글자 수 배분**으로 돌아갑니다 |

- 확인: 로그에 `EP1 사건 5개(강한 사건 2개 = 컷 2) → 컷 예산 7 / 레이아웃 11컷 — 배분 저울은 '글자 수'가 아니라 '일어난 사건'` 가 찍힙니다.
- 되돌리기: `--no-action-cuts`(본문 길이 배분로), 강한 사건의 컷 수는 `--strong-cut-weight 2`(2~3).
- `--chars-per-panel`은 이제 **폴백 전용**입니다(유닛이 없을 때만 저울로 쓰입니다).

**[2026-09-08] 기승전결 전달 보강** — 기승전결 분해는 언어모델에게 그대로 맡깁니다(한글 능력 신뢰).
교체한 것은 LLM이 만들어 낸 가이드를 **끝까지 전달하는 배선** 네 곳입니다.

| 문제(실제 로그 증거) | 조치 |
|---|---|
| `--ep 0`(0기준 습관)으로 가이드가 key 0에 저장되고 조회는 1기준 → 컷 스크립트 프롬프트가 `(가이드 없음)` | 0을 1로 흡수합니다. 추출 로그에 `가이드 4줄`이 떴다면 이제는 컷 스크립트에도 실제로 실립니다 |
| 장면별 가이드 슬라이스가 `[partner]`/`[sub]`까지 균등 분할 → 장면1이 기승전결의 3/4를 먹고 뒷장면이 어긋남 | `[protagonist]` 줄만 비례로 자르고, 회차 전체 줄은 마지막 장면에 붙입니다 |
| 출력 토큰 잘림으로 마지막 컷이 `{`인 채 소멸 → `침묵 컷 채움`(페이지가 뚫림) | 따옴표·괄호를 닫아 써 둔 필드만이라도 구제합니다(`JSON 잘림 → 마지막 컷 구제`) |
| 페이지가 2~3장일 때 레이아웃 기승전결 대응이 `결` 템플릿을 한 번도 쓰지 않음(마지막 장이 `전`으로 끝남) | 모든 페이지 수에서 첫 장=기·마지막 장=결을 보장합니다 |

### 3-7a) 본문 항목 1개 = 컷 1개 — `--no-item-cuts`로 예전 방식 회귀

> **항목 수는 지키습니다** — 기승전결 막 앵커가 깨져 막이 1개가 되어도 항목 저울은 그대로 돕니다(장면 1개 = 항목 6개까지 묶고 호출을 늘립니다). 실측: 본문 727자 · 항목 20개 → 이전엔 **6컷**, 지금은 **21컷(4페이지 · LLM 4회)**. 실행 화면의 `컷 예산` 안내도 글자 수가 아니라 항목 수를 봅니다.

[2026-09-09] 기본 동작입니다. 추출 단계에 본문을 **일어난 시간 순서대로 화면 항목 하나씩**으로 나눠 달라고 시킵니다. 항목은 셋 중 하나입니다.

| 항목 | 화면 | 끄는 법 |
|---|---|---|
| **행동** (무엇을 했다) | 지문(설명) + pose, 풍선 없음 | |
| **대사** (누가 무엇을 말했다) | 말풍선 1개 | |
| **속마음** (누가 무엇을 생각했다) | 속마음 풍선 1개 | |

- 항목 하나가 컷 하나가 됩니다(`cuts`는 항상 1). 그래서 "본문이 긴데 컷이 5개뿐" 같은 압축이 줄었습니다. 실측: 같은 원고로 11컷(2페이지) → **19컷(4페이지)**.
- 각 컷에 어떤 장치를 쓸지는 **코드가 본문에서 판정**합니다(인용문 → 대사, `속으로`/`~생각했다`/`…` → 속마음, 나머지 → 행동). LLM에게 맡기면 한 컷에 셋을 다 섞어 사건을 지웠기 때문입니다. 추출 단계가 항목에 `kind`를 붙여 주면 그것을 먼저 씁니다.
- 컷 스크립트 프롬프트에는 이렇게 들어갑니다.
```
[이 컷들의 화면 장치 — 본문에서 프로그램이 판정했습니다. 이 순서와 장치를 따르세요]
  - 컷 12 = **행동 컷** — 지문(caption_ko)과 pose로 보여주고, lines는 []로 둔다.
  - 컷 13 = **속마음 컷** — 속마음 풍선(lines kind=thought)을 반드시 넣는다.
```
- ★ 화면 문법 슬롯(회차 도입 요약 / 프롤로그 / 에필로그)은 자기 규칙이 있어 이 지침을 덮어쓰지 않습니다.
- `--no-item-cuts`를 붙이면 예전 방식(사건 단위, LLM이 사건당 컷 1~2개를 고름)으로 돌아갑니다.
- 관련해서 레이아웃도 고쳤습니다: 페이지 수 × 레이아웃 재추첨(`config.comic_layout_rolls`, 기본 10)을 함께 훑어 **목표 컷 수에 가장 가까운 구성**을 고릅니다. 예전에는 컷 수 사다리가(1페이지 4컷 / 2페이지 12컷) 너무 성급해 목표 7컷이 4컷으로 밀려 본문이 잘렸습니다.

### 3-7b) 같은 본문でも 배분을 흔드는 법 — `--vary` · 수다장이 모드

[2026-09-09] 배분은 원래 **완전 결정론**입니다(같은 입력 → 같은 컷 수, 같은 페이지 구성, 같은 표정). 재현성을 위해 그렇게 만들어 두었는데, 그래서 같은 원고를 두 번 뽑으면 화면이 똑같았습니다.

| 스위치 | 동작 |
|---|---|
| `--variation N` | 레이아웃 추첨 시드(`cut:회차:페이지:변동`)·배분 동점 처리·컷 예산(±1컷)에 N을 섞습니다. 같은 N이면 다시 결정론입니다 |
| `--vary` | 이번 실행의 N을 시계에서 뽑고 화면에 찍습니다 → `컷 배분 변동 : 4137 — 같은 배분을 고정이면 --variation 4137` |
| 기본값 0 | 예전과 동일(재현). CI/회귀 테스트는 이 값을 씁니다 |

**수다장이 모드(`--chatty`)** — 모든 컷의 아래에 설명(지문)이 붙습니다.

- 컷 스크립트 프롬프트에 "모든 컷에 `caption_ko`를 쓴다, 서술할 사건의 진전이 없는 컷이라도 그녀의 **행동·표정을 한 문장**으로 묘사한다"가 추가됩니다.
- 그래도 비어 남는 컷은 코드 쪽에서 **LLM에 한 번 더 부탁**해 채웁니다(그 컷의 pose·감정·화면 대사·장면 본문을 재료로 한 컷 한 문장). 응답이 깨진 컷만 장면 본문의 남은 문장 → 짧은 기본 문장 순으로 메웁니다(`notes`에 어느 경로로 채웠 남습니다).
- 설명 박스는 원래대로 컷 **하단**에 붙고, 글자가 많으면 폭이 넓어지고 폰트가 줄어 잘리지 않습니다(3-8 참조).

### 3-8) 컷 화면 문법 — 설명 박스 · 말풍선 · 속마음 풍선 · 의성어

한 컷의 화면 텍스트는 아래 **세 가지 중 하나**입니다. 무엇을 쓸지는 컷 대본 LLM이 본문에서 판단합니다.

| # | 화면 요소 | 발화 | 화면 그림 |
|---|---|---|---|
| 1 | **설명**(지문) | 작가 | 컷 **하단 왼쪽** 흰 박스 + 검은 테두리 + 검은 글씨 — **글자가 전부 보일 만큼만**(빈 공간으로 컷을 채우지 않습니다) |
| 2 | **대사** | 등장인물 | 만화 **말풍선 = 직사각형** (폭은 컷의 **20%** · 세로로 길게 · 아주 작은 삼각 꼬리 · 주인공=왼쪽 위, 상대방=오른쪽 위) |
| 2 | **속마음** | 주인공 | 만화 **속마음 풍선 = 타원** (폭은 컷의 20% · 세로는 그 폭에 글자를 넣는 데 필요한 만큼 + 작은 물방울 3개) |
| 3 | **설명 + 대사** | 둘 다 | **이벤트 컷** (회당 2~4컷만 권장 · 설명을 더 좁게 잡아 대화 자리를 남김) |
| 4 | **감정 표시** | 대사의 감정 | 풍선 곁의 작은 이모티콘 — 분노/놀람/땀/하트/음영/반짝/물음, **감정마다 색이 다름** |

- **설명 박스 폭(2026-09-09)**: 길이가 길면 컷 폭을 따라 자랍니다 — 평범 컷 **80%**까지, 대사가 있는 컷 **62%**까지, **★지문은 컷 폭 전체**를 씁니다(넓어야 줄 수가 줄어 글자가 안 잘립니다). 짧은 지문은 측정한 글자 폭만큼만 남긴다(컷을 빈 박스로 안 채웁니다).
- **풍선은 가로 20% · 세로로 길게(2026-09-09)**: 말풍선이 얼굴을 덮지 않게 폭을 컷 폭의 20%로 고정하고, 대사를 그 폭에 맞춰 접어 세로로 늘립니다(글자 크기도 줄여서) · 속마음 타원의 세로는 '그 폭에 글자를 넣는 데 필요한 최소'만 씁니다(세로가 컷 높이의 36%를 넘으면 폭을 28%까지 넓혀 줄 수를 줄입니다)
- **설명은 자르지 않습니다(2026-09-09)**: 지문은 길이로 잘지 않고(옛 `…` 토막 마감 폐지), 길면 박스가 자라고 그래도 안 들어가면 **글자 크기를 20px → 최소 11px까지 줄여** 다 담습니다. 실측: 132자=20px·6줄, 600자=19px·32줄, 3,000자=11px·90줄(3단 세로컷 기준, 전부 잘림 없음)
- 긴 대사는 **'…'로 버리지 않고 풍선 두 개에 나눠** 담습니다(끊는 곳은 문장부호·조사 앞)
- 화면에 화자 이름은 찍지 않습니다. 화면에 화자 이름은 찍지 않고 풍선 꼬리만 방향을 가리킵니다.
- 의성어/의태어(`sfx`)는 대형 흰 글씨 + 검은 윤곽으로 살짝 기운 각도로 들어갑니다.
- 모든 요소는 컷 안에서만 쓰이고 서로 겹치지 않으며, 배치는 **결정론**입니다(같은 입력 → 같은 자리).
- **풍선 자리**(화자별로 고정입니다):

  | 화자(`lines[].who`) | 풍선 자리 (2개일 때) | 꼬리 방향 |
  |---|---|---|
  | 주인공 | **왼쪽 위 → 왼쪽 아래** | 컷 중앙 쪽 (아주 짧게) |
  | 상대방 | **오른쪽 위 → 오른쪽 아래** | 컷 오른쪽 끝 (아주 짧게) |

- 컷 경계는 **굵은 검정선만** 그려집니다. 선과 그림 사이 흰 여백, 선 바깥의 옅은 회색 이중선은 없습니다(이웃한 컷과는 검은 선이 맞붙어 하나의 굵은 경계가 됩니다).
- 컷 스크립트(`episode_NN_script.json`)의 컷 필드: `caption_ko`(설명), `lines`(`{kind, who, text, emo}` ≤2 · `kind`는 `speech`/`thought` · `emo`는 `anger|surprise|sweat|heart|gloom|sparkle|question`), `sfx`(의성어).
  옛 `dialog: ["이름: 대사", …]` 출력도 그대로 받습니다(화자 자동 분리, `(...)`는 속마음으로 봅니다). `emo`를 비우면 대사 분위기에서 자동으로 추정합니다.
- 감정 표시가 필요 없으시면 `--no-emo-marks`를 붙여 주세요(풍선 자리·크기는 그대로입니다).

**★ 화면 문법이 강제되는 슬롯** — `data/cut.yaml`의 `tier.role`로 정하고, 코드는 role이 없어도 **각 페이지(=기승전결 막)의 첫 슬롯**을 자동으로 `summary`로 봅니다.

| 역할 | 어디에 | 화면 | 끄는 플래그 |
|---|---|---|---|
★ 큰 지문은 아래 **세 곳에만** 들어갑니다 (10회작 기준: 프롤로그 1 + 회차 앞 10 + 에필로그 1 = **12개**).

| 역할 | 어디에 | 화면 | 끄는 플래그 |
|---|---|---|---|
| **★프롤로그** | 회차집의 **첫 회차** 맨 앞 1컷 | 인물 없이 **배경만** + 작품의 문을 여는 큰 지문 | `--no-prologue` |
| **★회차 도입 요약** | **각 회차의 첫 컷** (회차당 1개) | 인물 없이 **배경만** + 그 회차 상황의 큰 지문 | `--no-summary-cuts` |
| **★에필로그** | **마지막 회차 끝** 1페이지 (전폭 **1칸**) | 컷을 **반투명**으로 처리하고 그 위에 여운의 큰 지문(대사 없음) | `--no-epilogue` |

- 중간 컷에는 ★ 박스가 붙지 않습니다(예전엔 기승전결 페이지마다 붙어 화면이 박스로 뒤덮였습니다).
- ★ 지문은 글자를 **1.25배** 크게 쓸 뿐 **컷 면적의 70%를 넘기지 않고**, 본문의 마지막 장면을 그대로 옮겨 적지도 않습니다(에필로그는 사건 *이후*의 여운).

```
# 에필로그 템플릿(data/cut.yaml → epilogue_aftermath) — tier에 role을 적으면 그 행이 ★슬롯이 됩니다
- id: epilogue_aftermath
  epilogue: true            # 이 템플릿은 일반 기승전결 페이지로 뽑히지 않고 회차 끝에 한 장 더 붙습니다
  tier_details:
    - tier: 1
      shares: [1.0]
      height: 0.5
      role: "epilogue"
```

#### 컷 크롭은 얼굴 중심 — `--no-face-crop`으로 옛 방식 회귀

렌더는 세로로 긴(1024x1344) 이미지로 나오고, 컷 슬롯은 가로로 넓은 것이 많습니다. 예전에는 비율을 맞추며 **세로 가운데**를 잘랐는데, 그러면 인물의 얼굴이 위쪽이라 통째로 잘려 나갔습니다. 그래서 크롭 창을 얼굴 위치로 잡습니다.

| 상황 | 동작 | 실측(얼굴이 컷 안에 통째로 남는 비율) |
|---|---|---|
| 얼굴 검출 성공 | 얼굴이 컷 세로의 30% 부근에 오게 잡고, 얼굴 높이가 컷의 40%가 될 때까지 살짝 당깁니다(상한 1.35x) | 전폭 행 75% → **98%**, 2단 전폭 69% → **95%** |
| 검출 실패 / OpenCV 없음 | 원본 **위에서 8%** 지점부터 자릅니다(배율은 올리지 않음) | 전폭 행 51% → **79%**, 2단 전폭 39% → **70%** |
| 세로 슬롯(잘릴 여지가 원본 높이의 10% 미만) | 예전처럼 가운데 | 100% 그대로(변경 없음) |

- 얼굴 검출은 OpenCV의 YuNet(DNN)을 씁니다. 애니메이션 얼굴은 학습 분포 밖이라 **우리 원본 150장에서 검출률 36%**(score≥0.5)에 그쳤습니다 — 그래서 검출은 보조 신호이고, 기본은 위 추정치입니다. OpenCV가 없어도 결과에 큰 차이는 없습니다.
- 켜는 방법: `pip install opencv-python-headless` 후 `venv/bin/python run_comic.py --get-face-model`(모델 227KB를 `data/models/`에 받습니다).
- 끄는 방법: `--no-face-crop`(세로 가운데 자르기 회귀). 상수는 `comic_page_merge.py`의 `FACE_CROP_TOP / FACE_H_TARGET / FACE_ZOOM_MAX`입니다.
- 참고: '위에서 20% 지점부터 자르기'도 시험해 봤으나(얼굴이 위쪽에 있다는 발상) 실측 55%로, 8% 지점(79%)에 밀렸습니다. 배율을 올리는 것도 위치를 모른 채 하면 창이 좁아져 오히려 잘렸습니다.

#### 페이지 템플릿 DB (`data/cut.yaml`) — 34종, 회차 안에서 반복하지 않습니다

[2026-09-09] 주신 20종(구 `test/cut_new.yaml` — 병합 후 정리)을 기존 14종에 더해 **34종**이 되었습니다.
`shares`(행 안 폭), `height`(행 높이), `center`(중앙 정렬), `role`(★화면 문법), `epilogue`(전용 페이지)를 같은 방식으로 읽습니다.

| 상황 | 선택지 | 설명 |
|---|---|---|
| 기 | 7종 | 도입 — 전폭 1컷으로 화면을 벌리는 것이 먼저 뽑힙니다 |
| 승 | 18종 | 전개 — 티키타카·질투·우연한 접촉·나뉜 화면 등 |
| 전 | 22종 | 전환 — 급정거, 오해가 터지는 컷, 손끝 클로즈업 등 |
| 결 | 11종 | 마무리 — 여운·배웅·빈 풍경 |
| (여운) | 2종 | `epilogue: true` — 일반 페이지로 뽑히지 않고 마지막 회차 끝에만 |

- **용도가 같은 페이지でも 다른 컷을 씁니다.** 페이지마다 독립 추첨이 아니라 **회차 안 재사용 추첨**입니다(12페이지 회차 → 12장 전부 다른 템플릿). 풀을 다 쓰는 초장편에서는 직전 페이지 것만 피하고 재사용합니다.
- **회차의 첫 페이지**는 첫 행이 전폭 1컷인 템플릿을 우대합니다(도입부가 좁은 3단 세로로 시작하던 증상의 방지책). 여운 페이지도 처음부터 전폭 1칸입니다.
- 템플릿 설명의 소품·장소는 **예시로만** 씁니다. 컷 스크립트 프롬프트에 "지킬 것은 분할 비율·크기·순서뿐, 본문에 없는 물건(우산·음식·벽 등)은 같은 크기의 다른 행동으로 바꾸세요"가 함께 전달됩니다.
**하나로 고정도 됩니다** — `--template`

```bash
venv/bin/python run_comic.py --list-templates                    # 34종 구경 (id · 단수 · 컷/페이지 · 기승전결)
venv/bin/python run_comic.py --episode inputs/… --template romcom_banter   # 이름 일부 일치도 허용
```

- 1종을 주면 회차의 **모든 페이지가 그 구성**이 됩니다(화면 문법이 회치 전체에서 통일됩니다). 실측: 3페이지 전부 `romcom_banter_6panels` → 컷 수 6+6+6(+여운 1) = 19컷.
- 여러 종을 쉼표로 주면 페이지마다 **순서대로 회전**합니다(고정된 격식을 반복하되 단조롭지는 않게).
- 고정하면 기승전결별 선택지 필터 · 회차 안 재사용 금지 · 전폭 비중 상한(`--wide-share`)을 모두 내려놓습니다(의도가 통일だから). 컷 수는 `페이지 수 × 슬롯 수`가 되고, 목표 컷 수에 닿을 때까지 페이지를 늘립니다(초과가 부족보다 낫다는 기존 판단 그대로).
- ★ 프롤로그/에필로그 페이지는 전용 규칙을 그대로 따릅니다(고정 템플릿 풀에서 제외되어 있습니다). 없는 id를 주면 `--list-templates`를 안내하며 종료합니다.
- 직접 늘리실 때: `data/cut.yaml`에 `- id: …`로 추가하면 되고, `situations`는 `기/승/전/결` 어휘를 써 주세요. 행의 `shares` 합은 1.0(`center: true`인 중앙 슬롯만 예외)입니다.

#### 화면 문법 폰트 (무료 · OFL) — `--get-fonts` 한 번

| 용도 | 폰트 (앞이 1순위, 뒤는 후보) | 라이선스 |
|---|---|---|
| 설명(지문) | Gowun Batang (세리프) | OFL 1.1 |
| 대사(말풍선) | **Poor Story** → Jua → Gaegu | OFL 1.1 |
| 속마음 | **Gaegu** (손글씨 붓체) → Do Hyeon | OFL 1.1 |
| 의성어 | Black Han Sans → Gaegu Bold | OFL 1.1 |

> 말풍선이 `□`로 깨지는 일이 있습니다 — 한글 폰트는 `…`(U+2026) 같은 기호가 빠져서요.
> 그래서 렌더 단계에서 **글자마다 그 폰트가 그릴 수 있는지 보고**, 못 그리면 그리는 폰트로 바꿔 그립니다
> (`font_can_draw`/`glyph_fallback`). 기본 대화 폰트 Poor Story는 실제 회차 대사에 쓰인 문자 267종을
> 전부 그리는 것으로 검사 통과했습니다. 후보로 준 Itim은 한글 자체가 없어 테스트에서 탈락했습니다.
>
> 다 합쳐도 전부 **SIL OFL 1.1**(무료·재배포 가능)입니다. `Gaegu Light`는 자동 다운로드 목록에 없습니다
> (3종이면 9MB를 넘는다) — 필요하시면 `data/fonts/Gaegu-Light.ttf`를 직접 넣으시면 후보가 됩니다.
> 속마음을 다른 글씨로 쓰려면: `--font-thought data/fonts/DoHyeon-Regular.ttf`

```
python3 run_comic.py --get-fonts        # data/fonts/로 받습니다(이미 있는 건 건너뜀 · .gitignore 대상)
python3 run_comic.py ... --font-dialog my.ttf --font-narration another.ttf   # 직접 지정도 됩니다
```

- 폰트를 받지 않으셔도 동작합니다 — OS 한글 폰트(Windows 맑은 고딕, Linux Noto CJK, macOS Apple SD고딕)로 렌더됩니다.
- 원하시는 폰트가 있으면 `data/fonts/GowunBatang-Bold.ttf`(설명), `data/fonts/Jua-Regular.ttf`(대사) 같은 **파일명으로 넣기만** 하면 우선 사용됩니다.
- 네 종류 모두 OFL이라 재배포·임베딩이 자유롭습니다. 맑은 고딕 같은 설치본은 **읽기만** 가능하니 repo에 함께 넣지 마세요.

### 3-8b) 표정은 컷마다 — 회차 고정 표정 방지

[2026-09-09] 실측: 뽑아낸 페이지의 표정이 전부 **같은 극단 표정**으로 고정돼 있었습니다. 원인은 화면 문법이 아니라 **태그 배선**이었습니다.

- 회차 태그셋 LLM이 `face`(회차 표정 1개)와 `expressions`(5개)를 정하면 `anima_gen._build_tag_block`이 그것을 **모든 컷**에 붙였습니다(실측 `face_tag = ahegao, wide eyes, tongue out, rolling eyes, flushed face`).
- 컷 스크립트는 컷의 감정(`emo`)을 이미 만들고 있었는데, 프롬프트에 넘기는 자리에 **빈 문자열**이 들어가 있었습니다.

| 지금 동작 | 근거 |
|---|---|
| 컷의 감정(7종) → 표정 태그로 번역해 그 컷의 얼굴로 쓴다 | `comic_gen._EMO_FACE_TAGS` / `_panel_face_emotion`(주인공 풍선 감정 우선, 없으면 화면 텍스트에서 추정) |
| 컷 표정이 있으면 회차 표정은 물러난다 | `anima_gen._build_tag_block` — `[AAA FACE]`/`[AAA EXPRESSION]`에서 회차 톤 제외 |
| 극단 표정(ahegao·heart-shaped pupils·rolling eyes·tongue out 등)은 **클라이맥스 컷에서만** 통과 | `anima_gen._calm_face` — 일상 컷까지 물드는 것을 막는다 |
| 태그셋 LLM 안내: 회차 표정은 평범하게, `expressions` 5개는 서로 다른 감정으로 | `anima_gen` 태그셋 프롬프트 |

| 감정 | 화면 이모티콘 | 렌더 표정 태그 |
|---|---|---|
| anger | ✕ 분노 | `angry, furrowed brow, angry shout` |
| surprise | ! 놀람 | `surprised, wide eyes, open mouth` |
| sweat | 땀 | `uneasy sweat, sweat drop, wavy mouth` |
| heart | 하트 | `lovey, blushing, soft smile` |
| gloom | 음영 | `sad, downcast eyes, wavy mouth` |
| sparkle | 반짝 | `happy, excited, sparkling eyes, open mouth` |
| question | ? | `confused, tilted head, open mouth` |

### 3-8c) 복장은 회차 기준도를 유지 — 컷 1컷의 의류 소실 방지

[2026-09-09] 실측: 이벤트가 아닌 p02 컷이 **옷 없이** 찍혔습니다. 그 컷에 보낸 프롬프트 끝이 이랬습니다.

```
… tattered school uniform, dirty clothes, cleavage, navel, midriff, thighs, … sensitive
```

컷 스크립트가 만든 `clothes`("tattered school uniform, dirty clothes")가 **회차 의상을 완전히 덮어써서**
의류 품목이 사라졌고, 남은 건 `cleavage/navel/midriff/thighs` 같은 부분 노출 태그뿐 → 모델은 벌거벗겼습니다.

| 장치 | 동작 |
|---|---|
| 회차 의상 기준도 병합 | 컷이 옷을 바꿀 때만 `config.clothes`(의상 기준도)를 컷 프롬프트에 함께 보냅니다 — `anima_gen._merge_clothes` |
| 덮어쓰기 vs 덧쓰기 | 컷 `clothes`가 회차와 **같은 품목**(교복+더러움)이면 합치고, **다른 품목**(비키니)이면 갈아입은 컷으로 보고 override만 씁니다 |
| 근거 없는 훼손·과노출 어구 차단 | `explicit` 상한이 아니면 `nude/topless/shirtless/…`를 걷고, 품목이 전부 사라지면 `tattered/ripped/torn/dirty/wet`도 버립니다 — `anima_gen._undress_guard` |
| 컷 스크립트 프롬프트 | "본문에 옷이 바뀌는 장면이 없으면 tattered/nude를 **먼저 제안하지 않는다**" — 근거 없이 넣으면 그 컷이 누드로 그려집니다 |

실측(같은 컷 재조립): `bimbo school uniform, tight clothes, short skirt, tattered school uniform, dirty clothes` — 품목이 살아 있어 교복이 유지됩니다.

### 3-8e) 컷별 연속 상태 시트 — 변한 것만 말하고, 나머지는 그대로

회차 태그를 만드는 LLM은 **본문 전체**를 요약합니다. 그래서 중반에 옷을 갈아입거나 표정이 바뀌면 그 목록이 회차 상수가 되어 **첫 컷부터** 후반 상태가 붙었습니다(실측: 컷 1부터 아헤가오·빔보 복장). 컷마다 시간을 따지는 항목을 LLM이 직접 관리하도록 바꿨습니다.

| # | 항목 | 태그 위치 | 예 |
|---|---|---|---|
| 1 | 표정 | `[AAA FACE]`/`[AAA EXPRESSION]` | `sad` → `ahegao` |
| 2 | 메이크업 | `[AAA MAKEUP]` | `natural makeup` → `heavy makeup, glossy lips` |
| 3 | 몸매·가슴·엉덩이 | `[AAA BODY]` | `petite, medium breasts` → `large breasts, wide hips` |
| 4 | 복장 | `[AAA CLOTHES]` | `school uniform` → `gold bra, gold miniskirt` |
| 5 | 악세사리 | `[AAA ACCESSORIES]` | `heart choker, earrings` |
| + | 머리 상태 | `[AAA HAIR]` | `hair undone, wet hair` |
| + | 몸의 흔적 | `[AAA MARKS]` | `tear trail, sweat, dirt on cheek` |
| + | 소지품 | `[PROPS]` | `umbrella, smartphone, wads of cash` |
| + | 지속 자세 | `[POSTURE]` | `on the ground, kneeling, hands bound` |
| **+10** | 장소 | `[BACKGROUND]` | `shopping street corner` → `rooftop at dusk` |
| **+11** | 시간대·조명 | `[BACKGROUND]` | `night` → `dusk, golden light` |
| **+12** | 배경에 보이는 것 | `[BACKGROUND]` | `crowd, neon signs` → `falling banknotes` |

- **규칙: 언급이 없으면 직전 컷 값 그대로.** LLM은 컷마다 변한 항목만 채우고 나머지는 `""`로 둡니다. 누적 계산은 프로그램이 합니다(`comic_gen.fold_cut_state`) — 그래서 토큰도 적고, LLM이 반복을 빼먹어도 연속성이 안 끊깁니다.
- 장소·시간·배경은 회차 태그가 **회차 전체의 장소 목록**이라 특히 중요합니다. 회치의 첫 컷과 장면이 바뀌는 컷에서 채우도록 프롬프트에 규칙이 있고, 도입 컷이 비워도 **앞으로 처음 명시된 장소**를 소급해 씁니다(다른 장소 배경이 섞이지 않습니다).
- 시작 값은 회차 시작 상태입니다. 회차 요약이 순서를 틀리면(실측: 첫 항목이 중반 의상) **본문을 본 컷 스크립트의 초반 다수값**이 이깁니다(`_head_majority`).
- 컷이 입은 그대로의 복장을 쓰는 컷은 `[AAA EXPOSURE]`를 그대로 유지하고, **옷을 실제로 갈아입은 컷부터** 회차 노출 어구를 떼어 새 옷에 이전 노출 노이즈가 옮지 않게 합니다.
- 극단 표정(로컬 `extreme_face` 어휘)은 여전히 클라이맥스 컷에만 허용되고, 컷이 `ahegao`를 명시해도 일상 컷에서는 걸러집니다.
- **형식은 한 줄 문자열을 권장합니다** — `"state": "face=sad; clothes=school uniform"` (객체도 인식합니다). 26B·Q4 계열은 중첩 객체를 자주 버리기 때문입니다(실측으로 문자열 형식의 준수율이 높았습니다).
- **모자라면 에러로 끝냅니다.** 인물이 나오는 첫 컷이 `face`/`clothes`를 비우면 회차 요약 태그가 대신 들어가 컷 1부터 중반 복장·표정이 붙습니다. 그래서 ① 빈 항목만 작은 호출 1회로 보충 → ② 그래도 첫 컷이 모자라면 **그 컷만** 따로 재확인 → ③ 여전히 모자라면 `PanelScriptError`로 멈추고 안내합니다(렌더는 시작되지 않음). `--no-strict-state`로 경고만 켤 수 있습니다.
  - 실측: 모델이 state를 아예 안 채운 회차에서 첫 컷 보충이 `clothes=shabby Japanese school uniform`을 회수해 통과했습니다.
- 확인: 로그 `EP1 컷 상태 시트: 시작 = 표정 … / 복장 … → 변화가 적힌 컷 N개`, 산출물 `comic/bookNNN/episode_NN_comic.json`의 `panels[i]["_state"]`.
- **상대방도 같은 식으로 유지됩니다.** 두 사람이 한 화면인 컷(`multi`/`pov`)에서는 주인공만 '지금'을 가지고
  상대는 회차 설정(평균)이었다가 고정이라, 상대가 회차 중반의 표정·복장으로 그려졌습니다. 이제 컷 상태에
  `p_face`(표정) · `p_clothes`(복장) · `p_hair` · `p_posture` 를 함께 적습니다(예: `p_face=angry; p_clothes=white shirt`).
  비우면 직전 컷이 유지되고, 상대에게만 `[BBB FACE]` / `[BBB CLOTHES]` / `[BBB ACCESSORIES]` / `[BBB MARKS]` /
  `[BBB PROPS]` / `[BBB POSTURE]` 으로 붙습니다. 한글·일본어로 온 값은 이 단계에서 버립니다(최종 프롬프트에서 파기되는 값).

### 3-8d) 이름 고정 — `#캐릭터 태그#`가 이름을 빼앗지 못하게

[2026-09-09] 실측: 시트에 `#Kirisaki Chitoge from Nisekoi#`를 넣었더니 주인공 이름이 'AMD 소녀'에서 **치토게**로 바뀌었습니다. 추출 LLM이 `#…#` 공식 캐릭터 태그를 이름 필드로 옮겨 적었기 때문입니다. `#태그`는 **그림을 그릴 때의 참조(트리거)**이고, 이름은 화면 지문·대사에 쓰이는 별개의 값입니다.

| 고정 방법 | 예 | 우선순위 |
|---|---|---|
| CLI | `--name "AMD 소녀" --name2 "카미유 렌"` | **가장 강함** (맨 나중에 적용) |
| 환경변수 | `COMIC_PIN_NAME="AMD 소녀" COMIC_PIN_NAME2="카미유 렌"` | 그 다음 |
| 비움 | (기본) | 추출 LLM이 본문에서 찾은 이름 사용 |

- 고정해도 `#캐릭터 태그#`는 그대로 모든 컷 프롬프트에 강제 주입됩니다(그림 참조는 유지, 이름만 고정).
- 추출 프롬프트 규칙도 보강했습니다 — 이름 필드에는 **본문에서 불리는 한국어 호칭**만 쓰고 `#태그`의 영문 이름·작품 제목을 옮기지 않습니다. 고정값이 있으면 그 이름을 쓰라고 따로 지시합니다.
- 확인: 실행 첫 줄의 `이름 고정 : 주인공 '…' · 상대방 '…'` (고정하지 않으면 `(추출이 정한 이름)`이라고 뜨고, `log/comic_input.log`의 `config 주입: {이름}/…` 줄에서 실제로 들어간 값을 볼 수 있습니다).

### 3-9) 로컬 전용 입력 — 단편 생성기 `progress/` 포맷 (`--special`)

`llm_shortnovel_generator_gui`가 `progress/`에 떨어뜨리는 산출물을 **변환 없이** 바로 받으실 수 있습니다.

```
progress/
  ep03_2218f2f3797744fe.txt                  ← 본문(기승전결 · [ACTION]/[TALK]/[INNER] · [장소/복장] 메타)
  character_sheet_ep03_2218f2f3797744fe.json  ← 회차별 캐릭터 시트(타락 진행에 따라 바뀝니다)
  progress_2218f2f3797744fe.json             ← 총 회차 수 등 진행 정보
```

```bash
# 회차 한 화(시트는 같은 디렉터리에서 자동 연결) — 렌더 없이 확인
python3 run_comic.py --special --episode ~/progress --ep 3 --dry-run --preview 2

# 전 회차(10화)를 한 권으로 — comic/book001/episode_01…10_pageXX.png
python3 run_comic.py --special --all-eps --episode ~/progress \
                     --plot-hash 2218f2f3797744fe --book 1 --safety safe --start-llm
```

`--special`은 **입력 경계의 스위치**입니다. 코어의 입력 계약은 여전히 *두 평문*이고,
번역은 `novel_progress.py`가 전담합니다(선택 import — 코어 import 그래프에 들어 있지 않습니다).

| 어댑터가 하는 일 | 왜 필요한지 |
|---|---|
| 머리(`=== Episode 1 ===`, `# 주인공 (…)`)·꼬리(`--- 캐릭터 시트 ---`) 블록 제거 | 그대로 두면 회차 메타가 매 컷 스크립트 호출에 반복 주입되어 예산을 잡아먹습니다 |
| `#####` 구분선 제거 | 이 repo의 마크다운 정리는 `# 다음 공백`만 헤딩으로 봐서 단독 `#####`가 본문에 남습니다 |
| `[장소/상황/시간/복장]` 메타 라인을 1막 지문으로 보존 | 원작에서 정보량 높은 복장·장소 서술입니다(1막 앞에서 잘려 나갔었습니다) |
| `[TALK]`→`이름: 대사`, `[INNER]`→`(속마음) …` | 원작에는 화자 표기가 없어 컷의 화자가 뒤바뀝니다 |
| **기승전결 앵커를 본문에서 직접 확보** | 지금까지는 4줄 앵커를 LLM이 一字不사본으로 베껴와야 했고, 한 글자만 어긋나도 4막 분할이 글자수 균등 분할로 후퇴했습니다 |
| 시트 JSON → 평문 시트(LLM 지문) | 키 이름이 회차마다 달라서(ep01은 한글 키, ep02~는 영문 키) 별칭 테이블로 함께 받습니다. 모르는 키는 `기타`로 남깁니다 |
| 시트 JSON → config **우선**주입(이름·성별·머리/눈/피부·표정·몸매) | 10화 동안 캐릭터가 갈라지지 않습니다. `breasts_size: "huge_breasts"` 같은 문자열 태그도 `body_shape`로 구제됩니다 |

여기까지가 LLM에게 **남는 일**입니다: 가이드(기승전결 요약) · `$행동 키워드` · 수위 ·
**한글 복장 → 영문 태그** 번역(`"…실크 슬립 원피스"` → `silky slip dress`). Anima는 영문 태그만
알기 때문에 `clothes`만은 시트 JSON보다 LLM 번역을 씁니다.

주의 세 가지:
- 시트 JSON이 없으면 본문 꼬리의 `--- 캐릭터 시트 ---`를 쓰고, 로그에 알려 드립니다.
- 회차 수는 `epNN_해시.txt` 기준입니다(실제 샘플에서 시트가 한 개 더 남아 있어도 본문 없는 화는 만들지 않습니다).
- 원작이 explicit해도 렌더는 청년향 정책입니다. `--safety safe`로 최종 프롬프트 수위를 고정하실 것을 권합니다.

동봉된 테스트 입력으로도 확인하실 수 있습니다(`inputs/ep90_deadbeef.txt` + 시트 JSON 두 종).

```bash
python3 run_comic.py --special --episode inputs --plot-hash deadbeef --all-eps --book 999 --dry-run
python3 selftest.py            # ⑨ 항목이 이 포맷을 검사합니다
```

### 3-10) 산출물

```
comic/bookNNN/episode_NN_pageXX.png        최종 페이지 (1024x1454 고정)
comic/bookNNN/episode_NN_pageXX_thumb.jpg  결과 확인용 축약본(1/4)
comic/bookNNN/episode_NN_comic.json        base_seed/seeds/컷/페이지 계획(재현성 키)
comic/bookNNN/episode_NN_script.json       dry-run 컷 스크립트
image/*.png                                컷 원본(렌더)
log/comic_gen.log, log/anima_gen.log, log/tag_out.txt   최종 프롬프트 기록(append)
```

---

## 4. 메모리 규칙 (16GB VRAM + 32GB RAM)

이 항목은 꼭 지켜져야 결과가 안전하게 나옵니다. 프로그램이 대신 처리하지만, 원인은 알고 계시면 편합니다.

1. **렌더 전에 LLM은 반드시 내려갑니다.** 컷 프롬프트(POV/multi 가이드는 LLM 호출)를 렌더 루프 **밖**에서
   전부 조립한 뒤 `keep_alive=0` → `ollama ps`가 비워진 것을 확인하고 렌더를 시작합니다.
   LLM이 렌더 중에 다시 올라가면 ComfyUI 출력이 NaN이 되어 **모든 컷이 검정 PNG(≈8KB)** 로 찍힙니다.
2. `run_comic.py`는 성공/실패/dry-run 어느 경로로 끝나도 `finally`에서 LLM을 반납합니다.
3. 종료 시 **자기가 띄운 ollama 서버**까지 내립니다(Linux process group / Windows `taskkill /T`).
   `--keep-llm`을 쓰시면 서버가 유지됩니다.
4. 같은 순간에 LLM과 ComfyUI가 동시에 뜨지 않도록, 회차를 연속 실행하실 때는 위 2·3에 맡기시면 됩니다.

---

## 5. 파일 지도

```
run_comic.py          런처(입력→추출→주입→렌더→합성, finally LLM 반납, pre-flight) **+ 모든 OS 분기**
run_ollama.sh         Linux/macOS 1줄 래퍼(`run_comic.py --start-llm …`)
run_ollama_win.bat    Windows 1줄 래퍼(더블클릭 가능, PYTHONUTF8 강제)
comic_input.py        평문 2종 → LLM 구조화 → config 주입 (+컨텍스트 예산/장면 분할/추출 map-reduce 병합)
novel_progress.py     [선택 어댑터] local 전용 progress/ 포맷 → 두 평문 + 막 앵커 + 시트 우선값 (--special)
comic_gen.py          본문→컷 수→페이지 계획, 장면별 컷 스크립트(설명/풍선/의성어 + ★요약·에필로그), 프롬프트 조립/렌더, JSON 복구
comic_page_merge.py   흰 프레임+검은 선 + 화면 문법(설명 박스/말풍선/속마음/의성어) 합성(행 비율 h_share 지원)
anima_gen.py          EP 태그 LLM 생성 + 태그블록/헤더 + ComfyUI 클라이언트
openAPI_control.py    LLM 클라이언트(ollama shim/router), 재시도, unload 3경로
config.py             전역 상태(plot.json, data/episode_setup.json)
LORA.md             선택 가능한 LoRA·UNet 안내(화풍을 고르실 때만 보는 문서 — 활성 키만 수록)
llm_server.py         [선택] ollama 대체 전용 LLM 서버
selftest.py           자가 점검 — 항목 수는 실행 결과에 출력됩니다(배송되는 inputs/ 샘플만 사용)
data/cut.yaml         페이지 템플릿 34종(기승전결, tier shares=폭, tier height=행 높이 예: climax_impact 4:6, **tier role=★서두 요약/에필로그**)
data/fonts/           [자동 다운로드] 화면 문법 폰트(OFL) — `--get-fonts`로 받습니다(.gitignore 대상)
data_comfyui/         워크플로 json · actions.yaml · angle.txt · prompt_pov.md · prompt_multi.md
inputs/               샘플(ep01.txt + sheet01.txt, 그리고 `--special` 검증용 ep90/ep91_deadbeef) — selftest가 읽는 입력도 이것뿐입니다
input_test/           [로컬 전용] 개인 검증 입력 — .gitignore라 배송에는 없습니다
order/                설계 메모(standalone 포크 흐름/추가 노트/Windows ollama)
```

---

## 6. 배송 전 정리

```bash
rm -rf venv __pycache__ image/* log/*
rm -rf comic/_demo comic/_attempt1          # 실험 산출물
python3 selftest.py                          # 빈 환경에서도 통과해야 완결입니다
```

`gguf/`(16GB)은 포함 여부와 무관하게 `plot.json: ollama_gguf`만 맞춰 주시면 됩니다(셸 env가 아닙니다).
Windows로 배포하실 경우 `venv`를 받는 쪽에서 `python -m venv venv` + `venv\Scripts\pip install -r requirements.txt`로
다시 만들어 주세요(`venv/bin`과 `venv\Scripts`는 호환되지 않습니다).

---

읽어 주셔서 감사합니다. 궁금한 점이 있으시면 `log/comic_gen.log`와 `python3 selftest.py` 결과를 함께 봐 주시면
원인을 금방 찾을 수 있습니다. 편하게 다시 불러 주세요.
