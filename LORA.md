# LORA.md — 선택 가능한 LoRA / 메인 모델(UNet) 안내

만화 컷을 그리는 ANIMA 렌더의 **화풍 선택지**를 모아 둔 문서입니다. `README.md`가 지나치게 두꺼워지지 않도록 분리했으니, 화풍을 고르실 때만 여기를 열어 주세요.

- 실행법 전체는 `README.md` 3-5절(자주 쓰는 플래그)에 있습니다. 여기에는 **화풍 선택지만** 정리해 두었습니다.
- 대상을 싣는 곳: `anima_gen.py`의 **`ANIMA_LORA_CONFIG`** (81키) — 여기 등록된 키만 선택 가능합니다(소스에서 주석 처리된 항목은 선택 대상이 아니라 이 문서에도 없습니다).
- 새 LoRA를 추가하실 때는 `ANIMA_LORA_CONFIG`에 1줄만 더해 주세요. 이 문서는 그 표를 스캔해 만들므로, 키가 바뀌면 다시 생성해 달라고 말씀해 주시면 됩니다(생성기는 repo에 두지 않습니다).

---

## 1. 화풍을 고르는 방법 3가지

| 방법 | 지정 위치 | 예시 |
|---|---|---|
| ① CLI 플래그 | `run_comic.py` | `--lora1 lora_mi1k --lora2 lora_sex` |
| ② 랜덤 (회마다 교체) | `run_comic.py` | `--lora-chg episode` |
| ③ 설정 파일 | `plot.json` | `"anima_style": "lora_lambton", "anima_lora": 1` |

우선순위는 **`--real` > `--sole` > `--lora1/--lora2` > `plot.json`** 입니다. `plot.json`의 `anima_unet`을 지정하시면 그 모델이 **항상 우선**합니다(`--real`/`--sole`의 UNET 풀을 쓰시려면 `"anima_unet": ""`로 비워 주세요).

`--lora1`(또는 `--lora2`)에 키를 넣으시면 표의 **LoRA1 파일·강도·trigger·기본 UNet**이 그대로 쓰이고, `trigger`는 프롬프트 헤더 앞단에 자동 주입됩니다. 없는 키를 넣으시면 로그에 에러를 남기고 `plot.json` 설정으로 돌아갑니다.

강도만 따로 맞추고 싶으시면 `--str1` / `--str2`를 쓰세요 (`ANIMA_LORA_CONFIG`를 편집하실 필요가 없습니다):

```bash
python run_comic.py --episode inputs/ep01.txt --lora1 lora_vassago --str1 0.8 \\
                    --lora2 lora_sex --str2 0.3        # 0이면 그 슬롯 OFF, 상한은 2.0
```

두 플래그는 키를 지명하지 않아도 걸립니다(`plot.json`의 `anima_style`, `lora_random`, `--lora-chg episode` 모두에 적용). 실제로 ComfyUI에 들어간 값은 `log/anima_gen.log`의 `[ComfyUI LoRA] lora_1=…(강도) … | trigger=…` 라인에서 확인됩니다.

```bash
# 쓸 수 있는 키 목록만 빠르게
python -c "import anima_gen as A; print('\n'.join(sorted(A.ANIMA_LORA_CONFIG)))"
```

## 2. 메인 모델(UNet) 선택지

| 풀 | 언제 쓰이나 | 구성 |
|---|---|---|
| `ANIMA_BASE_UNET_DEFAULT` | LoRA 키에 적힌 UNet이 없을 때의 폴백 | `anima_aestheticV11.safetensors` |
| `ANIMA_RANDOM_UNET_POOL` | `lora_random`/`--lora1` 모드 (세션당 1회 랜덤) | `anima_aestheticV11`, `waiANIMA_v10Base10` |
| `ANIMA_REAL_UNET_POOL` | `--real` (LoRA 전부 OFF) | `realDream_animaV4`, `beretMixAnimaReal28D_v10`, `uwazumimixAnima_uwazumimixAnimaV30` |
| `ANIMA_SOLE_UNET_POOL` | `--sole` (LoRA 전부 OFF, 애니메이션 화풍) | `pornmasterAnima_baseV1`, `fnMomentAnimaTurbo_v40NoTurbo`, `illustrijGEN_a1`, `riMixIllustriousAnima_riMixAnima`, `miaomiaoHarem_anima16`, `terraRisingUnity_v30Unity` |

> 로컬 실존 여부는 표의 **실존** 열에서 확인됩니다(이 머신의 ComfyUI 기준으로 스캔).

| 풀 | 모델 | 실존 |
|---|---|---|
| `ANIMA_BASE_UNET_DEFAULT` | `anima_aestheticV11` | ✅ |
| `ANIMA_RANDOM_UNET_POOL` | `anima_aestheticV11` | ✅ |
| `ANIMA_RANDOM_UNET_POOL` | `waiANIMA_v10Base10` | ✅ |
| `ANIMA_REAL_UNET_POOL` | `realDream_animaV4` | ✅ |
| `ANIMA_REAL_UNET_POOL` | `beretMixAnimaReal28D_v10` | ✅ |
| `ANIMA_REAL_UNET_POOL` | `uwazumimixAnima_uwazumimixAnimaV30` | ✅ |
| `ANIMA_SOLE_UNET_POOL` | `pornmasterAnima_baseV1` | ✅ |
| `ANIMA_SOLE_UNET_POOL` | `fnMomentAnimaTurbo_v40NoTurbo` | ✅ |
| `ANIMA_SOLE_UNET_POOL` | `illustrijGEN_a1` | ✅ |
| `ANIMA_SOLE_UNET_POOL` | `riMixIllustriousAnima_riMixAnima` | ✅ |
| `ANIMA_SOLE_UNET_POOL` | `miaomiaoHarem_anima16` | ✅ |
| `ANIMA_SOLE_UNET_POOL` | `terraRisingUnity_v30Unity` | ✅ |

---

## 3. 선택 가능한 LoRA 81키

- **랜덤** 열: `✔`는 `lora_random`(=`plot.json anima_lora:1` + `anima_style:"lora_random"`)과 `--lora-chg episode`의 후보가 된다는 뜻입니다 — 규칙은 `lora2`가 빈 키만 후보(`anima_gen._pick_random_lora_pair`).
- **캐릭터** 표시가 붙은 키는 서로 배제되어 랜덤 쌍에 최대 1개만 들어갑니다(`ANIMA_CHARACTER_LORA_KEYS`).
- 현재 활성 키 81개는 **전부 단독 LoRA(`lora2`가 빈 항목)** 이므로 전부 랜덤 후보입니다. 미리 조합해 둔 mix 키(lora_mix1 등)는 현재 전부 주석 처리 중이라 선택 대상이 아니지만, `--lora1 A --lora2 B`로 직접 조합하시는 것은 가능합니다.
- **실존** 열: `❌없음`은 이 머신의 ComfyUI에서 파일을 못 찾았다는 뜻입니다 — 그 키를 고르면 ComfyUI가 400으로 거부합니다.

### 기본 스타일 (2026-09-08부터 unet = anima_aestheticV11, 구 anima_baseV10 자리) — 8키
| 키 | LoRA1 (강도) | LoRA2 (강도) | 기본 UNet | prompt trigger | 랜덤 | 실존 | 비고 |
|---|---|---|---|---|---|---|---|
| `lora_fr9t` | `anima_fr9t_v1.1` (1.0) | — | `anima_aestheticV11` | `@fr9t,` | ✔ | ✅ | — |
| `lora_gpt` | `42` (1.0) | — | `anima_aestheticV11` | 없음 | ✔ | ✅ | — |
| `lora_hentai` | `Hentai_Studio_Quality_Anima-step00001300` (1.0) | — | `anima_aestheticV11` | `hentai_studio_quality, shiny skin,` | ✔ | ✅ | — |
| `lora_jewel` | `jewel_milk-step00012000` (1.0) | — | `anima_aestheticV11` | 없음 | ✔ | ✅ | — |
| `lora_mi1k` | `anima_mi1k_v1.2` (1.0) | — | `anima_aestheticV11` | `@mi1k,` | ✔ | ✅ | — |
| `lora_saboten` | `saboten` (1.0) | — | `anima_aestheticV11` | `@saboten,` | ✔ | ✅ | — |
| `lora_tianl` | `tianl` (1.0) | — | `anima_aestheticV11` | `@tianliang duohe fangdongye,` | ✔ | ✅ | — |
| `lora_wagashi` | `wagashi` (1.0) | — | `anima_aestheticV11` | `@wagashi_\(dagashiya\),` | ✔ | ✅ | — |

### 에스테틱 계열 (anima_aestheticV11) — 1키
| 키 | LoRA1 (강도) | LoRA2 (강도) | 기본 UNet | prompt trigger | 랜덤 | 실존 | 비고 |
|---|---|---|---|---|---|---|---|
| `lora_aioyaji` | `AIIlustOjisan_AnimaB_v01(@411llust0j1s4n)` (1.0) | — | `anima_aestheticV11` | `@411llust0j1s4n,` | ✔ | ✅ | — |

### 단독 LoRA (lora_random 후보) — 8키
> mix에 들어갔던 LoRA를 단독 KEY(lora2="")로 분리.
> lora_random은 lora2=="" 인 KEY만 선택 → mix의 보조 LoRA(lora2)가 버려지는 문제 방지.
> unet은 random 시 ANIMA_RANDOM_UNET_POOL로 덮어써짐 (직접 사용 시 기본값).

| 키 | LoRA1 (강도) | LoRA2 (강도) | 기본 UNet | prompt trigger | 랜덤 | 실존 | 비고 |
|---|---|---|---|---|---|---|---|
| `lora_alp_solo` | `ALPAnima2` (1.0) | — | `anima_aestheticV11` | `ALPAnima,` | ✔ | ✅ | — |
| `lora_gptimg2` | `gpt-image-2_anima-base1_v1-1` (1.0) | — | `anima_aestheticV11` | 없음 | ✔ | ✅ | — |
| `lora_nanashi` | `나나시2(@nanash1)` (1.0) | — | `anima_aestheticV11` | `(@nanashi1),` | ✔ | ✅ | — |
| `lora_sex` | `sex_style` (1.0) | — | `anima_aestheticV11` | 없음 | ✔ | ✅ | — |
| `lora_takamichi_solo` | `TAKAMICHI2010_202881` (1.0) | — | `anima_aestheticV11` | `TakamichiStyle,` | ✔ | ✅ | — |
| `lora_takamichistyle` | `TAKAMICHISTYLE_T5_200245` (1.0) | — | `anima_aestheticV11` | `TakamichiStyle,` | ✔ | ✅ | — |
| `lora_xipa_solo` | `xipa2026late03-05_AnimaB_v01` (1.0) | — | `anima_aestheticV11` | `@xipa2026late03-05,` | ✔ | ✅ | — |
| `lora_yzsss` | `yzsss_anima1.0_v0.2` (1.0) | — | `anima_aestheticV11` | `(@yzsss),` | ✔ | ✅ | — |

### 2026-08-24 추가 (단독 LoRA, ~/AI/ComfyUI/models/loras) — 8키
| 키 | LoRA1 (강도) | LoRA2 (강도) | 기본 UNet | prompt trigger | 랜덤 | 실존 | 비고 |
|---|---|---|---|---|---|---|---|
| `lora_buanime` | `BuAnime_NSFW_Style_Anima_BuAnime` (1.0) | — | `anima_aestheticV11` | 없음 | ✔ | ✅ | trigger 없음 확인 (캡션에 trigger 태그 X) |
| `lora_daioo` | `daioo` (1.0) | — | `anima_aestheticV11` | `@daioo,` | ✔ | ✅ | — |
| `lora_deadalus7` | `Deadalus7_anima_v10` (1.0) | — | `anima_aestheticV11` | `@deadalus7,` | ✔ | ✅ | 학습 캡션 소문자 확인 |
| `lora_hotate` | `hotate3333333_anima_v10` (1.0) | — | `anima_aestheticV11` | `@hotate3333333,` | ✔ | ✅ | — |
| `lora_meow25` | `meow25v8` (1.0) | — | `anima_aestheticV11` | `meow25,` | ✔ | ✅ | — |
| `lora_ren45` | `ren45_v1` (1.0) | — | `anima_aestheticV11` | 없음 | ✔ | ✅ | LyCORIS 학습, 메타데이터 없음 → trigger 미확인 |
| `lora_wadustyle` | `WaduStyle-Anima` (1.0) | — | `anima_aestheticV11` | 없음 | ✔ | ✅ | 순수 스타일 LoRA (캡션 전부 빈 문자열) → trigger 없음, epoch_1(1500step) 저장본 |
| `lora_zxcvnmweruo` | `zxcvnmweruo_anima_v10-000002` (1.0) | — | `anima_aestheticV11` | `@zxcvnmweruo,` | ✔ | ✅ | — |

### 2026-08-25 추가 (단독 LoRA, ~/AI/ComfyUI/models/loras) — 1키
| 키 | LoRA1 (강도) | LoRA2 (강도) | 기본 UNet | prompt trigger | 랜덤 | 실존 | 비고 |
|---|---|---|---|---|---|---|---|
| `lora_vassago` | `b529e7df-a73b-4735-8f70-a2377685975a.TA_trained` (1.0) | — | `anima_aestheticV11` | `v4ss4g0x, cartoon,` | ✔ | ✅ | — |

### 2026-08-25 캐릭터 LoRA (상호배제: ANIMA_CHARACTER_LORA_KEYS, 랜덤 페어에 최대 1개) — 8키
| 키 | LoRA1 (강도) | LoRA2 (강도) | 기본 UNet | prompt trigger | 랜덤 | 실존 | 비고 |
|---|---|---|---|---|---|---|---|
| `lora_evie` | `EvieStellarBlade_AnimaBaseV10_byKonan` (1.0) | — | `anima_aestheticV11` | `evie,` | ✔ | ✅ | 캐릭터 LoRA(상호배제) |
| `lora_hyeonbomi` | `Hyeon Bom-i Anima` (1.0) | — | `anima_aestheticV11` | `hnbmib,` | ✔ | ✅ | 캐릭터 LoRA(상호배제) |
| `lora_imdana` | `Im da-na Anima` (1.0) | — | `anima_aestheticV11` | `imdnab,` | ✔ | ✅ | 캐릭터 LoRA(상호배제) |
| `lora_jeongsooah` | `Jeong Soo Ah Anima` (1.0) | — | `anima_aestheticV11` | `jngsoahb,` | ✔ | ✅ | 캐릭터 LoRA(상호배제) |
| `lora_lasihyeon` | `La Sihyeon Anima` (1.0) | — | `anima_aestheticV11` | `lashnb,` | ✔ | ✅ | 캐릭터 LoRA(상호배제) |
| `lora_parkSORIM` | `Park So-rim Anima` (1.0) | — | `anima_aestheticV11` | `prksrmb,` | ✔ | ✅ | 캐릭터 LoRA(상호배제) |
| `lora_seoheeju` | `Seo Heeju Anima` (1.0) | — | `anima_aestheticV11` | `sohjub,` | ✔ | ✅ | 캐릭터 LoRA(상호배제) |
| `lora_woojiyoung` | `Woo Ji-young Anima` (1.0) | — | `anima_aestheticV11` | `wojyngb,` | ✔ | ✅ | 캐릭터 LoRA(상호배제) |

### 2026-08-25 스타일 LoRA (trigger 확인) — 15키
| 키 | LoRA1 (강도) | LoRA2 (강도) | 기본 UNet | prompt trigger | 랜덤 | 실존 | 비고 |
|---|---|---|---|---|---|---|---|
| `lora_akipeko` | `akipekoanima_v1_1-step00003000(@AkioA_I)` (1.0) | — | `anima_aestheticV11` | `@akipekostyle,` | ✔ | ✅ | 파일명 @AkioA_I와 다름 |
| `lora_alpp` | `alp` (1.0) | — | `anima_aestheticV11` | `alpp,` | ✔ | ✅ | — |
| `lora_anmshigui` | `ANMShigUi_F4H_1620` (1.0) | — | `anima_aestheticV11` | `anmshigui_f4h,` | ✔ | ✅ | — |
| `lora_citizenk` | `CitizenK_AnimaB_v01(@c1t1z3nk)` (1.0) | — | `anima_aestheticV11` | `@c1t1z3nk,` | ✔ | ✅ | — |
| `lora_fishine` | `fishine_anima` (1.0) | — | `anima_aestheticV11` | `@fishine,` | ✔ | ✅ | — |
| `lora_ghibli` | `ghibli_style_anima` (1.0) | — | `anima_aestheticV11` | `ghibli style,` | ✔ | ✅ | — |
| `lora_m4me` | `anima_m4me_v1.2-epoch16` (1.0) | — | `anima_aestheticV11` | `@m4me,` | ✔ | ✅ | civitai "Anima style: m4me" |
| `lora_meion` | `Meion_anima_style_lokr_2-000034(@meionANIMAstyle)` (1.0) | — | `anima_aestheticV11` | `@meionanimastyle,` | ✔ | ✅ | — |
| `lora_poju` | `[Po-Ju] Promiscuity C Doujin Style Anima` (1.0) | — | `anima_aestheticV11` | `po-ju,` | ✔ | ✅ | — |
| `lora_possummachine` | `possummachine-A1_v1` (1.0) | — | `anima_aestheticV11` | `@possummachine,` | ✔ | ✅ | — |
| `lora_shexyo` | `shexyo_AnimaB_v03` (1.0) | — | `anima_aestheticV11` | `sh3xy0 style,` | ✔ | ✅ | — |
| `lora_shinjiro` | `shinjiro_AnimaB_v01` (1.0) | — | `anima_aestheticV11` | `@sh1nj1r0,` | ✔ | ✅ | — |
| `lora_xipaearly` | `xipaearly2026_AnimaB_v01-1` (1.0) | — | `anima_aestheticV11` | `@x1p4early2026,` | ✔ | ✅ | — |
| `lora_yzs` | `yzs_anima_v0.3` (1.0) | — | `anima_aestheticV11` | `@yzsss,` | ✔ | ✅ | — |
| `lora_zeebc0ck` | `N9BMZFWQR63A8HGJPX2497K4G0` (1.0) | — | `anima_aestheticV11` | `zeebc0ck,` | ✔ | ✅ | 1장 학습, trigger 의심 |

### 2026-08-25 스타일 LoRA (trigger 없음) — 12키
| 키 | LoRA1 (강도) | LoRA2 (강도) | 기본 UNet | prompt trigger | 랜덤 | 실존 | 비고 |
|---|---|---|---|---|---|---|---|
| `lora_bluearchive` | `BlueArchiveStyleB1` (1.0) | — | `anima_aestheticV11` | 없음 | ✔ | ✅ | — |
| `lora_buanime_ultra` | `BuAnime_NSFW_Style_Anima_ultra` (1.0) | — | `anima_aestheticV11` | 없음 | ✔ | ✅ | — |
| `lora_cunny` | `cunny_animaV1.0-000009` (1.0) | — | `anima_aestheticV11` | 없음 | ✔ | ✅ | — |
| `lora_easonhot` | `Eason HOT` (1.0) | — | `anima_aestheticV11` | 없음 | ✔ | ✅ | — |
| `lora_flackstyle` | `flackstyle-000034_edited` (1.0) | — | `anima_aestheticV11` | 없음 | ✔ | ✅ | — |
| `lora_inakamonor` | `Inakamonor style2` (1.0) | — | `anima_aestheticV11` | 없음 | ✔ | ✅ | — |
| `lora_memaaani` | `MeMaAni V2 B` (1.0) | — | `anima_aestheticV11` | 없음 | ✔ | ✅ | — |
| `lora_ponchi` | `ponchi_v1` (1.0) | — | `anima_aestheticV11` | 없음 | ✔ | ✅ | — |
| `lora_rindou` | `rindou` (1.0) | — | `anima_aestheticV11` | 없음 | ✔ | ✅ | — |
| `lora_takeda2` | `Takeda_Hiromitsu_Anima_v2` (1.0) | — | `anima_aestheticV11` | 없음 | ✔ | ✅ | trigger 미확인 (W5 생성 테스트) |
| `lora_ujinangel` | `u-jin_angel_anima_v05` (1.0) | — | `anima_aestheticV11` | 없음 | ✔ | ✅ | — |
| `lora_zoda` | `zoda_anima_v2` (1.0) | — | `anima_aestheticV11` | 없음 | ✔ | ✅ | — |

### 2026-08-25 스타일 LoRA (trigger 미확인, 파일명 @ 힌트) — W5 생성 테스트 후 수정 — 5키
> (2anima oskar custom.safetensors는 4anima 버전과 동일 학습본 → 미등록)

| 키 | LoRA1 (강도) | LoRA2 (강도) | 기본 UNet | prompt trigger | 랜덤 | 실존 | 비고 |
|---|---|---|---|---|---|---|---|
| `lora_1uxsumildo` | `럭스수밀도3(@1uxsumildo)` (1.0) | — | `anima_aestheticV11` | `@1uxsumildo,` | ✔ | ✅ | — |
| `lora_a10` | `A10(@dpdlxps)` (1.0) | — | `anima_aestheticV11` | `@dpdlxps,` | ✔ | ✅ | 미확인 |
| `lora_amaduyu` | `아마즈유3(@amaduyu)` (1.0) | — | `anima_aestheticV11` | `@amaduyu,` | ✔ | ✅ | — |
| `lora_bunnyslop` | `BunnySlop_Ani_v5.56` (1.0) | — | `anima_aestheticV11` | 없음 | ✔ | ✅ | 미확인 (TF 비어있음, civitai title @sl0p) — W5 생성 테스트 |
| `lora_oskar` | `4anima oskar custom@oskar custom)` (1.0) | — | `anima_aestheticV11` | `oskar custom,` | ✔ | ✅ | 미확인 |

### 2026-08-28 추가 (단독 LoRA, ~/AI/ComfyUI/models/loras) — 9키
| 키 | LoRA1 (강도) | LoRA2 (강도) | 기본 UNet | prompt trigger | 랜덤 | 실존 | 비고 |
|---|---|---|---|---|---|---|---|
| `lora_daioo_v2` | `daioo v2` (1.0) | — | `anima_aestheticV11` | `@daioo,` | ✔ | ✅ | @daioo 750/1400 (학습 750장, reg 제외) — 기존 daioo v2 |
| `lora_kemuri_haku` | `Kemuri_Haku___Artist_Style___Anima` (1.0) | — | `anima_aestheticV11` | `kmurihku,` | ✔ | ✅ | [2026-09-08] trigger 수정: ai-toolkit 학습본은 ss_tag_frequency가 빈 캡션 1개({"1_":{"":1}})라 무정보 → modelspec.title='kmurihku' + civitai trainedWords=['kmurih… |
| `lora_kurorin` | `kurorin_AnimaB_v01-000011` (1.0) | — | `anima_aestheticV11` | `@kur0r1n,` | ✔ | ✅ | 의심 (34/190, AnimaB v01 패턴) |
| `lora_lambton` | `Lambton` (1.0) | — | `anima_aestheticV11` | 없음 | ✔ | ✅ | trigger 없음 (빈 캡션 1개) |
| `lora_ratatatat74` | `ratatatat74_AnimaB_v02` (1.0) | — | `anima_aestheticV11` | `@r4t4t4t4t,` | ✔ | ✅ | [2026-09-08] 확인: 31/31(100%) 등장 + civitai trainedWords=['@r4t4t4t4t,'] |
| `lora_shufflesong` | `shufflesongdatiankongstyle_animaBasev1` (1.0) | — | `anima_aestheticV11` | `@shufflesongdatiankongstyle,` | ✔ | ✅ | 의심 (43/657, 512+1024 2그룹) |
| `lora_softmanhua` | `softmanhuastyle_000006000` (1.0) | — | `anima_aestheticV11` | `softmanhuastyle,` | ✔ | ✅ | 1/1 캡션 trigger 확인 — [2026-09-08] civitai 권장 프레이즈: 'softmanhuastyle, 2d adult manhua illustration style, clean ink lineart, soft cel shading, pastel… |
| `lora_takeani_takeda` | `_takeani__Takeda_Hiromitsu__epoch_10` (1.0) | — | `anima_aestheticV11` | 없음 | ✔ | ✅ | trigger 없음 (빈 캡션 1개, Takeda Hiromitsu epoch_10) |
| `lora_xiaoluonion` | `xiaoluonionmix_v2` (1.0) | — | `anima_aestheticV11` | 없음 | ✔ | ✅ | 미확인 (메타데이터 없음) |

### 2026-08-28 추가 (기존 미등록 파일 보충) — 4키
| 키 | LoRA1 (강도) | LoRA2 (강도) | 기본 UNet | prompt trigger | 랜덤 | 실존 | 비고 |
|---|---|---|---|---|---|---|---|
| `lora_berserker` | `Berserker00R` (1.0) | — | `anima_aestheticV11` | 없음 | ✔ | ✅ | 미확인 (TF 비어있음, base=anima-base-v1.0) |
| `lora_glossy1` | `glossy1_epoch17` (1.0) | — | `anima_aestheticV11` | 없음 | ✔ | ✅ | 미확인 (LyCORIS kohya, trigger 정보 없음) |
| `lora_niji_sweet` | `ANIMA_Niji_Sweet_Spot_v4` (1.0) | — | `anima_aestheticV11` | 없음 | ✔ | ✅ | 미확인 (메타데이터 전무) |
| `lora_realism` | `realism-anima-v2` (1.0) | — | `anima_aestheticV11` | `@callmemaybe,` | ✔ | ✅ | 의심 (28/1179, base=realism-anima-v10) |

### 2026-09-08 추가 (신규 다운로드 6종 중 미등록 3종) — 2키
> 분석: util/analyze_new_loras_260908.py (safetensors 헤더 + tensor shape + civitai hash API)
> 나머지 3종(softmanhua/ratatat74/kemuri_haku)은 이미 등록본과 sha256 동일(재다운로드만) → 위 항목 trigger/코멘트 보정
> ★ unet 필드는 위 헤더 주석대로 전 항목 anima_aestheticV11.safetensors로 통일 (anima_baseV10 폐기)

| 키 | LoRA1 (강도) | LoRA2 (강도) | 기본 UNet | prompt trigger | 랜덤 | 실존 | 비고 |
|---|---|---|---|---|---|---|---|
| `lora_buanime_v2` | `BuAnime_NSFW_Style_Anima_BuAnimeV2` (1.0) | — | `anima_aestheticV11` | 없음 | ✔ | ✅ | — |
| `lora_cirima` | `Cirima_Anima` (1.0) | — | `anima_aestheticV11` | 없음 | ✔ | ✅ | — |

---

## 4. 고르기 전에 알아두실 점

1. **수위는 이 문서와 무관합니다.** 기본 정책은 청년향입니다 — 성기 태그는 프롬프트 단에서 걷혀 나가므로 성인 화풍 LoRA를 골라도 화면은 청년향으로 나옵니다.
2. **트리거는 자동 주입**됩니다. 표의 trigger를 직접 프롬프트에 넣지 않으셔도 됩니다. (주입 지점: `resolve_anima_lora()` → `artist_anima`. [2026-09-09] 이전 fork에서는 이 라인이 빠져 있어 트리거가 없는 그림이 나왔습니다.)
3. **세션 캐시**: 랜덤 계열(`lora_random`, `--lora-chg episode`, `--real/--sole`의 UNet)은 실행당 1회 정해져 그 세션 내내 유지됩니다. 같은 컷을 다시 그려도 화풍은 흔들리지 않습니다.
4. **강도 조정**: `--str1` / `--str2`(0.0~2.0)로 명령줄에서, 상시로 쓰실 때는 `ANIMA_LORA_CONFIG`의 `str1/str2`를 고치시면 됩니다. `--lora-chg increment`(회차가 깊어질수록 lora2 강도 증가)는 총 회차 설정을 벗어나면 강도가 1.0을 넘어버려 **폐지**되었습니다.
5. **파일 확인**: LoRA는 `<ComfyUI>/models/loras`, UNet은 `models/diffusion_models|unet|checkpoints`에서 찾습니다. ComfyUI 경로는 `plot.json`의 `comfyuidir`(또는 env `COMFYUI_DIR`)이 유일한 지정 지점입니다. LoRA 파일을 못 찾으면 그 슬롯을 **끄고** 로그에 남깁니다(렌더는 계속됩니다).
6. **슬롯이 켜지는 조건**: ComfyUI 템플릿(`data_comfyui/anima_spectrum_July11.json`)의 `lora_1/lora_2`는 기본값이 `on:false`입니다. 강도가 0보다 크면 `anima_gen._apply_lora_nodes()`가 `on=True`를 켜므로, "LoRA를 지정했는데 화풍이 그대로"라면 로그의 `[ComfyUI LoRA]` 라인을 먼저 봐 주세요.

```bash
# 선택 가능한 키 목록만 빠르게 확인
python -c "import anima_gen as A; print(chr(10).join(sorted(A.ANIMA_LORA_CONFIG)))"
```

