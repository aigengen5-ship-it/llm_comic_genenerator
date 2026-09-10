# 동작 FLOW — 평문 → 만화 페이지 (오프라인 리뷰용)

> 갱신: 2026-09-09. 이 문서는 **지금 코드에서 실제로 도는 순서**를 적습니다.
> `파일.py:줄번호`를 같이 적었으니 오프라인에서 그 자리로 바로 jumping 하세요.
> LLM 호출은 **5곳뿐**입니다 — §2 표가 이 문서의 핵심입니다.

---

## 0. 한눈에

```
 inputs/epNN.txt (평문 원고) ┐
 inputs/sheetNN.txt (캐릭터 시트) ┘
        │
        ▼  run_comic.py main (§1)
 [A] 프리플라이트 : config 10필드 · 폰트 · ComfyUI/ollama 포트 확인          run_comic.py:378
        │
        ▼
 [B] 입력 로딩 : 본문 + 시트 (+ special 어댑터)                              comic_input.load_inputs (978)
        │
        ▼
 [C] LLM#1 추출 : 기승전결 가이드 · 막 앵커(segments) · 화면 항목(units)      comic_input._extract_once (790)
        │        └ 항목 = 행동|대사|속마음, 항목 하나 = 컷 하나                comic_input.normalize_units (351)
        ▼
 [D] LLM#2 회차 태그 : face/exposure/background/표정 풀 … (렌더용)           anima_gen.init_anima_tags (1170)
        │
        ▼
 [E] 컷 예산 + 페이지 레이아웃(템플릿 34종, 재추첨)                            comic_gen.plan_pages_layout (283)
        │
        ▼
 [F] 장면 분할 : 항목 앵커를 본문에서 문자열로 찾아 자르기                    comic_input.split_acts_by_units (393)
        │
        ▼
 [G] LLM#3 컷 스크립트 : 장면 1회당 1호출 → 컷 JSON                           comic_gen.request_panel_script (1457)
        │   └ 코드 쪽 보정: ★지문/수다장이/장치 지침/중복 제거/전역 보정
        ▼
 [H] 컷 → 이미지 프롬프트 (결정론 조립 + 예외만 LLM#4/#5)                     comic_gen.build_panel_prompt (2110)
        │      #4 한글 gloss 번역(EP당 1회)                                  comic_gen.request_ko_glossary (1778)
        │      #5 POV/multi 컷 프롬프트 재작성                               comic_gen.py:1999
        ▼
 [I] ComfyUI 렌더 : /prompt POST → output 순회 수집, 컷별 seed               comic_gen.render_panel (2271)
        │
        ▼
 [J] 페이지 합성 : 얼굴 중심 크롭 + 행/열 + 설명/말풍선/속마음/의성어         comic_page_merge.compose_pages (1459)
        │
        ▼
 산출물: comic/bookNNN/episode_NN_pageXX.png · comic/bookNNN/episode_NN_script.json · log/comic_gen.log
```

---

## 1. 진입점 `run_comic.py` (실행 순서 그대로)

| 순서 | 하는 일 | 코드 |
|---|---|---|
| 0 | `runlog.start_run()` — 본 로그 3종 + tag_out을 **초기에 초기화**, `log/error.log`에 실행 구분자 (`--keep-logs`로 유지) |
| 1 | 인자 파싱 + 스위치 배선(모든 `--no-*`는 이 자리에서 `config`에 반영) | `run_comic.py:758~` |
| 2 | `local_settings.yaml` 적용 (우선순위: **CLI > env > local_settings > 코드 기본값**) | `config.apply_local_settings()` |
| 3 | 프리플라이트 화면: config 필수 10필드, 폰트 누락, ComfyUI/ollama 포트, 컷 배분 변동, 수다장이, 이름 고정 | `run_comic.py:378~` |
| 4 | 입력 로딩(본문/시트) | `:469` |
| 5 | **LLM#1 추출** (실패 시 1회 재시도) | `:473`, `:480` |
| 6 | 컷 예산 어림 계산·출력 (`본문 N자 → 컷 예산 M컷`) | `:503` |
| 7 | (렌더 시) ComfyUI 기동 시도 | `:518` |
| 8 | `--dry-run`이면: 태그 초기화 + 컷 스크립트 + 화면 텍스트 미리보기 후 종료 | `:528~` |
| 9 | 렌더 경로: 태그 초기화 → 컷 스크립트 → gloss → `comic_gen_episode`(렌더+합성) | `:529`, `:534`, `:569`, `:592` |

중요한 설계 판단 두 개:
- **프롬프트 조립은 렌더 루프 밖에서 끝낸다** (`comic_gen.py:2311` 부근 주석). 16GB VRAM에서 렌더 도중 17GB 모델이 다시 올라가면 컷 전량이 NaN(검정 이미지)이 됐다.
- LLM 사용 후 `keep_alive=0`으로 ollama에서 내린다 (`openAPI_control`의 release 경로).

---

## 2. LLM 호출 인벤토리 (여기만 외우시면 됩니다)

라우팅: `plot.json`의 `mainLLM` / `textLLM` 필드 → `resolve_text_model()` (`openAPI_control.py:302`). `textLLM`이 없으면 `mainLLM`으로 폴백하고 로그에 `[ERROR][TEXT_MODEL]`을 남깁니다. `num_ctx` 기본 **8192** (`openAPI_control.py:93`, 16GB VRAM + 32GB RAM 기준).

경로는 두 갈래입니다 — 리뷰할 때 혼동하지 마세요.
- **text/plot 경로**: `call_openai_for_text`(`textLLM` 우선) / `call_openai_for_plot`(`mainLLM`) — #1·#3·#4·#5와 수다장이 작문이 여기.
- **ANIMA 경로**: `openAI_response`(`mainLLM`) — #2 태그 생성이 여기. 즉 *렌더 태그는 textLLM이 아니라 mainLLM*이 만듭니다.

#2는 **에피소드 본문 원문을 보내지 않습니다** — 기승전결 가이드(`config.ep_corruption_guides_map`) + `$행동 키워드` + 캐릭터 설정만 갑니다 (`_generate_tags_via_llm` docstring, `anima_gen.py:1036`).

| # | 위치 | 용도 | 호출 | 파라미터 | 출력 | 실패 시 |
|---|---|---|---|---|---|---|
| 1 | `comic_input._extract_once` | 본문 → 기승전결 가이드 + `segments`(막 앵커) + `units`(화면 항목) | EP당 1회(창이 여러 개면 창마다) — 파싱 실패/예외 시 **2회**(2회는 `temperature=0.0`) → 실패하면 **체크포인트 3단**: `merge_extract_cached`(빈 칸만 이어받기) → `fill_missing_extract`(빈 항목만 재확인) → `infer_missing_from_profile`(공식 태그·직업 추론) → `save_extract_checkpoint` |
| 2 | `anima_gen._generate_tags_via_llm` (호출은 `anima_gen.py:1127` → `openAPI_control.openAI_response`, 825) | 회차 렌더 태그 일체 (face / makeup / exposure / parts / body / background / expressions / partner / location / time / safety) | EP당 1회 | `openAI_response` 경로 = plot.json `mainLLM` 해석 모델 (qwen 계열은 `enable_thinking=False`, 그 외 `repeat_penalty 1.15 + top_k 64`, 800s timeout·3회 재시도) | 태그 dict → `config.*_tag`, `config.current_level` | client 없거나 파싱 실패 → **결정론 fallback 태그**(`fb`) |
| 3 | `comic_gen.py:1625` (장면 루프 안) | 장면 본문 → **컷 스크립트 JSON** | **장면당 1회** (장면 = 항목 ≤6개 묶음, 본문 ≤1800자) | `call_openai_for_text`, `reasoning_effort="low"`, `enable_thinking=False` | 컷 배열(pose/camera/position/caption_ko/lines/clothes/climax…) | 장면당 `retry`회 재시도 → 여전히 미달이면 남은 컷을 **침묵 컷**으로 채움(`notes`에 기록) |

**재시도 요약(2026-09-09)** — 트랜스포트 재시도(`max_retries=3`)와 별개로, **JSON 파싱 실패**에도 재시도합니다: 추출 2회(2회는 `temperature=0.0`) · 태그 생성 2회(`_retry` 라벨) · 컷 스크립트 장면당 2회(`retry=2`). 그래도 모자라면 컷 스크립트는 침묵 컷/`PanelScriptError`, 태그는 결정론 fallback.
| 4 | `comic_gen.py:1792` (`request_ko_glossary`) | 컷에 남는 **한글 조각 → 영문 태그** 번역 | EP당 1회 (frag가 있을 때만) | `call_openai_for_plot`, `temperature=0.0`, `repeat_penalty=1.0` | `{"한글": "english tag"}` | gloss 없이 진행 → `sanitize_english`가 남은 한글을 지움 |
| 5 | `comic_gen.py:1999` (`_llm_compose_panel_prompt`) | **POV / multi 컷만** 태그 블록 → 자연어 이미지 프롬프트 재작성 | 해당 컷당 1회 | `call_openai_for_text`, system = `data_comfyui/prompt_pov.md` / `prompt_multi.md` | `##PROMPT##` 사이에 완성 프롬프트 | 없으면 결정론 `flatten_tag_block`으로 조립 (에이전트가 재작성한 것처럼 보이나 실제로는 예외 경로만 LLM) |
| + | `--chatty`일 때만 (`comic_gen.py:1407` → `_fill_chatty_narration`) | 지문 비어 있는 컷의 하단 설명 작문 | EP당 1회 (빈 컷이 있을 때만) | `call_openai_for_text`, `temperature=0.7` | 문자열 배열 | 장면 본문의 남은 문장 → 짧은 기본 문장 순으로 메움 |

### 2-1. 문자 예산 (왜 장면을 쪼개는가)
`num_ctx`를 넘기면 ollama는 **에러 없이 프롬프트 앞부분(규칙 블록)을 버립니다.** 그래서 한 번에 넣는 본문 글자 수를 코드에서 계산합니다.

```
본문 예산 = (num_ctx − EXTRACT_RESERVE_TOKENS) × CHARS_PER_TOKEN
          = (8192 − 2000) × 1.8 ≈ 11,145자        # comic_input.py:43~45, 105
장면 1회 예산 = BEAT_MAX_CHARS = 1,800자            # 컷 스크립트 호출
장면당 항목/컷 상한 = UNITS_PER_CALL_MAX = PANELS_PER_BEAT_MAX = 6
```

### 2-2. LLM에게 시키는 판단 / 코드가 정하는 것
| LLM이 정한다 | 코드가 정한다 |
|---|---|
| 사건의 경계, 항목의 종류(kind), 문장 복사(`at`) | 항목이 본문의 **몇 번째 문자**에서 시작하는지(문자열 일치로 앵커) |
| 컷의 pose 영문 문장, 대사/속마음 내용, 표정 감정 | 컷 **수**, 페이지·행·열 구성, 컷별 장치(행동/대사/속마음), 크롭, 풍선 자리 |
| --item-cuts off일 때만: 사건당 컷 수(1~2) | --item-cuts(기본)에서는 컷 수 = 항목 수, `cuts`는 강제로 1 |

---

## 3. 단계별 상세

### 3.1 [C] 추출 — `comic_input.extract` (807) / `_extract_once` (790)
- 프롬프트는 `에피소드 전문 + 시트 + 규칙 블록`. 본문은 §2-1 예산만큼만 넣습니다.
- 요구 항목: `guides`(기승전결 4문장), `segments`(각 막 첫 문장 복사 — 길면 앞 40자), `units`.
- **항목 1:1 모드**(기본 켬, `config.comic_item_cuts`): `units = [{at: "문장 복사", kind: "행동|대사|속마음", cuts: 1}]`, 항목 상한 24 (`ITEMS_MAX`).
  `--no-item-cuts`면 사건 단위(`cuts` 1~2 허용, 상한 14 = `MAX_UNITS`).
- 이름 고정: `--name/--name2`(또는 local `name/partner_name`, env `COMIC_PIN_NAME`)이 있으면 추출 결과가 그 이름을 강제로 따릅니다. 시트의 `#캐릭터 태그#`는 렌더 전용이고 화면 이름과 분리됩니다.
- 결과는 `config.ep_action_units[EP] = units`로 보관 (`comic_input.py:891`). 디스크 캐시는 없습니다(재실행 시 재호출).

### 3.2 [E] 컷 예산과 페이지 레이아웃 — `plan_pages_layout` (283)
1. 컷 예산 순서: 항목/사건 가중치 합(`target_panels_from_weights`, 440) → 없으면 글자 수(`target_panels`, 455). **항목 저울은 막 분할과 독립**이다 — `ep_beat_segments` 앵커가 깨져 막이 1개라도 `split_acts_by_units([body], units)`로 항목 수를 지킨다(`request_panel_script`의 `else` 분기, `comic_gen.py:1597` 부근). 하한은 `MIN_PANELS_AUTO=6`과 `막당 2컷`.
2. 페이지 수 어림 = 목표 컷 수 ÷ 템플릿 평균 슬롯(4.82) ± 범위(−2 ~ +3).
3. **페이지 수 × 레이아웃 재추첨**(`comic_layout_rolls=10`, `plan_pages(…, salt)`): 목표 컷 수에 가장 가까운 구성을 고르고, **모자란 쪽은 2.2배 벌점**(장면 압축이 컷 여유보다 나쁘다는 판단).
4. 템플릿 34종(`data/cut.yaml`)에서 **회차 안 재사용 추첨**(같은 회차에서 같은 템플릿 중복 금지) + **첫 페이지는 "첫 행만 전폭"인 믹스 템플릿 우대** + 전폭 비중 상한 `comic_wide_share_max=0.5`.
5. **템플릿 고정** (`--template`, `config.comic_templates_pin`): 1종이면 모든 페이지가 그 구성, 여러 종이면 페이지마다 순서 회전. 고정이면 ①·④의 필터(기승전결 선택지·재사용 금지·전폭 상한)와 재추첨(`rolls`)을 모두 내려놓는다. 컷 수 = 페이지 × 슬롯 수. ★ 전용 페이지는 그대로.
6. ★ 규칙: 프롤로그는 회차집 **첫 회차 앞 1컷**, 회차당 도입 요약 **1컷**, 에필로그는 **마지막 회차 끝 1페이지**. 10화작 = 1 + 10 + 1 = **12개**.

재현성: 시드는 `cut:회차:페이지:변동:salt`. `comic_variation=0`(기본)이면 같은 입력 → 같은 배분. `--vary`는 값을 뽑아 화면에 찍고, 그 값으로 `--variation N` 재실행하면 고정됩니다.

### 3.3 [F] 장면 분할 — `split_acts_by_units` (393)
- `at` 문자열을 본문에서 **정확한 복사 일치**로 찾아 자릅니다(모델은 숫자 오프셋을 못 맞춥니다).
- **항목 모드에서는 짧은 조각을 합치지 않는다** — 항목 20개를 붙이면 장면 1개에 눌려 컷 6개가 됐다(실측 727자 원고). 대신 **개수로만 묶는다**: 장면 1개(LLM 호출 1개) = 항목 ≤6, 그래서 20항목 → 장면 4개 `[6,6,6,2]` → 컷 20.
- 사건 모드(`--no-item-cuts`)에서는 기존처럼 짧은 조각(`< MIN_UNIT_CHARS=90`)을 앞 조각에 합치고, 막당 조각이 6개를 넘으면 같은 막 인접 조각부터 합칩니다.
- **합칠 때 컷 수는 합(sum)** — 예전엔 max를 써서 두 사건이 한 컷으로 눌렸습니다(짤림의 원인 중 하나).

### 3.4 [G] 컷 스크립트 — `request_panel_script` (1457), 컷 규칙은 `build_panel_script_prompt` (532)
프롬프트 블록 순서:
1. 회차 정보(EP/총 화수/화자 이름/시트 3종) + 본문 원문(이 장면 것만)
2. `[페이지 레이아웃(cut.yaml)]` — 슬롯마다 폭%/행 높이%/종류(`_layout_block`, 321) + **"소품·장소는 예시, 지킬 것은 분할 비율·크기·순서"** 지침
3. 컷 화면 텍스트 3택 규칙: ① 설명만 ② 대사·속마음만 ③ 설명+대사(큰 이벤트 컷, 회당 2~4)
4. **컷 연속 상태 시트**(`state`, 12종) — 표정/메이크업/몸/복장/악세사리 + 머리·흔적·소지품·지속 자세 + 장소·시간대·배경(12종, `STATE_KEYS`). **변한 항목만** 채우는 델타이고, 누적은 코드가 한다(`fold_cut_state`, `STATE_KEYS`). 시작 값은 `base_cut_state`(회차 시작 상태)이며 회차 요약이 순서를 틀리면 컷 초반 다수값이 이긴다(`_head_majority`).
5. **화면 장치 지침**(`device_hints`) — 코드가 본문에서 판정한 행동/대사/속마음 (`classify_device` 311, `split_for_cuts` 334). ★슬롯은 제외(자기 규칙이 있음).
6. 수다장이 규칙(`--chatty`일 때: 모든 컷에 지문, 사건 진전이 없으면 행동·표정 묘사)
7. 직전 컷 맥락(연속성) + `$행동 키워드` 의무 등장 + 클라이맥스 슬롯(기본 청년향이라 전부 비움)

코드 쪽 보정(호출 후):
- 미달 재시도 → 실패분은 침묵 컷
- `_fill_star_narration` (1431): ★ 슬롯 지문이 비면 그 장면 본문의 첫 문장으로 채움
- `_fill_chatty_narration` (1380): 지문 없는 컷은 **LLM#(보너스)** 작문 → 본문 문장 → 기본 문장
- 전역 보정(1657 부근): 슬롯 메타(page/tier) 부여, 어휘·복장·시선 통일, face/action 분류 재확인
- 정제 통계는 컷마다 찍지 않고 **회차 끝 한 줄**
- **말풍선·속마음 형태**는 두 갈래: 기본은 코드 벡터, `--balloon-style image`는 `data/balloons/` 자산 9슬라이스 합성(`comic_page_merge.paste_balloon_art`). 감정→변형 선택은 `pick_balloon_variant`(같은 페이지 중복 회피, 결정론). 꼬리·생각 물방울은 두 갈래 모두 벡터로 그려 화자 조준을 유지한다(꼬리 자산은 오른쪽 기준으로 하나만 만들고 붙일 때 회전·반전, 속마음 물방울은 원 자산 하나를 3번 축소해 **몸통 바깥**에 배치). 몸통 폭은 컷 20%×2.2 보스트·최소 높이 42%, 글자 안전여백은 자산 알파에서 자동 계산해 9슬라이스 사영 후 적용. 겹침은 알파 최대 병합(`_alpha_max`)으로 반투명이 짙어지지 않는다. 자산 없으면 벡터 폴백.
- **컷 연속 상태**(`STATE_KEYS` 21항목): 컷은 **변한 것만** 적고 나머지는 빈 값 = 직전 컷 유지, `fold_cut_state()`가 접는다. `place/time/background`는 씬이 바뀌는 컷에서 교체되고, 앞 컷이 비면 본문에 먼저 적힌 값을 시작 값으로 소급한다(회차 배경 태그는 회치 전체 장소를 담기 때문).
- **상대방 항목(`p_` 접두사)**: `p_face/p_makeup/p_body/p_clothes/p_accessories/p_hair/p_marks/p_props/p_posture` — 두 사람이 한 화면인 컷(`multi`/`pov`)에서 상대에게도 '지금'을 준다. `anima_gen._build_partner_block(episode, name_b, cut_state)`가 회차 설정보다 먼저 쓰고 빈 항목만 회차 값을 쓴다 → `[BBB FACE] [BBB CLOTHES] [BBB ACCESSORIES] [BBB MARKS] [BBB PROPS] [BBB POSTURE]`.
- **엄격 검사 게이트** `validate_panel_script` → 실패 시 ① `fill_missing_state`(빈 항목만 작은 호출 1회) → ② `fill_first_cut`(첫 컷만 따로, 못 읽으면 `temperature=0.0` 재시도, 답이 회치 중반 이후 상태(`clothes_late`/`face_style_late`)면 그 답을 받지 않음) → ③ 그래도 모자라면 `PanelScriptError` → `run_comic`이 `[오류]` 후 **rc 2**로 종료(렌더 전에 멈춘다). 우회: `--no-strict-state`. 근거는 언제나 **[에피소드 본문]의 이 컷 조각** — 회차 요약으로 빈 칸을 메우지 않는다.

### 3.5 [H] 컷 → 이미지 프롬프트 — `build_panel_prompt` (2110)
- 결정론 경로: 태그 블록(`[AAA FACE]/[AAA CLOTHES]/[BACKGROUND]/[SAFETY]`…) + 정석 뷰 토큰(`ANGLE` 프리셋) + 시선 정책(말풍선이 오른쪽이면 인물은 왼쪽).
- 예외 경로: `camera=pov` 또는 `multi` 컷만 **LLM#5**로 자연어 재작성(`prompt_pov.md` / `prompt_multi.md` 가이드).
- 얼굴 표정: 컷 감정이 있으면 그것을 쓰고, 회차 `face_tag`는 **클라이맥스 컷에만** 적용(일상 컷이 한 표정으로 고정되던 증상 방지). 극단 표정 어휘는 `local_settings.yaml`의 `extreme_face`로만 켜집니다.
- 복장: 컷 레벨 `clothes`는 회차 의상 태그와 **같은 종류면 병합**, 종류가 다르면 교체. 의류어가 하나도 남지 않으면 회차 의상을 되돌립니다(`_undress_guard`).
- 정제: `dedupe_flat`(중복 태그) → `sanitize_english`(한글 → gloss or 제거) → `strip_anatomy_tags`(성기 계열만 제거) → `ensure_char_tags`(#캐릭터 태그 보장 주입).
- 안전: 기본 `safe`(청년향) — 노출 상한 nsfw, 성기 태그는 양쪽에서 제거. `--allow-explicit`(local)에서만 `CLIMAX_VOCAB_EXPLICIT`이 살아납니다.

### 3.6 [I] 렌더 — `render_panel` (2271)
- ComfyUI `http://localhost:8188/prompt` POST (`anima_gen.py:1440`), 결과는 `~/AI/ComfyUI/output` 계열 디렉터리를 순회하며 newest 수집(`_comfyui_output_dirs`).
- 해상도는 **슬롯 화면비에 가장 가까운 것**을 고릅니다(`_res_for_aspect`) — 전폭은 1360x1024 계열, 세로 컷은 1024x1344 계열.
- seed 정책(요구사항): `base_seed = 컷 스크립트 해시` (`_base_seed`, 2212) → `컷 seed = base_seed + 컷 번호`. 같은 스크립트면 같은 이미지가 나옵니다.

### 3.7 [J] 페이지 합성 — `comic_page_merge.compose_pages` (1459)
- 페이지 고정 크기 1024x1454. 행 계획은 `_plan_rows` (859): cut.yaml의 `shares`(행 안 폭)·`height`(행 높이)·`center`를 따르고, spec 밖 컷은 2열로 토막.
- 크롭(`fit_cover`, 770)은 **얼굴 중심**입니다:

  | 경우 | 동작 | 실측(얼굴이 컷 안에 통째로 남는 비율) |
  |---|---|---|
  | YuNet 검출 성공(원본 150장 기준 36%) | 얼굴이 컷 세로의 30% 부근, 얼굴 높이 40%까지 당김(≤1.35x) | 전폭 75→**98%**, 2단 69→**95%** |
  | 검출 실패/OpenCV 없음 | 원본 **위에서 8%** 지점부터(배율 1.0) | 전폭 51→**79%**, 2단 39→**70%** |
  | 세로 여지가 원본 높이의 10% 미만(세로 슬롯) | 가운데 자르기 유지 | 100% 그대로 |

- 화면 문법: 설명 박스는 컷 **하단**, 폭 80%(대사와 공존 시 62%, ★도입 요약은 100%), 줄 제한 없음(글자가 많으면 **폰트가 먼저** 줄어듦, 하한 11px). 말풍선은 컷 폭의 20% 직사각형+꼬리, 속마음은 20% 타원+방울. 의성어·감정 표시(7종)는 컷 위/옆에 얹습니다.
- 에필로그 컷은 반투명(fade) + 큰 지문 1개.

---

## 4. 스위치 지도 (자주 쓰는 것만)

| 스위치 | 효과 | 기본 |
|---|---|---|
| `--dry-run` | 렌더 없이 컷 스크립트·레이아웃·화면 텍스트까지 | off |
| `--pages N` / `--no-cut-yaml` | 페이지 고정 / 템플릿 레이아웃 끄기 | auto |
| `--template ID[,ID]` / `--list-templates` | 페이지 템플릿 고정(1종=통일, n종=회전) / 목록 확인 | 자동 34종 |
| `--no-item-cuts` | 항목 1:1 → 사건 단위 회귀 | 항목 1:1 |
| `--wide-share F` | 전폭 컷 비중 상한 | 0.5 |
| `--vary` / `--variation N` | 컷 배분 변동(레이아웃·동점 처리·예산 ±1컷) | 0 (완전 재현) |
| `--chatty` | 모든 컷 하단에 설명(없으면 행동·표정 묘사) | off |
| `--no-face-crop` / `--get-face-model` | 얼굴 크롭 끄기 / YuNet 모델 수신 | 크롭 켬 |
| `--no-prologue` `--no-summary-cuts` `--no-epilogue` | ★ 3종 개별 OFF | 전부 켬 |
| `--no-emo-marks` | 감정 표시만 OFF | 켬 |
| `--name 렌 --name2 …` | 화면 이름 고정(시트 태그와 분리) | 추출이 정한 이름 |
| `--allow-explicit`(local) | 클라이맥스 어휘·노출 상한 해제 | off (청년향) |

우선순위: **CLI 인자 > 환경변수(`COMIC_*`) > `local_settings.yaml`(gitignore) > 코드 기본값**.
`local_settings.yaml`의 로컬 전용 키: `safety`, `ko_map`, `ko_map_safe`, `counter_alias`, `name`, `partner_name`, `climax_vocab`, `extreme_face`, `nude_words`, LoRA/서빙 경로 등 — 상세은 `README_local.md`(gitignore)에 있습니다.

---

## 5. 결정론 vs 변동 (무엇이 같고 무엇이 다른가)

| 결정론(해시/시드) | 변동 가능 |
|---|---|
| 컷 수·배분: `cut:회차:페이지:변동:salt` | `--variation N` / `--vary` (값을 알면 재현) |
| 컷 표정·각도 선택: `_det_choice(pool, key)` (crc32 — 프로세스마다 달라지는 `hash()` 안 씀) | 같은 변동 값이 섞임 |
| 이미지 seed: `base_seed(컷 스크립트 해시) + 컷 번호` | 스크립트가 바뀌면 이미지도 바뀜 |
| 장면 경계: 본문 문자열 일치 | 원고·추출 결과가 같으면 항상 같음 |

---

## 6. 고장 모드와 방어선

| 증상 | 방어선 | 코드 |
|---|---|---|
| 규칙 블록 잘림(응답만 옴) | 문자 예산 산식, 장면 1800자 상한 | `episode_char_budget` |
| LLM이 JSON을 깨뜨림 | ① 관대한 파서(`json_soft_fix`) ② 객체 단위 구제(`_salvage_objects`) ③ 재시도(추출 2회·태그 2회·컷 스크립트 장면당 2회) ④ 그래도 모자라면 침묵 컷/에러 | `comic_input._extract_once`, `anima_gen._generate_tags_via_llm`, `request_panel_script(retry=2)` | — 에러·경고는 `log/error.log`에 복제(실행 구분자 포함)
| 키 앞에 홀 글자(러 / U+2024)가 섞여 배열째 파싱 실패(실측) | `json_soft_fix`가 잡문자·이상 따옴표 정리 → 실패 시 `_salvage_objects`가 짝 맞는 `{}`만 주워拾음(부분 손실 < 전량 손실) | `comic_input.json_soft_fix`, `comic_gen._salvage_objects` |
| 컷이 state를 안 채워 회차 태그가 상태를 대체 | 엄격 게이트 + 보충 호출(빈 항목만 / 첫 컷만) → `PanelScriptError` | `comic_gen.validate_panel_script`, `fill_first_cut` |
| 규칙이 잘려 LLM이 규칙 일부를 못 봄(실측: `{pose_policy}`가 3-c 아래로 밀림) | 규칙 3 → 그 뒷줄 → 3-b/3-c 순으로 배치 고정 | `comic_gen.build_panel_script_prompt` |
| 키 앞 `"`가 U+2024 같은 유니코드로 디코딩됨(실측) | `json_soft_fix`(따옴표류 정규화·잡문자 제거·키 감싸기) — **정상 응답은 이 복구기를 거치지 않음**, 실패 시 오류 위치를 로그에 남김 | `comic_input.json_soft_fix`, `extract_json_obj_checked` |
| 사건 병합으로 스토리 압축 | 병합 시 컷 수 **합**, 레이아웃 목표 컷 수 재추첨 | `split_acts_by_units`, `plan_pages_layout` |
| 막 앵커가 안 보여 막이 1개가 된다 → 항목 20개가 6컷으로 압축 | 항목 저울을 막 분할 게이트 밖으로 분리 + 항목 모드 병합 금지(개수로만 묶기) | `comic_gen.request_panel_script` else 분기, `comic_input.split_acts_by_units` |
| ★ 박스가 화면을 덮음 | 면적 70% 상한 + 글자 1.25배(크기는 폰트로) + 위치 규칙 | `comic_page_merge` NARR_* |
| 지문이 잘림 | 줄 제한 제거 → 폭 성장 → 폰트 11px까지 | `comic_page_merge.py:136~144` |
| 한 표정으로 고정 | 컷 감정 우선 + 회차 표정은 클라이맥스만 | `anima_gen._calm_face` |
| 컷이 옷 없이 나옴 | 의류어 제거 후 의류 없으면 회차 의상 복원 | `anima_gen._undress_guard` |
| 컷 1부터 다른 장소 배경 | 상태 시트에 `place/time/background` 추가 + 도입 컷이 비우면 처음 명시된 장소로 소급 | `comic_gen.fold_cut_state`, `anima_gen`의 `[BACKGROUND]` |
| 컷 1부터 중반 복장/극단 표정 | 회차 태그를 시작/후반으로 분리(`clothes_late`·`face_style_late`) + **컷별 연속 상태 시트**로 이전(표정·화장·몸·옷·악세사리·머리·흔적·소지품·자세) | `comic_gen.fold_cut_state`, `anima_gen._build_tag_block(cut_state=)` |
| 머리가 잘린 컷 | 얼굴 앵커 크롭 + 8% 폴백 | `comic_page_merge.fit_cover` |
| 한글이 태그에 남음 | gloss 번역(EP 1회) + 미번역 제거 + 정제 요약 로그 | `request_ko_glossary`, `_prompt_san_flush` |
| LLM이 VRAM을 안 비움 | 프롬프트 조립을 렌더 밖에서 + `keep_alive=0` | `comic_gen.py:2311` 부근 |
| 렌더 0장 | ComfyUI 포트/경로 프리플라이트 + `생성된 컷 이미지 0장` 경고 | `run_comic.py:378` |

---

## 7. 이번 세션에서 바뀐 것 (숫자)

| 항목 | 전 | 후 |
|---|---|---|
| 컷 배분 저울 | 글자 수(600자/컷) → 사건(1~2컷) | **본문 항목 1 = 컷 1**(행동/대사/속마음) |
| 같은 원고 dry-run | 11컷(2페이지) | **19컷(4페이지)** |
| 목표 7컷 | 레이아웃 4컷(압축) | **7~9컷** (재추첨 10회, 부족 벌점 2.2배) |
| 화면 장치 | LLM이 자유 선택 | **코드가 판정해 컷마다 지정** |
| 크롭 | 세로 가운데(전폭에서 얼굴 보존 51%) | **얼굴 앵커/8% 폴백**(79~98%) |
| 템플릿 | 14종, 페이지마다 독립 추첨 | **34종, 회차 안 재사용**, 전폭 비중 0.5 상한 |
| 정제 로그 | 컷마다 1,557줄 | 회차 끝 **1줄** |
| selftest | — | **PASS 416 / FAIL 0** |

---

## 8. 오프라인 리뷰 체크리스트

**LLM 관점 (여기부터 보세요)**
1. `comic_input.py:460~530` — 추출 프롬프트. 항목 모드와 사건 모드의 `unit_rule` 갈림이 의도대로인가? (`항목 하나가 컷 하나`)
2. `comic_gen.py:532~` — 컷 스크립트 프롬프트. 규칙 번호가 밀리지 않았는지, `{device_block}{chatty_rule}`이 본문 규칙과 충돌하지 않는지.
3. `comic_gen.py:1570~1600` — 장치 판정부. `split_for_cuts`로 나눈 조각이 실제 컷 순서와 1:1인가? (★슬롯 제외 처리 확인)
4. `comic_gen.py:1999` — POV/multi 재작성. 캐릭터 정체 태그 사본 지시·한글 금지·`##PROMPT##` 마커가 유지되는지.
5. 시트/이름 분리: `comic_input.py` name_lock 규칙과 `ensure_char_tags` — 화면 이름과 렌더 태그가 섞이지 않는지.

**배분/레이아웃**
6. `comic_input.py:351~440` — `normalize_units`(항목 강제 1컷), `split_acts_by_units`(합 보존).
7. `comic_gen.py:132~320` — `plan_pages`, 고정 경로는 `plan_pages`의 `pinned`/`_pin_i` (여기서 기승전결 필터·재사용 금지가 꺼진다)
(salt·재사용 금지·전폭 우대), `plan_pages_layout`(재추첨).

**화면**
8. `comic_page_merge.py:733~860` — 얼굴 검출/크롭 상수(`FACE_CROP_TOP=0.08`, `FACE_H_TARGET=0.40`, `FACE_ZOOM_MAX=1.35`).
9. `comic_page_merge.py:1322~` — 설명/풍선/속마음/의성어 배치 상수(`BALLOON_W_RATIO=0.20`, `FONT_FLOOR=11`).

**확인 질문 (직접 돌려볼 것)**
```bash
venv/bin/python selftest.py                                   # PASS 416 / FAIL 0
venv/bin/python run_comic.py --episode inputs/ep90_deadbeef.txt --ep 1 --total-episodes 3 --dry-run
venv/bin/python run_comic.py --episode inputs/ep90_deadbeef.txt --ep 1 --no-item-cuts --dry-run   # 회귀 비교
tail -80 log/comic_gen.log | grep -E "화면 장치|컷 스크립트 완성|프롬프트 정제"
```
