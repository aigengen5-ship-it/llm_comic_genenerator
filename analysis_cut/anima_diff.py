#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""두 레포의 anima_gen.py를 함수 단위로 비교해 markdown으로 적는다 (2026-09-16~).

  이 repo(만화)의 anima_gen.py 는 단편 생성기 레포(정본)의 것을 fork한 것이다.
  같은 이름의 함수라도 서로 다른 방향으로 발전한 것이 있어서, "어느 쪽이 앞섰는지"를
  한눈에 보지 않으면 제자리-sync를 반복하게 된다. 그래서 비교를 스크립트로 만들었다.

  사용 (정본 경로는 기계마다 다르므로 인자로 준다 — 커밋되는 파일에 절대경로를 박지 않는다):
    venv/bin/python analysis_cut/anima_diff.py \
        --novel /path/to/llm_shortnovel_generator_gui/anima_gen.py
    → ./anima_gen_diff.md (gitignore 됨 — 로컬 문서)
"""
import argparse
import ast
import difflib
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

# ── 사람이 판정한 것 (자동 계측은 "차이 크기"까지만 안다. 방향은 사람이 적는다) ──────────────
VERDICT = {
    "comfyui_run_anima": (
        "각자 진화",
        "우리: seed/queue_count 지정·prompt_id 회수(ids_out)·컷별 negative(_merge_extra_negative)·"
        "워크플로 덤프(dump_workflow_once). 정본: _tidy_prompt_text(문법 정규화)·_flatten_prompt·"
        "POV/이벤트를 아는 창 게이트·safety_tag 인자. **512 창 게이트는 2026-09-16에 우리 쪽으로 이식 완료.**"),
    "_wait_and_copy_image": ("우리 우위", "우리는 ComfyUI /history 로 프롬프트 상태를 직접 확인"
                            "(_wait_and_copy_by_history)하고 listdir OSError를 견딘다. 정본은 이름+mtime 추측."),
    "queue_prompt": ("우리 우위", "우리는 prompt_id 를 돌려주고 400 응답 본문을 로그에 남긴다. 정본은 응답을 버린다."),
    "resolve_anima_lora": ("우리 우위", "우리는 해석 코어를 _resolve_anima_lora_core 로 나누고 CLI 오버라이드·"
                           "파일 실존 검사·트리거 동기화를 한 곳에서 한다."),
    "_comfyui_output_dirs": ("우리 우위", "우리는 개인/기기 절대경로를 박지 않고 plot.json → OS별 흔한 위치로 "
                              "폴백(Windows 아키라 포함). 정본은 `/run/media/...` 하드코딩 2줄이 남아 있다."),
    "_extract_json": ("우리 우위", "우리는 comic_gen의 관대한 JSON 파서로 폴백한다(잡문자 하나로 태그가 빈 사고 방지)."),
    "_pick_observer_visible": ("우리 우위", "우리는 key로 결정적으로 고른다(재실행 시 같은 그림)."),
    "neg_safety_terms": ("동기화됨", "2026-09-16에 정본의 폴백(review_safety[화] → plot.json extended)을 받아들였다."),
    "_build_simple_prompt_header": (
        "각자 진화",
        "정본: subject_position(주인공 배치)·카운터 정규화. 우리: 좌/우 랜덤을 해시로 결정·군중 포즈에서 "
        "카운터 승격·이중 마침표/공백 정리. ★정본의 좌/우 어구(\"He is standing on the right side.\")는 "
        "split screen 어구로 지목된 상태 — 우리 헤더도 같은 어구를 쓰므로 게이트가 잡아준다."),
    "_build_partner_block": ("우리 우위", "우리는 상대방을 회색 실루엣 고정 그룹으로 쓴다(실측 개선). "
                              "정본은 아직 (bald featureless faceless naked nude ... invisible man:3.0) — "
                              "정본 감사에서 자신이 지목한 '유령 남성' 태그군 그대로다."),
    "_partner_simple_tag": ("우리 우위", "위와 같은 이유. 실루엣이 기본, --partner-invisible 일 때만 정본식 그룹."),
    "_partner_body_token": ("각자 진화", "우리는 회차별 파라미터를 받고 정본은 config만 본다."),
    "init_anima_tags": ("각자 진화", "정본은 소설 진행(plot.json/progress)에서 태그를 모으고, 우리는 컷 스크립트 "
                        "기반이라 회차 태그셋·복장 램프·캐릭터 시트 오버라이드 순으로 세운다. 함수 이름만 같다."),
    "_build_tag_block": ("각자 진화", "정본은 BREAK 5섹션을 짓는 함수, 우리는 같은 이름으로 컷 태그 블록을 짓는다."),
    "get_episode_content": ("각자 진화", "입력 소스가 다르다(정본 progress/, 우리 컷 스크립트+시트)."),
    "_is_no_position": ("동일(표기만)", "주석 표현만 다르다."),
    "_split_top_level_commas": ("동일(표기만)", "주석 표현만 다르다."),
}

# 정본 전용 함수를 카테고리로 묶는다 (규칙: (이름 정규식, 카테고리, 한 줄))
GUI_CATS = [
    (r"^_anima_|^_fit_anima_token_window$", "2026-09-16 창 게이트·POV 가드", "정본이 토큰 창 감사로 만든 것 — 창 게이트·가중치·좌/우·화각 치환. 우리도 같은 이름으로 이식했다(아래 표 참고)."),
    (r"^_tidy|^_flatten|^_balance_parens|^_fix_|^line_merge$|^_repair_tag_sections$|^_dedupe_weighted_tags$",
     "LLM 저작 결과 수리", "정본은 LLM이 5섹션을 직접 쓰므로 문법·구두점·섹션 상한을 사후 수리한다."),
    (r"^_detect_tag_loop$|^_analyze_tag_loop$|^_worst_gram_repeat$|^_levenshtein$",
     "퇴행 루프 감지", "qwen 커레이션이 같은 문장을 반복해 뱉는 증상을 정본은 별도 스테이지로 잡아낸다."),
    (r"^_canonicalize_counters$|^_norm_tag$|^_split_tags$|^join_tags$|^_strip_weights$|^_tag_sections$|^_normalize_character_names$",
     "태그 정규화 헬퍼", "카운터(1 boy→1boy)·이름 로마자화 등 표기 통일."),
    (r"^_choose_angle$|^_extract_angle_choice$|^_format_angle_menu$|^_angle_llm_active$|^_canon_angle_fallback$",
     "화각 메뉴·선택", "정본은 angle.txt 프리셋을 LLM에게 메뉴로 보여주고 답을 고른다(우리는 컷 스크립트가 camera를 직접 쓴다)."),
    (r"^_filter_observer_section$|^_build_partner_observer$|^_build_special_guide$|^_pick_special_pose$",
     "관찰자·스페셜 가이드", "POV 관찰자 섹션·스페셜 포즈 안내문."),
    (r"^_is_male$|^_pronouns$|^_normalize_position_sentence$|^_resolve_breasts_size$|^_resolve_hip_size$|^_cut_loop_tail$|^_join_header_body$|^_join_sentences$|^_generate_review_notes$|^line_read$",
     "성별·문장 이어붙이기 헬퍼", "정본이 소설 파이프라인에서 쓰는 소품. 우리는 같은 역할을 comic_gen 안의 헬퍼로 한다."),
    (r"^_verify_and_adjust_tags$",
     "태그 자체 검증", "정본은 LLM 답을 한 번 더 읽어 태그를 고친다 — 우리 컷 스크립트 경로에도 같은 자기 검증이 있는지 확인할 것."),
    (r"^(anima_gen_simple|anima_gen_standing|anima_setup|build_final_tag|build_prompt_header|random_event|get_standing_pose|get_safety_order|get_exposed_line|_determine_safety|_get_step_content|_get_step_num|_build_episode_info|_build_episode_info_split|_build_tag_block)$|^_extract_.*from_(step|episode|sheet)$|^_scene_|^_with_scene_tags$|^_load_episode_from_progress$|^_load_character_sheet_from_progress$|^_load_actions_yaml$|^_load_additional_tags$|^_load_prompt_guide$|^_match_actions$|^_norm_action_key$|^_split_action_category$|^_extract_angle|^_gen$",
     "소설 파이프라인 전용", "step/progress 기반 — 만화 경로에는 대응물이 없다."),
]
OUR_CATS = [
    (r"^comfy_|^_wait_and_copy_by_history$|^_fetch_comfy_output$|^png_complete$|^dump_workflow_once$|^_comfy_api$|^_comfyui_roots$|^_comfyui_output_dirs$",
     "ComfyUI 큐·히스토리", "컷이 전부 나와야 페이지를 합치는 만화 파이프라인의 핵심(정본은 이미지 2장 돌려놓고 끝)."),
    (r"^_apply_(lora|unet|detailer)|^_lora_exists$|^_unet_exists$|^_lora_te_tensors$|^_resolve_anima_lora_core$|^_norm_lora|^_sync_artist_trigger$",
     "LoRA·노드 해석", "CLI 오버라이드·실존 검사·템플릿 디테일러 OFF 를 한 곳에서."),
    (r"^_cap_safety$|^_cut_exposure$|^_hot_exposure$|^_nude_words$|^_undress_guard$|^_partner_|^explicit_allowed$|^_merge_clothes$|^strip_anatomy_tags$|^partner_char_tag_line$|^protagonist_char_tag_line$|^ensure_char_tags$|^_char_tag_list$|^_extreme_face$|^_calm_face$|^_cap_safety$",
     "수위·복장·캐릭터 고정", "노출 램프(회차 등급+컷 위치), 옷 훼손 방지, 캐릭터 태그 단일화."),
    (r"^_anima_|^_fit_anima_token_window$|^set_close_framing$", "2026-09-16 창 게이트(우리 구현)",
     "평문 프롬프트를 추정 토큰으로 재고 넘치면 자른다 — 정본의 BREAK 섹션 인식형 게이트의 만화판."),
    (r"^set_extra_negative$|^_merge_extra_negative$|^_det_choice$|^simplify_partner_section$|^_dedupe_csv$|^_generate_tags_via_llm$|^_guideline_lines$|^_llm_tag_str$|^_apply_detailer_switch$|^_canon_camera$|^_camera_canon_active$",
     "컷별 렌더·결정성", "컷별 negative, 해시 기반 결정(재실행 보존), 태그 캐논컬."),
]

H = "#"


def funcs(path):
    src = open(path, encoding="utf-8").read().splitlines()
    tree = ast.parse("\n".join(src))
    out = {}
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[n.name] = ("\n".join(src[n.lineno - 1:n.end_lineno]), ast.get_docstring(n) or "")
    return out


def cat(name, rules):
    for pat, c, note in rules:
        if re.search(pat, name):
            return c, note
    return "기타", ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--novel", default=os.environ.get("NOVEL_ANIMA_GEN", ""),
                    help="정본(단편 생성기) anima_gen.py 경로")
    ap.add_argument("--comic", default=os.path.join(REPO, "anima_gen.py"))
    ap.add_argument("--out", default=os.path.join(REPO, "anima_gen_diff.md"))
    a = ap.parse_args()
    if not a.novel or not os.path.exists(a.novel):
        sys.exit("--novel 에 정본 anima_gen.py 경로를 주세요 (환경변수 NOVEL_ANIMA_GEN 도 됩니다).")

    G, C = funcs(a.novel), funcs(a.comic)
    common = sorted(set(G) & set(C))
    only_g = sorted(set(G) - set(C))
    only_c = sorted(set(C) - set(G))
    diff_rows = []
    for n in common:
        d = [l for l in difflib.unified_diff(G[n][0].splitlines(), C[n][0].splitlines(),
                                            lineterm="", n=0) if l[:1] in "+-"]
        if not d:
            continue
        code = [l for l in d if not l[1:].strip().startswith("#")]
        diff_rows.append((n, len(d), bool(code)))
    same = [n for n in common if n not in {r[0] for r in diff_rows}]

    L = []
    A = L.append
    A(f"{H}# `anima_gen.py` 비교 — 이 repo(만화) ↔ 단편 생성기(정본)")
    A("")
    A(f"> 생성: `analysis_cut/anima_diff.py` · {time.strftime('%Y-%m-%d %H:%M')} · 이 파일은 `.gitignore` 대상(로컬).")
    A("> 왼쪽 = **정본**(단편 생성기), 오른쪽 = **우리**(만화 생성기). 같은 파일에서 출발해 두 방향으로 자랐다.")
    A("")
    A("| | 정본 | 우리 |")
    A("|---|---:|---:|")
    A(f"| 경로 | `{a.novel}` | `{a.comic}` |")
    A(f"| 크기 | {os.path.getsize(a.novel):,} B | {os.path.getsize(a.comic):,} B |")
    A(f"| 최상위 함수 | {len(G)} | {len(C)} |")
    A(f"| 공통 | {len(common)} (그중 동일 {len(same)}, 차이 {len(diff_rows)}) | |")
    A(f"| 한쪽에만 | {len(only_g)} | {len(only_c)} |")
    A("")

    A(f"{H}{H} 정리 — 어디가 앞섰나")
    A("")
    A("| 항목 | 판정 | 내용 |")
    A("|---|---|---|")
    for n, v in [(k, v) for k, v in VERDICT.items() if v[0]]:
        A(f"| `{n}` | {v[0]} | {v[1]} |")
    A("")
    A("**이미 반영한 것(2026-09-16)**: 512 슬롯 토큰 창 게이트·좌/우 좌표 제거·약한 가중치 강등·"
      "한글 제거·나이대 압축·`neg_safety_terms` 폴백. 근거와 실측은 `README_local.md` 5-6절.")
    A("")

    A(f"{H}{H} 공통 함수 중 구현이 다른 것 ({len(diff_rows)}개)")
    A("")
    A("diff 줄 수가 클수록 두 레포가 다른 생각을 하고 있습니다. 판정이 빈 칸은 '차이만 확인, 미조사'입니다.")
    A("")
    A("| 함수 | diff 줄 | 판정 | 근거 |")
    A("|---|---:|---|---|")
    for n, ln, has_code in sorted(diff_rows, key=lambda x: -x[1]):
        v = VERDICT.get(n)
        if v is None and not has_code:
            v = ("주석만 다름", "코드는 같고 설명 문장만 다르다 — 동기화 불필요.")
        if v is None and ln <= 10:
            v = ("미소 차이", "인자 이름·문구 수준 차이만 확인. 동작 차이가 있는지는 다음 비교 때 확인한다.")
        if v is None and n.startswith("_anima_"):
            v = ("2026-09-16 이식본", "정본에서 가져왔지만 우리 프롬프트 형태에 맞춰 다시 썼다 "
                  "(정본은 BREAK 섹션을 알아듣고 섹션 상한·POV 가드를 적용하고, 우리는 평문에서 토큰량만 본다).")
        v = v or ("미조사", "")
        A(f"| `{n}` | {ln} | {v[0]} | {v[1]} |")
    A("")

    ALIAS = {"_sync_anima_trigger_for_episode": "_sync_artist_trigger"}   # 이름까지 바뀐 것은 손으로 적는다
    alias = dict(ALIAS)
    for n in only_g:
        b = n.lstrip("_")
        if b in C:
            alias[n] = b
    A(f"{H}{H} 정본에만 있는 함수 ({len(only_g)}개)")
    A("")
    if alias:
        A("아래 것은 **정본에는 접두 언더스코어(`_`)만 다르고 같은 일을 하는 함수가 우리 쪽에 있습니다** — "
          "따로 가져올 필요가 없습니다. " + ", ".join(f"정본 `{k}` ↔ 우리 `{v}`" for k, v in sorted(alias.items())) + ".")
        A("")
    buckets = {}
    for n in only_g:
        c, note = cat(n, GUI_CATS)
        buckets.setdefault(c, [note, []])[1].append(n)
    A("| 카테고리 | 개수 | 함수 | 무엇을 하는 것들인가 |")
    A("|---|---:|---|---|")
    for c in sorted(buckets, key=lambda k: -len(buckets[k][1])):
        note, names = buckets[c]
        shown = []
        for x in names:
            shown.append(f"`{x}`" + (f" (=우리 `{alias[x]}`)" if x in alias else ""))
        A(f"| {c} | {len(names)} | " + ", ".join(shown) + f" | {note} |")
    A("")

    A(f"{H}{H} 우리에만 있는 함수 ({len(only_c)}개)")
    A("")
    buckets = {}
    for n in only_c:
        c, note = cat(n, OUR_CATS)
        buckets.setdefault(c, [note, []])[1].append(n)
    A("| 카테고리 | 개수 | 함수 | 무엇을 하는 것들인가 |")
    A("|---|---:|---|---|")
    for c in sorted(buckets, key=lambda k: -len(buckets[k][1])):
        note, names = buckets[c]
        A(f"| {c} | {len(names)} | " + ", ".join(f"`{x}`" for x in names) + f" | {note} |")
    A("")

    A(f"{H}{H} 다음에 동기화할 때 우선순위로 보면")
    A("")
    A("| 순위 | 항목 | 쪽 | 이유 |")
    A("|---|---|---|---|")
    A("| 1 | `_tidy_prompt_text`(문법·구두점 정규화) | 정본→우리 | 우리도 LLM이 컷 프롬프트를 짓는다. "
      "창 게이트 앞에 두면 잘리기 전에 잘못된 구두점(이중 쉼표, `and and`)이 준다 — 미이식 항목. |")
    A("| 2 | `_analyze_tag_loop`(퇴행 루프 감지) | 정본→우리 | 우리는 `_dedupe_weighted_tags`로 겹침만 지운다. "
      "루프 자체를 잡으면 잘라낼 양이 줄어든다. |")
    A("| 3 | `_canonicalize_counters` | 정본→우리 | `1 boy`→`1boy`. 우리는 헤더에서 직접 맞추지만 LLM이 본문을 "
      "바꾸면 무너진다. |")
    A("| 4 | 창 게이트의 섹션 인식(POV 가드·섹션 상한) | 우리→정본 | 우리는 평문 재단이라 섹션 상한이 없다. "
      "정본은 섹션을 안다 — 두 방식을 합치면 양쪽 다 이득. |")
    A("| 5 | `_comfyui_output_dirs` 의 하드코딩 경로 제거 | 우리→정본 | 정본에는 개인 마운트 절대경로가 남아 있다. |")
    A("| — | BREAK 5섹션 재조립·`_format_angle_menu` | 가져오지 않음 | 우리 컷 프롬프트는 LLM이 한 장의 문장으로 "
      "짓고, 화각은 컷 스크립트가 고른다. |")
    A("")
    A(f"{H}{H} 다시 만들기")
    A("")
    A("```bash")
    A("venv/bin/python analysis_cut/anima_diff.py --novel <정본 anima_gen.py 경로>")
    A("```")
    A("")

    with open(a.out, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print(f"wrote {a.out}  ({len(L)}줄, 공통 {len(common)}/차이 {len(diff_rows)}, 정본전용 {len(only_g)}, 우리전용 {len(only_c)})")


if __name__ == "__main__":
    main()
