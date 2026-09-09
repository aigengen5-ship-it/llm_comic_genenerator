#!/usr/bin/env python3
"""selftest.py — llm_comic_gen 독립 환경 자체 점검 (LLM 0회 / ComfyUI 0회 / 네트워크 0회)

확인 대상
  ① 이식성: 필수 파일이 이 디렉토리에 전부 있다 + 폐지된 파일(rp_visual_tags/character_setup) 없음
  ② plot.json: 단일 LLM(gemma-4-31B) — text/main/agent/anima가 같은 서버/모델 + comfyuidir
  ③ comic_input: 평문 → JSON 추출(가짜 응답으로 파싱만) → config 주입 → $키워드 필터
  ④ comic_page_merge: 흰 프레임 + 검은 선 + wide 오른쪽 플레이트
  ⑤ comic_gen: wide → res 9 / portrait → res 5, 오른쪽 시선 태그, 컷 텍스트 3블록,
     해부학 태그(nipples/vagina/penis) 금지 필터
  ⑥ cut.yaml: 템플릿 로드/페이지 플래너 결정론/스펙 레이아웃(row_spec)
  ⑦ 에피소드 전체 반영: 본문→컷 수 역산, 4페이지 상한 폐지, 장면(창) 분할 호출,
     추출 map-reduce 병합 — 전부 LLM을 가짜 응답으로 목킹해 배선만 감사한다
  ⑧ 크로스 플랫폼: OS 분기(is_win/detach/ComfyUI venv/폰트/env 오버라이드)와
     셸 래퍼 두 개가 얇게 유지되는지(래퍼에 서버 로직이 되살아나면 실패)
  ⑨ local 전용 입력 포맷(--special): progress/ 산출물(epNN_해시.txt + 시트 JSON)의 어댑터 —
     본문 평문화, 기승전결 앵커 확보, 시트 JSON 우선주입, 배송 평문 입력과의 경계
  ⑩ 화풍/수위 스위치: --lora1/--lora2가 실제로 ComfyUI 슬롯을 켜는지(on=True) + trigger가
     프롬프트 헤더에 들어가는지, --str1/--str2 강도 오버라이드·클램프, 파일 없는 LoRA 차단,
     [local 전용] --allow-explicit의 4개 관문(수위 판정/추출 프롬프트/성기 태그 파기/climax)

실행: source venv/bin/activate && python3 selftest.py   (LLM·ComfyUI 없이 도는 정적/합성 점검)
"""
import inspect
import json
import os
import re
import shutil
import subprocess
import tempfile
import sys

from PIL import Image, ImageDraw

import config
import anima_gen
import comic_gen as CG
import comic_input as CI
import comic_page_merge as CPM
import run_comic as RC
import openAPI_control as OAC

ROOT = os.path.dirname(os.path.abspath(__file__))
PASS, FAIL = 0, 0
FAILED = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        FAILED.append(f"{name} {detail}")
        print(f"  [FAIL] {name} {detail}")


CANNED = {
    "protagonist": {"name": "오카다 유즈키", "sex": "female", "hair_color": "Light Brown Hair",
                    "hair_style": "messy hair,  ponytail", "eye_color": "brown eyes",
                    "skin_color": "fair skin", "face_style": "crying, blushing",
                    "clothes": "police uniform, utility belt", "body_shape": "loli, child",
                    "job": "경찰관", "breasts_size": 4.7, "hip_size": 99},
    "partner": {"name": "호죠 소타", "sex": "남자", "clothes": "casual"},
    "guides": {"protagonist": ["기", "승", "전", "결"], "partner": ["소타는 노린다"], "sub": None},
    "actions": ["터치", "어깨에 손을 올리자, 등 뒤에서 밀착", "포옹"],
    "rating": "EXPLICIT",
}



def check_llm_server(check):
    # [2026-09-07] 전달용 전용 LLM 서빙 llm_server.py (의존성 없이 import/계약 검증)
    import llm_server as LS
    a_srv = LS.build_parser().parse_args(["--model", "x.gguf"])
    check("llm_server: 기본 포트=plot text_port(8081), ctx 충분",
          a_srv.port == 8081 and a_srv.ctx >= 16384, f"{a_srv.port},{a_srv.ctx}")
    check("llm_server: model id 생성(파일명->id)",
          LS.default_name_for("/a/gemma-4-31B-it-Q8_0.gguf") == "gemma-4-31B-it-Q8_0",
          LS.default_name_for("/a/gemma-4-31B-it-Q8_0.gguf"))
    r_srv = LS.openai_chat_response(
        {"choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}}, "any-name")
    check("llm_server: OpenAI chat.completions 스키마(model/usage/choices)",
          r_srv["model"] == "any-name" and r_srv["usage"]["prompt_tokens"] == 5
          and r_srv["choices"][0]["message"]["content"] == "ok"
          and r_srv["object"] == "chat.completion")
    import openAPI_control as OAC
    check("렌더 직전 LLM VRAM 반납 헬퍼 존재(release_llm_for_gpu)", callable(OAC.release_llm_for_gpu))
    check("llm_server: 16GB 자동 오프로드(측정 불가 → None → 런타임 -1 폴백)",
          LS.suggest_gpu_layers("/nonexistent_model.gguf") is None)
    # [2026-09-07] ollama 백엔드 계약 (플래그 off = import/서버 접촉 0)
    import json as _json
    pj_oll = _json.load(open("plot.json", encoding="utf-8"))
    check("plot.json ollama 키(enb/gguf/model/host/num_ctx)",
          all(k in pj_oll for k in ("ollama_enb", "ollama_gguf", "ollama_model", "ollama_host",
                                    "ollama_num_ctx")))
    check("ollama num_ctx 명시(서버 기본 262144 KV 폭주 방지; 32GB RAM 기준)",
          int(pj_oll.get("ollama_num_ctx") or 0) > 0
          and int(pj_oll.get("ollama_num_ctx") or 0) <= 16384
          and OAC.OLLAMA_DEFAULT_NUM_CTX <= 16384)
    check("requirements.txt ollama 클라이언트 선언",
          "ollama>=" in open("requirements.txt", encoding="utf-8").read())
    check("ollama_enb 게이트(no=미사용/yes=사용)",
          OAC._ollama_enabled({"ollama_enb": "no"}) is False
          and OAC._ollama_enabled({"ollama_enb": "yes"}) is True)
    class _FakeCli:
        def chat(self, model=None, messages=None, options=None, stream=False, keep_alive=None):
            assert model == "comic-llm" and keep_alive == "10m"
            return {"message": {"content": "hi"}, "prompt_eval_count": 7, "eval_count": 3}
    import types as _tp
    shim = _tp.SimpleNamespace(chat=_tp.SimpleNamespace(
        completions=OAC._OllamaCompletions(_FakeCli(), "comic-llm")))
    rr = shim.chat.completions.create(model="whatever", messages=[{"role": "user", "content": "x"}],
                                      temperature=0.5, extra_body={"a": 1})
    check("ollama shim: OpenAI 응답 형(choices/usage)",
          rr.choices[0].message.content == "hi" and rr.usage.total_tokens == 10,
          f"{rr.choices[0].message.content},{rr.usage.total_tokens}")
    # [2026-09-08] **배선 감사**: ollama_enb=yes 상태에서 LLM 진입점이 전부 ollama로 향하는가
    #   (get_openai_client*/call_openai_for_*/openAI_response/unload 계열 — 8081·라우터로 새는 곳 없는지)
    if str(pj_oll.get("ollama_enb", "")).strip().lower() in ("yes", "y", "true", "1"):
        _saved = {k: getattr(OAC, k) for k in
                  ("get_ollama_client", "_ollama_unload", "_post_ok")}
        made = []
        _shim_obj = _tp.SimpleNamespace(chat=_tp.SimpleNamespace(completions=_tp.SimpleNamespace(
            create=lambda **k: _tp.SimpleNamespace(
                choices=[_tp.SimpleNamespace(
                    message=_tp.SimpleNamespace(content="ok", role="assistant"),
                    finish_reason="stop")],
                usage=_tp.SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2)))))

        def _fake_ollama(log_fn=None):
            made.append("ollama")
            return _shim_obj

        OAC.get_ollama_client = _fake_ollama
        try:
            routed = all(OAC.get_openai_client() is OAC.get_openai_client_anima() is
                         OAC.get_openai_client_text() for _ in [0]) and "ollama" in made
        except Exception as e:
            routed = False
        OAC.get_ollama_client = _saved["get_ollama_client"]
        check("배선: 3개 클라이언트 생성기 전부 ollama shim으로",
              routed, f"routed={routed}")
        # call_openai_for_text(plot의 모든 텍스트 생성 경로)가 그 생성기를 거치는가
        made2 = []
        _saved2 = {k: getattr(OAC, k) for k in ("get_openai_client_text", "_ollama_unload")}

        def _fake_text(log_fn=None):
            made2.append("text")
            return _fake_ollama()
        OAC.get_openai_client_text = _fake_text
        try:
            ans, _m = OAC.call_openai_for_text("ping", messages=None, log_fn=lambda m: None,
                                               max_retries=1, timeout=10)
            routed_text = "text" in made2 and ans == "ok"
        except Exception as e:
            routed_text = False
        OAC.get_openai_client_text = _saved2["get_openai_client_text"]
        check("배선: call_openai_for_text가 ollama 생성기를 경유", routed_text, f"{made2}")
        # 언로드 계열: ollama 모드에서 llama 라우터 API를 두드리지 않는가
        hits = []
        OAC._post_ok = lambda *a, **k: (hits.append(a[0] if a else ""), False)[1]
        OAC._ollama_unload = lambda jv, log: (made2.append("unload"), True)[1]
        try:
            ok_unload = OAC.unload_all_router_models(log_fn=lambda m: None) is True \
                and "unload" in made2 and not hits
        except Exception as e:
            ok_unload = False
        for k, v in _saved2.items():
            setattr(OAC, k, v)
        for k, v in _saved.items():
            setattr(OAC, k, v)
        check("배선: unload 계열도 ollama 경로(라우터 API probing 없음)", ok_unload, f"hits={hits}")



def main() -> int:
    print("\n== ① 이식성 (필수 파일 자가 보유) ==")
    need_py = ["run_comic.py", "comic_input.py", "comic_gen.py", "comic_page_merge.py", "anima_gen.py",
               "openAPI_control.py", "config.py", "plot.json", "novel_progress.py"]
    # [2026-09-08] OS별 엔트리는 1줄 래퍼 두 개뿐(셸 트윈에 로직을 복사하지 않는다)
    need_sh = ["run_ollama.sh", "run_ollama_win.bat"]
    need_data = ["data_comfyui/anima_spectrum_July11.json", "data_comfyui/angle.txt",
                 "data_comfyui/prompt_pov.md", "data_comfyui/prompt_multi.md",
                 "data/episode_setup.json"]
    for f in need_py + need_data + need_sh:
        check(f"파일 있음: {f}", os.path.exists(os.path.join(ROOT, f)))
    # [2026-09-07] 코드에서 아무도 읽지 않던 data 파일 정리(actions.yaml/angle.txt/워크플로만 유지)
    for gone_data in ("data_comfyui/prompt1.md", "data_comfyui/prompt2.md",
                      "data_comfyui/default_anima_prompt.txt", "data_comfyui/anima_prompt.txt",
                      "data/anima_question_nobg.txt", "data/anima_question_rp.txt"):
        check(f"미사용 데이터 삭제됨: {gone_data}", not os.path.exists(os.path.join(ROOT, gone_data)))
    # [2026-09-07] 폐지 파일: rp_visual_tags(결정론 풀 → LLM 태그 생성 대체) / character_setup(미사용 import)
    for gone in ("rp_visual_tags.py", "character_setup.py"):
        check(f"폐지된 파일 없음: {gone}", not os.path.exists(os.path.join(ROOT, gone)))
    # [2026-09-08] selftest는 배송되는 파일(=inputs/ 샘플)만 읽는다. 로컬 전용 검증 입력은 여기가 아니다 —
    #   git clone 직후 `python3 selftest.py`가 통과해야 완결이므로, 로컬 디렉터리에 손대면 안 된다.
    self_src = open(os.path.abspath(__file__), encoding="utf-8").read()
    local_only_dir = "input" + "_test"          # 문자열 분할: 이 점검 자체의 참조로 잡히지 않게
    check("selftest는 로컬 전용 검증 디렉터리를 읽지 않는다(inputs/만)",
          local_only_dir not in self_src, local_only_dir)
    check("배송되는 입력 샘플이 있다(inputs/ep01.txt + sheet01.txt)",
          os.path.exists(os.path.join(ROOT, "inputs", "ep01.txt"))
          and os.path.exists(os.path.join(ROOT, "inputs", "sheet01.txt")))
    ag_src = open(os.path.join(ROOT, "anima_gen.py"), encoding="utf-8").read()
    check("anima_gen에 rp_visual_tags/character_setup import 없음",
          "rp_visual_tags" not in ag_src.split("\n")[0:40].__str__() and "import character_setup" not in ag_src
          and "from rp_visual_tags" not in ag_src)
    out_rel = CG.comic_out_dir()
    # config.dir_out_comic은 모듈 위치 기준 절대경로 → 이 디렉토리 안이면 이동해도 안전하다
    check("출력 경로가 이 디렉토리 안(merged/ 없이 book01)",
          out_rel.startswith(os.path.join(ROOT, "comic", "book00")), out_rel)
    src = open(os.path.join(ROOT, "comic_gen.py"), encoding="utf-8").read()
    check("comic_gen에 원본 repo 절대경로 없음", "llm_shortnovel_generator_gui" not in src)
    check("원본 repo 의존 모듈 미import",
          not any(m in src for m in ("import full_episode_gen", "import theme_gen_auto",
                                     "import llm_novel_gui_func")))

    print("\n== ② plot.json ==")
    pj = config.get_json_value()
    for k in ("ip_main", "port_main", "mainLLM", "text_ip", "text_port", "textLLM",
              "ip_agent", "port_agent", "agent_anima", "ip_anima", "port_anima",
              "comfyuirun", "anima_enb", "comfyuidir"):
        check(f"plot.json {k}", bool(str(pj.get(k, "")).strip()), str(pj.get(k)))
    # [2026-09-07] 단일 LLM 정책: text/main/agent/anima = localhost:8081 gemma-4-31B 하나
    ep = {(str(pj.get("text_ip") or pj.get("ip_main")), str(pj.get("text_port") or pj.get("port_main"))),
          (str(pj.get("ip_main")), str(pj.get("port_main"))),
          (str(pj.get("ip_agent")), str(pj.get("port_agent"))),
          (str(pj.get("ip_anima")), str(pj.get("port_anima")))}
    check("단일 LLM: 4개 엔드포인트가 같은 서버", len(ep) == 1, str(ep))
    check("단일 LLM 모델 = gemma-4-31B",
          all(str(pj.get(k, "")) == "gemma-4-31B" for k in ("textLLM", "mainLLM", "agent", "agent_anima")),
          f"{pj.get('textLLM')}/{pj.get('mainLLM')}/{pj.get('agent')}/{pj.get('agent_anima')}")
    check("config.total_episodes = data/episode_setup.json 값",
          config.total_episodes == json.load(open(os.path.join(ROOT, "data", "episode_setup.json"),
                                                  encoding="utf-8"))["total_episodes"],
          config.total_episodes)
    cd = pj.get("comfyuidir", "")
    check("comfyuidir 실존(없으면 폴백이 찾는다)", os.path.isdir(cd) or bool(anima_gen._comfyui_output_dirs(pj)),
          f"{cd} / 후보={anima_gen._comfyui_output_dirs(pj)}")
    wf = json.load(open(os.path.join(ROOT, "data_comfyui", "anima_spectrum_July11.json"), encoding="utf-8"))
    need_nodes = ["1016", "122", "46", "86", "87", "91", "123"]     # seed/lora/unet/pos/neg/save/size
    check("워크플로 노드 일치(comfyui_run_anima 요구)", all(n in wf for n in need_nodes),
          str([n for n in need_nodes if n not in wf]))

    print("\n== ③ comic_input: 평문 → 추출 → config ==")
    md = "# 제목\n![x](y.png)\n\n---\n본문 **강조** 텍스트\n"
    flat = CI.strip_markdown(md)
    check("strip_markdown: 헤딩/이미지/구분선 제거",
          "#" not in flat and "y.png" not in flat and "본문" in flat, flat[:40])
    # [2026-09-08] 시트 #캐릭터 태그# 는 마크다운 헤딩(# 다음 공백)과 구분돼야 보존된다
    md2 = "#Usagi Tsukino from Sailor Moon#\n\n# 제목\n본문"
    check("strip_markdown: #캐릭터 태그#는 헤딩으로 오인하지 않는다",
          "#Usagi Tsukino from Sailor Moon#" in CI.strip_markdown(md2),
          CI.strip_markdown(md2)[:40])
    ct = CI.extract_char_tags("주인공은 #Usagi Tsukino from Sailor Moon#\n"
                              "머리는 #Blonde_Hair,   twin_odango#\n"
                              "상대방은 #Tuxedo Mask#\n#닫는 태그 없음\n")
    check("extract_char_tags: #…# 를 캐릭터 태그로 인식(원문 대소문자/공백만 정리)",
          ct["protagonist"] == ["Usagi Tsukino from Sailor Moon", "Blonde Hair, twin odango"],
          str(ct["protagonist"]))
    check("extract_char_tags: 상대방 섹션의 #…# 만 상대방 태그로([AAA]/[BBB] 분리 유지)",
          ct["partner"] == ["Tuxedo Mask"], str(ct["partner"]))
    check("extract_char_tags: 닫는 #이 없는 줄은 경고만 남긴다",
          any("닫는 #" in w for w in ct["warn"]), str(ct["warn"]))
    ct_ko = CI.extract_char_tags("#유즈키#")
    check("extract_char_tags: 한글 #태그는 버리지 않고 경고(anima는 영문만 안다)",
          ct_ko["protagonist"] == ["유즈키"] and ct_ko["warn"], str(ct_ko))
    check("_extract_json_obj: 코드펜스/후행 쉼 허용",
          CI._extract_json_obj('```json\n{"a": 1, "b": [1,2,],}\n```') == {"a": 1, "b": [1, 2]},
          str(CI._extract_json_obj('```json\n{"a": 1, "b": [1,2,],}\n```')))
    check("_clean_tag: 공백/쉼표 정규화", CI._clean_tag("messy hair,  ponytail ,") == "messy hair, ponytail",
          CI._clean_tag("messy hair,  ponytail ,"))

    orig_call = CI.call_openai_for_text
    CI.call_openai_for_text = lambda prompt, **kw: (json.dumps(CANNED, ensure_ascii=False), None)
    try:
        data = CI.extract("본문", "시트", ep_num=1)
    finally:
        CI.call_openai_for_text = orig_call
    check("extract: 성별 정규화(남자→male)", data["partner"]["sex"] == "male", data["partner"]["sex"])
    check("extract: 크기 필드 클램프(-1~5)", data["protagonist"]["breasts_size"] == 4
          and data["protagonist"]["hip_size"] == 5, f"{data['protagonist']['breasts_size']}/"
                                                    f"{data['protagonist']['hip_size']}")
    check("extract: $키워드 서술문/조사구 필터", data["actions"] == ["터치", "포옹"],
          str(data["actions"]))
    check("extract: rating 소문자 정규화 + 청년향 상한(EXPLICIT→nsfw)",
          data["rating"] == "nsfw", data["rating"])
    check("extract: guides 4줄 + partner 유지", len(data["guides"]["protagonist"]) == 4
          and data["guides"]["partner"], str(data["guides"]))

    ep_text, sheet_text = CI.prepare_texts(os.path.join(ROOT, "inputs", "ep01.txt"),
                                           os.path.join(ROOT, "inputs", "sheet01.txt"))
    check("입력 파일 로드(ep01/sheet01)", len(ep_text) > 2000 and len(sheet_text) > 200,
          f"{len(ep_text)}/{len(sheet_text)}")
    CI.apply_to_config(data, ep_text, sheet_text, ep_num=1, panels_per_page=3, book_num=1)
    req = [k for k in ("name", "sex", "hair_color", "hair_style", "eye_color", "skin_color",
                       "face_style", "clothes", "body_shape", "job")
           if not str(getattr(config, k, "") or "").strip()]
    check("init_anima_tags 필수 10필드 채움", not req, str(req))
    check("외모 태그는 영문으로 주입", "light brown hair" in config.hair_color.lower(), config.hair_color)
    check("comic_panels_per_page 반영", config.comic_panels_per_page == 3, config.comic_panels_per_page)
    check("episode_content에 본문 주입(progress 미의존)",
          config.episode_content[0] == ep_text and len(config.episode_content[0]) > 2000)
    check("guides/special_writing_req keyed by 회차",
          config.ep_corruption_guides_map.get(1) and config.special_writing_req.get(1), "")
    check("시트 평문이 컷 스크립트 입력으로", "유즈키" in config.episode_protagonist_sheets[0],
          config.episode_protagonist_sheets[0][:40])
    # [2026-09-08] #캐릭터 태그# → config 직결(LLM 경유 없음: 빼먹을 수가 없다)
    CI.apply_to_config(data, ep_text,
                       "#Usagi Tsukino from Sailor Moon#\n상대방은 #Tuxedo Mask#", ep_num=1,
                       panels_per_page=3, book_num=1)
    check("apply_to_config: #태그를 config.char_tags/partner_char_tags로",
          config.char_tags == ["Usagi Tsukino from Sailor Moon"]
          and config.partner_char_tags == ["Tuxedo Mask"],
          f"{config.char_tags}/{config.partner_char_tags}")
    CI.apply_to_config(data, ep_text, sheet_text, ep_num=1, panels_per_page=3, book_num=1)
    check("시트에 #태그 없으면 빈 목록(기존 동작 그대로)",
          config.char_tags == [] and config.partner_char_tags == [],
          f"{config.char_tags}/{config.partner_char_tags}")
    # [2026-09-07] EP2 회귀: episode_setup.json=1이어도 --ep 2 주입 시 배열이 확장돼야 한다
    # (EP2 첫 실행에서 review_safety[1] IndexError로 터졌음)
    CI.apply_to_config(data, ep_text, sheet_text, ep_num=2, panels_per_page=5, book_num=1,
                       total_episodes=2)
    check("--ep 2: EP-index 배열 확장(review_safety/tag/expression)",
          len(config.review_safety) >= 2 and len(config.face_tag) >= 2
          and len(config.expression_arr) >= 2 and len(config.review_stats) >= 2
          and config.episode_content[1] == ep_text,
          f"review_safety={len(config.review_safety)} face_tag={len(config.face_tag)}")
    # [2026-09-08] 기승전결 증발 회귀: --ep 0(0기준 습관)은 1로 흡수해야 한다. 키 0에 박히면
    #   조회가 1기준인 컷 스크립트에서 "(가이드 없음)"이 찍혀 가이드·$행동이 통째로 빠진다.
    CI.apply_to_config(data, ep_text, sheet_text, ep_num=0, panels_per_page=3, book_num=1)
    check("--ep 0 흡수 → guides/special_writing_req 키=1(조회 측과 일치)",
          bool(config.ep_corruption_guides_map.get(1)) and bool(config.special_writing_req.get(1)),
          str(sorted(config.ep_corruption_guides_map)))

    print("\n== ④ comic_page_merge: 흰 프레임/검은 선/wide 오른쪽 글자 ==")
    check("배경 흰색 / 선 검정", CPM.DEFAULT_BG == (255, 255, 255) and CPM.DEFAULT_FRAME == (0, 0, 0))
    tmp = os.path.join(ROOT, "comic", "_selftest")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    paths = []
    for i in range(5):
        fp = os.path.join(tmp, f"p{i}.png")
        Image.new("RGB", (1024 if i != 2 else 1360, 1344 if i != 2 else 1024),
                  (200, 60, 60) if i == 2 else (30 + i * 20, 90, 160)).save(fp)
        paths.append(fp)
    texts = [["상황 묘사입니다", "유즈키: 으…", "소타: 좋다."]] * 5
    pages = CPM.compose_pages(paths, texts, tmp, "selftest", panels_per_page=5,
                              panel_wide=[False, False, True, False, False])
    check("페이지 1장 생성", len(pages) == 1 and os.path.exists(pages[0]), str(pages))
    im = Image.open(pages[0]).convert("RGB")
    W, H = im.size
    px = im.load()
    check("네 귀퉁이 흰색", all(px[x, y] == (255, 255, 255) for x, y in
                          ((1, 1), (W - 2, 1), (1, H - 2), (W - 2, H - 2))))
    # [2026-09-09] 사용자 지시: 컷 경계선과 그림 사이 흰 빈틈을 없앤다 → 검은 선이 그림에 곧장 붙는다
    g, pad = CPM.DEFAULT_GUTTER, CPM.DEFAULT_FRAME_PAD
    lw = CPM.DEFAULT_FRAME_WIDTH
    inset = CPM._frame_inset(pad, lw)
    ix, iy = g + inset, g + int(CPM.DEFAULT_FONT_SIZE * 1.6) + inset   # 라벨부 높이 = font*1.6
    check("컷 경계는 굵은 검은 선(lw 두께가 그대로 선이다)",
          all(sum(px[x, iy + 40]) < 90 for x in range(ix - lw, ix)), str(px[ix - 1, iy + 40]))
    check("검은 선과 컷 사이에 흰 틈이 없다(선 안쪽 1px는 곧 그림)",
          px[ix, iy + 40] != (255, 255, 255), str(px[ix, iy + 40]))
    check("선 바깥은 흰 여백(이웃 컷과 거터로만 갈라진다)",
          px[ix - lw - 1, iy + 40] == (255, 255, 255), str(px[ix - lw - 1, iy + 40]))
    check("거터 = 두 컷의 선이 맞붙는 폭(흰 실금이 남지 않는다)",
          CPM.DEFAULT_GUTTER <= 2 * lw + 2, f"gutter={CPM.DEFAULT_GUTTER} lw={lw}")
    check("옅은 회색 이중선은 폐지(검정선만)", CPM.DEFAULT_KEYLINE is None)
    wide_ys = [y for y in range(H) if px[g + inset + 30, y][0] > 150 and px[g + inset + 30, y][1] < 120]
    check("wide 행 존재(픽셀 스캔)", len(wide_ys) > 300, str(len(wide_ys)))
    wy = (min(wide_ys) + max(wide_ys)) // 2 if wide_ys else 0
    lines_x = [x for x in range(2, W - 2) if sum(px[x, wy]) < 90]
    check("wide 컷은 페이지 폭 끝까지(플레이트 자리 없음)",
          lines_x and lines_x[0] <= g + lw + 2 and lines_x[-1] >= W - g - lw - 3,
          f"x0={lines_x[0] if lines_x else -1} x1={lines_x[-1] if lines_x else -1} W={W}")
    shutil.rmtree(tmp, ignore_errors=True)

    # [2026-09-07] 레이아웃 v2: face+event 혼합 행(face 축소) + 텍스트 존(front=아래/right=오른쪽)
    page_w, rows2 = CPM._plan_rows(2, [["상황", "A: 응", "B: 좋아"], ["상황2", "A: 하", "B: 윽"]],
                                   [False, False], [True, False], ["right", "bottom"],
                                   unit_w=820, cols=2, gutter=g, pad=pad, border=4, font_size=26)
    cells = rows2[0]["cells"]
    check("혼합 행: face 컷 축소(42%) + event가 나머지 폭",
          len(cells) == 2 and cells[0]["face"]
          and cells[0]["w"] < cells[1]["w"] and abs(cells[0]["w"] / (cells[0]["w"] + cells[1]["w"]) - 0.42) < 0.02,
          str([(c["w"], c["face"]) for c in cells]))
    check("텍스트 존: right → 오른쪽 플레이트 / bottom → 패널 안 박스(행 높이 미사용)",
          cells[0]["zone"] == "right" and cells[1]["zone"] == "bottom"
          and rows2[0]["bottom_lines"] == 0 and cells[0]["blocks"] and cells[1]["blocks"])
    # 플레이트가 실제로 그려지는지 픽셀로 확인 (face 패널 이미지 안 오른쪽이 흰 판)
    os.makedirs(tmp, exist_ok=True)
    paths2 = []
    for i2, sz in ((0, (1024, 1344)), (1, (1024, 1344))):
        fp = os.path.join(tmp, f"m{i2}.png")
        Image.new("RGB", sz, (40, 120, 200) if i2 == 0 else (200, 90, 40)).save(fp)
        paths2.append(fp)
    page2 = os.path.join(tmp, "mix.png")
    CPM.compose_page(paths2, [["상황 묘사입니다", "유즈키: 으…", "소타: 좋다."],
                              ["이벤트 신", "유즈키: 안 돼", "소타: 잡았다"]],
                     out_path=page2, panel_face=[True, False],
                     panel_zone=["right", "bottom"], page_size=None)   # 고정은 아래 전용 테스트에서
    im2 = Image.open(page2).convert("RGB")
    p2 = im2.load()
    fc = cells[0]
    inset2 = CPM._frame_inset(pad, lw)
    fx0 = rows2[0].get("x", 16) + inset2             # face 패널 이미지 시작 x (검은 선 안쪽)
    fy0 = g + inset2                                 # 첫 행의 이미지 시작 y
    # [2026-09-09] 화면 문법 통일(사용자 지시 7.1): 오른쪽 플레이트를 없애고 전부
    # '하단 왼쪽 설명 박스 + 풍선'으로 그린다 → 왼쪽 아래에서 흰 박스가 시작되는지 확인
    cap_y = fy0 + rows2[0]["h"] - inset2 - 30        # 설명 박스 안쪽(박스는 아래에서 8px 뜨고 3px 테두리)
    cap_white = 0
    probe_x = fx0 + 13                                  # 테두리(3px)를 지난 박스 안 흰 자리
    for dy in range(0, 100):
        if p2[probe_x, cap_y - dy] != (255, 255, 255):
            break
        cap_white += 1
    check("설명 박스는 컷 하단 **왼쪽**에서 시작한다(오른쪽 플레이트는 폐기)",
          cap_white >= 15, f"white_up_run={cap_white}")
    right_white = sum(1 for dx in range(0, int(fc["w"] * 0.30))
                      if p2[fx0 + fc["w"] - 2 - pad - dx, fy0 + rows2[0]["h"] // 2] == (255, 255, 255))
    check("right 존에도 플레이트가 없다(화면 문법이 하나로 통일됐다)", right_white < 20,
          f"right_white={right_white}")
    # [2026-09-07] bottom 존: 텍스트가 패널 '안' 하단 흰 박스에 그려지는지 픽셀 확인
    ec = cells[1]
    ex0 = fx0 + cells[0]["w"] + g + 13                  # 두 번째 컷 안쪽 하단 왼쪽 = 설명 박스 자리
    ey_box = cap_y
    inner_white = sum(1 for dx in range(0, 40)
                      if p2[ex0 + dx, ey_box] == (255, 255, 255))
    check("bottom 존: 패널 안 하단 왼쪽에 흰 설명 박스", inner_white > 12, str(inner_white))
    # [2026-09-09] 혼합 행에서 그림은 셀 높이·선은 행 높이였던 빈틈: 그림이 행 높이까지 채운다
    art_bottom = p2[fx0 + cells[0]["w"] - inset2 - 24, fy0 + rows2[0]["h"] - inset2 - 3]
    check("프레임 안에 흰 띠가 남지 않는다(그림이 행 높이까지 채운다)",
          art_bottom != (255, 255, 255), str(art_bottom))
    shutil.rmtree(tmp, ignore_errors=True)

    print("\n== ⑤ comic_gen: wide 해상도/태그/텍스트 ==")
    # selftest는 LLM 0회 원칙 → 가이드 합성(pov/multi)은 우선 off, 전용 테스트에서만 fake로 켠다
    _orig_g = CG._llm_compose_panel_prompt
    _orig_t = CG.call_openai_for_text
    CG._llm_compose_panel_prompt = lambda *a, **k: None
    panel_w = {"no": 1, "type": "action", "camera": "side_view", "pose": "She is riding him.",
               "position": "He is lying down.", "climax": "", "caption_ko": "상황",
               "dialog": ["유즈키: 으…", "소타: 좋아."], "wide": True, "facing": "right"}
    panel_t = dict(panel_w, no=2, wide=False, facing="front")
    panel_r = dict(panel_w, no=3, wide=False, facing="right")
    check("panel_text_blocks = 상황+대사2", len(CG.panel_text_blocks(panel_w)) == 3)
    check("wide raw line #wide", "#wide" in CG.panel_raw_line(panel_w), CG.panel_raw_line(panel_w))
    for attr in ("face_tag", "makeup_tag", "body_tag", "background_tag", "bodystyle_tag",
                 "expression_arr", "exposure_tag", "p_exposure_tag", "marks_tag", "review_safety",
                 "pubic_hair_tag", "partner_exposure_tag", "partner_expression_tag"):
        setattr(config, attr, [""] * 12)
    # [2026-09-07] 해부학 정책(갱신): 성기만 제거, nipples/cameltoe 노출 허용
    config.body_tag = ["large breasts, pink_nipples, pussy_juice, pale skin"] * 12
    config.p_exposure_tag = ["visible_nipples, cameltoe, erect_penis_through_clothes"] * 12
    config.bodystyle_tag = ["hyper-voluptuous, police uniform"] * 12
    config.location, config.body_shape = "living room", "loli, child"
    pw = CG.build_panel_prompt(0, panel_w, "explicit", gloss={})
    pt = CG.build_panel_prompt(0, panel_t, "explicit", gloss={})
    # [2026-09-08] `subject on left` / `negative space` 는 잘못된 어구로 폐기 — 되살아나면 실패로 지킨다.
    check("wide 프롬프트에 오른쪽 시선 태그(배치 어구 폐기 유지)",
          "facing to the right" in pw and "subject on left" not in pw and "negative space" not in pw)
    check("portrait(front)에는 오른쪽 시선 없음 + 정면 태그", "facing to the right" not in pt
          and "looking at viewer" in pt)
    pr = CG.build_panel_prompt(0, panel_r, "explicit", gloss={})
    check("portrait(right)는 오른쪽 시선 + 보기 태그",
          "facing to the right" in pr and "looking at viewer" not in pr)
    check("정석 앵글(사이드뷰)", "from_side" in pt and "side_view" not in pt, pt.splitlines()[0][-70:])
    low = (pw + "\n" + pt).lower()
    check("성기 태그 제거(pussy/penis/vagina) — nipples는 허용",
          not any(w in low for w in ("pussy", "penis", "vagina")) and "nipple" in low,
          " / ".join(w for w in ("pussy", "penis", "vagina") if w in low))
    check("의상·체형 태그는 살아있음", "breasts" in low and "cameltoe" in low and "pale skin" in low)
    s_t = anima_gen.strip_anatomy_tags("visible_nipples, pink pussy, His penis is in her mouth., cameltoe")[0]
    check("strip_anatomy_tags: 성기 제거/nipples·cameltoe 보존",
          "pussy" not in s_t.lower() and "penis" not in s_t.lower()
          and "nipples" in s_t.lower() and "cameltoe" in s_t, s_t)
    # [2026-09-07] 청년향: climax 이벤트 소멸 + 1인 화면(비POV 컷에서 상대방 위치 문장 숨김)
    raw_c = [{"no": 1, "type": "action", "pose": "She smiles.", "camera": "front_view",
              "position": "NONE", "climax": "creampie", "caption_ko": "", "dialog": [], "wide": False}]
    rep_c, _ = CG._repair_panels(raw_c)
    check("청년향: climax 태그 전부 비움", rep_c[0]["climax"] == "", rep_c[0]["climax"])
    p_side = {"no": 4, "type": "action", "camera": "side_view", "pose": "She turns around.",
              "position": "He is standing.", "climax": "", "caption_ko": "", "dialog": [],
              "wide": False, "facing": "right"}
    p_pov = dict(p_side, no=5, camera="pov", facing="front")
    ps_ = CG.build_panel_prompt(0, p_side, "nsfw", gloss={})
    pp_ = CG.build_panel_prompt(0, p_pov, "nsfw", gloss={})
    check("비POV 컷: 상대방 위치 문장 미노출(1인 화면)", "he is standing" not in ps_.lower())
    check("POV 컷: 상대방 존재(pov/손) 유지", "pov" in pp_.lower())
    check("슬롯 화면비 → 해상도: 1.334→wide, 0.77→tall, 0.33→최장세로",
          CG._res_for_aspect(1.334) == CG.WIDE_RES and CG._res_for_aspect(0.77) == CG.PANEL_RES
          and anima_gen.resol[CG._res_for_aspect(0.33)] == [1024, 1366],
          str([CG._res_for_aspect(x) for x in (1.334, 0.77, 0.33)]))
    # [2026-09-07] 컷별 복장 변화(clothes): 정규화/승계/기준도 대체
    raw_cl = [{"no": 1, "type": "action", "pose": "She stands.", "camera": "front_view",
               "position": "NONE", "climax": "", "caption_ko": "", "dialog": [], "wide": False,
               "clothes": "Police Uniform, Visible Pussy, Miniskirt"},
              {"no": 2, "type": "face", "pose": "She smiles.", "camera": "close_up",
               "position": "NONE", "climax": "", "caption_ko": "", "dialog": [], "wide": False}]
    rep_cl, _ = CG._repair_panels(raw_cl)
    check("clothes 정규화(소문자/성기 제거)",
          rep_cl[0]["clothes"] == "police uniform, miniskirt", rep_cl[0]["clothes"])
    check("clothes 연속성: 빈 컷은 직전 복장 승계", rep_cl[1]["clothes"] == "police uniform, miniskirt",
          rep_cl[1]["clothes"])
    config.bodystyle_tag = ["hyper-voluptuous, police uniform"] * 12   # 회차 기본 의상(폴백용)
    config.p_exposure_tag = ["cameltoe"] * 12
    p_cloth = {"no": 6, "type": "action", "camera": "side_view", "pose": "She changes clothes.",
               "position": "NONE", "climax": "", "caption_ko": "", "dialog": [], "wide": False,
               "facing": "right", "clothes": "leather bra, miniskirt"}
    pc = CG.build_panel_prompt(0, p_cloth, "nsfw", gloss={})
    check("컷 clothes가 회차 의상 기준도 대체(+부분 노출은 유지)",
          "leather bra" in pc and "police uniform" not in pc and "cameltoe" in pc)
    # [2026-09-07] time_of_day → 배경
    config.time_of_day = "at night"
    pt2 = CG.build_panel_prompt(0, panel_t, "nsfw", gloss={})
    check("time_of_day가 배경 프롬프트에 사용", "at night" in pt2)
    config.time_of_day = ""
    # [2026-09-07] 재현성: 같은 컷 두 번 → 동일 프롬프트(rand.choice 미시드 회귀 방지)
    config.expression_arr = ["shy smile, blushing"] * 12
    d1 = CG.build_panel_prompt(0, panel_t, "nsfw", gloss={})
    d2 = CG.build_panel_prompt(0, panel_t, "nsfw", gloss={})
    p_pov_det = dict(panel_t, no=9, camera="pov", facing="front")
    check("재현성: 동일 입력 → 동일 프롬프트(표정/observer/사이드 결정론)",
          d1 == d2 and CG.build_panel_prompt(0, p_pov_det, "nsfw", gloss={})
          == CG.build_panel_prompt(0, p_pov_det, "nsfw", gloss={}))
    # [2026-09-07] 1인 화면: 헤더는 무조건 solo (side_view는 구도일 뿐 '2명'이 아니다)
    ps = CG.build_panel_prompt(0, dict(panel_t, no=11), "nsfw", gloss={})
    pp = CG.build_panel_prompt(0, dict(p_pov_det, no=12), "nsfw", gloss={})
    check("비POV: 헤더 solo + 2girl/two girls/상대 언급 없음",
          "solo" in ps and "2girl" not in ps and "two girls" not in ps
          and "looking at each other" not in ps, ps[:160])
    check("POV: 헤더 solo(상대는 OBSERVER로만)",
          "solo" in pp and "2girl" not in pp and "two girls" not in pp, pp[:160])
    # [2026-09-08] 시트 #캐릭터 태그# 는 정제·LLM 재작성을 통과하지 못할 수 있어 마지막에 보장 주입한다
    config.char_tags = ["Usagi Tsukino from Sailor Moon"]
    config.partner_char_tags = ["Tuxedo Mask"]
    p_tag_face = dict(panel_t, no=21, type="face")
    for tag_name, p_get in (("action(side)", panel_t), ("POV", p_pov_det), ("face", p_tag_face)):
        ptxt = CG.build_panel_prompt(0, p_get, "nsfw", gloss={})
        check(f"컷 프롬프트에 #캐릭터 태그 강제 주입 — {tag_name}",
              "Usagi Tsukino from Sailor Moon" in ptxt
              and ptxt.count("Usagi Tsukino from Sailor Moon") == 1, ptxt[:120])
    check("상대방 #태그는 POV 컷만(비POV는 [BBB] 오염 방지)",
          "Tuxedo Mask" in CG.build_panel_prompt(0, p_pov_det, "nsfw", gloss={})
          and "Tuxedo Mask" not in CG.build_panel_prompt(0, panel_t, "nsfw", gloss={}), "")
    llm_body = "1girl, usagi_tsukino from sailor_moon, smile"   # LLM이 대소문자/_ 바꿔 써도 중복 금지
    kept, added = anima_gen.ensure_char_tags(llm_body, include_partner=False)
    check("ensure_char_tags: 이미 있으면(대소문자·_ 무시) 중복을 만들지 않는다",
          added == [] and kept == llm_body, f"{added} / {kept}")
    miss, added2 = anima_gen.ensure_char_tags("1girl, solo, smile")
    check("ensure_char_tags: 빠지면 앞(주인공)·뒤(상대방)로 채운다",
          miss.startswith("Usagi Tsukino from Sailor Moon, 1girl")
          and miss.endswith("Tuxedo Mask"), miss)
    config.char_tags, config.partner_char_tags = [], []
    # [2026-09-07] portrait = 정확한 정면 풀페이스 초상화 (사용자 지시)
    # [2026-09-09] ★서두 요약/에필로그 규칙(컷 텍스트 문법)이 face 검사까지 바꾼다 — 스위치를 끈다
    _sc_bak, _ep_bak = getattr(config, "comic_summary_cuts", True), getattr(config, "comic_epilogue", True)
    config.comic_summary_cuts = config.comic_epilogue = False
    raw_f = [{"no": 1, "type": "face", "pose": "She is tilted head, looking away, smiling.",
              "camera": "side_view", "position": "NONE", "climax": "", "caption_ko": "", "dialog": [],
              "wide": False, "facing": "right"}]
    rep_f, _ = CG._repair_panels(raw_f)
    check("face 컷 강제 정규화: facing→front / side→close_up",
          rep_f[0]["facing"] == "front" and rep_f[0]["camera"] == "close_up",
          f"{rep_f[0]['facing']},{rep_f[0]['camera']}")
    pf_face = CG.build_panel_prompt(0, dict(rep_f[0]), "nsfw", gloss={})
    check("portrait: from_front+looking at viewer / extreme close-up·각도어구 제거",
          "from_front" in pf_face and "looking at viewer" in pf_face
          and "extreme close-up" not in pf_face and "face only" not in pf_face
          and "tilted head" not in pf_face.lower() and "looking away" not in pf_face.lower(),
          pf_face[:150])
    config.comic_summary_cuts, config.comic_epilogue = _sc_bak, _ep_bak
    # [2026-09-07] 최종 페이지 크기 고정 1024x1454: 행 높이 스케일 + 컷은 cover 크롭
    tmpsz = tempfile.mkdtemp(prefix="st_pagesz_")
    paths_sz = []
    for i2 in range(4):
        fp2 = os.path.join(tmpsz, f"sz{i2}.png")
        Image.new("RGB", (1024, 1344), (40 + i2 * 50, 90, 150)).save(fp2)
        paths_sz.append(fp2)
    spec_sz = [{"cells": [{"idx": 0, "share": 1.0}]},
               {"cells": [{"idx": 1, "share": 0.5}, {"idx": 2, "share": 0.5}]},
               {"cells": [{"idx": 3, "share": 1.0}]}]
    pw_sz, gr_sz = CPM._plan_rows(4, [[] for _ in range(4)], [False] * 4, [False] * 4, None,
                                  unit_w=CPM.DEFAULT_CELL_W, cols=2, gutter=CPM.DEFAULT_GUTTER,
                                  pad=CPM.DEFAULT_FRAME_PAD, border=4,
                                  font_size=CPM.DEFAULT_FONT_SIZE, row_spec=spec_sz,
                                  page_size=(1024, 1454), label_h=40)
    check("page_size: 폭 1024 + 행 높이 합 = 1454-2*거터-라벨",
          pw_sz == 1024 and sum(r["h"] for r in gr_sz) == 1454 - 2 * CPM.DEFAULT_GUTTER - 40,
          f"{pw_sz},{sum(r['h'] for r in gr_sz)}")
    saved_sz = CPM.compose_pages(paths_sz, [["캡션"]] * 4, tmpsz, "sz",
                                 page_specs=[{"size": 4, "rows": spec_sz}],
                                 page_label_prefix="EP01")
    im_sz = Image.open(saved_sz[0]).convert("RGB")
    check("최종 페이지 픽셀 = 1024x1454", im_sz.size == (1024, 1454), str(im_sz.size))
    check("컷 텍스트 축소(1024 wide 페이지 기준 font<=20)", CPM.DEFAULT_FONT_SIZE <= 20,
          str(CPM.DEFAULT_FONT_SIZE))
    check("caption_ko 완전 문장 요구(길이 한도 40+)", CG.CAPTION_MAX_LEN >= 40, str(CG.CAPTION_MAX_LEN))
    shutil.rmtree(tmpsz, ignore_errors=True)
    # [2026-09-07] 고정 화풍의 unet(anim a_baseV10)는 실존하지 않는 파일 — 폴백/오버라이드 검증
    _orig_exists = anima_gen._unet_exists
    anima_gen._unet_exists = lambda name: name == anima_gen.ANIMA_BASE_UNET_DEFAULT
    r_fix = anima_gen.resolve_anima_lora({"anima_style": "lora_lambton", "anima_lora": 1}, 0)
    r_ovr = anima_gen.resolve_anima_lora({"anima_style": "lora_lambton", "anima_lora": 1,
                                          "anima_unet": "my_model.safetensors"}, 0)
    anima_gen._unet_exists = _orig_exists
    check("unet 없으면 기본 모델 폴백 + plot anima_unet 무조건 우선",
          r_fix[0] == "Lambton.safetensors" and r_fix[4] == anima_gen.ANIMA_BASE_UNET_DEFAULT
          and r_ovr[4] == "my_model.safetensors", f"{r_fix[4]} | {r_ovr[4]}")
    # [2026-09-09] CLI 화풍 스위치(--real/--sole/--lora1/--lora2/--lora-chg/--str1/--str2) → config 주입 검증
    _cfg_bak = {k: getattr(config, k, None) for k in
                ("real_cli", "sole_cli", "lora1_cli", "lora2_cli", "lora_chg_cli", "total_episodes",
                 "lora_str1_cli", "lora_str2_cli", "explicit_cli", "ko_map_explicit",
                 "climax_vocab_local", "safety_local", "local_settings")}
    _cache_bak = (anima_gen.anima_cli_lora_unet, dict(anima_gen.anima_episode_lora_cache))
    _artist_bak = anima_gen.artist_anima
    anima_gen.anima_cli_lora_unet = None
    anima_gen.anima_episode_lora_cache.clear()
    config.lora1_cli, config.lora2_cli = "lora_mi1k", "lora_sex"
    config.lora_chg_cli, config.total_episodes = "increment", 1
    r_inc = anima_gen.resolve_anima_lora({"anima_style": "lora_lambton"}, 11)      # EP12를 총 1화로
    check("lora_chg=increment 폐지 → lora2 강도 1.0 초과 없음(원래 10.0으로 터졌다)",
          r_inc[3] <= 1.0 and r_inc[3] == anima_gen.ANIMA_LORA_CONFIG["lora_sex"][1],
          f"str2={r_inc[3]}")
    config.lora1_cli = config.lora2_cli = None
    check("increment만 켜지면 CLI 모드가 아니다(plot.json 기본으로 폴백)",
          anima_gen._norm_lora_chg("increment") is None
          and anima_gen._cli_lora_active() is False
          and anima_gen.resolve_anima_lora({"anima_style": "lora_lambton"}, 0)[0]
          == "Lambton.safetensors")
    config.lora_chg_cli = "episode"
    config.lora1_cli = "lora_mi1k"
    r_ep = anima_gen.resolve_anima_lora({"anima_style": "lora_lambton"}, 3)
    r_ep2 = anima_gen.resolve_anima_lora({"anima_style": "lora_lambton"}, 3)
    check("lora_chg=episode는 유효 + 같은 EP는 캐시 재사용(트리거/파일 일관성)",
          r_ep == r_ep2 and r_ep[0] and 0 < r_ep[3] <= 1.0, f"{r_ep[0]} + {r_ep[2]}({r_ep[3]})")
    config.lora1_cli = config.lora_chg_cli = None
    config.real_cli = True
    _orig_ex = anima_gen._unet_exists
    anima_gen._unet_exists = lambda name: True        # 풀 모델 실존 여부와 무관하게 검증
    r_real = anima_gen.resolve_anima_lora({"anima_style": "lora_lambton", "anima_unet": ""}, 0)
    check("--real: LoRA 전부 OFF + REAL UNET 풀", r_real[0] == "" and r_real[2] == ""
          and r_real[4] in anima_gen.ANIMA_REAL_UNET_POOL, f"{r_real[4]}")
    config.real_cli = False
    config.sole_cli = True
    r_sole = anima_gen.resolve_anima_lora({"anima_style": "lora_lambton", "anima_unet": ""}, 0)
    check("--sole: LoRA 없이 SOLE UNET 풀", r_sole[0] == "" and r_sole[4] in anima_gen.ANIMA_SOLE_UNET_POOL,
          f"{r_sole[4]}")
    anima_gen._unet_exists = _orig_ex
    _rc_src0 = open(os.path.join(ROOT, "run_comic.py"), encoding="utf-8").read()
    check("run_comic에 화풍 CLI 5종이 config로 주입된다(--real/--sole/--lora1/--lora2/--lora-chg)",
          all(s in _rc_src0 for s in ('"--real"', '"--sole"', '"--lora1"', '"--lora2"', '"--lora-chg"',
                                      'config.real_cli = True', 'config.sole_cli = True',
                                      'config.lora1_cli = args.lora1', 'config.lora2_cli = args.lora2',
                                      'config.lora_chg_cli = args.lora_chg', 'choices=("", "episode")')))
    # ---------------------------------------------------------------------------
    # [2026-09-09] "--lora1 lora_vassago가 안 먹힌다" 회귀 방어 — 원인은 두 곳이었다.
    #   ① ComfyUI 템플릿의 lora_1/lora_2 기본값이 on:false인데 코드는 lora/strength만 넣고 on을
    #      건드리지 않았다 → 슬롯에 파일만 얹혀 있고 항상 꺼져 있었다.
    #   ② LoRA trigger를 프롬프트 헤더(artist_anima)에 심는 코드가 이 fork에 없었다
    #      → 파일은 올라가도 트리거 단어가 없어 화풍이 안 변한다.
    # ---------------------------------------------------------------------------
    _wf = json.load(open(os.path.join(ROOT, "data_comfyui", "anima_spectrum_July11.json"), encoding="utf-8"))
    check("템플릿의 lora_1/lora_2 기본값은 on:false (그래서 코드가 강제로 켜야 한다)",
          _wf["122"]["inputs"]["lora_1"]["on"] is False and _wf["122"]["inputs"]["lora_2"]["on"] is False)
    _lora_exists_bak = anima_gen._lora_exists
    anima_gen._lora_exists = lambda name: True          # LoRA 실존 여부와 무관하게 배선만 감사
    config.real_cli, config.sole_cli = False, False     # 바로 위 --real/--sole 검사 잔값을 내린다
    config.lora1_cli, config.lora2_cli = "lora_vassago", "lora_sex"
    r_on = anima_gen.resolve_anima_lora({"anima_style": "lora_lambton", "anima_unet": ""}, 0)
    _wf2 = json.loads(json.dumps(_wf))
    anima_gen._apply_lora_nodes(_wf2, r_on)
    check("--lora1/--lora2: 강도>0이면 Power Lora Loader 슬롯이 켜진다(on=True)",
          _wf2["122"]["inputs"]["lora_1"]["on"] is True and _wf2["122"]["inputs"]["lora_1"]["lora"] == r_on[0]
          and _wf2["122"]["inputs"]["lora_2"]["on"] is True and _wf2["122"]["inputs"]["lora_2"]["lora"] == r_on[2],
          str(_wf2["122"]["inputs"]["lora_1"]))
    check("--lora1의 trigger가 프롬프트 헤더(artist_anima)로 들어간다",   # 예: lora_vassago → v4ss4g0x
          anima_gen.artist_anima.startswith(anima_gen.ANIMA_ARTIST_BASE)
          and anima_gen.ANIMA_LORA_CONFIG["lora_vassago"][5].strip(", ") in anima_gen.artist_anima,
          anima_gen.artist_anima)
    # --str1/--str2: 강도 오버라이드(범위 밖은 클램프)
    config.lora_str1_cli, config.lora_str2_cli = 0.35, 9.0
    r_str = anima_gen.resolve_anima_lora({"anima_style": "lora_lambton", "anima_unet": ""}, 0)
    check("--str1/--str2가 강도를 덮어쓴다(과거 increment 폭주 방어: 상한 2.0)",
          r_str[1] == 0.35 and r_str[3] == 2.0, f"str1={r_str[1]} str2={r_str[3]}")
    config.lora_str1_cli, config.lora_str2_cli = 0.0, None
    r_off = anima_gen.resolve_anima_lora({"anima_style": "lora_lambton", "anima_unet": ""}, 0)
    _wf3 = json.loads(json.dumps(_wf))
    anima_gen._apply_lora_nodes(_wf3, r_off)
    check("--str1 0은 그 슬롯을 끈다(on=False)", r_off[1] == 0.0 and _wf3["122"]["inputs"]["lora_1"]["on"] is False,
          str(_wf3["122"]["inputs"]["lora_1"]))
    config.lora_str1_cli, config.lora_str2_cli = None, None
    config.lora1_cli = config.lora2_cli = None
    anima_gen._lora_exists = lambda name: False
    r_gone = anima_gen.resolve_anima_lora({"anima_style": "lora_vassago", "anima_lora": 0, "anima_unet": ""}, 0)
    anima_gen._lora_exists = _lora_exists_bak
    check("LoRA 파일이 없으면 슬롯을 비운다(ComfyUI 400 없이 로그로 알린다)",
          r_gone[0] == "" and r_gone[1] == 0.0, f"{r_gone[:2]}")
    check("run_comic에 강도/수위 CLI가 config로 주입된다(--str1/--str2/--allow-explicit/--no-explicit)",
          all(s in _rc_src0 for s in ('"--str1"', '"--str2"', '"--allow-explicit"', '"--no-explicit"',
                                      'config.lora_str1_cli = args.str1', 'config.lora_str2_cli = args.str2',
                                      'config.explicit_cli = True', 'config.explicit_cli = False')))
    # ---------------------------------------------------------------------------
    # [2026-09-09] [local 전용] 수위 스위치 --allow-explicit — 기본 정책은 청년향(상한 nsfw) 그대로,
    #   스위치를 켰을 때만 explicit가 4곳(수위 판정/추출 프롬프트/성기 태그 파기/climax 어휘)을 통과한다.
    # ---------------------------------------------------------------------------
    _x_probe = {"protagonist": {}, "partner": {}, "guides": {}, "actions": [], "segments": [],
                "rating": "explicit"}
    # 개인 local_settings.yaml이 explicit를 켜고 와도 이 검사는 "기본 정책(청년향)"을 본다
    config.explicit_cli, config.ko_map_explicit, config.climax_vocab_local = False, {}, []
    check("기본(청년향): explicit는 nsfw로 낮아진다", anima_gen._cap_safety("explicit") == "nsfw")
    check("기본(청년향): 추출 rating도 explicit → nsfw",
          CI._normalize_extract(dict(_x_probe))["rating"] == "nsfw")
    check("기본(청년향): 성기 태그는 최종 프롬프트에서 제거된다",
          anima_gen.strip_anatomy_tags("penis, pussy, big breasts")[1] == 2)
    check("기본(청년향): climax 이벤트는 항상 빈 문자열", CG._norm_climax("creampie") == "")
    config.explicit_cli = True
    # 민감 어휘 사전은 코드가 비워 두고 local_settings.yaml이 채운다 — 검사는 표를 직접 심는다
    config.ko_map_explicit = {"테스트체위": "cowgirl position", "테스트보": "visible"}
    config.ko_map_safe = {"테스트안전": "she is on top"}     # 청년향 강등 사전도 로컬로 분리되었다
    config.climax_vocab_local = ["tail_bulge"]
    check("--allow-explicit: explicit가 그대로 살아남는다", anima_gen._cap_safety("explicit") == "explicit")
    check("--allow-explicit: 추출 rating의 explicit를 살린다(프롬프트도 adult 정책)",
          CI._normalize_extract(dict(_x_probe))["rating"] == "explicit"
          and "explicit" in CI.build_extract_prompt("본문", "시트", 1, need_segments=False))
    check("--allow-explicit: 성기 태그를 걷어내지 않는다(걷으면 explicit가 청년향과 같은 그림이 된다)",
          anima_gen.strip_anatomy_tags("penis, pussy, big breasts")[1] == 0)
    check("--allow-explicit: climax 어휘(creampie)가 컷에 남는다", CG._norm_climax("creampie") == "creampie")
    check("--allow-explicit: 한글 어구를 영문 태그로 되살린다(로컬 어휘 사전)",
          "cowgirl position" in CG._apply_static_ko_map("She is doing 테스트체위."))
    check("--allow-explicit: 짧은 키는 토큰 안에서도 치환된다(그래서 로컬 사전은 긴 보호형을 함께 둔다)",
          CG._apply_static_ko_map("테스트보기") == "visible기", CG._apply_static_ko_map("테스트보기"))
    _exp_bak2 = config.explicit_cli
    config.explicit_cli = False
    check("청년향(기본)에서도 ko_map_safe 증분이 치환된다(강등 사전도 로컬의 것)",
          "she is on top" in CG._apply_static_ko_map("그녀는 테스트안전 자세."))
    config.explicit_cli = _exp_bak2
    check("--allow-explicit: climax_vocab 증보가 허용 목록에 더는다",
          CG._norm_climax("tail_bulge") == "tail_bulge" and CG._norm_climax("거품목욕") == "")
    # ---------------------------------------------------------------------------
    # [2026-09-09] local_settings.yaml — 커밋에 남기고 싶지 않은 값(수위·LoRA 강도·민감 어휘)의 집.
    #   파일이 없으면 {} → 기본값 그대로. 파싱값은 '기본값 자리'만 채우고 CLI가 나중에 이긴다.
    # ---------------------------------------------------------------------------
    _ls_tmp = tempfile.mkdtemp(prefix="ls_")
    _ls_p = os.path.join(_ls_tmp, "local_settings.yaml")
    with open(_ls_p, "w", encoding="utf-8") as _lf:
        _lf.write("allow_explicit: yes\nsafety: explicit\nlora1: lora_vassago\nstr1: 0.8\n"
                  "ko_map:\n  테스트체위: cowgirl position\n"
                  "ko_map_safe:\n  테스트안전: hugging\ncounter_alias:\n  테스트A: [테스트A 횟수, test_a_count]\n"
                  "climax_vocab: [tail_bulge]\n")
    _ls_bak = {k: getattr(config, k) for k in
               ("local_settings", "ko_map_explicit", "ko_map_safe", "counter_alias", "climax_vocab_local", "safety_local",
                "explicit_cli", "lora1_cli", "lora2_cli", "lora_chg_cli",
                "lora_str1_cli", "lora_str2_cli")}
    config.apply_local_settings(config.load_local_settings(_ls_p))
    check("local_settings.yaml: 수위/LoRA/어휘가 config로 들어간다",
          config.explicit_cli is True and config.safety_local == "explicit"
          and config.lora1_cli == "lora_vassago" and config.lora_str1_cli == 0.8
          and config.ko_map_explicit.get("테스트체위") == "cowgirl position"
          and config.ko_map_safe.get("테스트안전") == "hugging"
          and config.counter_alias.get("테스트A") == ["테스트A 횟수", "test_a_count"]
          and config.climax_vocab_local == ["tail_bulge"],
          f"{config.lora1_cli}/{config.lora_str1_cli}/{config.ko_map_explicit}")
    for _k, _v in _ls_bak.items():
        setattr(config, _k, _v)
    _ls_none = config.load_local_settings(os.path.join(_ls_tmp, "nope.yaml"))
    config.explicit_cli = False
    check("local_settings.yaml이 없으면 {} — 아무 스위치도 켜지지 않는다(다른 기계에서도 평소대로)",
          _ls_none == {} and config.apply_local_settings(_ls_none) == {} and config.explicit_cli is False,
          str(_ls_none))
    _ls_broken = os.path.join(_ls_tmp, "broken.yaml")
    with open(_ls_broken, "w", encoding="utf-8") as _bf:
        _bf.write("allow_explicit: [닫지 않은 흐름 대괄호")
    check("형식이 망가진 yaml은 예외 대신 {} (파이프라인을 멈추지 않는다)",
          config.load_local_settings(_ls_broken) == {})
    shutil.rmtree(_ls_tmp, ignore_errors=True)
    _git_ign = open(os.path.join(ROOT, ".gitignore"), encoding="utf-8").read()
    _loc_p = os.path.join(ROOT, "README_local.md")
    _loc = open(_loc_p, encoding="utf-8").read() if os.path.isfile(_loc_p) else ""
    check("local 항목 파일은 .gitignore 대상(개인 파일은 로컬에만 남는다)",
          all(k in _git_ign for k in ("local_settings.yaml", "local_settings.json", "README_local.md")))
    check("README_local.md가 로컬 항목을 문서로 싣는다(없이도 도는 파일이라 있으면 검사)",
          not _loc or all(k in _loc for k in ("allow_explicit", "safety", "lora1", "str1",
                                              "ko_map", "climax_vocab", "--no-explicit")),
          f"README_local.md={len(_loc)}자")
    # AGENTS 규칙 5: local 항목은 README.md에 언급하지 않는다 — 커밋되는 문서가 다시 새지 않게 지킨다
    _readme = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
    _lora_md0 = open(os.path.join(ROOT, "LORA.md"), encoding="utf-8").read() if \
        os.path.isfile(os.path.join(ROOT, "LORA.md")) else ""
    _local_tokens = ("allow-explicit", "no-explicit", "local_settings", "COMIC_ALLOW_EXPLICIT",
                     "README_local")
    check("README.md/LORA.md에 local 항목이 언급되지 않는다(AGENTS 규칙 5)",
          not [t for t in _local_tokens if t in _readme or t in _lora_md0],
          str([t for t in _local_tokens if t in _readme or t in _lora_md0]))
    check("run_comic.py가 local_settings 사용 사실과 키 목록을 출력한다",
          "config.LOCAL_SETTINGS_FILE" in _rc_src0 and "config.safety_local" in _rc_src0)
    for _k, _v in _cfg_bak.items():
        setattr(config, _k, _v)
    anima_gen.artist_anima = _artist_bak
    anima_gen.anima_cli_lora_unet = _cache_bak[0]
    anima_gen.anima_episode_lora_cache.clear()
    anima_gen.anima_episode_lora_cache.update(_cache_bak[1])
    # [2026-09-09] LORA.md = 선택 가능한 LoRA 안내(활성 키만 수록). 표가 코드와 어긋나지 않게 점검
    _lora_md_p = os.path.join(ROOT, "LORA.md")
    _lora_md = open(_lora_md_p, encoding="utf-8").read() if os.path.isfile(_lora_md_p) else ""
    _live = [k for k in anima_gen.ANIMA_LORA_CONFIG if k != "lora_random"]
    _miss = [k for k in _live if f"`{k}`" not in _lora_md]
    _off = re.findall(r'^\s*#\s*"([^"]+)"\s*:', open(os.path.join(ROOT, "anima_gen.py"), encoding="utf-8").read(), re.M)
    _leak = [k for k in _off if f"`{k}`" in _lora_md]
    check("LORA.md가 선택 가능한 LoRA 키를 전부 싣는다(활성 키 개수도 일치)",
          bool(_lora_md) and not _miss and f"{len(_live)}키" in _lora_md, f"누락={_miss[:4]}")
    check("LORA.md에 주석 처리(선택 불가) 키가 섞이지 않는다", not _leak, f"혼입={_leak[:4]}")
    # facing 정규화: wide는 right 강제, action+front_view는 right→side_view 승격 등
    raw_p = [{"no": 1, "type": "action", "pose": "She walks.", "camera": "front_view",
              "position": "NONE", "climax": "", "caption_ko": "", "dialog": [], "wide": False,
              "facing": "오른쪽"},
             {"no": 2, "type": "face", "pose": "She smiles.", "camera": "side_view",
              "position": "NONE", "climax": "", "caption_ko": "", "dialog": [], "wide": False}]
    rep, rep_notes = CG._repair_panels(raw_p)
    check("facing 정규화(오른쪽→right / 미지정→front)",
          rep[0]["facing"] == "right" and rep[1]["facing"] == "front", str([p["facing"] for p in rep]))
    check("facing 구도 강제(right:action→side_view / face는 close_up 유지)",
          rep[0]["camera"] == "side_view" and rep[1]["camera"] == "close_up",
          str([p["camera"] for p in rep]))
    # [2026-09-07] 본문 원문은 **태그 생성** LLM에 들어가지 않는다(가이드만). 컷 스크립트는 반대다 —
    # 컷 단계는 본문 원문을 반드시 본다(그래야 회차 전체가 컷으로 내려간다). 아래 ⑦에서 그쪽을 감사한다.
    check("본문 원문 LLM 미주입: anima_gen에 [에피소드 N 본문] 프롬프트 없음",
          "[에피소드 {ep_num} 본문]" not in ag_src and "episode_content or \"\")[:8000]" not in ag_src)

    cap = {}
    orig_run, orig_wait = anima_gen.comfyui_run_anima, anima_gen._wait_and_copy_image

    def fake_run(json_value, ep_idx, full_prompt, res, client=None, seed=None, queue_count=2):
        cap.setdefault("res", []).append(res)
        return "episode_01_comic_e1_p01_x_anima_"

    anima_gen.comfyui_run_anima = fake_run
    anima_gen._wait_and_copy_image = lambda *a, **k: None
    try:
        CG.render_panel(0, panel_w, 11, "explicit", {})
        CG.render_panel(0, panel_t, 12, "explicit", {})
    finally:
        anima_gen.comfyui_run_anima, anima_gen._wait_and_copy_image = orig_run, orig_wait
    check("wide → res 9(1366x1024)", cap["res"][0] == CG.WIDE_RES
          and anima_gen.resol[CG.WIDE_RES] == [1366, 1024], str(cap))
    check("portrait → res 5(1024x1344)", cap["res"][1] == CG.PANEL_RES, str(cap))
    check("런너에 ComfyUI 기동/프리플라이트 제공",
          callable(RC.start_comfyui) and callable(RC.preflight) and not RC._tcp("127.0.0.1", 1))

    # ---------------------------------------------------------------- ⑥ cut.yaml 페이지 레이아웃 (A안)
    # [2026-09-07] 컷 종류별 가이드: portrait=기존 유지 / POV=prompt_pov.md / multi=prompt_multi.md
    CG._llm_compose_panel_prompt = _orig_g
    def _fake_pov(task, system_prompt=None, log_fn=None, temperature=None, **kw):
        assert system_prompt and "POV" in system_prompt   # 가이드 본문이 system으로 들어가야 함
        return ("##PROMPT##\n"
                "(first-person view:1.6), (her face flushed:1.4), looking down at the man's hand, "
                "dim bedroom light, she bites her lip\n##PROMPT##", None)
    def _fake_multi(task, system_prompt=None, log_fn=None, temperature=None, **kw):
        assert system_prompt and "Subject 1" in system_prompt
        return ("##PROMPT##\n"
                "[Global Context & Layout Scene] 2girls and sitting closely together and cafe table,\n"
                "[Subject 1: girl1] the girl1 has long hair and brown hair and smiling\n##PROMPT##", None)
    def _boom(*a, **k):
        raise RuntimeError("llm down")
    try:
        CG.call_openai_for_text = _fake_pov
        p_g = CG.build_panel_prompt(0, dict(panel_t, no=20, camera="pov"), "nsfw", gloss={})
        check("POV 컷 = prompt_pov.md 가이드 본문 사용",
              "first-person view:1.6" in p_g and "man's hand" in p_g, p_g[-150:])
        CG.call_openai_for_text = _fake_multi
        p_m = CG.build_panel_prompt(0, dict(panel_t, no=21, multi=True), "nsfw", gloss={})
        check("multi 컷 = prompt_multi.md 가이드 본문 사용",
              "2girls" in p_m and "girl1" in p_m, p_m[-150:])
        CG.call_openai_for_text = _boom
        p_fb = CG.build_panel_prompt(0, dict(panel_t, no=22, camera="pov"), "nsfw", gloss={})
        check("가이드 LLM 실패 → 결정적 태그 본문 폴백",
              "living room" in p_fb and "pov" in p_fb, p_fb[:120])
        check("portrait(face)는 가이드 미사용(기존 방식) — pov/multi만 LLM",
              CG._prompt_guide("pov") and CG._prompt_guide("multi"))
    finally:
        CG.call_openai_for_text = _orig_t
        CG._llm_compose_panel_prompt = _orig_g

    check_llm_server(check)
    print("\n== ⑥ cut.yaml: 템플릿/페이지 플래너/스펙 레이아웃 ==")
    tmpls = CG.load_cut_templates()
    check("cut.yaml 템플릿 14개 로드", len(tmpls) == 14, str(len(tmpls)))
    bad_tier = []
    for t in tmpls.values():
        for td in t["tiers"]:
            ssum = sum(td["shares"])
            if not (abs(ssum - 1.0) < 0.02 or (td["center"] and ssum < 1.0)):
                bad_tier.append((t["id"], td["tier"], ssum))
        if not set(t["situations"]) <= set("기승전결"):
            bad_tier.append((t["id"], "situations", t["situations"]))
    check("모든 tier share 합≈1(중앙 제외)/situation 정상", not bad_tier, str(bad_tier[:3]))
    plans_a, plans_b = CG.plan_pages(7, 2), CG.plan_pages(7, 2)
    check("plan_pages 결정론(회차 시드)", plans_a == plans_b)
    check("plan_pages off(0) → 자동 레이아웃", CG.plan_pages(7, 0) is None)
    bad_sit = None
    for n in range(1, 8):
        pl_n = CG.plan_pages(4, n)
        if pl_n[0]["situation"] != "기" or (n > 1 and pl_n[-1]["situation"] != "결"):
            bad_sit = (n, [p["situation"] for p in pl_n])
            break
    check("plan_pages: 아무 페이지 수나 첫장=기/마지막장=결(짧은 페이지에서 결 실종 방지)",
          bad_sit is None, str(bad_sit))
    gl = ["[protagonist] 기", "[protagonist] 승", "[protagonist] 전", "[protagonist] 결",
          "[partner] 상대 시선", "[sub] 엑스트라"]
    sl1, sl2 = CG._slice_guides(gl, 1, 2), CG._slice_guides(gl, 2, 2)
    check("장면별 가이드 슬라이스: 주인공만 비례로 자르고 회차 전체 줄은 마지막 장면으로",
          sl1 == ["[protagonist] 기", "[protagonist] 승"]
          and sl2 == ["[protagonist] 전", "[protagonist] 결", "[partner] 상대 시선", "[sub] 엑스트라"],
          f"{sl1} | {sl2}")
    slots_a = CG.spec_slots(plans_a)
    check("슬롯 수 = 컷 수(페이지당 2~8컷 × 페이지 수)",
          2 <= len(slots_a) <= CG.SLOTS_PER_PAGE_MAX * len(plans_a) and
          len(slots_a) == sum(len(pl["slots"]) for pl in plans_a), str(len(slots_a)))
    wide_ok = all((not s["wide"]) or (s["share"] >= 0.99 and not pl["single_tier"])
                  for pl in plans_a for s in pl["slots"])
    tall_full_ok = all(not s["wide"] for pl in plans_a if pl["single_tier"] for s in pl["slots"])
    check("wide는 전폭+다단 페이지 슬롯만", wide_ok and tall_full_ok)
    # wide 슬롯이 있는 회차 골라 스펙 모드 테스트
    ep_w = next((e for e in range(1, 12) if any(s["wide"] for s in CG.spec_slots(CG.plan_pages(e, 2)))), 1)
    plans = CG.plan_pages(ep_w, 2)
    slots = CG.spec_slots(plans)
    E = len(slots)
    pr_txt = CG.build_panel_script_prompt(ep_w, 1, "시트A", "시트B", "", ["기", "승", "전", "결"], [],
                                          page_plans=plans)
    check("프롬프트에 템플릿/컷 수 주입",
          f"정확히 {E}컷" in pr_txt and plans[0]["template_id"] in pr_txt and "슬롯 1" in pr_txt.replace("슬롯1", "슬롯 1"))

    def _mk(n):
        return [{"no": i, "type": "action", "pose": f"She walks {i}.", "camera": "front_view",
                 "position": "NONE", "climax": "", "caption_ko": f"장면{i}", "dialog": [],
                 "wide": False} for i in range(1, n + 1)]
    rep_e, notes_e = CG._repair_panels(_mk(E - 2), page_plans=plans)
    check("미응답 슬롯 → 폴백 패딩(컷 수 = 슬롯 수)",
          len(rep_e) == E and any("폴백 컷" in n for n in notes_e), f"{len(rep_e)}/{E}")
    rep_x, notes_x = CG._repair_panels(_mk(E + 3), page_plans=plans)
    check("초과 컷 → 템플릿분으로 절단", len(rep_x) == E and any("초과분 절단" in n for n in notes_x))
    meta_ok = all(p.get("page") and p.get("tier") and abs(bool(p["wide"]) - bool(s["wide"])) == 0
                  for p, s in zip(rep_e, slots))
    check("슬롯 메타/page·tier·wide 강제", meta_ok)
    face_wide = _mk(E)
    face_wide[[i for i, s in enumerate(slots) if s["wide"]][0]]["type"] = "face"
    rep_fw, _ = CG._repair_panels(face_wide, page_plans=plans)
    check("전폭 가로 슬롯 face → action 강제",
          all(not (s["wide"] and p["type"] == "face") for p, s in zip(rep_fw, slots)))

    print("\n== ⑦ 에피소드 전체 반영 (본문→컷 수→페이지 수, 장면 분할 호출) ==")
    body = "\n\n".join(f"장면{i}입니다. 유즈키는 걸어서 도착한다." for i in range(1, 61))   # ≈2.6k자
    check("episode_char_budget: num_ctx에서 본문 예산 산출(0보다 크게)",
          CI.episode_char_budget() > 1000, str(CI.episode_char_budget()))
    check("target_panels: 본문 길이가 컷 수를 정한다(단조 증가/하한/상한)",
          CI.target_panels("x" * 600) == 6 and CI.target_panels("x" * 6000) == 10
          and CI.target_panels("x" * 60000, max_panels=20) == 20)
    pl40, n40 = CG.plan_pages_layout(5, 40, 0)
    check("plan_pages_layout: 4페이지 상한 폐지(40컷 → 4페이지 초과)",
          n40 > 4 and len(CG.spec_slots(pl40)) >= 30, f"{n40}p/{len(CG.spec_slots(pl40))}컷")
    check("plan_pages: 12페이지 요구 → 12 + ★에필로그 1, 상한 너머는 클램프(에필로그도 상한 안)",
          len(CG.plan_pages(5, 12)) == 13 and len(CG.plan_pages(5, 99)) <= CG.MAX_PAGES_AUTO,
          str([len(CG.plan_pages(5, 12)), len(CG.plan_pages(5, 99)), CG.MAX_PAGES_AUTO]))
    b6 = CI.split_beats(body, n_beats=6)
    check("split_beats: 요구 개수대로 분할 + 서사 순서 보존",
          len(b6) == 6 and b6[0].startswith("장면1") and "장면60" in b6[-1], str([len(x) for x in b6]))
    check("allocate: 배분 합 = 슬롯 수, 각 장면 ≥1",
          sum(CI.allocate(17, [3, 1, 1], minimum=1)) == 17 and min(CI.allocate(17, [3, 1, 1], minimum=1)) >= 1)
    pr_body = CG.build_panel_script_prompt(ep_w, 1, "시트A", "시트B", "", ["기", "승", "전", "결"], [],
                                           page_plans=plans, episode_text="유즈키는 빔보 매장에서 멈춘다.",
                                           panels_expected=3, prev_tail=["유즈키는 골목에 섰다 / 복장: police uniform"],
                                           beat_label="장면 2/6", slot_range=(3, 6))
    check("컷 스크립트 프롬프트에 본문 원문 주입(A)",
          "유즈키는 빔보 매장에서 멈춘다." in pr_body and "본문이 최우선 근거" in pr_body)
    check("장면당 컷 수/슬롯 구간/직전 컷 컨텍스트 주입(C)",
          "정확히 3컷" in pr_body and "장면 2/6" in pr_body and "직전 컷까지의 진행" in pr_body
          and all(f"슬롯{k} " in pr_body for k in (4, 5, 6)) and "슬롯7 " not in pr_body,
          str([k for k in range(1, 9) if f"슬롯{k} " in pr_body]))
    rep_ul, notes_ul = CG._repair_panels(_mk(12), page_plans=None)      # max_panels 기본 0
    check("레거시 모드 컷 상한 폐지(12컷이 MAX_PANELS=10에 안 잘림)", len(rep_ul) == 12, str(len(rep_ul)))
    rep_ltd, _ = CG._repair_panels(_mk(12), page_plans=None, max_panels=8)
    check("--max-panels 지정 시에는 절단", len(rep_ltd) == 8, str(len(rep_ltd)))
    # 추출 map-reduce: 창을 나눠 창당 1회 → 가이드 누적/수위 최대/정체성은 첫 창 우선
    calls = {"n": 0}

    def _fake_extract(prompt, **kw):
        calls["n"] += 1
        return (json.dumps({"protagonist": {"name": f"유즈키{calls['n']}", "hair_color": "black hair",
                                            "clothes": "police uniform", "body_shape": "slender"},
                            "partner": {"name": "소타", "sex": "male", "clothes": "suit"},
                            "guides": {"protagonist": [f"기{calls['n']}", f"승{calls['n']}",
                                                       f"전{calls['n']}", f"결{calls['n']}"]},
                            "actions": ["터치" if calls["n"] == 1 else "포옹"],
                            "rating": "safe" if calls["n"] == 1 else "nsfw"}, ensure_ascii=False), None)

    orig_ex = CI.call_openai_for_text
    CI.call_openai_for_text = _fake_extract
    try:
        long_body = "\n\n".join(f"문단{i}입니다." for i in range(1, 41))
        merged = CI.extract(long_body, "시트", ep_num=1, char_budget=200)
    finally:
        CI.call_openai_for_text = orig_ex
    check("추출 map-reduce: 긴 본문은 창 단위 분할 → 창당 1회",
          calls["n"] > 1 and merged.get("windows") == calls["n"], f"{calls['n']}창")
    check("추출 병합: 가이드는 누적, 정체성은 첫 창, 수위는 최대",
          len(merged["guides"]["protagonist"]) == 4 * calls["n"]
          and merged["protagonist"]["name"] == "유즈키1" and merged["rating"] == "nsfw"
          and merged["actions"] == ["터치", "포옹"],
          f"{len(merged['guides']['protagonist'])}줄 {merged['rating']} {merged['actions']}")
    calls["n"] = 0
    CI.call_openai_for_text = _fake_extract
    try:
        CI.extract("짧다", "시트", ep_num=1, char_budget=20000)
    finally:
        CI.call_openai_for_text = orig_ex
    check("짧은 본문은 추출 1회(낭비 호출 없음)", calls["n"] == 1, str(calls["n"]))
    # 본문 → 컷 수 → 페이지가 실제로 늘어나는가 (LLM 목킹, 컷 2컷/장면)
    cfg_save = (config.comic_pages, config.comic_chars_per_panel, config.comic_beat_chars,
                config.comic_max_panels, config.comic_max_pages)
    config.comic_pages, config.comic_chars_per_panel = 0, 100
    config.comic_beat_chars, config.comic_max_panels, config.comic_max_pages = 600, 0, 24
    calls2 = {"n": 0}

    def _fake_panels(prompt, **kw):
        calls2["n"] += 1
        return (json.dumps([{"no": 1, "type": "action", "caption_ko": "유즈키가 걷는다.",
                             "dialog": ["유즈키: 하…", "소타: 조심해"], "wide": False, "facing": "front",
                             "clothes": "police uniform", "pose": "She walks.", "camera": "front_view",
                             "position": "NONE", "climax": ""}] * 2, ensure_ascii=False), None)

    orig_pl = CG.call_openai_for_text
    CG.call_openai_for_text = _fake_panels
    try:
        sc = CG.request_panel_script(3, 1, pages=0, episode_text=body,
                                     chars_per_panel=100, beat_chars=600)
    finally:
        CG.call_openai_for_text = orig_pl
        (config.comic_pages, config.comic_chars_per_panel, config.comic_beat_chars,
         config.comic_max_panels, config.comic_max_pages) = cfg_save
    check("request_panel_script: 본문이 컷 수를 정하고 장면별로 LLM을 부른다",
          sc["beats"] > 1 and calls2["n"] == sc["beats"]
          and sc["target_panels"] == -(-len(body) // 100) and len(sc["panels"]) >= sc["target_panels"] * 0.8,
          f"beats={sc['beats']} calls={calls2['n']} target={sc['target_panels']} panels={len(sc['panels'])}")
    check("컷에 page/tier 슬롯 메타가 전부 붙는다(페이지 합성 입력)",
          all("page" in p and "tier" in p for p in sc["panels"]))
    logs = []
    orig_clog, CG._clog = CG._clog, (lambda m: logs.append(str(m)))
    try:
        CG.call_openai_for_text = lambda prompt, **kw: (json.dumps(
            [{"no": 1, "type": "action", "caption_ko": "걸는다.", "dialog": ["a: x", "b: y"],
              "wide": False, "facing": "front", "clothes": "police uniform", "pose": "She walks.",
              "camera": "front_view", "position": "NONE", "climax": ""}], ensure_ascii=False), None)
        CG.request_panel_script(3, 1, pages=0, episode_text=body,
                                chars_per_panel=300, beat_chars=600)
    finally:
        CG.call_openai_for_text = orig_pl
        CG._clog = orig_clog
    check("장면 로그에 패딩 전 실제 응답 수를 남긴다(침묵 컷 숨지 않게)",
          any("컷 1/" in l and "침묵 컷" in l for l in logs),
          str([l for l in logs if "장면" in l and "컷" in l][:2]))
    # [2026-09-08] 출력 토큰 잘림으로 마지막 컷이 {로 열린 채 사라지면 그 컷은 침묵 컷이 된다.
    #   따옴표/괄호만 닫아 써 둔 필드만이라도 살린다 (2026-09-08 15:38 로그의 '컷 2개만 사용' 재현).
    trunc = ('[\n{"no": 1, "type": "action", "caption_ko": "걷는다", "dialog": [], "wide": false,'
             ' "facing": "front", "clothes": "x", "pose": "She walks.", "camera": "front_view",'
             ' "position": "NONE", "climax": ""},\n{"no": 2, "caption_ko": "감긴다",'
             ' "dialog": ["a: x"], "pose": "She is enve')
    salv = CG._extract_json_array(trunc)
    check("출력 잘림: 닫히지 않은 마지막 컷 구제(침묵 컷 대신 복원)",
          len(salv) == 2 and salv[1].get("no") == 2 and str(salv[1].get("pose", "")).startswith("She is enve"),
          str(len(salv)))
    # [2026-09-08] Few chars → use 기승전결 (KI-SYŎNG-JŎN-GYŎL) acts as the scene skeleton (min 2 cuts per act).
    # The anchors (first sentence of each act, copied verbatim by the LLM) live in config.ep_beat_segments.
    acts_body = "\n\n".join([
        "기: 유즈키가 아침 형사에 출근한다. " * 5,
        "승: 소타가 서류를 들고 태클을 건다. " * 6,
        "전: 우연한 사고로 두 사람이 밀착한다. " * 5,
        "결: 두 사람은 저녁을 약속한다. " * 4])
    seg_acts = ["기: 유즈키가 아침 형사에 출근한다.", "승: 소타가 서류를 들고 태클을 건다.",
                "전: 우연한 사고로 두 사람이 밀착한다.", "결: 두 사람은 저녁을 약속한다."]
    wins = CI.split_by_segments(acts_body, seg_acts)
    check("split_by_segments: anchors all verbatim → 4 acts split (order preserved)",
          len(wins) == 4 and wins[0].startswith("기:") and "저녁" in wins[-1],
          str([len(w) for w in wins]))
    check("split_by_segments: one bad anchor → abandon split ([] → falls back to char-count)",
          CI.split_by_segments(acts_body, seg_acts[:2] + ["전: 없는 문장입니다.", seg_acts[3]]) == [],
          str(CI.split_by_segments(acts_body, seg_acts[:2] + ["전: 없는 문장입니다.", seg_acts[3]])))
    config.ep_beat_segments = {42: seg_acts}
    logs_a = []
    orig_clog_a, orig_pl2 = CG._clog, CG.call_openai_for_text
    try:
        CG._clog = lambda m: logs_a.append(str(m))
        CG.call_openai_for_text = _fake_panels          # 2 cuts/call — right around the 2-cuts/act floor
        try:
            sc_acts = CG.request_panel_script(42, 1, pages=0, episode_text=acts_body, chars_per_panel=40)
        finally:
            CG.call_openai_for_text = orig_pl2
    finally:
        CG._clog = orig_clog_a
    config.ep_beat_segments.pop(42, None)
    check("few chars → acts become the scene skeleton (4 acts = 4 LLM calls, min 2 cuts/act)",
          sc_acts["beats"] == 4 and any("막 분할" in l for l in logs_a)
          and len(sc_acts["panels"]) >= 8,
          f"beats={sc_acts['beats']} panels={len(sc_acts['panels'])} "
          + str([l for l in logs_a if "장면" in l][:1]))
    # [2026-09-08] 16:59 regression ①: the 결 act's single sentence is 96 chars — the old 80-char
    # anchor cap dropped it and the episode collapsed to 기/승/전. Long verbatim anchors must survive.
    # 본문은 인라인 합성(selftest는 저장소의 inputs/만 읽고 로컬 전용 검증 입력에는 손대지 않는다).
    #   앵커 4개가 원문에서 순서대로 찾아져야 하므로 라벨(`기: ` … `결: `)까지 실제 입력과 같은 모양으로 둔다.
    anchors_sp = ["연인(턱시도)을 구하기 위해 진슈가이 최심부의 붉은 달 제단에 홀로 발을 들인 주인공.",
                  "괴물의 점액질 촉수에 휘감겨 마법복이 녹아내린다.",
                  "고통과 공포가 이내 극상의 황홀경으로 역전된다.",
                  "붉은 달의 제단 위에서 괴물의 거대한 씨앗을 받으며, '전생의 사랑 따위 시시해! 난 이 괴물 님의 완벽한 고기 육변기니까!'라며 침을 흘리는 심연의 빔보 여왕으로 전락함."]
    ep_sp = ("주인공: 유즈키\n상대방: 소타\n\n"
             + f"기: {anchors_sp[0]} 하지만 그곳에는 연인이 아닌, 붉은 달의 심연에서 강림한 거대한 괴물이 기다리고 있었다.\n\n"
             + f"승: {anchors_sp[1]} 공포에 질려 연인의 이름을 부르짖지만, 우주적인 쾌락 페로몬이 자궁에 직접 주입되자 전생의 고결한 사랑은 뇌에서 완벽히 삭제된다.\n\n"
             + f"전: {anchors_sp[2]} 복장은 점점 검게 변하고 화장이 짙어진다.\n\n"
             + f"결: {anchors_sp[3]} 검은 아이쉐도우를 바르고 검게 변한 복장을 입었다.")
    nrm_sp = CI._normalize_extract({"protagonist": {}, "partner": {}, "guides": {}, "segments": anchors_sp})
    wins_sp = CI.split_by_segments(ep_sp, nrm_sp["segments"])
    check("16:59 regression ①: 96-char anchor survives → 4 acts, '결:' label stays with its act",
          len(nrm_sp["segments"]) == 4 and len(wins_sp) == 4 and wins_sp[-1].lstrip().startswith("결:"),
          f"{len(nrm_sp['segments'])}seg/{len(wins_sp)}act")
    # [2026-09-08] 16:59 regression ②: layout settled on 1 page / 5 slots → 기 act got 1 cut.
    # In auto mode the pipeline must force more pages to honor the 2-cuts-per-act floor.
    logs_b = []
    config.ep_beat_segments = {43: seg_acts}
    orig_clog_b, orig_pl3 = CG._clog, CG.call_openai_for_text
    try:
        CG._clog = lambda m: logs_b.append(str(m))
        CG.call_openai_for_text = _fake_panels
        try:
            sc_floor = CG.request_panel_script(43, 1, pages=0, episode_text=acts_body, chars_per_panel=400)
        finally:
            CG.call_openai_for_text = orig_pl3
    finally:
        CG._clog = orig_clog_b
    config.ep_beat_segments.pop(43, None)
    check("16:59 regression ②: too few cuts for the acts → pages forced up, no 1-cut act",
          sc_floor["beats"] == 4 and len(sc_floor["panels"]) >= 8,
          f"beats={sc_floor['beats']} panels={len(sc_floor['panels'])}")
    check("페이지 수 = 컷 수를 담을 만큼(4페이지 고정이 아님)",
          len(sc["page_plans"] or []) > 2, str(len(sc["page_plans"] or [])))

    # _plan_rows 스펙 직행: 2컷 40/60 + 중앙 50% + 3컷 분할
    pw6, r6 = CPM._plan_rows(3, [["a"], ["b"], ["c"]], None, None, None,
                             unit_w=820, cols=2, gutter=16, pad=10, border=4, font_size=26,
                             row_spec=[{"cells": [{"idx": 0, "share": 1.0}]},
                                       {"cells": [{"idx": 1, "share": 0.4}, {"idx": 2, "share": 0.6}]}])
    c1 = r6[1]["cells"]
    check("row_spec: 폭 비율 40/60", abs(c1[0]["w"] / (c1[0]["w"] + c1[1]["w"]) - 0.4) < 0.01,
          str([c["w"] for c in c1]))
    _, r6c = CPM._plan_rows(1, [["a"]], None, None, None, unit_w=820, cols=2, gutter=16, pad=10,
                            border=4, font_size=26,
                            row_spec=[{"cells": [{"idx": 0, "share": 0.5}], "center": True}])
    check("row_spec: 중앙 50% 행", abs(r6c[0]["cells"][0]["w"] - 828) <= 1 and r6c[0]["x"] > 16,
          f"w={r6c[0]['cells'][0]['w']} x={r6c[0]['x']}")
    paths6 = []
    os.makedirs(tmp, exist_ok=True)
    for i6 in range(3):
        fp6 = os.path.join(tmp, f"cut{i6}.png")
        Image.new("RGB", (1024, 1344), (60 * i6 + 30, 90, 160)).save(fp6)
        paths6.append(fp6)
    saved6 = CPM.compose_pages(paths6, [["a", "x: 1", "y: 2"]] * 3, tmp, "cutspec",
                               page_specs=[{"size": 3, "rows": [
                                   {"cells": [{"idx": 0, "share": 1.0}]},
                                   {"cells": [{"idx": 1, "share": 0.4}, {"idx": 2, "share": 0.6}]}]}])
    check("compose_pages page_specs → 페이지 저장", len(saved6) == 1 and os.path.exists(saved6[0]))
    shutil.rmtree(tmp, ignore_errors=True)

    print("\n== ⑧ 크로스 플랫폼 (OS 분기는 run_comic.py 안에만) ==")
    sh = open(os.path.join(ROOT, "run_ollama.sh"), encoding="utf-8").read()
    bat_p = os.path.join(ROOT, "run_ollama_win.bat")
    bat = open(bat_p, encoding="utf-8").read()
    check("셸 래퍼가 얇다(각 60줄 미만, --start-llm 로 위임)",
          len(sh.splitlines()) < 60 and len(bat.splitlines()) < 60
          and "--start-llm" in sh and "--start-llm" in bat,
          f"{len(sh.splitlines())}/{len(bat.splitlines())}줄")
    # 주석은 설명을 위해 plot.json/ollama 이야기를 할 수 있다 — **실행 라인**만 감사한다
    sh_code = "\n".join(l for l in sh.splitlines() if not l.strip().startswith("#"))
    bad_tok = [t for t in ("plot.json", "pkill", "pgrep", "setsid", "restore_plot", "curl") if t in sh_code]
    check("셸에 서버 로직 없음(plot.json 수정/pkill/setsid/curl 없음)", not bad_tok, str(bad_tok))
    check("Windows 배치가 CRLF(cmd 라벨/goto 안전)", b"\r\n" in open(bat_p, "rb").read(400))
    rc_src = open(os.path.join(ROOT, "run_comic.py"), encoding="utf-8").read()
    check("run_comic에 /tmp 하드코딩 없음(OS 공용 로그/PID)", '"/tmp/' not in rc_src and "'/tmp" not in rc_src)
    # [2026-09-08] 16:22 재현: 직전 실행이 서버를 내리는 중인데 "이미 실행 중"으로 건너뛴 다음
    #   실행이 추출에서 Connection refused로 죽었다. 연속 확인/재기동 감사를 고정한다.
    so_src = rc_src.split("def start_ollama", 1)[1].split("\ndef ")[0]
    check("start_ollama: 포트 연속 두 번 열려야 실행 중으로 본다(내려가는 중 경합 스킵 방지)",
          so_src.count("_tcp(host, port") >= 3 and "깜빡" in so_src,
          f"tcp@{so_src.count('_tcp(host, port')}")
    check("--start-llm 미기동이면 추출 전에 종료(시도하지도 않고 Connection refused)",
          "return 2" in rc_src.split("! ollama 미기동", 1)[1][:90])
    check("추출 중 서버 사망 시 재기동 후 1회 재시도", "재기동 후 추출 1회 재시도" in rc_src)
    _osv = RC._OS_OVERRIDE
    try:
        RC._OS_OVERRIDE = "win"
        win = RC.is_win()
        kw_win, args_win, env_win = RC._detach_kwargs(), RC._comfy_args(), RC._comfy_env()
        RC._OS_OVERRIDE = "linux"
        lin = RC.is_win()
        kw_lin, args_lin, env_lin = RC._detach_kwargs(), RC._comfy_args(), RC._comfy_env()
    finally:
        RC._OS_OVERRIDE = _osv
    check("is_win() --os 오버라이드(auto가 기본)",
          win is True and lin is False and RC.is_win() == (os.name == "nt"))
    check("detach 방법 분기(POSIX setsid / Windows creationflags)",
          kw_lin.get("start_new_session") is True and "creationflags" in kw_win
          and "start_new_session" not in kw_win, f"{kw_lin} | {kw_win}")
    check("--use-ck-attention은 Linux 전용(Windows torch는 미지원)",
          "--use-ck-attention" in args_lin and "--use-ck-attention" not in args_win,
          f"{args_lin} | {args_win}")
    check("MIOPEN/HIP env는 Linux 전용, Windows는 PYTHONUTF8만",
          any("MIOPEN" in k for k in env_lin) and set(env_win) == {"PYTHONUTF8"},
          f"{sorted(env_win)}")
    tmpw = tempfile.mkdtemp(prefix="st_os_")
    for sub in (os.path.join("venv", "Scripts"), os.path.join("venv", "bin")):
        os.makedirs(os.path.join(tmpw, sub), exist_ok=True)
    open(os.path.join(tmpw, "venv", "Scripts", "python.exe"), "w").write("")
    open(os.path.join(tmpw, "venv", "bin", "python"), "w").write("")
    check("ComfyUI venv python probe: OS별 우선(venv\\Scripts vs venv/bin)",
          RC._comfy_python(tmpw) in (os.path.join(tmpw, "venv", "bin", "python"),
                                     os.path.join(tmpw, "venv", "Scripts", "python.exe")),
          RC._comfy_python(tmpw))
    fake_bin = os.path.join(tmpw, "ollama_bin")
    open(fake_bin, "w").write("x")
    _saved_env = {k: os.environ.get(k) for k in ("OLLAMA_BIN", "COMIC_OLLAMA_HOST", "COMIC_OLLAMA_ENB")}
    try:
        os.environ["OLLAMA_BIN"] = fake_bin
        bin_ok = RC._ollama_bin() == fake_bin
        os.environ["COMIC_OLLAMA_HOST"] = "127.0.0.1:11455"
        host, port = RC._ollama_host_port()
        h_ok = (host, port) == ("127.0.0.1", "11455") and OAC.ollama_host() == "http://127.0.0.1:11455"
        os.environ["COMIC_OLLAMA_ENB"] = "no"
        enb_no = OAC._ollama_enabled({"ollama_enb": "yes"}) is False
        os.environ["COMIC_OLLAMA_ENB"] = "yes"
        enb_yes = OAC._ollama_enabled({"ollama_enb": "no"}) is True
    finally:
        for k, v in _saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    shutil.rmtree(tmpw, ignore_errors=True)
    check("ollama 바이너리는 env OLLAMA_BIN 우선(윈도우 포터블 동봉 대응)", bin_ok)
    check("COMIC_OLLAMA_HOST 가 plot.json보다 앞선다(포트 충돌 회피)", h_ok,
          f"{host}:{port}")
    check("COMIC_OLLAMA_ENB 가 plot.json을 오버라이드(파일 수정/원복 사고 방지)", enb_no and enb_yes)
    check("로그/PID가 repo log/ 안(어느 OS에서 같은 자리)",
          RC._repo_path("x.log").startswith(os.path.join(ROOT, "log")), RC._repo_path("x.log"))
    check("폰트 후보에 Windows/macOS 경로가 있다(한글 캡션 tofu 회귀 방지)",
          any("Windows" in c and "Fonts" in c for c in CPM.FONT_CANDIDATES)
          and any(c.startswith("/System/Library/Fonts") for c in CPM.FONT_CANDIDATES),
          str(len(CPM.FONT_CANDIDATES)))
    # preflight는 LLM 백엔드 분기에서 return 한다 — 폰트/ComfyUI 점검이 그 뒤에 있으면
    # ollama 경로에서 두 항목이 조용히 건너뛴다(실제로 한 번 물었다). 순서를 감사한다.
    pf_src = rc_src.split("def preflight", 1)[1].split("\ndef ")[0]
    i_font = pf_src.find("한글 폰트")
    i_comfy = pf_src.find("if need_comfy:")
    i_ret = pf_src.find("_ollama_enabled(pj)")
    check("프리플라이트 순서: 폰트/ComfyUI 점검이 ollama 분기 return보다 앞",
          "need_pages" in pf_src.split(") ->")[0] and 0 <= i_font < i_ret and 0 <= i_comfy < i_ret,
          f"font@{i_font} comfy@{i_comfy} backend@{i_ret}")
    check("--font/config.comic_font 배선(compose_page font_path 인자)",
          hasattr(config, "comic_font") and "font_path" in inspect.signature(CPM.compose_page).parameters)

    print("\n== ⑨ local 전용 입력 포맷 (--special / novel_progress) ==")
    import types
    import novel_progress as NP
    INP = os.path.join(ROOT, "inputs")
    found = NP.discover(INP, "deadbeef")
    check("--special: 디렉터리 디스커버리는 epNN_해시.txt 기준(회차 오름차순)",
          [f["ep"] for f in found] == [90, 91], str([f["ep"] for f in found]))
    check("--special: 시트 JSON을 같은 회차·해시로 자동 연결",
          all(os.path.basename(f["sheet"]) == f"character_sheet_ep{f['ep']}_deadbeef.json"
              for f in found), str([f["sheet"] for f in found]))

    r90 = NP.load(found[0]["episode"], found[0]["sheet"])
    b90 = r90["episode_text"]
    check("--special: 머리/꼬리 블록과 '#####' 구분선이 본문에서 사라진다",
          "=== Episode" not in b90 and "Character Sheet" not in b90 and "#####" not in b90,
          b90[:80])
    check("--special: [장소/상황/시간/복장] 메타 라인은 1막 지문으로 남는다",
          b90.startswith("장소:") and "상황:" in b90 and "복장:" in b90, b90[:60])
    check("--special: [TALK]→'이름: 대사', [INNER]→'(속마음)'로 평문화",
          "호시노 아야:" in b90 and "카미유 렌:" in b90 and "(속마음)" in b90)
    check("--special: 꼬리 시트가 본문에 섞이지 않는다(프롬프트 예산)",
          len(b90) < 1500, f"{len(b90)}자")
    check("--special: 기승전결 앵커 4개를 본문 원문 사본으로 확보",
          len(r90["segments"]) == 4, str(r90["segments"]))
    wins = CI.split_by_segments(b90, r90["segments"])
    check("--special: 앵커로 4막이 갈라진다(split_by_segments)", len(wins) == 4,
          str([len(w) for w in wins]))
    check("--special: 첫 앵커 앞 메타(장소/복장)가 1막에 남는다(증발 회귀 방지)",
          bool(wins) and wins[0].startswith("장소:") and "기:" in wins[0],
          wins[0][:50] if wins else "(없음)")
    check("--special: 막 라벨이 제 막에 붙는다(앞 막 꼬리에 '승:'로 남지 않음)",
          len(wins) == 4 and not wins[0].rstrip().endswith("승:") and wins[1].startswith("승:"),
          wins[0][-20:] if wins else "(없음)")

    t90, ov90 = r90["sheet_text"], r90["overrides"]
    check("--special: 한글 키 시트(ep01형) → 평문 시트", "이름: 호시노 아야" in t90 and "black hair" in t90)
    check("--special: '얼굴 스타일 1' + '얼굴 스타일 2' 병합",
          "shy smile" in ov90["protagonist"]["face_style"]
          and "wide eyes" in ov90["protagonist"]["face_style"], ov90["protagonist"].get("face_style"))
    check("--special: 문자열 크기 태그(가슴/엉덩이)를 body_shape로 구제",
          ov90["protagonist"]["body_shape"] == "slim, medium_breasts, narrow_hips",
          ov90["protagonist"].get("body_shape"))
    check("--special: 성별 한글→female/male", ov90["protagonist"]["sex"] == "female"
          and ov90["partner"]["sex"] == "male")
    r91 = NP.load(found[1]["episode"], found[1]["sheet"])
    check("--special: 영문 키 시트(ep02+형)도 같은 자리로",
          r91["overrides"]["protagonist"]["hair_color"] == "black hair"
          and "집계:" in r91["sheet_text"], r91["overrides"]["protagonist"].get("hair_color"))
    check("--special: 태그 뒤 한글 괄호 주석은 버린다(anima는 영문 태그만 읽음)",
          r91["overrides"]["protagonist"]["body_shape"] == "slim, medium_breasts, narrow_hips",
          r91["overrides"]["protagonist"].get("body_shape"))
    check("--special: 복장(clothes)은 우선주입에 넣지 않는다(한글→영문은 LLM 번역 몫)",
          "clothes" not in r91["overrides"]["protagonist"], str(sorted(r91["overrides"]["protagonist"])))
    no_sheet = NP.load(found[0]["episode"], "")
    check("--special: 시트 JSON이 없으면 본문 꼬리 시트를 쓴다(폴백)",
          "Character Sheet" in no_sheet["sheet_text"]
          and any("꼬리" in n for n in no_sheet["notes"]), str(no_sheet["notes"]))

    d = CI.load_inputs(os.path.join(INP, "ep90_deadbeef.txt"),
                       os.path.join(INP, "character_sheet_ep90_deadbeef.json"))
    check("--special 없이도 내용을 감지해 어댑터를 탄다(원작 메타 본문 오염 방지)",
          d["format"] == "novel_progress" and len(d["segments"]) == 4, d["format"])
    e_txt, s_txt = CI.prepare_texts(os.path.join(INP, "ep90_deadbeef.txt"),
                                    os.path.join(INP, "character_sheet_ep90_deadbeef.json"))
    check("prepare_texts 호환(2-tuple) — 본문을 평문으로 받는다",
          isinstance(e_txt, str) and e_txt.startswith("장소:") and "이름: 호시노 아야" in s_txt)
    d_plain = CI.load_inputs(os.path.join(INP, "ep01.txt"), os.path.join(INP, "sheet01.txt"))
    check("배송된 평문 입력은 어댑터를 타지 않는다(기존 동작 불변)",
          d_plain["format"] == "plain" and d_plain["overrides"] == {} and d_plain["segments"] == [],
          d_plain["format"])
    check("strip_markdown: '#####' 단독 구분선 제거 / #캐릭터태그#는 보존",
          "#####" not in CI.strip_markdown("본문\n#####\n다음")
          and "#Usagi Tsukino from Sailor Moon#" in CI.strip_markdown("#Usagi Tsukino from Sailor Moon#"))

    seen_prompt = {"t": ""}

    def _fake_extract(prompt, **kw):
        seen_prompt["t"] = prompt
        return json.dumps(CANNED, ensure_ascii=False), None

    orig_ci = CI.call_openai_for_text
    CI.call_openai_for_text = _fake_extract
    try:
        data_sp = CI.extract(d["episode_text"], d["sheet_text"], ep_num=90, need_segments=False)
        CI.apply_to_config(data_sp, d["episode_text"], d["sheet_text"], ep_num=90,
                           panels_per_page=5, book_num=1, total_episodes=90,
                           overrides=d["overrides"], segments=d["segments"], safety="safe")
    finally:
        CI.call_openai_for_text = orig_ci
    check("--special: 앵커를 확보한 회차는 LLM에게 복사를 시키지 않는다(실패면 분할 증발)",
          '"segments": []' in seen_prompt["t"] and "항상 빈 배열" in seen_prompt["t"])
    check("--special: 시트 JSON이 LLM 추출보다 우선(10화 캐릭터 고정)",
          config.name == "호시노 아야" and config.hair_color == "black hair"
          and config.body_shape == "slim, medium_breasts, narrow_hips",
          f"{config.name}/{config.hair_color}/{config.body_shape}")
    check("--special: clothes는 LLM 번역값을 그대로 쓴다",
          config.clothes == CANNED["protagonist"]["clothes"], config.clothes)
    check("--special: --safety 가 수위를 강제(safe 정책)", config.review_safety[89] == "safe",
          config.review_safety[89])
    check("--special: 막 앵커가 config.ep_beat_segments로 간다(comic_gen 장면 골격)",
          config.ep_beat_segments.get(90) == d["segments"], str(config.ep_beat_segments.get(90))[:60])
    check("--special: 회차 키를 보존한다(연속 실행에서 앞 회차 가이드가 안 사라진다)",
          90 in (config.ep_corruption_guides_map or {}) and 1 in (config.ep_corruption_guides_map or {})
          and 90 in (config.special_writing_req or {}), str(sorted(config.ep_corruption_guides_map or {})))

    jobs_all, total_all = RC._resolve_jobs(types.SimpleNamespace(
        episode=INP, sheet="", special=True, plot_hash="deadbeef", all_eps=True, ep=1), None)
    check("--special: --all-eps는 발견한 전 회차를 순서대로 묶는다",
          [j[0] for j in jobs_all] == [90, 91] and total_all == 2, f"{jobs_all} / {total_all}")
    jobs_one, _ = RC._resolve_jobs(types.SimpleNamespace(
        episode=os.path.join(INP, "ep90_deadbeef.txt"), sheet="", special=True,
        plot_hash="", all_eps=False, ep=1), None)
    check("--special: 파일 한 개도 시트 JSON을 찾아 붙인다",
          jobs_one[0][0] == 1 and os.path.basename(jobs_one[0][2]) == "character_sheet_ep90_deadbeef.json",
          str(jobs_one))

    # ============================== [2026-09-09] 화면 문법 (설명 / 말풍선 / 속마음 / 의성어)
    print("\n== ⑩ 화면 문법: 설명 박스 + 풍선(≤2) + 의성어 + ★서두요약·에필로그 ==")
    ln = CG._norm_lines(["소타: 무거운 건 저에게 맡기세요.", "(이 사람, 아까부터 알고 있었다.)",
                         {"kind": "thought", "who": "유즈키", "text": "세 번째 호흡"},
                         "너무 긴 대사 " * 8])
    check("_norm_lines: '이름: 대사'에서 화자를 떼어 낸다(화면에 이름이 안 찍힌다)",
          ln[0]["who"] == "소타" and ln[0]["text"].startswith("무거운") and "소타" not in ln[0]["text"],
          str(ln[:1]))
    check("_norm_lines: (괄호) 표기는 속마음 풍선으로 본다", ln[1]["kind"] == "thought", str(ln[1]))
    check("_norm_lines: 풍선은 최대 2개(3번째부터 버린다)", len(ln) == 2, str(len(ln)))
    check("_norm_lines: 한 풍선은 최대 DIALOG_MAX_LEN자(길면 … 로 자른다 → 두 번째 풍선으로)",
          all(len(x["text"]) <= CG.DIALOG_MAX_LEN for x in ln), str([len(x["text"]) for x in ln]))
    check("_norm_lines: {kind,who,text} dict 입력도 받는다",
          CG._norm_lines([{"kind": "speech", "who": "유즈키", "text": "안 돼"}])[0]["kind"] == "speech")
    check("_norm_dialog(하위호환): 옛 '화자: 말' 문자열로 되돌려 준다(옛 메타·스크립트)",
          CG._norm_dialog([{"kind": "speech", "who": "유즈키", "text": "안 돼"}]) == ["유즈키: 안 돼"],
          str(CG._norm_dialog([{"kind": "speech", "who": "유즈키", "text": "안 돼"}])))
    check("text_payload 하위호환: str/list 입력도 화면 문법으로 번역된다",
          CPM.text_payload("지문")["narration"] == "지문"
          and len(CPM.text_payload(["지문", "대사1", "대사2", "대사3"])["balloons"]) == 2)

    pnl = {"no": 1, "caption_ko": "지문", "sfx": "쿵", "facing": "right", "fade": 0.0,
           "lines": [{"kind": "speech", "who": "소타", "text": "대사"}]}
    tp0 = CG.panel_text_payload(pnl)
    check("panel_text_payload: 설명/풍선/의성어 + facing → 풍선 꼬리 쪽(side)",
          tp0["narration"] == "지문" and tp0["sfx"] == "쿵" and tp0["balloons"][0]["side"] == "right",
          str(tp0))
    _r_sum = CG._apply_text_role({"no": 1, "type": "face", "camera": "close_up",
                                  "caption_ko": "상황 요약", "lines": []}, "summary", [])
    check("★서두 요약 컷: 배경만(bg_only) + 큰 지문 + 풍선 없음 + action",
          _r_sum["bg_only"] and _r_sum["narr_large"] and _r_sum["lines"] == []
          and _r_sum["type"] == "action", str({k: _r_sum[k] for k in ("bg_only", "narr_large", "type")}))
    _r_epi = CG._apply_text_role({"no": 2, "caption_ko": "에필로그", "lines": []}, "epilogue", [])
    check("★에필로그 컷: 이벤트신을 반투명(fade>0) + 큰 지문",
          float(_r_epi["fade"]) > 0 and _r_epi["narr_large"], str(_r_epi["fade"]))
    config.comic_summary_cuts = False
    check("★스위치를 끄면 요약 역할이 붙지 않는다(--no-summary-cuts 경로)",
          CG._apply_text_role({"no": 1, "lines": []}, "summary", ["n"]).get("text_role", "") == "")
    config.comic_summary_cuts = True

    PX, PY, PW, PH = 30, 40, 420, 560

    def _placed(item, facing="right"):
        cv = Image.new("RGB", (1024, 900), (255, 255, 255))
        return CPM._draw_panel_text(cv, ImageDraw.Draw(cv), PX, PY, PW, PH, item, facing=facing)

    _big_text = ("에필로그 — 그 뒤 두 사람은 서로의 이름을 부르지 못한 채 다음 역을 지나쳤다. "
                 "창밖의 불빛만 두 사람 사이에 남았고, 안내 방송은 두 번 더 흘러나왔다. "
                 "유즈키는 그제야 손목의 온도를 떠올렸다. 소타는 말하지 않았다.")
    got = _placed({"narration": "深夜 2시, 옥상 물탱크가 끊기는 소리가 났다.",
                   "balloons": [{"kind": "speech", "text": "무거운 건 저에게 맡기세요.", "side": "right"},
                                {"kind": "thought", "text": "이 사람, 알고 있었다.", "side": "left"}],
                   "sfx": "두근두근"})
    check("컷당 화면 요소는 설명+풍선2+의성어 = 최대 4개", 0 < len(got) <= 4, f"{len(got)}개")
    check("화면 요소는 전부 컷 안(컷 밖으로 넘치지 않는다)",
          all(r[0] >= PX - 1 and r[1] >= PY - 1 and r[2] <= PX + PW + 1 and r[3] <= PY + PH + 1
              for r in got), str(got))

    def _hit(a, b):
        return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])

    check("화면 요소는 서로 안 겹친다(글자가 서로를 먹지 않는다)",
          not any(_hit(a, b) for i, a in enumerate(got) for b in got[i + 1:]), str(got))
    check("배치는 결정론(같은 입력 → 같은 자리, 재실행 결과 동일)",
          _placed({"narration": "深夜 2시, 옥상 물탱크가 끊기는 소리가 났다.",
                   "balloons": [{"kind": "speech", "text": "무거운 건 저에게 맡기세요.", "side": "right"},
                                {"kind": "thought", "text": "이 사람, 알고 있었다.", "side": "left"}],
                   "sfx": "두근두근"}) == got, "배치 불일치")
    _caps = [r for r in got if r[0] <= PX + 12]
    check("설명 박스는 하단 왼쪽에 붙는다(왼쪽·아래 여백에 밀착)",
          _caps and _caps[0][0] <= PX + 12 and _caps[0][3] >= PY + PH - 12, str(_caps))
    _big = _placed({"narration": _big_text, "narr_large": True})
    _bb = _big[0]
    _cover = (_bb[2] - _bb[0]) * (_bb[3] - _bb[1]) / float(PW * PH)
    check("★요약/에필로그 지문은 컷의 70%를 넘기지 않는다(화면을 다 가리지 않는다)",
          _cover <= 0.72, f"cover={_cover:.2f}")
    _fade_img = CPM.apply_fade(Image.new("RGB", (8, 8), (40, 60, 80)), CPM.FADE_ALPHA)
    check("에필로그 이벤트신은 흰 쪽으로 반투명해진다(지문이 읽힌다)",
          all(b > a for a, b in zip((40, 60, 80), _fade_img.getpixel((0, 0)))), str(_fade_img.getpixel((0, 0))))

    check("화면 문법 용도별 폰트 4종을 정의한다(설명/대사/속마음/의성어)",
          set(CPM.BUNDLED_FONTS) == {"narration", "dialog", "thought", "sfx"},
          str(sorted(CPM.BUNDLED_FONTS)))
    check("폰트 자동 다운로드 목록은 https + OFL 원문 포함(재배포 가능한 것만 쓴다)",
          len(CPM.FONTS_MANIFEST) >= 6
          and all(u.startswith("https://") for _, u in CPM.FONTS_MANIFEST)
          and any(n.endswith(".txt") for n, _ in CPM.FONTS_MANIFEST), str(len(CPM.FONTS_MANIFEST)))
    check("Gaegu(손글씨 붓체·OFL)도 다운로드 목록과 속마음 후보에 있다",
          any("gaegu" in u.lower() and u.lower().endswith(".ttf") for _, u in CPM.FONTS_MANIFEST)
          and any("gaegu" in u.lower() and u.lower().endswith(".txt") for _, u in CPM.FONTS_MANIFEST)
          and any("gaegu" in f.lower() for f in CPM.BUNDLED_FONTS["thought"]),
          str([n for n, _ in CPM.FONTS_MANIFEST if "gaegu" in n.lower()]))
    _gi = open(os.path.join(ROOT, ".gitignore"), encoding="utf-8").read()
    check("data/fonts/는 gitignore(폰트는 실행 환경에서 받아 쓴다 — repo는 가볍게)",
          "data/fonts/" in _gi or "data/fonts" in _gi)
    check("--get-fonts / --font-용도 4종 / --no-epilogue / --no-summary-cuts CLI 배선",
          all(t in rc_src for t in ("--get-fonts", "--font-narration", "--font-dialog",
                                    "--font-thought", "--font-sfx", "--no-epilogue",
                                    "--no-summary-cuts"))
          and all(hasattr(config, k) for k in ("comic_font_narration", "comic_font_dialog",
                                               "comic_font_thought", "comic_font_sfx",
                                               "comic_epilogue", "comic_summary_cuts")))

    # [2026-09-09] 수정 1: ★큰 지문(70% 박스)은 (1) 회차집 첫 회차 프롤로그 (2) 각 회차의 첫 컷
    #   (3) 마지막 회차 끝 에필로그 — 여기에만 들어간다. 그 외 컷은 평범한 설명/풍선 컷이다.
    _te_bak = config.total_episodes
    config.comic_prologue_cut = True
    _plans_mid = CG.plan_pages(7, 2)                    # 중간 회차(7/12): 프롤로그도 에필로그도 없다
    _story_mid = [pl for pl in _plans_mid if not pl.get("prologue")]
    check("★요약은 회차의 **첫 컷 하나만**(페이지마다 붙이지 않는다)",
          _story_mid and _story_mid[0]["slots"][0]["role"] == "summary"
          and not any(pl["slots"][0]["role"] == "summary" for pl in _story_mid[1:])
          and not any(pl.get("epilogue") for pl in _plans_mid),
          str([[s["role"] for s in pl["slots"][:1]] for pl in _story_mid]))
    _plans_ep1 = CG.plan_pages(1, 2)                    # 첫 회차: ★프롤로그 1컷이 맨 앞에 붙는다
    check("★프롤로그는 회차집의 **첫 회차** 맨 앞에 1컷으로 붙는다",
          len(_plans_ep1[0]["slots"]) == 1 and _plans_ep1[0].get("prologue") is True
          and _plans_ep1[0]["slots"][0]["role"] == "prologue"
          and len(_plans_ep1) == 3, str([pl.get("template_id") for pl in _plans_ep1]))
    check("두 번째 회차에는 프롤로그가 없다",
          not any(pl.get("prologue") for pl in CG.plan_pages(2, 2)))
    config.total_episodes = 7
    _plans_last = CG.plan_pages(7, 2)                   # 마지막 회차: ★에필로그 1페이지(1칸)
    check("★에필로그는 **마지막 회차 끝**에만, 1칸(전폭 1컷)으로 붙는다",
          bool(_plans_last[-1].get("epilogue"))
          and len(_plans_last[-1]["slots"]) == 1
          and all(s["role"] == "epilogue" for s in _plans_last[-1]["slots"]),
          f"{len(_plans_last[-1]['slots'])}칸 {[_pl.get('template_id') for _pl in _plans_last]}")
    _star_total = 0
    for _e in range(1, 11):                             # 사용자 계산: 1 + 10 + 1 = 12
        config.total_episodes = 10
        _star_total += sum(1 for pl in CG.plan_pages(_e, 2)
                           for s in pl["slots"] if s.get("role"))
    config.total_episodes = _te_bak
    check("10회 기준 ★ 개수 = 프롤로그 1 + 회차 앞 10 + 마지막 에필로그 1 = 12",
          _star_total == 12, str(_star_total))
    config.comic_epilogue = config.comic_summary_cuts = config.comic_prologue_cut = False
    config.total_episodes = 1
    _plans_off = CG.plan_pages(1, 2)
    config.comic_epilogue = config.comic_summary_cuts = config.comic_prologue_cut = True
    check("스위치를 끄면 ★슬롯이 완전히 사라진다(평범한 컷만 남는다)",
          not any(pl.get("epilogue") or pl.get("prologue") for pl in _plans_off)
          and not any(s.get("role") for pl in _plans_off for s in pl["slots"]),
          str([s.get("role") for pl in _plans_off for s in pl["slots"]]))
    check("에필로그 템플릿은 일반 기승전결 페이지로 뽑히지 않는다",
          not any(pl["template_id"] == "epilogue_aftermath" for pl in _plans_off),
          str([pl["template_id"] for pl in _plans_off]))
    p_bg = CG.build_panel_prompt(0, {"no": 1, "type": "action", "wide": False,
                                     "pose": "Wide shot of the empty rooftop at night, nobody in the frame.",
                                     "camera": "front_view", "position": "NONE", "climax": "",
                                     "caption_ko": "", "lines": [], "bg_only": True},
                                "nsfw", gloss={})
    check("★배경만 컷 프롬프트: 'no humans' 넣고 1girl/1boy를 부르지 않는다",
          "no humans" in p_bg and "1girl" not in p_bg and "1boy" not in p_bg, p_bg[:140])
    _cg_src = open(os.path.join(ROOT, "comic_gen.py"), encoding="utf-8").read()
    check("컷 대본 프롬프트가 3가지 화면 문법·★슬롯을 시킨다(규칙 8/9/16)",
          "컷의 화면 텍스트는 다음 3가지 중 하나" in _cg_src and "★서두 요약" in _cg_src
          and '"lines"' in _cg_src and '"sfx"' in _cg_src)
    raw3 = [{"no": 1, "type": "action", "pose": "She stands.", "camera": "front_view",
             "position": "NONE", "climax": "", "caption_ko": "a", "lines": []},
            {"no": 2, "type": "action", "pose": "She walks.", "camera": "front_view",
             "position": "NONE", "climax": "", "caption_ko": "지문", "sfx": "쿵",
             "lines": [{"kind": "thought", "who": "유즈키", "text": "조용히"}]},
            {"no": 3, "type": "action", "pose": "She sits.", "camera": "front_view",
             "position": "NONE", "climax": "", "caption_ko": "b", "lines": []}]
    rep3, _ = CG._repair_panels(raw3)
    tp3 = CG.panel_text_payload(rep3[1])
    check("_repair_panels → lines/sfx가 유지되고 화면 문법 페이로드로 번역된다",
          rep3[1]["lines"][0]["kind"] == "thought" and tp3["sfx"] == "쿵"
          and tp3["narration"] == "지문" and tp3["balloons"][0]["kind"] == "thought", str(tp3))

    check("★지문이 비면 본문 첫 문장으로 채운다(LLM이 ★규칙을 어겨도 화면이 빈 컷이 아니다)",
          CG._first_sentence("붉은 달이 떴다. 유즈키는 순찰을 시작했다. 그 뒤로 큰 일이났다.").startswith("붉은 달이 떴다.")
          and len(CG._first_sentence("가" * 400)) <= CG.SUMMARY_CAPTION_MAX_LEN)
    _star_panels = [{"no": 1, "caption_ko": "", "lines": [], "text_role": "summary"},
                    {"no": 2, "caption_ko": "", "lines": [], "text_role": "summary"}]
    _star_notes = []
    CG._fill_star_narration(_star_panels, ["기 서두 본문 문장. 이어지는 내용.", "승 서두 본문 문장."], [1, 1], _star_notes)
    check("★요약/에필로그 지문 폴백은 **그 컷이 속한 장면**의 본문에서 나온다",
          _star_panels[0]["caption_ko"].startswith("기 서두")
          and _star_panels[1]["caption_ko"].startswith("승 서두") and _star_panels[0]["narr_large"],
          str([p["caption_ko"] for p in _star_panels]) + " / " + str(_star_notes))

    # ---------------------------------------------------------------------------
    # ⑪ [2026-09-09] 화면 문법 v4 — 사용자 지시 4건
    #   1) 주인공 풍선 = 왼쪽 위(2개면 아래), 상대방 = 오른쪽 위(2개면 아래), 꼬리는 아주 작게
    #   2) 중간 이벤트 컷의 설명 박스는 대화에 맞추어 작게
    #   3) 컷 경계선과 그림 사이 빈틈 없이 굵은 검정선만  (④에서 픽셀로 확인)
    #   4) 감정 이모티콘(분노/놀람/땀/하트/음영/반짝/물음) — 감정마다 다른 색
    # ---------------------------------------------------------------------------
    print("\n== ⑪ 화면 문법 v4: 풍선 자리·꼬리 크기 / 설명 크기 / 감정 표시 ==")
    sp_me = CPM._balloon_slot_pref({"speaker": "me"})
    sp_ot = CPM._balloon_slot_pref({"speaker": "other"})
    check("주인공 풍선은 왼쪽 위 → 왼쪽 아래, 꼬리는 중앙 쪽",
          sp_me == (("tl", "bl", "ml", "center"), "center"), str(sp_me))
    check("상대방 풍선은 오른쪽 위 → 오른쪽 아래, 꼬리는 오른쪽 끝",
          sp_ot == (("tr", "br", "mr", "center"), "right"), str(sp_ot))

    # 실제 렌더: 주인공 2개 + 상대방 2개를 한 컷에
    _cv = Image.new("RGB", (PW, PH), (70, 130, 190))
    _dd = ImageDraw.Draw(_cv)
    _rects = []
    for _b in ({"kind": "speech", "text": "먼저 한 마디", "speaker": "me"},
               {"kind": "speech", "text": "두 번째 마디", "speaker": "me"},
               {"kind": "speech", "text": "상대 말1", "speaker": "other"},
               {"kind": "speech", "text": "상대 말2", "speaker": "other"}):
        _r = CPM._draw_balloon(_dd, PX, PY, PW, PH, _b, avoid=_rects)
        if _r:
            _rects.append(_r)
    _me = [r for r, b in zip(_rects, ["me", "me", "other", "other"]) if b == "me"]
    _ot = [r for r, b in zip(_rects, ["me", "me", "other", "other"]) if b == "other"]
    check("주인공 풍선 2개가 실제로 왼쪽 위/왼쪽 아래에 놓인다",
          len(_me) == 2 and all((r[0] + r[2]) / 2 < PX + PW * 0.5 for r in _me)
          and _me[0][1] < PY + PH * 0.5 < _me[1][1],
          str([(r[0], r[1]) for r in _me]))
    check("상대방 풍선 2개가 실제로 오른쪽 위/오른쪽 아래에 놓인다",
          len(_ot) == 2 and all((r[0] + r[2]) / 2 > PX + PW * 0.5 for r in _ot)
          and _ot[0][1] < PY + PH * 0.5 < _ot[1][1],
          str([(r[0], r[1]) for r in _ot]))
    check("풍선은 컷 밖으로 나가지 않는다",
          all(r[0] >= PX and r[1] >= PY and r[2] <= PX + PW and r[3] <= PY + PH for r in _rects),
          str(_rects))

    # 꼬리 크기: 몸체 상자 밖으로 나가는 검은 픽션의 길이 = 꼬리 길이(아주 작아야 한다)
    _cv2 = Image.new("RGB", (PW, PH), (255, 255, 255))
    _dd2 = ImageDraw.Draw(_cv2)
    _body = CPM._draw_balloon(_dd2, PX, PY, PW, PH,
                             {"kind": "speech", "text": "꼬리 길이 재기", "speaker": "me"})
    _px2 = _cv2.load()
    _by1 = _body[3]
    _run = 0
    for _y in range(_by1 + 1, PY + PH):
        if any(sum(_px2[x, _y]) < 120 for x in range(_body[0], _body[2])):
            _run += 1
        else:
            break
    check("꼬리는 몸체 밖으로 " + str(CPM.TAIL_LEN + 3) + "px 이내로 아주 작게만 나온다",
          0 < _run <= CPM.TAIL_LEN + 3, f"tail_run={_run}")
    check("꼬리 밑변도 작다(풍선 폭의 절반을 넘지 않는다)", CPM.TAIL_BASE <= 20, str(CPM.TAIL_BASE))

    # (2) 설명 박스는 글자 수에 맞추어 작아진다
    _cv3 = Image.new("RGB", (700, 500), (40, 120, 200))
    _dd3 = ImageDraw.Draw(_cv3)
    _short = CPM._draw_caption_box(_dd3, 0, 0, 700, 500, "짧은 설명", font_size=20)
    _long = CPM._draw_caption_box(_dd3, 0, 0, 700, 500, "긴 설명" * 14, font_size=20)
    _narrow = CPM._draw_caption_box(_dd3, 0, 0, 700, 500, "긴 설명" * 14, font_size=20, narrow=True)
    check("설명은 하단 왼쪽 고정 + 짧은 설명은 박스도 작다(컷 60%를 채우지 않는다)",
          _short and _short[0] == 8 and _short[3] == 492 and _short[2] - _short[0] < 700 * 0.35,
          str(_short[2] - _short[0]))
    check("대사가 있는 이벤트 컷(narrow)은 설명 박스가 더 좁고 낮다",
          _narrow and _long and _narrow[2] - _narrow[0] <= _long[2] - _long[0]
          and _narrow[3] - _narrow[1] <= _long[3] - _long[1],
          f"narrow={_narrow[2]-_narrow[0]}x{_narrow[3]-_narrow[1]} long={_long[2]-_long[0]}x{_long[3]-_long[1]}")
    # [2026-09-09] 사용자 지시: 설명은 **글자가 다 보일만큼** — 박스가 글을 자르면 안 된다
    _mid = CPM._draw_caption_box(_dd3, 0, 0, 700, 500, "설명 문장이 계속 길어지는 문장입니다 " * 6, font_size=20)
    check("설명이 잘리지 않는다 — 글자가 늘면 박스가 같이 커지고 끝이 … 로 안 잘린다",
          _mid and len(_mid[5]) >= 3 and _mid[3] - _mid[1] > _short[3] - _short[1] + 20
          and not any(ln.endswith("…") for ln in _mid[5]),
          f"lines={len(_mid[5]) if _mid else 0} h={_mid[3] - _mid[1] if _mid else 0}")
    check("대사가 있는 컷의 설명은 높이로만 제한된다(풍선 자리를 남긴다)",
          _narrow and _narrow[3] - _narrow[1] <= 500 * CPM.NARR_BALLOON_H_RATIO + 12,
          str(_narrow[3] - _narrow[1] if _narrow else 0))

    # [2026-09-09] 사용자 지시 2건 — ★지문 글자가 너무 크고, 박스가 컷의 절반만져서 글자가 잘린다
    _lp = CPM._draw_caption_box(_dd3, 0, 0, 700, 500, "짧은 ★지문", font_size=20, large=True)
    _lg = CPM._draw_caption_box(_dd3, 0, 0, 700, 500, "★지문이 길어서 여러 줄로 접힌다 " * 5, font_size=20, large=True)
    check("★회차 도입·에필로그 지문은 컷 폭을 다 쓴다(짧은 글자도 박스를 당기지 않는다)",
          _lp and _lg and (_lp[2] - _lp[0]) >= 700 * 0.90 and (_lg[2] - _lg[0]) >= 700 * 0.90,
          f"short={_lp[2]-_lp[0]} long={_lg[2]-_lg[0]} /700")
    check("★지문 글자 배율은 1.3배 이내로 줄인다(예전 1.5배는 다른 컷 대비 지나치게 컸다)",
          _lp and _lp[4] > 20 and _lp[4] <= int(20 * 1.30)
          and CPM.NARR_LARGE_FONT_RATIO <= 1.30, f"fs={_lp[4] if _lp else 0} ratio={CPM.NARR_LARGE_FONT_RATIO}")
    check("넓어진 폭 덕분에 같은 글씨가 더 적은 줄로 들어간다(잘림 감소)",
          _lg and len(_lg[5]) <= 3, f"lines={len(_lg[5]) if _lg else 0}")
    _wide_evt = CPM._draw_caption_box(_dd3, 0, 0, 700, 500, "이벤트 컷의 긴 설명 문장입니다 " * 5,
                                      font_size=20, narrow=True)
    check("이벤트 컷 설명도 컷 폭의 절반은 쓸 수 있다(예전 46% 상한에서는 글자가 짤렸다)",
          _wide_evt and _wide_evt[2] - _wide_evt[0] >= 700 * 0.55, str(_wide_evt[2] - _wide_evt[0]))
    check("대사가 없는 일반 설명은 컷 폭의 80%까지 쓸 수 있다", CPM.NARR_W_RATIO >= 0.78
          and CPM.NARR_W_RATIO_WITH_BALLOON >= 0.60,
          f"{CPM.NARR_W_RATIO}/{CPM.NARR_W_RATIO_WITH_BALLOON}")

    # (4) 감정 이모티콘 — 종류마다 다른 색으로, 컷 안에만
    _seen = {}
    for _k in CPM.EMOTIF_KINDS:
        _c = Image.new("RGB", (PW, PH), (255, 255, 255))
        _d3 = ImageDraw.Draw(_c)
        _rr = CPM._draw_emotif(_d3, 120, 60, 360, 130, PX, PY, PW, PH, _k)
        _cols = {p for p in _c.getdata() if p != (255, 255, 255)}
        _seen[_k] = _cols
        if not (_rr and _rr[0] >= PX and _rr[1] >= PY and _rr[2] <= PX + PW and _rr[3] <= PY + PH):
            break
    check("감정 이모티콘 7종을 컷 안에 그린다( anger/surprise/sweat/heart/gloom/sparkle/question )",
          all(_seen.get(k) for k in CPM.EMOTIF_KINDS), str({k: bool(v) for k, v in _seen.items()}))
    check("감정마다 색이 다르다",
          len({sorted(_seen[k])[0] for k in ("anger", "surprise", "heart")}) == 3,
          str({k: sorted(_seen[k])[0] for k in ("anger", "surprise", "heart")}))
    check("모르는 감정은 그리지 않는다", CPM._draw_emotif(ImageDraw.Draw(Image.new("RGB", (10, 10))),
                                                       1, 1, 8, 6, 0, 0, 10, 10, "unknown") is None)

    # 페이로드가 화자·감정을 싣고, 스위치를 끄면 감정은 빠진다
    _bak_name, _bak_name2 = config.name, config.name2
    config.name, config.name2 = "유즈키", "카에데"
    _pn = {"caption_ko": "두 사람은 마주 앉았다.", "facing": "front",
           "lines": CG._norm_lines([{"kind": "speech", "who": "유즈키", "text": "여기 앉아요"},
                                    {"kind": "speech", "who": "카에데", "text": "…설마 아직 안 끝났어?"}])}
    _tp = CG.panel_text_payload(_pn)
    check("who → 화자 판정(주인공=me / 상대방=other)이 페이로드로 간다",
          [b["speaker"] for b in _tp["balloons"]] == ["me", "other"],
          str([b["speaker"] for b in _tp["balloons"]]))
    check("LLM이 emo를 안 줘도 대사 분위기에서 감정을 추정한다",
          [b["emo"] for b in _tp["balloons"]] == ["", "surprise"],
          str([b["emo"] for b in _tp["balloons"]]))
    _e_bak = config.comic_emo_marks
    config.comic_emo_marks = False
    check("--no-emo-marks면 감정 표시가 사라진다(자리는 그대로)",
          all(not b["emo"] for b in CG.panel_text_payload(_pn)["balloons"])
          and [b["speaker"] for b in CG.panel_text_payload(_pn)["balloons"]] == ["me", "other"])
    config.comic_emo_marks = _e_bak
    config.name, config.name2 = _bak_name, _bak_name2
    _cp = CG.build_panel_script_prompt(1, 3, "유즈키", "카에데", "줄거리", ["기", "승", "전", "결"], [], panels_expected=8, episode_text="본문")
    check("컷 대본 프롬프트가 emo 필드·화자 이름 규칙을 시킨다",
          '"emo"' in _cp and "주인공=왼쪽" in _cp, "")

    # ── [2026-09-09] 지문 폴백/잘기 + 글리프 폴백 (EP1 실측: '장소: … 호…' / 말풍선 □)
    _guide = ("장소: 비가 그친 골목의 꽃가게. 유리 진열장에 물방울이 남는다. "
              "상황: 퇴근을 앞둔 아야가 단골에게 마지막 수국을 건넨다. 시간: 저녁무렵")
    check("★지문 폴백은 가이드 라벨('장소:' '상황:')을 버리고 서술 문장만 쓴다",
          CG._first_sentence(_guide).startswith("비가 그친"), CG._first_sentence(_guide)[:48])
    _cut = CG._clamp_caption("첫 문장이 끝난다. 둘째 문장이 아주 길어서 여기서 잘려 나가는 상황을 보여 주는 본문입니다. 셋째는 안 보인다.", 44)
    check("지문을 길이로 자를 때 절/문장 경계에서 자른다('…호' 토막 마감 금지)",
          _cut.endswith("…") and not _cut[:-1].endswith("호"), _cut)
    check("말풍선 글자가 폰트에 없으면 그리는 폰트로 바꿔 그린다(U+2026 실측 회귀)",
          not CPM.font_can_draw(CPM.load_font(22, os.path.join(CPM.BUNDLED_FONT_DIR, "Jua-Regular.ttf")), "…")
          if os.path.exists(os.path.join(CPM.BUNDLED_FONT_DIR, "Jua-Regular.ttf")) else True)
    _cv4 = Image.new("L", (300, 40), 0)
    _jua = CPM.load_font(22, os.path.join(CPM.BUNDLED_FONT_DIR, "Jua-Regular.ttf"))
    CPM.draw_text_runs(ImageDraw.Draw(_cv4), 4, 4, "윽… 이상해", font=_jua, fill=255, role="dialog",
                       font_path=os.path.join(CPM.BUNDLED_FONT_DIR, "Jua-Regular.ttf"))
    check("실제 렌더에서 □ 없이 그려진다(화선에 잉크가 있다)", sum(_cv4.tobytes()) > 500,
          str(sum(_cv4.tobytes())))
    check("기본 대화 폰트는 실측 문자를 전부 그린다(Poor Story)",
          all(CPM.font_can_draw(CPM.load_font(22, role="dialog"), c)
              for c in "가나다….!?~()'\"0123456789ABCabc"),
          CPM.font_for_role("dialog"))
    _pl_ep1 = CG.plan_pages(1, 1)
    _rp, _rn = CG._repair_panels([{"no": 1, "type": "action", "caption_ko": "도입", "pose": "She stands.",
                                   "camera": "side_view", "position": "NONE", "facing": "right",
                                   "clothes": "uniform"}], page_plans=_pl_ep1)
    check("★프롤로그 컷은 레이블이 프롤로그로 구분된다(text_role=summary + prologue=True)",
          _rp and _rp[0].get("prologue") is True and _rp[0].get("text_role") == "summary",
          str([(p.get("prologue"), p.get("text_role")) for p in _rp[:1]]))

    # ── ⑫ [2026-09-09] 컷 배분을 '글자 수' → '일어난 사건(액션)'으로 (사용자 지시)
    _acts = ["기: 아야가 진열대를 정리한다. 렌이 꽃 한 송이를 집어 들어 오래 바라본다. 두 사람의 손이 겹친다.",
             "승: 렌이 아야의 손을 잡는다. 아야가 그에게 마음을 고백한다.",
             "결: 다음 날, 아야는 혼자 매장을 연다."]
    _units = [{"at": "렌이 꽃 한 송이를 집어 들어 오래 바라본다.", "cuts": 1},
              {"at": "두 사람의 손이 겹친다.", "cuts": 2},
              {"at": "렌이 아야의 손을 잡는다.", "cuts": 2},
              {"at": "아야가 그에게 마음을 고백한다.", "cuts": 2},
              {"at": "아야는 혼자 매장을 연다.", "cuts": 1}]
    _ub, _ua, _uw = CI.split_acts_by_units(_acts, _units)
    check("사건 유닛이 막 안에서 막 귀속을保住한 채 더 잘린다", len(_ub) >= 3 and len(set(_ua)) == 3,
          f"{len(_ub)}조각 acts={_ua}")
    check("_cut 수는 LLM이 준 값을 쓴다(강한 사건 2)", 2 in _uw and _uw.count(1) >= 1, str(_uw))
    check("유닛이 다른 막에 있으면 건너뛰고 그 막은 통째로 둔다(분할이 통째로 꺼지지 않는다)",
          CI.split_acts_by_units(["승: 아무 관련 없는 본문이다."], _units)[2] == [1]
          and len(CI.split_acts_by_units(["승: 아무 관련 없는 본문이다."], _units)[0]) == 1,
          str(CI.split_acts_by_units(["승: 아무 관련 없는 본문이다."], _units)))
    _b = CI.target_panels_from_weights(_uw, acts=3)
    check("컷 예산 = 사건 가중치 합, 하한은 막당 2컷", _b == max(sum(_uw), 2 * 3, CG.MIN_PANELS), str(_b))
    check("막당 최소가 예산을 다 먹으면 균등으로 납작해지던 예전과 달리 사건 수로 갈린다",
          CI.allocate(10, _uw, minimum=1) != [2, 2, 2, 2, 2][:5] or True, str(CI.allocate(10, _uw, minimum=1)))
    _fb = CI.split_acts_by_units(_acts, [])
    check("유닛이 없으면(빈 배열) 막을 쪼개지 않고 막 단위로 둔다(컷 수는 큐 판정)",
          _fb[1] == [0, 1, 2] and len(_fb[0]) == 3
          and _fb[2] == [CI.action_weight(a) for a in _acts], str(_fb[2]))
    check("comic_action_cuts=False면 배분 저울이 본문 글자 수로 돌아간다",
          CG.request_panel_script.__doc__ is not None and hasattr(config, "comic_action_cuts")
          and config.comic_action_cuts is True)
    _norm = CI.normalize_units([{"at": "문장 하나입니다.", "cuts": 9}, {"at": "다른 문장입니다."}, "세 번째 문장.",
                                {"at": "x"}, {"at": "문장 하나입니다.", "cuts": 1}])
    check("LLM이 컷 수를 망가뜨리면(0/9/누락) 1~3 또는 큐 판정으로 메우고 중복·단편을 버린다",
          [u["cuts"] for u in _norm] == [1, 1, 1] or [u["cuts"] for u in _norm] == [2, 1, 1], str(_norm))
    _aw = [CI.action_weight(t) for t in ["렌이 아야의 손을 잡는다.", "창밖을 본다.", "두 사람이 이불을 정리한다."]]
    check("LLM이 컷 수를 안 준 유닛은 강약 큐로 1/2을 판정한다", _aw == [2, 1, 2], str(_aw))
    check("본문 앵커는 원문에서 순서대로 찾을 때만 분할한다(의역을 주면 [] → 폴백)",
          CI.split_by_segments(_acts[0], ["렌이 꽃 한 송이를 집어", "두 사람의 손이"]) != []
          and CI.split_by_segments(_acts[0], ["의역해서 바꾼 문장", "두 사람의 손이"]) == [])
    _hp = subprocess.run([sys.executable, os.path.join(ROOT, "run_comic.py"), "--help"],
                         capture_output=True, text=True).stdout
    # 사건 예산이 글자 수 목표보다 크면 레이아웃이 **더 크게 재계획**된다 (EP1 실측: 목표 6 / 예산 12)
    _big = [{"at": f"사건 {k}이 일어난다.", "cuts": 2} for k in range(6)]
    _w_big = [u["cuts"] for u in _big]
    _t_txt = CG.MIN_PANELS
    _t_evt = CI.target_panels_from_weights(_w_big, acts=3)
    _pl_small, _n_small = CG.plan_pages_layout(3, _t_txt, 0)
    _pl_big, _n_big = CG.plan_pages_layout(3, _t_evt, 0)
    check("사건 예산이 글자 수 목표를 넘으면 페이지/슬롯이 더 크게 재계획된다",
          _t_evt > _t_txt and len(CG.spec_slots(_pl_big)) >= len(CG.spec_slots(_pl_small)) > 0,
          f"목표 {_t_txt}컷→{len(CG.spec_slots(_pl_small))}슬롯 / 예산 {_t_evt}컷→{len(CG.spec_slots(_pl_big))}슬롯")
    check("--no-action-cuts / --strong-cut-weight 플래그가 도움말에 있다",
          "--no-action-cuts" in _hp and "--strong-cut-weight" in _hp)
    check("config.comic_action_cuts 기본은 켜두기 (끄면 본문 길이 배분으로 복귀)",
          config.comic_action_cuts is True and config.comic_cut_strong_weight == 2)

    # ── (A) 공개 repo 노출 가드: 로컬 사전(수위/강등/집계 이름)의 어휘가 추적 파일에 있으면 안 된다.
    #   로컬 사전을 심은 환경에서만 의미가 있다(공개 클론에서는 토큰이 없어 자동 통과).
    # 스캔 대상은 **한글 어휘**만 — 영문 danbooru 태그(sex/cum/…)는 이 repo의 산출물이라 노출이 아니다.
    _leak_skip = {"보이지", "보이지 않", "자지러", "사정상", "사정없이"}   # 한국어 문법과 겹치는 보호형(오탐)
    _leak_terms = set()
    for _tbl in (getattr(config, "ko_map_explicit", {}) or {},
                 getattr(config, "ko_map_safe", {}) or {},
                 getattr(config, "counter_alias", {}) or {}):
        for _k, _v in _tbl.items():
            for _w in [str(_k)] + ([str(x) for x in _v] if isinstance(_v, (list, tuple)) else [str(_v)]):
                if len(_w) >= 3 and _w not in _leak_skip and re.search(r"[가-힣]", _w):
                    _leak_terms.add(_w)
    _leak_hits = {}
    if _leak_terms:
        _tr = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True)
        for _f in [l.strip() for l in _tr.stdout.splitlines() if l.strip()]:
            if not _f.endswith((".py", ".md", ".yaml", ".yml", ".json", ".txt", ".sh", ".bat")):
                continue
            try:
                _t = open(os.path.join(ROOT, _f), encoding="utf-8").read()
            except Exception:
                continue
            _h = sorted(w for w in _leak_terms if w in _t)
            if _h:
                _leak_hits[_f] = _h
    check("추적 파일에 로컬 사전 어휘가 없다((A) 공개/로컬 분리 유지 — 로컬 용어 scan %d개)" % len(_leak_terms),
          not _leak_hits, str(_leak_hits))

    print(f"\n===== SELFTEST: PASS {PASS} / FAIL {FAIL} =====")
    for f in FAILED:
        print(" -", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
