# llm_comic_gen 독립 환경 구축 + EP1 테스트 (2026-09-07)

- 대상: `/run/media/XxXxX/AIDATA/AI/LLM/llm_comic_gen` (원본 repo에서 comic 서브셋을 떼어 낸 fork)
- 요구: 입력 = ① 에피소드 ② 캐릭터 시트(평문, LLM이 구조화 필드로 추출) ③ 페이지당 컷 수 / 출력 = 만화

## 1. copied / deleted

**복사(그대로 재사용, 코드 변경 없음)**

| 파일 | 이유 |
|---|---|
| `comic_page_merge.py` | 페이지 합성(흰 프레임/검은 선/wide 플레이트) — 외부 의존 0 |
| `comic_gen.py` | 컷 스크립트/프롬프트/렌더/합성 흐름 |
| `anima_gen.py` | `_build_tag_block`, `_build_simple_prompt_header`, `_parse_action_entry`, `comfyui_run_anima`, `init_anima_tags`, `_comfyui_output_dirs` |
| `rp_visual_tags.py`, `character_setup.py` | 결정론 태그 풀(anima_gen import 체인 포함) |
| `openAPI_control.py` | OpenAI 클라이언트/재시도/unload/text 엔드포인트 |
| `config.py` | 전역 상태 저장소(plot.json, data/episode_setup.json을 import 때 읽음) |
| `data_comfyui/*` | actions.yaml(포즈 DB), angle.txt, anima_spectrum_July11.json(워크플로), prompt1/2.md, 외모 태그 리스트류 |
| `order/additional.md` | `_load_additional_tags()`가 읽는 상황별 NSFW 태그 |
| `plot.json` | 엔드포인트/LoRA/comfyuidir (comfyuidir를 실 설치 경로로 교체) |

**제외(독립성 확보)** — 원본 repo에 남아 있는 것들:
`llm_novel_gui_textual.py`, `llm_novel_gui_func.py`, `full_episode_gen.py`, `plot_gen.py`, `theme_gen_auto.py`,
`rp.py`, `run_main.sh`, `merged/`, `result/`, `progress/`, `data/config_export*.yaml`, `util/`, `comic/`(결과물)
→ `anima_gen`이 이들을 import 하지 않으므로(확인: `character_setup`, `rp_visual_tags`, `config`, `openAPI_control` 뿐)
그대로 두면 ImportError다. `progress/`·`merged/`는 **없어도 동작**한다:
`_load_episode_from_progress` / `_load_character_sheet_from_progress`는 `config.plot_hash`가 비면 즉시 폴백하고,
`comic_gen._current_book_no()`는 `merged/`가 없으면 1(또는 `config.comic_book_num`)을 쓴다.

## 2. 새로 만든 층 (평문 입력)

```
comic_input.py   평문 2종 → gemma 추출(JSON) → config 주입
run_comic.py     CLI 런처(프리플라이트/ComfyUI 기동/렌더/썸네일/결과 요약)
selftest.py      LLM·ComfyUI·네트워크 0회 자체 점검 67항목
data/episode_setup.json  {"total_episodes": 1}   ← config 배열 크기 결정(import 때)
inputs/ep01.txt · inputs/sheet01.txt
```

`comic_input.extract()`가 뽑는 것 (호출 1회, textLLM=gemma-4-31B, temperature 0.2):

| 필드 | 형태 |소비자|
|---|---|---|
| protagonist 10필드 | 외모·복장은 **소문자 영문 태그**, name/job은 한국어 | `init_anima_tags` 필수 검증 + `_build_tag_block` |
| breasts_size / hip_size | -1~5 (클램프) | `rp_visual_tags.get_visual_tags/get_body_anatomy_tags` |
| partner name/sex/clothes | 한국어 이름 + 영문 옷 태그 | 파트너 태그/대사 화자명 |
| guides.protagonist 4줄 | 기승전결 한국어 1문장 | `comic_gen._guides_for` → 컷 순서 |
| actions ≤4 | **2~6글자 명사구**(조사구/서술문 기각) | `$키워드` → pose 주입 |
| rating | safe/sensitive/nsfw/explicit | `config.review_safety[ep]`(비우면 자동) |

`run_comic.py` 흐름: 입력 읽기 → 추출 → config 주입 → 프리플라이트(LLM 4종/ComfyUI/출력 후보)
→ `init_anima_tags` → `comic_gen.comic_gen_episode`(컷 생성+렌더+합성) → 썸네일.

## 3. EP1 실측

```
python3 run_comic.py --episode inputs/ep01.txt --sheet inputs/sheet01.txt --panels-per-page 5
→ 359.3s / 컷 8장(face 4, action 4 / wide 3) / 페이지 2장 / base_seed 1924812104
```
- 평문 시트(877B)→추출: `오카다 유즈키/female`, `light brown hair`, `police uniform, utility belt`,
  `loli, child`, 상대방 `호죠 소타/male`, 가이드 4줄, 행동 `['터치','밀착']`, rating=nsfw
- wide: 컷 1·5·7 → 1360x1024(1366 지정, ComfyUI 8배수 보정), 나머지 1024x1344
- pose에 남은 한글 0건(`$키워드`도 정적 사전으로 영문화해 주입)
- 결과: `comic/book001/episode_01_page01.png` 1688x4946 / `episode_01_page02.png` 1688x4781
  (+ `*_thumb.jpg`, `episode_01_comic.json`) — 첫 시도는 `comic/_attempt1/` 에 보존

## 4. selftest (LLM 0회)

`python3 selftest.py` → **PASS 67 / FAIL 0**

- 필수 파일 자가 보유 + 원본 repo 절대경로/의존 모듈 없음
- plot.json 14키 + `comfyuidir` 실존 + 워크플로 노드(1016 seed / 122 lora / 46 unet / 86 pos / 87 neg / 91 save / 123 size)
- comic_input: 마크다운 평문화, 관대한 JSON 파싱, 태그 정규화, 가짜 응답으로 extract→config 주입,
  `$키워드` 서술문/조사구 기각, 성별·크기·rating 정규화
- comic_page_merge: 흰 프레임/검은 선/흰 여백 2px/wide 오른쪽 플레이트(픽셀 검증)
- comic_gen: wide→res9, portrait→res5(가짜 큐로 캡처), 왼쪽 배치 태그, 정석 앵글, 컷 텍스트 3블록

## 5. fork 동기화 규칙 (중요)

이 디렉토리는 **fork**다. 원본 repo를 고치면 같이 옮겨야 한다(이번에 실제로 둘 다 고쳤다):

- `$키워드` 주입을 정적 사전(`_KO_STATIC_EN`)으로 영문화 — `comic_gen._repair_panels`
  (한글을 pose에 넣으면 `sanitize_english`가 지워버려 체위 정보가 사라졌다)
- `_KO_STATIC_EN` 확장: 중립 스킨십 어휘 위주로 키를 늘리고, 강한 체위·성행위어 키는 로컬 사전(local_settings.yaml)으로 분리

동기화 대상 파일: `comic_gen.py`, `comic_page_merge.py`, `anima_gen.py`, `openAPI_control.py`, `config.py`
(독자 파일: `comic_input.py`, `run_comic.py`, `selftest.py` — 원본에 없음)

## 6. 남은 점

- LLM 2서버(텍스트=gemma, 태그/컷=qwen)가 모두 떠 있어야 한다. 하나뿐이면 plot.json의 `text_*`를 그 서버로 맞출 것.
- `--total-episodes`는 `data/episode_setup.json`(기본 1)보다 크게 못 준다(config 배열이 import 때 결정된다).
- 입력 시트가 매우 장황하면 추출 temperature=0.2에서도 태그가 흔들린다(같은 입력인데 `hip_size`가 99→5로 클램프된 사례처럼
  모델이 범위를 넘겨온다). 추출 결과를 눈으로 보려면 `--dry-run --preview 1`을 먼저 돌린다.
- ComfyUI는 이 machine의 `/run/media/XxXxX/AIDATA/AI/ComfyUI`에 하드 의존(기동 커맨드 `run_comic.COMFY_*`).

---

## 7. [2026-09-08] 에피소드 전체 반영 (본문 → 컷 수 → 페이지 수)

**증상**: 회차 본문(9천 자)을 넣어도 컷은 최대 24개(4페이지)이고 내용은 "기승전결 4문장"만 반영됐다.

**원인 3중**
1. 컷 스크립트 프롬프트가 본문 원문을 **아예 읽지 않았다** (`grep -rn episode_content` → `comic_gen.py` 0건).
   LLM이 축약한 `config.ep_corruption_guides_map` 4문장만 입력이었다.
2. `plan_pages`가 1~4페이지로 클램프, 템플릿 슬롯 ≤8컷/페이지 → 컷 상한 24. `_repair_panels`가 초과분을 잘랐다.
3. 추출은 본문 12,000자를 호출 1회에 넣으려 했다 — `ollama_num_ctx`(기본 8192)를 넘기면 ollama는 에러 없이
   프롬프트 **앞부분(규칙 블록)을 잘라낸다**. 한국어 실측 ≈1.8자/토큰(run.log: 707자 본문 → 1531토큰).

**바꾼 것**
- **A 본문 주입** — `comic_gen._episode_text()` → `build_panel_script_prompt(episode_text=…)`.
  본문이 보인 회차에만 규칙 15("본문이 최우선 근거, 본문에 없는 사건 금지")를 붙인다.
- **B 컷 수 역산** — `comic_input.target_panels()`(본문 ÷ `--chars-per-panel`, 기본 600) →
  `comic_gen.plan_pages_layout()`이 템플릿 평균 슬롯(≈5.2컷/페이지)로 어림한 페이지 수 ±2를 훓어
  목표에 가장 가까운 구성을 고른다(부족쪽에 1.6배 패널티 = 세분화 선호).
  1~4페이지 클램프 / `MAX_PANELS=10` 절단 / 죽은 상수 `CUT_SPEC_MAX_PANELS`를 제거(기본 무제한, `--max-panels`).
- **C 장면 분할** — `comic_input.split_beats()`가 빈 줄→문장 순으로 장면을 나누고(순서 보존),
  슬롯을 장면 길이에 비례 배분(`allocate`, 큰 나머지 방식). 장면당 LLM 1회 + 직전 컷 캡션·복장을 컨텍스트로 넘긴다.
  패딩은 **장면 안에서** 채운다(맨 뒤로 밀면 page/tier가 통째로 어긋난다).
  추출도 같은 예산(`episode_char_budget()`)으로 창을 나눠 창당 1회 → `_merge_extracts()`가 병합
  (가이드 누적 ≤24줄, 이름·외모는 첫 창 = 캐릭터 일관성, rating 최대, $행동 합집합 ≤6).

**실측** (gemma-4-26B-A4B Q4_K_M, num_ctx 8192, Anima/ComfyUI 실렌더):

| 본문 | 추출 창 | 장면(LLM 호출) | 컷/페이지 | 총 LLM 호출 | dry-run | 본 실행(렌더 포함) |
|---|---|---|---|---|---|---|
| 9,034자 (ep01) | 1 | 3 | 16컷 / 3페이지 | 4 | 56초 | **249초** (16/16 렌더, 검은 컷 0) |
| 27,121자 (ep01×3) | 3 | 8 | 43컷 / 9페이지 | 12 | 141초 | **643초** (43/43 렌더, 검은 컷 0) |

렌더 결과 페이지는 전부 1024×1454 고정, wide 슬롯은 1360×1024로 빠진다. 장면당 호출이 5~6컷으로 작아
26B Q4의 JSON 손상은 보이지 않았고, 미응답도 ep01 실렌더 런에서 2컷(장면1·2 각 1컷)뿐이었다
(→ ‘침묵 컷’으로 채워지고 로그에 `컷 4/5 (미응답 1컷)`로 남는다).

이전은 본문 길이와 무관하게 `--pages ≤ 4` × 슬롯 ≤ 8 = **최대 24컷** 고정이었다.
렌더는 컷 수에 선형으로 늘어난다(컷 1장 = ComfyUI 1회).

**fork 동기화**: A/B/C 중 원본 repo와 공유 파일은 `comic_gen.py`·`config.py`다. 같은 방향이 필요하면
`comic_input`의 `target_panels/split_beats/allocate/episode_char_budget`을 같이 옮길 것.

---

## 8. [2026-09-08] Windows 지원 — 셸 트윈 없이 OS 분기를 Python으로

**의견 대립**: `run_ollama.sh`를 Windows용으로 복제(`run_ollama_win.sh/.bat`)할까?
결론은 **"셸을 나누는 건 괜찮고, 로직을 복사하는 건 안 된다"**. 원본 셸이 실제로 소유한 규칙이
너무 많았기 때문이다 — 모델명 = gguf 파일명 접두, plot.json 임시 전환 + trap 원복, num_ctx 주입,
`/proc/PID/cmdline` 검사로 서버进程만 kill. 이걸 PowerShell/cmd에 재현하면 **세 번째 구현**이 되고,
Git Bash 기준으로는 `/proc` 미보장·`pkill`/`setsid` 부재·`taskkill` 경로 머싱·`cygpath`(gguf 경로를
`/c/...`로 주면 네이티브 python/ollama가 못找)·cp949 콘솔까지 얹힌다.

**이관한 것** (`run_comic.py`가 서버 생명주기를 소유)
- `is_win()` — 기본은 `os.name == "nt"` 자동 감지, `--os win|linux`는 오버라이드 전용.
- `_detach_kwargs()` — POSIX `start_new_session`(=setsid) / Windows `creationflags`.
  **함정**: `start_new_session`는 Windows에서 ValueError 대신 *조용히 무시*된다(CPython이
  `unused_start_new_session`로 받는다) → 안 주면 콘솔 창 ✕로 서버가 같이 죽는다.
- `_comfy_python()`(`venv\Scripts\python.exe` vs `venv/bin/python`), `_comfy_args()`(ck-attention은 Linux 전용),
  `_comfy_env()`(MIOPEN/HIP은 Linux ROCm 전용 → Windows는 `PYTHONUTF8`만), `_repo_path()`(로그/PID를 `log/`로).
- `start_ollama()/stop_ollama()` — 바이너리 탐색(env→PATH→OS별 설치 위치), 전용 HOST,
  `OLLAMA_MODELS` 격리(`OLLAMA_MODELS_DIR`), `log/ollama_serve.pid` 기록, Windows는 `taskkill /T /F`로
  **작업 트리**(자식 llama-server까지) 종료. 우리가 띄운 서버만 내린다.
- 셸은 1줄 래퍼 2개: `run_ollama.sh`, `run_ollama_win.bat`(CRLF, `PYTHONUTF8=1`, `stop`/`plan` 단축).

**plot.json을 더 이상 고치지 않는다**: `COMIC_OLLAMA_ENB=yes|no`, `COMIC_OLLAMA_HOST` env가 plot.json을
오버라이드(`openAPI_control._ollama_enabled/ollama_host`). 예전 방식은 셸 trap이 안 돌면
`ollama_enb=yes`가 파일에 고착됐다.

**폰트**: `comic_page_merge.FONT_CANDIDATES`에 Windows(malgunbd/malgun/gulim/msgothic/yumin)·macOS
(AppleSDGothicNeo)·repo 번들(`data/fonts/`) 후보를 추가하고 `--font`를 열었다. Linux 후보만 있으면
Windows에서 캡션·대사가 전부 □(tofu)로 찍히고, 그건 렌더가 성공해도 결과물이 hỏng다.
pre-flight가 폰트 유무를 찍는다(경고만 하고 중단은 안 한다).

**자가 점검**: selftest ⑧(170 항목) — 래퍼 두께/실행 라인의 로직 잔존 여부, `--os` 오버라이드,
detach 분기, ck-attention/MIOPEN OS 분리, venv probe 우선순위, env 오버라이드 3종, 폰트 후보.
셸 트윈을 다시 만들면 이 체크가 실패로 잡는다.
