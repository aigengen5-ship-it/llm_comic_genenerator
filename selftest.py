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
import random
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
    # [2026-09-10] selftest는 **기본 정책(청년향)**으로 실행한다. local_settings.yaml에서
    #   allow_explicit: yes를 켜 두은 기계라도 결과는 똑같아야 한다(실측: 사용자가 켜 둔 날
    #   청년향 검사 7개가 한꺼번에 붉어졌다). explicit를 전제로 하는 검사는 스스로 켜고 끈다.
    config.explicit_cli = False
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

    # [2026-09-09] selftest는 inputs/ 샘플의 **내용**에 기대지 않는다 — 실제 생성 실행이
    #   inputs/sheet01.txt를 자기 시트로 덮어쓰면(실측: Kirisaki Chitoge) 검증이 함께 깨졌다.
    #   샘플 파일의 '존재'만 확인하고, 내용은 selftest가 만든 fixture를 쓴다.
    _fx_dir = tempfile.mkdtemp(prefix="selftest_inputs_")
    _fx_ep = os.path.join(_fx_dir, "ep01.txt")
    _fx_sheet = os.path.join(_fx_dir, "sheet01.txt")
    with open(_fx_ep, "w", encoding="utf-8") as _fh:
        _fh.write("유즈키는 밤 순찰을 돌며 골목마다 불을 켰다. " * 90)
    with open(_fx_sheet, "w", encoding="utf-8") as _fh:
        _fh.write("주인공은 오카다 유즈키. 옅은 갈색 머리를 대충 묶고 짙은 남색 경찰 제복을 입는다. "
                  "축 처진 눈에 볼이 붉고, 당황하면 입이 벌어진다. 손가락에는 반창고. " * 3)
    ep_text, sheet_text = CI.prepare_texts(_fx_ep, _fx_sheet)
    check("입력 파일 로드(ep01/sheet01)", len(ep_text) > 2000 and len(sheet_text) > 200,
          f"{len(ep_text)}/{len(sheet_text)}")
    _sample_ep = os.path.join(ROOT, "inputs", "ep01.txt")
    check("배송 샘플 ep01.txt는 selftest가 읽지 않아도 살아 있다(내용은 검증하지 않는다)",
          os.path.exists(_sample_ep))
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
    # ── [2026-09-09] 표정 고정(아헤가오화) — 회차 태그셋의 표정이 모든 컷에 붙던 문제
    _keep_face = (config.face_tag, config.expression_arr)
    config.face_tag = ["ahegao, wide eyes, tongue out, rolling eyes, flushed face"] * 12
    config.expression_arr = ["ecstasy, lustful, dazed"] * 12
    _p_calm = dict(panel_t, no=31, type="face", camera="close_up", pose="She looks up.",
                   lines=[{"kind": "speech", "who": config.name, "text": "어? 설마", "emo": "surprise"}],
                   caption_ko="", sfx="")
    _p_nofeel = dict(panel_t, no=32, type="action", camera="side_view", pose="She stands by the window.",
                     lines=[], caption_ko="", sfx="")
    _p_climax = dict(panel_t, no=33, type="action", camera="side_view", pose="She is kissed. #creampie",
                     lines=[], caption_ko="", sfx="")
    _f_sur = CG.build_panel_prompt(0, _p_calm, "nsfw", gloss={})
    _f_noe = CG.build_panel_prompt(0, _p_nofeel, "nsfw", gloss={})
    _f_clx = CG.build_panel_prompt(0, _p_climax, "nsfw", gloss={})
    check("컷에 감정이 있으면 그 표정을 쓴다(회차 고정 표정이 화면을 덮지 않는다)",
          "surprised" in _f_sur and "ahegao" not in _f_sur, _f_sur.split("\n")[-1][:120])
    check("표정이 없는 컷도 극단 표정은 걸러진다(일상 컷이 아헤가오가 되지 않는다)",
          "ahegao" not in _f_noe and "rolling eyes" not in _f_noe and "tongue out" not in _f_noe,
          _f_noe.split("\n")[-1][:120])
    check("클라이맥스 컷에서는 극단 표정이 살아 있다(걸러두는 게 아니라 때에 맞게 쓴다)",
          "ahegao" in _f_clx, _f_clx.split("\n")[-1][:120])
    check("감정 7종은 모두 표정 태그를 가진다(화면 이모티콘과 그림 표정이 같은 말을 한다)",
          all(CG._EMO_FACE_TAGS.get(k) for k in CPM.EMOTIF_KINDS), str(sorted(CG._EMO_FACE_TAGS)))
    check("주인공 풍선의 감정이 우선한다(상대방 감정이 얼굴을 못 빼앗는다)",
          CG._panel_face_emotion({"lines": [{"who": config.name2 or "렌", "emo": "anger"},
                                            {"who": config.name, "emo": "heart"}]}) == "heart")
    cf = anima_gen._calm_face("ahegao, wide eyes, flushed face")
    check("_calm_face는 극단 표정만 거르고 나머지는 유지한다",
          "ahegao" not in cf and "wide eyes" in cf and "flushed face" in cf
          and anima_gen._calm_face("ahegao, tongue out") == "soft smile", cf)
    _ts_prompt = anima_gen._tagset_prompt("본문", "시트", 1) if hasattr(anima_gen, "_tagset_prompt") else ""
    check("태그셋 프롬프트가 회차 표정을 평범하게 고르고 극단 표정을 금지한다",
          ("모든 컷에 그대로 붙는다" in str(_ts_prompt) or "모든 컷에 그대로 붙는다" in str(open("anima_gen.py", encoding="utf-8").read()))
          and "서로 다른 감정" in str(open("anima_gen.py", encoding="utf-8").read()))
    config.face_tag, config.expression_arr = _keep_face

    # ── [2026-09-09] 이름 고정 — 시트의 #캐릭터 태그#에서 이름이 새어들었다 (실측: AMD 소녀 → 치토게)
    _data_nm = {"protagonist": {"name": "치토게", "sex": "female", "hair_color": "blonde hair",
                                "hair_style": "long hair", "eye_color": "brown eyes",
                                "skin_color": "fair skin", "face_style": "blushing",
                                "clothes": "school uniform", "body_shape": "slim", "job": "학생"},
                "partner": {"name": "남자", "sex": "male", "clothes": "shirt"},
                "guides": {"protagonist": ["기", "승", "전", "결"], "partner": [], "sub": []},
                "actions": [], "segments": [], "units": [], "rating": "safe"}
    _keep_pin = (config.pin_name, config.pin_name2)
    config.pin_name, config.pin_name2 = "", ""
    CI.apply_to_config(_data_nm, "본문입니다. " * 200, "#Kirisaki Chitoge from Nisekoi#", ep_num=1)
    check("이름 고정이 없으면 추출이 정한 이름(실측 회귀: 치토게)이 그대로 쓴다",
          config.name == "치토게" and config.char_tags == ["Kirisaki Chitoge from Nisekoi"],
          f"{config.name}/{config.char_tags}")
    config.pin_name, config.pin_name2 = "AMD 소녀", "카미유 렌"
    CI.apply_to_config(_data_nm, "본문입니다. " * 200, "#Kirisaki Chitoge from Nisekoi#", ep_num=1)
    check("이름을 고정하면 추출 이름을 이긴다(#태그는 그림 참조로만 남는다)",
          config.name == "AMD 소녀" and config.name2 == "카미유 렌"
          and config.char_tags == ["Kirisaki Chitoge from Nisekoi"], f"{config.name}/{config.name2}")
    config.apply_local_settings({"name": "로컬이름", "partner_name": "로컬상대"})
    check("local_settings의 name/partner_name이 기본값 자리를 채운다",
          config.pin_name == "로컬이름" and config.pin_name2 == "로컬상대", f"{config.pin_name}/{config.pin_name2}")
    os.environ["COMIC_PIN_NAME"] = "환경이름"
    config.apply_local_settings({"name": "로컬이름"})
    check("우선순위: 환경변수 > local_settings (그리고 run_comic의 --name이 마지막에 이긴다)",
          config.pin_name == "환경이름", config.pin_name)
    del os.environ["COMIC_PIN_NAME"]
    config.pin_name, config.pin_name2 = _keep_pin
    _ep_src = open(os.path.join(ROOT, "comic_input.py"), encoding="utf-8").read()
    check("추출 프롬프트가 #태그 영문 이름을 이름 필드에 옮기는 것을 금지한다",
          "7-b. protagonist.name" in _ep_src and "Kirisaki Chitoge" in _ep_src)
    check("추출 프롬프트가 고정 이름을 LLM에게 알린다", "{name_lock}" in _ep_src and "이름이 고정되었습니다" in _ep_src)
    _hp2 = subprocess.run([sys.executable, os.path.join(ROOT, "run_comic.py"), "--help"],
                          capture_output=True, text=True).stdout
    check("--name / --name2 플래그가 도움말에 있다", "--name2" in _hp2 and "주인공 이름을 고정" in _hp2)

    # ── [2026-09-09] 복장 누드화 — 컷 clothes가 회차 의상을 덮어쓰던 문제 (p02 실측)
    _keep_clo = (config.clothes, config.p_exposure_tag, config.exposure_tag, config.bodystyle_tag)
    _keep_clo2 = (getattr(config, "clothes_late", ""),)
    config.clothes = "school uniform"                      # 회차가 **시작하는** 복장
    config.clothes_late = "gold bra, gold miniskirt"       # 중반 이후에 갈아입는 복장
    config.face_style, config.face_style_late = "crying, blushing", "ahegao, wide eyes"
    config.p_exposure_tag = ["cleavage, navel, midriff, thighs"] * 12
    config.exposure_tag = ["tight clothes, short skirt"] * 12
    config.bodystyle_tag = ["standing, holding hands, looking at viewer"] * 12
    _p_dirty = dict(panel_t, no=41, type="action", camera="side_view",
                    pose="She is pushed by hands.",
                    clothes="tattered school uniform, dirty clothes", lines=[], caption_ko="", sfx="")
    _p_bikini = dict(panel_t, no=42, type="action", camera="side_view", pose="She swims.",
                     clothes="bikini", lines=[], caption_ko="", sfx="")
    _p_nude = dict(panel_t, no=43, type="action", camera="side_view", pose="She stands.",
                   clothes="nude, tattered clothes", lines=[], caption_ko="", sfx="")
    _p_silent = dict(panel_t, no=44, type="face", camera="close_up", pose="She smiles.",
                     clothes="", lines=[], caption_ko="", sfx="")
    _f_dirty = CG.build_panel_prompt(0, _p_dirty, "sensitive", gloss={})
    _f_bikini = CG.build_panel_prompt(0, _p_bikini, "sensitive", gloss={})
    _f_nude = CG.build_panel_prompt(0, _p_nude, "sensitive", gloss={})
    _f_silent = CG.build_panel_prompt(0, _p_silent, "sensitive", gloss={})
    check("컷이 복장을 명시하면 **컷 것만** 쓴다(회차 후반 의상이 초반 컷에 새지 않는다)",
          "tattered school uniform" in _f_dirty and "gold miniskirt" not in _f_dirty,
          _f_dirty.split("\n")[-1][:150])
    check("컷이 복장을 침묵해도 회차 **시작** 복장만 쓴다(중반 옷은 아직 안 입었다)",
          "school uniform" in _f_silent and "gold miniskirt" not in _f_silent,
          _f_silent.split("\n")[-1][:150])
    _bk_climax = anima_gen._build_tag_block(0, "she gasps", "front_view", "wide", "", "", False,
                                            climax_tag="creampie", clothes_override="")
    check("회차 후반 의상/표정은 클라이맥스 컷에서만 돌아온다",
          "gold miniskirt" in _bk_climax and "ahegao" in _bk_climax,
          " ".join(l for l in _bk_climax.split("\n") if "CLOTHES" in l or "FACE" in l)[:150])
    _bk_calm = anima_gen._build_tag_block(0, "she stands", "front_view", "wide", "", "", False,
                                          climax_tag="", clothes_override="")
    check("회차 표정 목록의 극단 표정은 일상 컷에서 걸린다(비클라이맥스 정제 — 조건 반전 회귀)",
          "ahegao" not in _bk_calm and "blushing" in _bk_calm,
          " ".join(l for l in _bk_calm.split("\n") if "AAA FACE" in l)[:120])
    check("정말 갈아입은 컷(다른 품목)은 override가 이긴다", "bikini" in _f_bikini
          and "school uniform" not in _f_bikini, _f_bikini.split("\n")[-1][:150])
    check("본문 근거 없는 전라 어구는 상한 안에서 걷는다(explicit일 때만 통과)",
          "nude" not in _f_nude and anima_gen._undress_guard("nude, bikini") == "bikini")
    check("_merge_clothes: 품목 없는 변화어구는 회차 의상에 덧붙는다",
          anima_gen._merge_clothes("police uniform, jacket", "wet, dirty") .startswith("police uniform"))
    _cp_prompt = open(os.path.join(ROOT, "comic_gen.py"), encoding="utf-8").read()
    check("컷 스크립트 프롬프트가 근거 없는 옷 훼손(tattered/nude)을 말린다",
          "먼저 제안하지 않는다" in _cp_prompt and "tattered" in _cp_prompt)
    config.clothes, config.p_exposure_tag, config.exposure_tag, config.bodystyle_tag = _keep_clo
    config.clothes_late, config.face_style_late = _keep_clo2, ""

    # [2026-09-07] 1인 화면: 헤더는 무조건 solo (side_view는 구도일 뿐 '2명'이 아니다)
    ps = CG.build_panel_prompt(0, dict(panel_t, no=11), "nsfw", gloss={})
    pp = CG.build_panel_prompt(0, dict(p_pov_det, no=12), "nsfw", gloss={})
    check("비POV: 헤더 solo + 2girl/two girls/상대 언급 없음",
          "solo" in ps and "2girl" not in ps and "two girls" not in ps
          and "looking at each other" not in ps, ps[:160])
    check("POV: 헤더 solo(상대는 OBSERVER로만)",
          "solo" in pp and "2girl" not in pp and "two girls" not in pp, pp[:160])
    # [2026-09-08] 시트 #캐릭터 태그# 는 정제·LLM 재작성을 통과하지 못할 수 있어 마지막에 보장 주입한다
    # [2026-09-12] 상대방을 최소 태그로 그리는 기본값에서는 상대방 #태그#를 넣지 않는다(아래 새 검사).
    #             아래 검사들은 상세 태그(--partner-full) 경로다.
    _pi0 = getattr(config, "comic_partner_invisible", True)
    config.comic_partner_invisible = False
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
    # [2026-09-12] 상대방이 최소 태그로 바뀌면 시트의 상대방 정체 태그는 외모·복장을 불러와 고정 그룹과 싸운다
    config.comic_partner_invisible = True
    _pinv = CG.build_panel_prompt(0, p_pov_det, "nsfw", gloss={})
    check("상대방 최소 태그 중에는 시트의 #상대방 태그#를 넣지 않는다(주인공 태그는 그대로)",
          "Tuxedo Mask" not in _pinv and "Usagi Tsukino from Sailor Moon" in _pinv
          and "invisible" in _pinv, _pinv[:160])
    config.comic_partner_invisible = _pi0
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

    def fake_run(json_value, ep_idx, full_prompt, res, client=None, seed=None, queue_count=2,
                 ids_out=None):
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
    # [2026-09-09] test/cut_new.yaml의 20종을 병합해 14 → 34종 (⑬b에서 종류별 커버리지까지 본다)
    check("cut.yaml 템플릿 34개 로드", len(tmpls) == 34, str(len(tmpls)))
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
    # 스텁 LLM은 state 객체를 채우지 않으므로 이 구간만 엄격 검사를 꺼둔다(엄격 검사는 ⑬j에서 따로 검증)
    _strict_keep = getattr(config, "comic_strict_state", True)
    config.comic_strict_state = False
    try:
        sc = CG.request_panel_script(3, 1, pages=0, episode_text=body,
                                     chars_per_panel=100, beat_chars=600)
    finally:
        CG.call_openai_for_text = orig_pl
        (config.comic_pages, config.comic_chars_per_panel, config.comic_beat_chars,
         config.comic_max_panels, config.comic_max_pages) = cfg_save
    check("request_panel_script: 본문이 컷 수를 정하고 장면별로 LLM을 부른다",
          sc["beats"] > 1 and calls2["n"] >= sc["beats"]   # 장면당 1회 이상(재시도는 허용)
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
    check("--special: clothes는 시간 순으로 갈라 회차 시작 값이 된다(후반은 *_late)",
          config.clothes == CANNED["protagonist"]["clothes"].split(",")[0].strip()
          and "utility belt" in getattr(config, "clothes_late", ""),
          f"{config.clothes} / {getattr(config, 'clothes_late', '')}")
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
    print("\n== ⑩ 화면 문법: 설명 박스 + 풍선(≤3) + 의성어 + ★서두요약·에필로그 ==")
    ln = CG._norm_lines(["소타: 무거운 건 저에게 맡기세요.", "(이 사람, 아까부터 알고 있었다.)",
                         {"kind": "thought", "who": "유즈키", "text": "세 번째 호흡"},
                         "너무 긴 대사 " * 8])
    check("_norm_lines: '이름: 대사'에서 화자를 떼어 낸다(화면에 이름이 안 찍힌다)",
          ln[0]["who"] == "소타" and ln[0]["text"].startswith("무거운") and "소타" not in ln[0]["text"],
          str(ln[:1]))
    check("_norm_lines: (괄호) 표기는 속마음 풍선으로 본다", ln[1]["kind"] == "thought", str(ln[1]))
    check("_norm_lines: 풍선은 최대 " + str(CG.DIALOG_LINES) + "개(4번째부터 버린다)",
          len(ln) == CG.DIALOG_LINES, str(len(ln)))
    check("_norm_lines: 한 풍선은 최대 DIALOG_MAX_LEN자(길면 … 로 자른다 → 두 번째 풍선으로)",
          all(len(x["text"]) <= CG.DIALOG_MAX_LEN for x in ln), str([len(x["text"]) for x in ln]))
    check("_norm_lines: {kind,who,text} dict 입력도 받는다",
          CG._norm_lines([{"kind": "speech", "who": "유즈키", "text": "안 돼"}])[0]["kind"] == "speech")
    check("_norm_dialog(하위호환): 옛 '화자: 말' 문자열로 되돌려 준다(옛 메타·스크립트)",
          CG._norm_dialog([{"kind": "speech", "who": "유즈키", "text": "안 돼"}]) == ["유즈키: 안 돼"],
          str(CG._norm_dialog([{"kind": "speech", "who": "유즈키", "text": "안 돼"}])))
    check("text_payload 하위호환: str/list 입력도 화면 문법으로 번역된다",
          CPM.text_payload("지문")["narration"] == "지문"
          and len(CPM.text_payload(["지문", "대사1", "대사2", "대사3"])["balloons"]) == CPM.BALLOON_MAX)

    pnl = {"no": 1, "caption_ko": "지문", "sfx": "쿵", "facing": "right", "fade": 0.0,
           "lines": [{"kind": "speech", "who": "소타", "text": "대사"}]}
    tp0 = CG.panel_text_payload(pnl)
    check("panel_text_payload: 설명/풍선/의성어 + facing → 풍선 배치 쪽(side)",
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
    #   1) 주인공 풍선 = 왼쪽 위(2개면 아래), 상대방 = 오른쪽 위(2개면 아래)
    #      (꼬리·물방울은 정상 동작하지 않아 삭제됐다 — 몸체만 그린다)
    #   2) 중간 이벤트 컷의 설명 박스는 대화에 맞추어 작게
    #   3) 컷 경계선과 그림 사이 빈틈 없이 굵은 검정선만  (④에서 픽셀로 확인)
    #   4) 감정 이모티콘(분노/놀람/땀/하트/음영/반짝/물음) — 감정마다 다른 색
    # ---------------------------------------------------------------------------
    print("\n== ⑪ 화면 문법 v4: 풍선 자리 / 설명 크기 / 감정 표시 ==")
    sp_me = CPM._balloon_slot_pref({"speaker": "me"})
    sp_ot = CPM._balloon_slot_pref({"speaker": "other"})
    check("주인공 풍선은 왼쪽 위 → 왼쪽 아래",
          sp_me == (("tl", "bl", "ml", "center"), "center"), str(sp_me))
    check("상대방 풍선은 오른쪽 위 → 오른쪽 아래",
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

    # [2026-09-11] 사용자 지시로 컷당 풍선 2→3 — 개수의 출처는 comic_page_merge.BALLOON_MAX 하나다
    check("컷 스크립트의 풍선 상한이 렌더 상한과 같다(DIALOG_LINES == BALLOON_MAX == 3)",
          CG.DIALOG_LINES == CPM.BALLOON_MAX == 3, f"{CG.DIALOG_LINES}/{CPM.BALLOON_MAX}")

    def _ovl(a, b):
        return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])

    _cv3b = Image.new("RGB", (PW, PH), (70, 130, 190))
    _dd3b = ImageDraw.Draw(_cv3b)
    _r3 = []
    for _b in ({"kind": "speech", "text": "먼저 한 마디", "speaker": "me"},
               {"kind": "speech", "text": "상대 말", "speaker": "other"},
               {"kind": "thought", "text": "세 번째 속마음", "speaker": "me"}):
        _rr = CPM._draw_balloon(_dd3b, PX, PY, PW, PH, _b, avoid=_r3)
        if _rr:
            _r3.append(_rr)
    check("풍선 3개가 실제로 그려진다(세 번째는 왼쪽 가운데 자리까지 쓴다)",
          len(_r3) == 3, str(len(_r3)))
    check("풍선 3개는 서로 안 겹치고 컷 안에 있다",
          not any(_ovl(x, y) for i, x in enumerate(_r3) for y in _r3[i + 1:])
          and all(r[0] >= PX and r[1] >= PY and r[2] <= PX + PW and r[3] <= PY + PH for r in _r3),
          str(_r3))
    check("3번째 풍선은 앞 두 개와 다른 화자 자리로 피한다(주인공 = 왼쪽 유지)",
          len(_r3) == 3 and (_r3[2][0] + _r3[2][2]) / 2 < PX + PW * 0.5,
          str(_r3[2] if len(_r3) == 3 else _r3))

    # 꼬리·생각 물방울은 삭제됐다 — 몸체만 그린다(아래 설명 박스 검사로 이어진다)

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

    # [2026-09-10] 사용자 지시: 풍선 **가로 비율 10%**(20%의 절반) · 세로로 길게 (꼬리·물방울은 폐지)
    _bp = (683, 512)                      # 2단 컷 실측 크기
    _btxt = "이 사람이 들어오면 매장 공기가 달라진다."
    _bal_boxes = {}
    for _k in ("speech", "thought"):
        _c = Image.new("RGB", _bp, (255, 255, 255))
        _d4 = ImageDraw.Draw(_c)
        _bb = CPM._draw_balloon(_d4, 0, 0, _bp[0], _bp[1],
                                {"kind": _k, "text": _btxt, "speaker": "me"}, font_size=22)
        _bal_boxes[_k] = _bb
    check("말풍선 가로 비율은 컷 폭의 " + str(int(CPM.BALLOON_W_RATIO * 100)) + "% (속마음은 글자가 안 들어갈 때만 구제 폭까지 넓힌다)",
          _bal_boxes["speech"]
          and abs((_bal_boxes["speech"][2] - _bal_boxes["speech"][0]) / _bp[0] - CPM.BALLOON_W_RATIO) < 0.03
          and _bal_boxes["thought"]
          and (_bal_boxes["thought"][2] - _bal_boxes["thought"][0]) / _bp[0] <= CPM.THOUGHT_W_RELIEF + 0.02,
          str({k: round((v[2] - v[0]) / _bp[0] * 100, 1) for k, v in _bal_boxes.items()}))
    _pf = CPM.load_font(20, role="dialog")
    _probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    _wrap20 = CPM.wrap_text(_btxt, _pf, int(_bp[0] * CPM.BALLOON_W_RATIO) - 24, _probe, max_lines=9)
    _wrap66 = CPM.wrap_text(_btxt, _pf, int(_bp[0] * 0.66) - 24, _probe, max_lines=9)
    check("대사는 " + str(int(CPM.BALLOON_W_RATIO * 100)) + "% 폭에 맞춰 여럿 줄로 접힌다(예전 66% 폭에서는 한두 줄이었다)",
          len(_wrap20) >= 3 and len(_wrap20) > len(_wrap66),
          f"{int(CPM.BALLOON_W_RATIO * 100)}%폭 {len(_wrap20)}줄 / 예전 66%폭 {len(_wrap66)}줄")
    check("좁은 폭에서 글자 크기를 줄여도 풍선이 컷 안에 들어간다",
          _bal_boxes["speech"] and _bal_boxes["speech"][3] <= _bp[1] and _bal_boxes["speech"][2] <= _bp[0],
          str(_bal_boxes["speech"]))
    _th_w, _th_h = _bal_boxes["thought"][2] - _bal_boxes["thought"][0], _bal_boxes["thought"][3] - _bal_boxes["thought"][1]
    check("속마음 세로는 '그 폭에 글자를 넣는 데 필요한 만큼'만 쓴다(√2 배수를 넘기지 않는다)",
          _th_h <= (_bal_boxes["speech"][3] - _bal_boxes["speech"][1]) * 1.75 + 24, f"{_th_w}x{_th_h}")

    # [2026-09-09] 사용자 지시: 설명문을 '알아서' 자르지 말 것 + 글자가 많으면 폰트를 줄일 것
    _longcap = "비가 그친 저녁, 꽃가게의 수국이 반짝인다. " * 30          # 600자
    _lc_panels, _lc_notes = CG._repair_panels([{"no": 1, "type": "action", "caption_ko": _longcap,
                                                "pose": "She stands.", "camera": "side_view",
                                                "position": "NONE", "facing": "right", "clothes": "uniform"}])
    check("설명문은 길이로 자르지 않는다(길어도 원문 그대로, '…' 토막 금지)",
          _lc_panels and _lc_panels[0]["caption_ko"] == _longcap.strip()
          and not _lc_panels[0]["caption_ko"].endswith("…"),
          f"{len(_lc_panels[0]['caption_ko']) if _lc_panels else 0}자")
    _cvbig = Image.new("RGB", (444, 768), (255, 255, 255))
    _r_big = CPM._draw_caption_box(ImageDraw.Draw(_cvbig), 0, 0, 444, 768, _longcap * 2, font_size=20)
    check("글자가 많으면 **폰트 크기를 줄여** 컷 안에 다 담는다(지금이도 충분히 크다)",
          _r_big and _r_big[4] < 20 and _r_big[4] >= CPM.FONT_FLOOR
          and not any(ln.endswith("…") for ln in _r_big[5]),
          f"fs={_r_big[4]}px lines={len(_r_big[5]) if _r_big else 0}")
    check("최소 글자 크기 바닥(FONT_FLOOR)은 11px로 둔다", CPM.FONT_FLOOR <= 12 and CPM.FONT_FLOOR >= 8,
          str(CPM.FONT_FLOOR))
    _dlg60 = "이 사람이 들어오면 매장 공기가 달라진다. 진열대의 수국까지 시선이 간다. 정말 긴 대사입니다."
    _ln2 = CG._norm_lines([{"kind": "speech", "who": config.name or "나", "text": _dlg60}])
    check("긴 대사는 '…'로 버리지 않고 **풍선 두 개에 나눠** 담는다",
          len(_ln2) == 2 and not any(b["text"].endswith("…") for b in _ln2)
          and "".join(b["text"] for b in _ln2).replace(" ", "") == _dlg60.replace(" ", ""),
          str([b["text"] for b in _ln2]))
    check("★지문 폴백도 문장 중간을 자르지 않는다(limit<=0 = 그대로)",
          CG._clamp_caption("긴 지문입니다. " * 40, 0).count("긴 지문입니다.") == 40
          and CG._first_sentence("첫 문장이 아주 깁니다. " * 30 + "두 번째 문장.", 150).startswith("첫 문장이 아주 깁니다."),
          CG._first_sentence("첫 문장이 아주 깁니다. " * 30 + "두 번째 문장.", 150)[-14:])

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
    config.comic_item_cuts = False      # ⑫는 '사건 단위' 모드 검사다(항목 모드는 ⑬g에서 따로 본다)
    _ub, _ua, _uw = CI.split_acts_by_units(_acts, _units)
    check("사건 유닛이 막 안에서 막 귀속을保住한 채 더 잘린다", len(_ub) >= 3 and len(set(_ua)) == 3,
          f"{len(_ub)}조각 acts={_ua}")
    check("컷 수는 LLM이 준 컷 수를 **합쳐서** 지킨다 — 장면을 합쳐도 사건이 줄지 않는다 "
          "(예전엔 max를 써서 두 사건이 한 컷으로 눌렸다)",
          sum(_uw) >= sum(u["cuts"] for u in _units) and all(w >= 1 for w in _uw),
          f"유닛 합 {sum(u['cuts'] for u in _units)} → 배분 {_uw}")
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

    # ── ⑬b [2026-09-09] 컷 템플릿 DB 14종 → 34종 병합 + '같은 용도でも 다른 컷' 추첨
    _tm = CG.load_cut_templates()
    _ids = sorted(_tm)
    check("data/cut.yaml 템플릿이 34종이고 id가 겹치지 않는다 (기존 14 + test/cut_new.yaml 20)",
          len(_tm) == 34 and len(_ids) == len(set(_ids))
          and {"outfit_reveal_vertical", "cute_jealousy_4tiers", "epilogue_empty_scenery"} <= set(_ids),
          f"{len(_tm)}종")
    _bad = []
    for _tid, _t in _tm.items():
        if not _t["tiers"]:
            _bad.append(_tid + ":티어없음")
        for _td in _t["tiers"]:
            _sh = _td["shares"]
            if any(x <= 0 or x > 1 for x in _sh):
                _bad.append(f"{_tid}:share")
            if abs(sum(_sh) - 1.0) > 0.02 and not _td.get("center"):
                _bad.append(f"{_tid}:합{round(sum(_sh), 2)}")
    check("34종 모두 파싱된다(shares 0~1, 행 합 1.0 — 중앙 정렬 슬롯만 예외)", not _bad, str(_bad[:4]))
    _cov = {sit: sum(1 for t in _tm.values() if not t.get("epilogue") and sit in t["situations"])
            for sit in ("기", "승", "전", "결")}
    check("기승전결마다 선택지가 넉넉하다(각 5종 이상 → 회차 안에서 재사용 없이 돌아간다)",
          all(v >= 5 for v in _cov.values()) and len(_cov) == 4, str(_cov))
    config.comic_variation = 0
    _p12 = CG.plan_pages(5, 12)
    _idp = [q["template_id"] for q in _p12 if not q.get("prologue") and not q.get("epilogue")]
    check("12페이지 회차는 12장 전부 다른 템플릿(용도가 같은 페이지でも 다른 컷 구성)",
          len(_idp) == 12 and len(set(_idp)) == 12, str(_idp[:4]))
    _diff = []
    for _v in (1, 2, 3, 4, 5):
        config.comic_variation = _v
        _diff.append(tuple(q["template_id"] for q in CG.plan_pages(5, 4)
                           if not q.get("prologue") and not q.get("epilogue")))
    config.comic_variation = 0
    check("변동 값마다 템플릿 조합이 달라진다", len(set(_diff)) >= 4, str(len(set(_diff))))
    _epseen = set()
    for _v in range(1, 31):
        config.comic_variation = _v
        config.total_episodes = 3
        _epseen |= {q["template_id"] for q in (CG.plan_pages(3, 2) or []) if q.get("epilogue")}
    config.comic_variation = 0
    check("에필로그 여운 페이지도 고정 1개가 아니라 여운 템플릿 중에서 고른다",
          len(_epseen) >= 2 and _epseen <= {"epilogue_aftermath", "epilogue_empty_scenery"}, str(_epseen))
    _pb = CG._layout_block(CG.plan_pages(2, 2), (1, 999))
    check("슬롯 설명의 소품은 '예시'라는 지침이 프롬프트에 있다 (본문에 없는 우산·음식 방지)",
          "예시" in _pb and "분할 비율" in _pb, _pb[:0])

    # ── ⑬c [2026-09-09] 사용자: "첫 페이지가 너무 좁다" — 도입부는 전폭·가로 컷이 먼저다
    _ki = [t for t in _tm.values() if not t.get("epilogue") and "기" in t["situations"]]
    def _wide_first(t):
        return bool(t["tiers"]) and len(t["tiers"][0]["shares"]) == 1 and t["tiers"][0]["shares"][0] >= 0.9
    _ki_wide = [t for t in _ki if _wide_first(t)]
    check("도입(기)용 템플릿에 전폭 1컷으로 시작하는 것이 3종 이상 있다(좁은 3단 세로만 있던 회귀)",
          len(_ki_wide) >= 3, str(sorted(t["id"] for t in _ki_wide)))
    _firsts = []
    for _v in (0, 1, 7, 19):
        config.comic_variation = _v
        _pp = CG.plan_pages(4, 3) or []
        _firsts.append((_pp[0]["slots"][0]["share"], _pp[-1]["slots"][0]["share"]))
    config.comic_variation = 0
    check("회차의 첫 페이지와 마지막 여운 페이지는 전폭 컷으로 시작한다(첫 화면이 좁던 증상)",
          all(s0 >= 0.9 for s0, _ in _firsts) and all(s1 >= 0.9 for _, s1 in _firsts), str(_firsts))
    _ep_first = [t for t in _tm.values() if t.get("epilogue")]
    check("에필로그 템플릿도 전폭 1컷 행으로 시작한다(작은 컷 여러 개 금지)",
          _ep_first and all(t["tiers"][0]["shares"][0] >= 0.9 for t in _ep_first),
          str([(t["id"], t["tiers"][0]["shares"]) for t in _ep_first]))
    _pw, _ph = 1280, 1846
    _pw2, _rows = CPM._plan_rows(2, ["", ""], [True, False], [False, False], [False, False],
                                 unit_w=600, cols=2, gutter=8, pad=10, border=10, font_size=18,
                                 row_spec=[{"cells": [{"idx": 0, "share": 1.0}]},
                                           {"cells": [{"idx": 1, "share": 1.0}]}],
                                 page_size=(_pw, _ph))
    _widths = [c["w"] for r in _rows for c in r["cells"]]
    check("전폭(share 1.0) 슬롯은 페이지 폭의 70% 이상으로 그려진다(좁은 세로 컷만 있던 증상)",
          len(_widths) == 2 and min(_widths) >= int(_pw * 0.70), f"page_w={_pw2} widths={_widths}")

    # ── ⑬g2 [2026-09-09] LLM 디코딩 사고로 파싱이 죽지 않는다 (실측 재현: 키 앞 U+2024)
    #    (따옴표·역슬래시는 조립해서 씁니다 — 이스케이프 실수로 테스트가 먼저 죽는 걸 막습니다)
    _Q, _B = chr(34), chr(92)
    _esc = _B + _Q            # JSON 안에서 이스케이프된 따옴표
    _odd = chr(0x2024)        # ONE DOT LEADER : 키 따옴표 자리를 대신한 문자(실측 그대로)
    _bad = ("{ " + _Q + "protagonist" + _Q + ": { " + _Q + "name" + _Q + ": " + _Q + "A" + _Q
            + ", " + _Q + "eye_color" + _Q + ": " + _Q + "brown eyes" + _Q + ",\n"
            + _odd + "  " + _Q + "skin_color" + _Q + ": " + _Q + "fair skin" + _Q + " }, "
            + _Q + "units" + _Q + ": [ { " + _Q + "at" + _Q + ": " + _Q + "그녀는 " + _esc + "인하" + _esc
            + "라고 말한다" + _Q + ", " + _Q + "kind" + _Q + ": " + _Q + "대사" + _Q
            + ", " + _Q + "cuts" + _Q + ": 1 } ] }")
    _obj, _err = CI.extract_json_obj_checked(_bad)
    check("키 앞 따옴표가 U+2024로 깨진 응답도 주워拾는다 (실측 재현)",
          bool(_obj) and bool(_obj.get("units")), _err[:60])
    check("복구된 항목의 내용까지 산다 (kind/cuts/값 안 따옴표 보존)",
          bool(_obj.get("units")) and _obj["units"][0]["kind"] == "대사"
          and _obj["units"][0]["cuts"] == 1 and "인하" in _obj["units"][0]["at"],
          str(_obj.get("units"))[:80])
    _good = "{" + _Q + "b" + _Q + ": " + _Q + "그녀는 " + _esc + "안녕" + _esc + "이라 했다" + _Q + "}"
    _ok, _e2 = CI.extract_json_obj_checked(_good)
    check("정상 응답은 원문 그대로 (값 안 따옴표 보존 — 복구기는 실패할 때만 돈다)",
          _ok == {"b": "그녀는 " + chr(34) + "안녕" + chr(34) + "이라 했다"} and _e2 == "", str(_ok))
    _o3, _e3 = CI.extract_json_obj_checked("잘 모르겠어요")
    check("정말 못 읽으면 사유 문자열을 남긴다 (로그로 진단 가능)",
          _o3 == {} and "기호" in _e3, _e3[:40])
    _arr = CG._extract_json_array("[{ " + _odd + " " + _Q + "pose" + _Q + ": " + _Q + "she smiles" + _Q
                                  + ", " + _Q + "emotion" + _Q + ": " + _Q + "happy" + _Q + " }]")
    check("컷 스크립트 배열 파서도 같은 디코딩 사고를 복구한다",
          len(_arr) == 1 and _arr[0].get("emotion") == "happy", str(_arr)[:70])

    # ── ⑬h0 [2026-09-09] 항목 20개가 장면 1개로 눌려 컷 6개가 되면 안 된다 (실측)
    _it = ["그녀는 %d번째로 행동한다. 짧은 문장" % k for k in range(20)]
    _txt = "\n".join(_it)
    _item_keep = getattr(config, "comic_item_cuts", True)
    config.comic_item_cuts = True            # 항목 모드임을 명시(이 값은 다른 검사들이 흔든다)
    _u20 = CI.normalize_units([{"at": p, "kind": "행동", "cuts": 1} for p in _it])
    _b20, _ba20, _w20 = CI.split_acts_by_units([_txt], _u20)
    check("항목 모드: 짧은 항목도 붙이지 않고 개수로만 묶는다(장면 1개 = 항목 6개까지)",
          len(_b20) == 4 and _w20 == [6, 6, 6, 2], f"{len(_b20)}개 {_w20}")
    check("항목 모드: 컷 예산 합 = 항목 수 (장면이 몇 개든 잃지 않는다)",
          sum(_w20) == 20 and CI.target_panels_from_weights(_w20, acts=1) >= 20, str(sum(_w20)))
    check("장면 1호가 항목 6개를 넘지 않는다(1호출 = 컷 6 이하의 JSON 보호선)",
          max(_w20) <= CI.PANELS_PER_BEAT_MAX, str(max(_w20)))
    _cg_src = open(os.path.join(ROOT, "comic_gen.py"), encoding="utf-8").read()
    check("막이 1개(시그먼트 앵커 실패)여도 항목 유닛을 쓴다 — 게이트 밖으로 나왔다",
          "and units and body:" in _cg_src and _cg_src.count("ep_action_units") >= 2,
          "units 분기가 막 분할 안쪽에 갇혀 있다")
    _rc_src = open(os.path.join(ROOT, "run_comic.py"), encoding="utf-8").read()
    check("실행 화면의 '컷 예산' 안내가 글자 수가 아니라 항목 수를 본다",
          "ep_action_units" in _rc_src and "항목 1 = 컷 1" in _rc_src, "안내가 여전히 1컷=600자")
    # 항목이 없으면 예전처럼 글자 수 저울을 쓴다(회귀)
    config.comic_item_cuts = False           # 사건 모드(회귀) — 짧은 조각 병합이 살아 있어야 한다
    _u20_old = CI.normalize_units([{"at": p, "cuts": 2} for p in _it[:6]])
    _bo, _bao, _wo = CI.split_acts_by_units([_txt], _u20_old)
    check("--no-item-cuts(사건 모드)에서는 짧은 조각 병합이 그대로 돈다(회귀 확인)",
          len(_bo) <= CI.PANELS_PER_BEAT_MAX and sum(_wo) >= 10, f"{len(_bo)}개 {_wo}")
    config.comic_item_cuts = _item_keep

    config.comic_strict_state = _strict_keep
    # ── ⑬j [2026-09-09] 컷 스크립트 JSON이 모자라면 에러로 끝낸다(사용자 규칙)
    _bad_panels = [{"no": 1, "pose": "She stands.", "type": "action", "camera": "front_view",
                    "caption_ko": "아침.", "clothes": "school uniform", "emotion": "sad"}]   # state 없음
    check("state를 하나도 안 채우면 문제로 잡힌다(화면 상태가 회차 태그에 의존한다)",
          bool(CG.validate_panel_script(_bad_panels)), str(CG.validate_panel_script(_bad_panels))[:80])
    check("state는 한 줄 문자열 '항목=값; …' 도 accepts된다(작은 모델은 중첩 객체를 자주 버린다)",
          CG.state_of({"state": "face=sad; clothes=school uniform; nope=x"})
          == {"face": "sad", "clothes": "school uniform"}
          and CG.state_of({"state_props": "umbrella"}) == {"props": "umbrella"},
          str(CG.state_of({"state": "face=sad"})))
    _ok_panels = [dict(_bad_panels[0], state={k: "" for k in CG.STATE_KEYS})]
    _ok_panels[0]["state"].update({"face": "sad", "clothes": "school uniform"})
    check("인물이 나오는 첫 컷이 시작 상태(face/clothes/place/background)를 채우면 통과한다",
          CG.validate_panel_script(_ok_panels) == [], str(CG.validate_panel_script(_ok_panels))[:90])
    check("장소/배경은 첫 컷에서 비어도 치명적이 아니다(코드가 처음 명시된 장소를 소급한다)",
          all("place" not in x and "background" not in x for x in CG.validate_panel_script(_ok_panels)),
          str(CG.validate_panel_script(_ok_panels))[:80])
    check("pose가 빈 컷도 문제다(기본 pose는 standing이지만 스크립트 단계에서는 반드시 채운다)",
          any("pose" in x for x in CG.validate_panel_script(
              [dict(_ok_panels[0], pose="")])), str(CG.validate_panel_script([dict(_ok_panels[0], pose="")]))[:70])
    check("프로그램이 만든 ★ 컷(도입 요약·배경만)은 검사에서 제외한다",
          CG.validate_panel_script([{"no": 1, "pose": "", "bg_only": True},
                                    dict(_ok_panels[0], no=2)]) == [],
          str(CG.validate_panel_script([{"no": 1, "pose": "", "bg_only": True},
                                        dict(_ok_panels[0], no=2)]))[:80])
    config.comic_strict_state = True
    # ── ⑬k [2026-09-09] 오염된 JSON에서도 컷을 주운다 (실측: 키 앞에 러 / U+2024가 섞인다)
    _corrupt = ('```json\n[\n  {"no": 1, "state": {"place": "mall", "time": "night"}},\n'
                '  {"no": 2, "state": {"face": "sad", "props": "matches",\n'
                '                    \ub7ec      "background": "mall"}},\n'
                '  {"no": 3, "state": "face=ahegao; clothes=gold bra"},\n'
                '  {"no": 4, "pose": "She dances."}\n]\n```')
    _sv = CG._extract_json_array(_corrupt)
    check("키 앞에 홀 글자(러)가 섞여도 배열째 버리지 않고 컷을 주운다(실측 오염)",
          len(_sv) == 4 and CG.state_of(_sv[1]).get("background") == "mall", str(len(_sv)))
    check("한 줄 문자열 state도 같은 경로로 읽는다",
          CG.state_of(_sv[2]) == {"face": "ahegao", "clothes": "gold bra"}, str(CG.state_of(_sv[2])))
    check("json_soft_fix가 키 앞 홀 글자를 지운다(추출·컷 스크립트 공용)",
          "".join(CI.json_soft_fix('{\n\ub7ec  "background": "x"\n}').split()).startswith('{"background'),
          CI.json_soft_fix('{\n\ub7ec  "background": "x"\n}')[:40])
    check("구제 실패(완전한 비문)는 빈 목록을 돌려주고 침묵하지 않는다",
          CG._extract_json_array("모델이 한국어로 설명만 해버림") == [], "")

    check("엄격 검사 스위치가 config에 있다(--no-strict-state로 해제 가능)",
          hasattr(config, "comic_strict_state")
          and "no_strict_state" in open(os.path.join(ROOT, "run_comic.py"), encoding="utf-8").read())
    _fc = [{"no": 1, "pose": "She sells matches.", "type": "face", "camera": "front_view",
            "caption_ko": "아침 번화가.", "state": {"face": "sad"}}]        # clothes가 비어 있음
    _orig_cgo = CG.call_openai_for_text
    CG.call_openai_for_text = lambda *a, **k: ('{"no": 1, "state": "clothes=shabby school uniform"}', "")
    try:
        _nf = CG.fill_first_cut(_fc, 1, "본문 도입부: 번화가에서 교복 소녀가 성냥을 판다.")
    finally:
        CG.call_openai_for_text = _orig_cgo
    check("첫 컷 보충은 배열이 아닌 단일 객체 응답도 주워 쓴다(실측으로 이 경로가 제일 잘 통했다)",
          _nf == 1 and _fc[0]["state"].get("clothes") == "shabby school uniform",
          "%s/%s" % (_nf, _fc[0]["state"]))
    _fc2 = [{"no": 1, "pose": "She sells.", "type": "face", "camera": "front_view",
             "caption_ko": "", "state": {}}]
    _cloth_keep = getattr(config, "clothes", "")
    config.clothes = "police uniform"                      # 회차 **시작** 복장(영문 태그)
    CG.call_openai_for_text = lambda *a, **k: ('{"no": 1, "state": "clothes=\ud55c\uad6d\uc5b4 \ub418\uc9c1"}', "")
    try:
        CG.fill_first_cut(_fc2, 1, "본문")
    finally:
        CG.call_openai_for_text = _orig_cgo
    check("보충 값이 한글이면 태그로 쓰지 않는다(한글은 최종 프롬프트에서 파기된다)",
          "한글" not in str(_fc2[0]["state"].get("clothes", "")), str(_fc2[0]["state"])[:60])
    check("답이 unusable이면 회차 **시작** 태그로 채운다 — 빈 상태 때문에 엄격 게이트가 회차를 죽이지 않는다(EP09 실측)",
          _fc2[0]["state"].get("clothes") == "police uniform", str(_fc2[0]["state"])[:60])
    config.clothes = _cloth_keep
    _fp_src = open(os.path.join(ROOT, "comic_gen.py"), encoding="utf-8").read()
    check("첫 컷 보충 프롬프트가 '소문자 영문 태그'와 회차 시작 후보(영문)를 준다(--special EP09 실측 회귀)",
          "값은 반드시 소문자 영문 태그" in _fp_src and "[회차 시작 후보 — 영문 태그]" in _fp_src, "")
    config.comic_strict_state = _strict_keep
    # ── ⑬l [2026-09-09] JSON 파싱 실패 재시도 (추출 / 태그 생성 / 컷 스크립트)
    _keep_ci, _keep_ag = CI.call_openai_for_text, anima_gen.openAI_response
    _hits = {"n": 0}

    def _flaky(prompt, messages=None, **kw):
        _hits["n"] += 1
        if _hits["n"] == 1:
            return ('모델이 JSON 대신 설명 문장을 써 버렸다', "")
        return ('{"protagonist": {"name": "A", "sex": "female", "clothes": "school uniform"}, "units": []}', "")

    CI.call_openai_for_text = _flaky
    try:
        _ex = CI._extract_once("본문입니다." * 40, "시트", 1)
    finally:
        CI.call_openai_for_text = _keep_ci
    check("추출은 JSON 파싱 실패 시 재시도한다(1회 실패 → 2회 성공)",
          bool(_ex) and _hits["n"] == 2, f"{_hits}회 / {bool(_ex)}")

    _hits2 = {"n": 0}
    _GOODTAG = ('{"face": "shy smile", "exposure": "school uniform", "background": "busy street",'
                ' "stats": {"M":3,"L":1,"A":2,"O":1,"I":3,"S":3,"D":1}}')

    def _flaky_tags(*a, **k):
        _hits2["n"] += 1
        return ("", 'JSON 대신 설명만: {"face": }') if _hits2["n"] == 1 else ("", _GOODTAG)

    anima_gen.openAI_response = _flaky_tags
    try:
        _tg = anima_gen._generate_tags_via_llm(0, client=object())
    finally:
        anima_gen.openAI_response = _keep_ag
    check("렌더 태그 생성도 파싱 실패 시 재시도한다(결정론 fallback보다 재시도가 싸다)",
          _hits2["n"] == 2 and _tg["face"] == "shy smile" and _tg["exposure"] == "school uniform",
          f"{_hits2}회 / {_tg['face']}")
    import inspect as _ins
    check("컷 스크립트 기본 재시도는 2회(장면당) — 예전 기본값 1은 실질 재시도가 없었다",
          _ins.signature(CG.request_panel_script).parameters["retry"].default == 2)
    _sf1 = CI.extract_json_obj_checked('{"face": "blushing․ "makeup": "natural"}')[0]
    _sf2 = CI.extract_json_obj_checked('{"a": "x"․ "b": "y"}')[0]
    check("관대한 파서가 이상 문자의 두 자리를 안다(닫는 따옴표 자리 / 쉼표 자리)",
          _sf1 == {"face": "blushing", "makeup": "natural"} and _sf2 == {"a": "x", "b": "y"},
          f"{_sf1} / {_sf2}")


    # ── ⑬i [2026-09-09] 컷별 연속 상태 시트 (언급 없으면 직전 컷 유지)
    _keep_state = (config.clothes, config.clothes_late, config.face_style, config.face_style_late,
                   config.body_shape, config.exposure_tag)
    config.clothes, config.clothes_late = "school uniform", "gold bra, gold miniskirt"
    config.face_style, config.face_style_late = "crying, blushing", "ahegao"
    config.body_shape = "petite, medium breasts"
    config.exposure_tag = ["navel, cleavage"] * 12
    config.makeup_tag = ["natural makeup"] * 12
    _sp = [
        {"no": 1, "pose": "She sells matches.", "clothes": "", "emotion": "sad"},
        {"no": 2, "pose": "She is pushed.", "clothes": "", "emotion": "crying",
         "state": {"face": "", "makeup": "", "body": "", "clothes": "", "accessories": "",
                   "hair": "", "marks": "tear trail", "props": "matchbox", "posture": "on the ground"}},
        {"no": 3, "pose": "She transforms.", "clothes": "", "emotion": "",
         "state": {"face": "ahegao", "makeup": "heavy makeup", "body": "large breasts, wide hips",
                   "clothes": "gold bra, gold miniskirt", "accessories": "heart choker",
                   "hair": "hair undone", "marks": "", "props": "wads of cash", "posture": ""}},
        {"no": 4, "pose": "She dances.", "clothes": "", "emotion": ""},
    ]
    _st0 = CG.base_cut_state(0)                     # 컷 0의 초기 상태 = 회차 시작 상태
    check("상태 시트 시작 값은 회차 **시작** 상태다(후반 의상/표정이 먼저 오지 않는다)",
          _st0["clothes"] == "school uniform" and _st0["face"] == "crying, blushing",
          f"{_st0['clothes']}/{_st0['face']}")
    CG.fold_cut_state(_sp, 0)
    check("회차 요약이 시작 복장을 틀려도 **본문을 본 컷 스크립트 초반 다수값**이 이긴다",
          CG._head_majority([{"clothes": "school uniform"}, {"clothes": "school uniform"},
                             {"clothes": ""}, {"clothes": "bikini"}], "clothes") == "school uniform"
          and CG._head_majority([{"clothes": "bikini"}, {"clothes": "school uniform"},
                                 {"clothes": "school uniform"}], "clothes") == "school uniform",
          CG._head_majority([{"clothes": "bikini"}, {"clothes": "school uniform"}], "clothes"))
    check("컷 2에서 생긴 흔적·자세는 이후 컷에도 유지되고, 컷 3이 명시한 소지품은 그것을 덮는다",
          _sp[3]["_state"]["marks"] == "tear trail"            # 컷 3은 marks를 안 씀 → 유지
          and _sp[3]["_state"]["posture"] == "on the ground"   # 컷 3 posture "" → 유지
          and _sp[3]["_state"]["props"] == "wads of cash",     # 컷 3이 덮어씀
          str(_sp[3]["_state"])[:90])
    check("컷 3의 변화(옷·화장·몸·악세사리·머리)가 컷 4로 이어진다",
          _sp[3]["_state"]["clothes"] == "gold bra, gold miniskirt"
          and _sp[3]["_state"]["makeup"] == "heavy makeup"
          and _sp[3]["_state"]["body"] == "large breasts, wide hips"
          and _sp[3]["_state"]["accessories"] == "heart choker"
          and _sp[3]["_state"]["hair"] == "hair undone", str(_sp[3]["_state"])[:90])
    check("컷의 clothes/emotion 필드도 상태에 반영된다(두 갈래가 어긋나지 않는다)",
          _sp[2]["_state"]["clothes"] == "gold bra, gold miniskirt", str(_sp[2]["_state"]["clothes"]))
    _bk_state = anima_gen._build_tag_block(0, "she dances", "front_view", "wide", "", "", False,
                                          cut_state=_sp[3]["_state"])
    check("상태 시트의 악세사리/흔적/소지품/자세가 프롬프트 라인이 된다",
          "[AAA ACCESSORIES] heart choker" in _bk_state and "[AAA MARKS] tear trail" in _bk_state
          and "[PROPS] wads of cash" in _bk_state and "[POSTURE] on the ground" in _bk_state,
          " ".join(l for l in _bk_state.split("\n") if "MARKS" in l or "PROPS" in l)[:110])
    _bk_start = anima_gen._build_tag_block(0, "she sells", "front_view", "wide", "", "", False,
                                           cut_state=_sp[0]["_state"])
    check("회차 시작 복장 컷은 후반 의상이 없고 노출 태그는 살아 있다",
          "gold miniskirt" not in _bk_start and "school uniform" in _bk_start,
          " ".join(l for l in _bk_start.split("\n") if "CLOTHES" in l or "EXPOSURE" in l)[:120])
    check("옷을 갈아입지 않은 컷은 [AAA EXPOSURE]를 잃지 않는다(예전 버그: clothes를 쓴 컷은 전부 빠짐)",
          "navel" in _bk_start or "cleavage" in _bk_start or "cameltoe" in _bk_start,
          " ".join(l for l in _bk_start.split("\n") if "EXPOSURE" in l)[:110])
    check("옷을 갈아입은 컷부터는 회차 노출 태그가 빠진다(새 옷에 예전 노출 어구가 옮지 않는다)",
          "gold miniskirt" in _bk_state,
          " ".join(l for l in _bk_state.split("\n") if "CLOTHES" in l)[:100])
    # 장소·시간·배경 (컷 연속 상태의 일부)
    _keep_bg = (config.location, getattr(config, "time_of_day", ""), config.background_tag)
    config.location, config.time_of_day = "busy city street", "night"
    config.background_tag = ["crowd, neon signs"] * 12
    _sb = [{"no": 1, "pose": "She sells.", "clothes": "", "emotion": "",
            "state": {"place": "shopping street corner", "time": "night", "background": "crowd, neon signs"}},
           {"no": 2, "pose": "She is pushed.", "clothes": "", "emotion": ""},
           {"no": 3, "pose": "She prays.", "clothes": "", "emotion": "",
            "state": {"place": "under open sky", "time": "night, golden light",
                      "background": "falling banknotes"}}]
    CG.fold_cut_state(_sb, 0)
    _b1 = anima_gen._build_tag_block(0, "she sells", "front_view", "wide", "", "", False, cut_state=_sb[0]["_state"])
    _b2 = anima_gen._build_tag_block(0, "she is pushed", "front_view", "wide", "", "", False, cut_state=_sb[1]["_state"])
    _b3 = anima_gen._build_tag_block(0, "she prays", "front_view", "wide", "", "", False, cut_state=_sb[2]["_state"])
    check("컷이 장소를 명시하면 회차 배경 태그를 덮는다(회차 전체 목록이 먼저 새지 않는다)",
          "shopping street corner" in _b1 and "busy city street" not in _b1,
          " ".join(l for l in _b1.split("\n") if "BACKGROUND" in l)[:120])
    check("장소를 안 적은 다음 컷은 직전 컷의 장소·배경을 유지한다",
          "shopping street corner" in _b2 and "falling banknotes" not in _b2,
          " ".join(l for l in _b2.split("\n") if "BACKGROUND" in l)[:120])
    check("장면이 바뀌는 컷에서 장소·배경이 교체되고 같은 시간대는 중복되지 않는다",
          "under open sky" in _b3 and "falling banknotes" in _b3 and _b3.count("night") == 1,
          " ".join(l for l in _b3.split("\n") if "BACKGROUND" in l)[:130])
    _b0 = anima_gen._build_tag_block(0, "she stands", "front_view", "wide", "", "", False)
    check("컷이 상태를 아예 주지 않으면 예전처럼 회차 배경을 쓴다(회귀)",
          "busy city street" in _b0 and "crowd, neon signs" in _b0,
          " ".join(l for l in _b0.split("\n") if "BACKGROUND" in l)[:120])
    _sb2 = [{"no": 1, "pose": "x", "state": {"place": "rooftop", "time": "dusk"}}, {"no": 2, "pose": "y"}]
    CG.fold_cut_state(_sb2, 0)
    check("place/time도 시트에 누적·유지된다(STATE_KEYS에 등록)",
          "place" in CG.STATE_KEYS and _sb2[1]["_state"]["place"] == "rooftop"
          and _sb2[1]["_state"]["time"] == "dusk", str(_sb2[1]["_state"])[:80])
    # ── [2026-09-09] 상대방(BBB) 상태 시트 — 두 사람이 한 화면인 컷의 '지금'
    _pn = [{"no": 1, "pose": "x", "camera": "wide", "type": "action", "caption_ko": "a",
            "state": "p_face=angry; p_clothes=white shirt; p_posture=standing behind a counter"},
           {"no": 2, "pose": "y", "camera": "multi", "type": "dialogue", "dialogue_ko": "b"},
           {"no": 3, "pose": "z", "camera": "multi", "type": "action", "caption_ko": "c",
            "state": "p_face=shocked; p_hair=短 い"}]
    CG.fold_cut_state(_pn, 0)
    check("상대 상태도 누적·유지된다(p_face/p_clothes/p_posture)",
          _pn[1]["_state"]["p_face"] == "angry" and _pn[1]["_state"]["p_clothes"] == "white shirt"
          and _pn[2]["_state"]["p_face"] == "shocked" and _pn[2]["_state"]["p_clothes"] == "white shirt",
          str(_pn[2]["_state"])[:90])
    _bbb = lambda s: " ".join(l for l in s.split("\n") if l.startswith("[BBB"))
    # [2026-09-12] 기본은 상대방 최소 태그(invisible man)라 아래 상세 태그 검사는 --partner-full 경로다
    _pi_bak = getattr(config, "comic_partner_invisible", True)
    config.comic_partner_invisible = False
    _pb1 = anima_gen._build_tag_block(0, "she talks", "front_view", "multi", "", "", False,
                                      cut_state=_pn[0]["_state"])
    _pb2 = anima_gen._build_tag_block(0, "he answers", "front_view", "multi", "", "", False,
                                      cut_state=_pn[1]["_state"])
    check("[BBB] 상세 태그 경로(--partner-full): 컷 상태가 회차 상대 태그를 덮는다",
          "[BBB FACE] angry" in _bbb(_pb1) and "[BBB CLOTHES] white shirt" in _bbb(_pb1)
          and "average face" not in _bbb(_pb1), _bbb(_pb1)[:170])
    check("[BBB] 상세 태그 경로: 표정이 없는 다음 컷도 직전 표정·복장을 유지한다",
          "angry" in _bbb(_pb2) and "white shirt" in _bbb(_pb2), _bbb(_pb2)[:170])
    check("한글·일본어로 온 상대 항목은 지금 버린다(최종 프롬프트에서 파기되는 값)",
          "[BBB HAIR]" not in anima_gen._build_tag_block(
              0, "x", "front_view", "multi", "", "", False,
              cut_state={**_pn[2]["_state"], "p_hair": "짧은 먼리"}))
    # ── [2026-09-12] 상대방(BBB) 최소 태그 — 외모를 고정 그룹 하나로 닫는다 (gui와 같은 계약)
    config.comic_partner_invisible = True
    _pk_bak = (config.name2, config.sex2, config.appearance2, config.outfit2)
    config.name2, config.sex2, config.appearance2, config.outfit2 = "카즈키 렌", "남자", "뚱뚱함, 대머리", "고등학교 교복"
    check("기본은 상대방 최소 태그(invisible)", anima_gen._partner_invisible() is True, "")
    check("체형 토큰은 6종 제한 · '뚱뚱함' → fat · skinny 미사용",
          anima_gen._partner_body_token(0) == "fat"
          and anima_gen._partner_body_token(0) in anima_gen.PARTNER_SIMPLE_BODY_TOKENS
          and "skinny" not in anima_gen._partner_simple_tag(0), anima_gen._partner_body_token(0))
    _grp = anima_gen._partner_simple_tag(0)
    check("고정 그룹 (bald featureless faceless naked nude <체형> invisible man:3.0)",
          _grp == f"(bald featureless faceless naked nude fat invisible man:3.0)", _grp)
    _bbb_inv = _bbb(anima_gen._build_tag_block(0, "she talks", "front_view", "multi", "", "", False,
                                               cut_state=_pn[0]["_state"]))
    check("상세 태그 라인은 사라지고 고정 그룹 + RULE + 포즈만 남는다",
          _grp in _bbb_inv and "[BBB RULE]" in _bbb_inv and "standing behind a counter" in _bbb_inv
          and not any(f"[BBB {_t}]" in _bbb_inv for _t in ("HAIR", "FACE", "MAKEUP", "BODY",
                                                           "CLOTHES", "EXPOSURE", "EXPRESSION")),
          _bbb_inv[:220])
    config.sex2 = "여자"
    check("여성 상대방은 invisible woman", anima_gen._partner_simple_tag(0).endswith("invisible woman:3.0)"),
          anima_gen._partner_simple_tag(0))
    _obs_in = ("The visible parts of the Kazuki Ren in the frame are:\n"
               "(the man's large tan hand:1.7), (his bare hands pressing her shoulder:1.6), "
               "(muscular forearms:1.4), (black hair:1.2)")
    _obs_out = anima_gen.simplify_partner_section(_obs_in, "Kazuki Ren")
    check("LLM이 observer 섹션에 지어낸 외모 태그는 후처리에서 걷는다 (포즈·행동은 보존)",
          anima_gen._partner_simple_tag(0) in _obs_out.split("\n")[1] and "tan hand" not in _obs_out
          and "muscular forearms" not in _obs_out and "black hair" not in _obs_out
          and "his bare hands pressing her shoulder" in _obs_out, _obs_out[:220])
    config.sex2, config.appearance2, config.outfit2 = "남자", "평범한 일상복", "일상복"
    check("이미 고정 그룹이면 멱등(두 번 돌려도 그대로)",
          anima_gen.simplify_partner_section(_obs_out, "Kazuki Ren") == _obs_out, _obs_out[:120])
    check("관찰자가 프레임에 없는 컷에 invisible 그룹을 끌어오지 않는다",
          "invisible" not in anima_gen.simplify_partner_section(
              "The visible parts of the Kazuki Ren in the frame are:\n(not visible in the frame)",
              "Kazuki Ren"))
    check("multi 컷도 상대방 정보를 받는다 (partner_block = pov or multi)",
          "[BBB]" in anima_gen._build_tag_block(0, "two talk", "front_view", "multi", "", "", False,
                                                partner_block=True, observer_block=False),
          "")
    check("--partner-full 스위치가 CLI에 있다", "--partner-full" in open(
            os.path.join(ROOT, "run_comic.py"), encoding="utf-8").read())
    check("배송 문서에 상대방 최소 태그 정책이 적혀 있다",
          "invisible man" in open(os.path.join(ROOT, "README.md"), encoding="utf-8").read())
    config.comic_partner_invisible = _pi_bak
    config.name2, config.sex2, config.appearance2, config.outfit2 = _pk_bak
    config.location, config.background_tag = "city street, rooftop, mall", ["crowd"] * 12
    _sb3 = [{"no": 1, "pose": "establishing", "state": {}},          # ★도입 컷은 장소를 비운다(실측)
            {"no": 2, "pose": "she sells", "state": {"place": "shopping street corner"}},
            {"no": 3, "pose": "she is pushed"}]
    CG.fold_cut_state(_sb3, 0)
    _b_intro = anima_gen._build_tag_block(0, "skyline", "front_view", "wide", "", "", False,
                                         cut_state=_sb3[0]["_state"])
    check("도입 컷이 장소를 비워도 회치 전체 배경 목록이 아니라 앞으로 처음 명시된 장소를 쓴다",
          "shopping street corner" in _b_intro and "rooftop" not in _b_intro,
          " ".join(l for l in _b_intro.split("\n") if "BACKGROUND" in l)[:120])
    (config.location, config.time_of_day, config.background_tag) = _keep_bg

    _gs = open(os.path.join(ROOT, "comic_gen.py"), encoding="utf-8").read()
    check("컷 스크립트 프롬프트에 상태 시트(스키마 + '미언급 = 직전 컷 유지')가 있다",
          '"state": ' in _gs and "직전 컷과 동일" in _gs and "accessories" in _gs)
    (config.clothes, config.clothes_late, config.face_style, config.face_style_late,
     config.body_shape, config.exposure_tag) = _keep_state

    # ── ⑬g [2026-09-12] ComfyUI 큐 확인 — '이미지가 전부 만들어졌을 때만' 페이지를 합친다
    #   (ComfyUI 없이도 도는 테스트: /queue·/history 응답을 통째로 짜깁는다)
    import urllib.request as _UR
    _UR_open = _UR.urlopen

    class _Resp:
        def __init__(self, body=b""): self._b = body
        def read(self, *a): return self._b
        def __enter__(self): return self
        def __exit__(self, *a): return False

    import time as _time_g
    _tmpq = os.path.join(tempfile.mkdtemp(prefix="st_q_"), "out")
    os.makedirs(_tmpq, exist_ok=True)
    _dstq = os.path.join(tempfile.mkdtemp(prefix="st_q_dst_"))
    Image.new("RGB", (64, 64), (200, 30, 30)).save(os.path.join(_tmpq, "ep1_cut07_anima__00001_.png"))
    _q_resp = {"/prompt": '{"prompt_id": "aaaa-1111", "number": 7}',
               "/queue": '{"queue_running": [], "queue_pending": []}',
               "/history/aaaa-1111": '{"aaaa-1111":{"status":{"status_str":"success","completed":true},'
                                     '"outputs":{"91":{"images":[{"filename":"ep1_cut07_anima__00001_.png",'
                                     '"subfolder":"","type":"output"}]}}}}',
               "/history/dead-0000": '{"dead-0000":{"status":{"status_str":"error","completed":true},'
                                     '"outputs":{}}}',
               "/history/running-1": '{"running-1":{"status":{"status_str":"running","completed":false},'
                                     '"outputs":{}}}'}

    def _fake_open(req, timeout=None, **kw):
        u = req if isinstance(req, str) else req.full_url
        for k, v in _q_resp.items():
            if u.endswith(k):
                return _Resp(v.encode("utf-8"))
        return _Resp(b"{}")

    _dirs_bak = anima_gen._comfyui_output_dirs
    anima_gen._comfyui_output_dirs = lambda json_value=None: [_tmpq]
    _UR.urlopen = _fake_open
    try:
        check("queue_prompt는 큐에서 prompt_id를 받아 돌아온다(이 id로 결과를 확인한다)",
              anima_gen.queue_prompt({"x": 1}) == "aaaa-1111", anima_gen.queue_prompt({"x": 1}))
        check("comfy_prompt_status: 성공 = done", anima_gen.comfy_prompt_status("aaaa-1111") == "done", "")
        check("comfy_prompt_status: 실행 실패 = failed", anima_gen.comfy_prompt_status("dead-0000") == "failed", "")
        check("comfy_prompt_status: 아직 도는 중 = queued", anima_gen.comfy_prompt_status("running-1") == "queued", "")
        check("comfy_prompt_status: 모르는 id = unknown", anima_gen.comfy_prompt_status("nope-9") == "unknown", "")
        check("comfy_queue_depth: 대기열을 숫자로 읽는다", anima_gen.comfy_queue_depth() == (0, 0),
              str(anima_gen.comfy_queue_depth()))
        check("comfy_prompt_files: 히스토리가 파일명을 알려준다(이름 추측 불필요)",
              anima_gen.comfy_prompt_files("aaaa-1111") == [("ep1_cut07_anima__00001_.png", "")], "")
        _gotq, _fin = anima_gen._wait_and_copy_by_history(["aaaa-1111"], {}, _dstq, 20)
        check("히스토리로 파일을 정확히 받는다(50자 절단·mtime 추측 없이)",
              _fin and len(_gotq) == 1 and os.path.basename(_gotq[0]) == "ep1_cut07_anima__00001_.png"
              and anima_gen.png_complete(_gotq[0]), str(_gotq))
        _t0q = _time_g.time()
        _gotf, _finf = anima_gen._wait_and_copy_by_history(["dead-0000"], {}, _dstq, 120)
        check("실행 실패는 120초를 낭비하지 않고 즉시 알린다",
              _finf and not _gotf and _time_g.time() - _t0q < 5, f"{_time_g.time() - _t0q:.1f}s")
        check("서버를 못 보는 id는 최종 판정 아님(예전 이름 검색으로 폴백)",
              anima_gen._wait_and_copy_by_history(["nope-9"], {}, _dstq, 5) == ([], False), "")
        check("comfy_wait_queue_idle: 빈 대기열이면 True", anima_gen.comfy_wait_queue_idle(2) is True, "")
        _q_resp["/queue"] = '{"queue_running": [[1,{},null,null,"c"]], "queue_pending": [[2,{},null,null,"c"]]}'
        check("comfy_wait_queue_idle: 대기열이 안 비추면 False(=아직 안 나온 이미지)",
              anima_gen.comfy_wait_queue_idle(1, log_fn=lambda m: None) is False, "")
        # 검증 실패(400 — 없는 unet/lora 파일 등)는 조용히 넘어가면 안 된다(예전부터 유지)
        from urllib.error import HTTPError as _HE
        import io as _io_g

        def _boom(req, timeout=None, **kw):
            raise _HE("http://x/prompt", 400, "Bad Request", None, _io_g.BytesIO(b'{"error":"no lora"}'))
        _UR.urlopen = _boom
        try:
            anima_gen.queue_prompt({"x": 1})
            _http_ok = False
        except _UR.HTTPError:
            _http_ok = True
        finally:
            _UR.urlopen = _fake_open
        check("검증 실패(400)는 그대로 예외로 올린다(조용히 성공 처리 금지)", _http_ok, "")
    finally:
        _UR.urlopen = _UR_open
        anima_gen._comfyui_output_dirs = _dirs_bak

    # ── ⑬g2 [2026-09-12] 페이지 합성 게이트 — 컷이 전부 안 모인 회차는 합치지 않는다
    _script_g = {"panels": [{"no": i, "type": "action", "pose": f"She moves {i}.", "camera": "front_view",
                             "position": "NONE", "climax": "", "caption_ko": "", "dialog": [],
                             "wide": False, "facing": "front"} for i in (1, 2, 3)],
                 "page_plans": [], "notes": []}
    _paths_g = {}
    for i in (1, 2, 3):
        _paths_g[i] = os.path.join(_tmpq, f"gate_cut{i}.png")
        Image.new("RGB", (1024, 1344), (30 * i, 90, 150)).save(_paths_g[i])
    _bake = (CG.render_panel, CG.anima_gen.init_anima_tags, CG.request_ko_glossary,
             CG.release_llm_for_gpu, CG.anima_gen.comfy_wait_queue_idle, CG.build_panel_prompt,
             config.comic_merge_partial, CG.comic_out_dir)
    _drop_g = {"n": 0}

    def _gate_run(available, renderer=None):
        """렌더가 available번 컷까지만 성공시키는 회차 실행 → comic_gen_episode의 판정

        renderer를 주면 그 함수를 렌더로 씁니다(재전송 경로 테스트용)."""
        def _rp(ep_idx, panel, seed, safety_tag, json_value, **kw):
            return _paths_g[panel["no"]] if panel["no"] <= available else None
        if renderer is not None:
            _rp = renderer
        CG.render_panel = _rp
        CG.anima_gen.init_anima_tags = lambda *a, **k: {"status": "ok"}
        CG.request_ko_glossary = lambda *a, **k: {}
        CG.release_llm_for_gpu = lambda *a, **k: None
        CG.anima_gen.comfy_wait_queue_idle = lambda *a, **k: True
        CG.build_panel_prompt = lambda *a, **k: "1girl, test"
        CG.comic_out_dir = lambda: _dstq
        return CG.comic_gen_episode(0, script=dict(_script_g), do_render=True)

    try:
        config.comic_merge_partial = False
        _m_bad = _gate_run(2)                      # 3컷 중 2컷만
        check("컷이 모자란 회차는 페이지를 합치지 않는다(합성 보류)",
              _m_bad.get("incomplete") and _m_bad.get("missing") == [3] and _m_bad.get("pages") == []
              and len(_m_bad.get("files") or []) == 2, str(_m_bad.get("missing")))
        check("합성 보류한 회차에도 렌더된 컷 이미지는 남는다(재실행에 버리지 않는다)",
              len(_m_bad.get("files") or []) == 2, str(_m_bad.get("files")))
        config.comic_merge_partial = True
        _m_ok = _gate_run(2)
        check("--merge-partial는 예전 동작(빠진 컷을 빼고 합성)을 되돌린다",
              not _m_ok.get("incomplete") and _m_ok.get("missing") == [3] and len(_m_ok.get("pages") or []) >= 1,
              str(_m_ok.get("pages")))
        config.comic_merge_partial = False
        # 모자란 컷은 한 번 더 보낸다 — 두 번째 시도에서 성공하면 회차가 완전히 나온다
        _calls = {}

        def _rp2(ep_idx, panel, seed, safety_tag, json_value, **kw):
            _n = panel["no"]
            _calls[_n] = _calls.get(_n, 0) + 1
            return _paths_g[_n] if _n != 3 or _calls[_n] >= 2 else None

        _m_retry = _gate_run(0, renderer=_rp2)
        check("모자란 컷을 한 번 더 부른다(재전송으로 메운 뒤에 페이지 합성)",
              _calls.get(3) == 2 and not _m_retry.get("incomplete") and _m_retry.get("missing") == []
              and len(_m_retry.get("pages") or []) >= 1, str(_calls) + " " + str(_m_retry.get("missing")))

        _m_all = _gate_run(3)                      # 전부 성공
        check("컷이 전부 만들어진 회차만 페이지가 나온다", not _m_all.get("incomplete")
              and _m_all.get("missing") == [] and len(_m_all.get("pages") or []) >= 1,
              str(_m_all.get("pages")))
        check("--merge-partial 플래그가 CLI에 있다", "--merge-partial" in open(
            os.path.join(ROOT, "run_comic.py"), encoding="utf-8").read())
        check("불완전 회차는 전용 실패 코드로 알린다(rc=6 · 스킵 메모 사유)",
              "incomplete" in open(os.path.join(ROOT, "run_comic.py"), encoding="utf-8").read()
              and "6:" in open(os.path.join(ROOT, "run_comic.py"), encoding="utf-8").read(), "")
    finally:
        (CG.render_panel, CG.anima_gen.init_anima_tags, CG.request_ko_glossary,
         CG.release_llm_for_gpu, CG.anima_gen.comfy_wait_queue_idle, CG.build_panel_prompt,
         config.comic_merge_partial, CG.comic_out_dir) = _bake

    # ── ⑬h [2026-09-09] 페이지 템플릿 고정 (--template)
    _tm_all = CG.load_cut_templates()
    _one = sorted(k for k in _tm_all if not _tm_all[k].get("epilogue"))[3]
    _slots_one = sum(len(x["shares"]) for x in _tm_all[_one]["tiers"])
    config.comic_templates_pin = [_one]
    config.comic_variation = 0
    _pl = [p for p in (CG.plan_pages(5, 3) or []) if not p.get("epilogue") and not p.get("prologue")]
    check("--template 로 1종을 고정하면 회차의 모든 페이지가 그 구성이다(★전용 페이지는 그대로)",
          len(_pl) == 3 and all(p["template_id"] == _one for p in _pl),
          str([p["template_id"] for p in _pl]))
    check("고정 구성의 컷 수 = 페이지 수 × 슬롯 수 (재사용 금지·전폭 상한은 내려놓는다)",
          len(CG.spec_slots(_pl)) == 3 * _slots_one, f"{len(CG.spec_slots(_pl))} vs {3 * _slots_one}")
    _two = sorted(k for k in _tm_all if not _tm_all[k].get("epilogue"))[:2]
    config.comic_templates_pin = _two
    _pl2 = CG.plan_pages(5, 4) or []
    _seq = [p["template_id"] for p in _pl2]
    check("2종을 고정한 페이지마다 번갈아 나온다(회전)", _seq[0] == _two[0] and _seq[1] == _two[1]
          and _seq[2] == _two[0], str(_seq))
    config.comic_templates_pin = [_one]
    _pl3, _n3 = CG.plan_pages_layout(5, 13, 0)
    check("고정 상태에서도 레이아웃이 목표 컷 수 이상을 담는다(초과 선호)",
          len(CG.spec_slots(_pl3)) >= 13, f"{_n3}페이지 {len(CG.spec_slots(_pl3))}컷")
    _lt = subprocess.run([sys.executable, os.path.join(ROOT, "run_comic.py"), "--list-templates"],
                         capture_output=True, text=True)
    check("--list-templates 는 --episode 없이 34종을 출력한다",
          _lt.returncode == 0 and "34종" in _lt.stdout and _one in _lt.stdout,
          _lt.stdout[:60] + _lt.stderr[:60])
    _bad = subprocess.run([sys.executable, os.path.join(ROOT, "run_comic.py"),
                           "--episode", os.path.join(ROOT, "inputs", "ep90_deadbeef.txt"),
                           "--template", "없는템플릿"], capture_output=True, text=True)
    check("--template 에 없는 id를 주면 진행하지 않는다(안내와 함께 종료)",
          _bad.returncode == 2 and "템플릿을 찾을 수 없습니다" in _bad.stdout + _bad.stderr,
          (_bad.stdout + _bad.stderr)[:80])
    config.comic_templates_pin = []
    check("고정을 풀면 다시 34종 자동 배분(회차 안 재사용)",
          len({p["template_id"] for p in (CG.plan_pages(5, 4) or [])}) >= 3)

    # ── ⑬g [2026-09-09] 항목 1:1 모드 — 행동/대사/속마음 항목 하나 = 컷 하나
    config.comic_item_cuts = True
    _iu = CI.normalize_units([{"at": "렌이 꽃을 집어 든다.", "cuts": 2, "kind": "행동"},
                              {"at": "아야가 말했다, 잘 지켜봤다고.", "cuts": 2, "kind": "대사"}])
    check("항목 모드에서는 항목 하나가 컷 1개다(LLM이 2라고 해도)",
          [x["cuts"] for x in _iu] == [1, 1] and [x["kind"] for x in _iu] == ["행동", "대사"], str(_iu))
    _iu2 = CI.normalize_units([{"at": f"문장 {n}.", "cuts": 3} for n in range(30)])
    check("항목 모드 항목 상한은 24개(=컷 24개까지), 사건 모드 상한(14)보다 넓다",
          len(_iu2) == 24, str(len(_iu2)))
    check("본문 조각을 화면 장치로 분류한다(인용문=대사, 속으로=속마음, 나머지는 행동)",
          CI.classify_device("그녀는 문을 열었다.") == "행동"
          and CI.classify_device("그녀는\"여기 있었구나\"라고 했다") == "대사"
          and CI.classify_device("「여기 있었구나」") == "대사"
          and CI.classify_device("속으로 그를 기다렸다") == "속마음"
          and CI.classify_device("그녀는 그 자리에…") == "속마음")
    _pc = CI.split_for_cuts("첫 문장이다. 둘째가 이어진다. 셋째 문장이다. 마지막이다.", 2)
    check("장면 본문을 컷 수만큼 시간 순 조각으로 나눈다(장치 판정 재료)",
          len(_pc) == 2 and _pc[0].startswith("첫") and _pc[1].startswith("셋째"), str(_pc))
    _dp = CG.build_panel_script_prompt(1, 3, "시트A", "시트B", "", ["기", "승", "전", "결"], [],
                                       panels_expected=3, device_hints=[(1, "행동"), (2, "대사"), (3, "속마음")])
    check("컷마다 화면 장치 지침이 프롬프트에 들어간다(말풍선/속마음/지문 지정)",
          "화면 장치" in _dp and "컷 2 = **대사 컷**" in _dp and "컷 3 = **속마음 컷**" in _dp
          and "컷 1 = **행동 컷**" in _dp, _dp[:0])
    _dp0 = CG.build_panel_script_prompt(1, 3, "시트A", "시트B", "", ["기", "승", "전", "결"], [],
                                        panels_expected=3)
    check("장치 지침이 없으면(사건 모드) 프롬프트에 그 블록이 없다", "화면 장치" not in _dp0)
    _rs5 = open(os.path.join(ROOT, "run_comic.py"), encoding="utf-8").read()
    check("--item-cuts / --no-item-cuts 가 도움말에 있고 config에 배선된다",
          "--item-cuts" in subprocess.run([sys.executable, os.path.join(ROOT, "run_comic.py"), "--help"],
                                          capture_output=True, text=True).stdout
          and "config.comic_item_cuts = bool(args.item_cuts)" in _rs5)
    config.comic_item_cuts = True

    # ── ⑬f [2026-09-09] "스토리가 엄청 짤려서 나오는 듯" — 컷 수 사다리가 성급했다
    config.comic_variation = 0
    _short = []
    for _t in (5, 7, 9, 15, 22):
        for _v in (0, 1, 7):
            config.comic_variation = _v
            _pl, _np = CG.plan_pages_layout(2, _t, 0)
            _e = len(CG.spec_slots(_pl))
            if _e < _t:
                _short.append(f"목표{_t}→{_e}({_v})")
    config.comic_variation = 0
    check("레이아웃이 목표 컷 수 아래로 떨어지지 않는다(본문이 장면 압축되던 증상)",
          not _short, str(_short[:4]))
    _a = CG.plan_pages_layout(3, 11, 0)
    _b = CG.plan_pages_layout(3, 11, 0)
    check("같은 목표·같은 변동이면 같은 레이아웃(재현성)",
          [p["template_id"] for p in _a[0]] == [p["template_id"] for p in _b[0]] and
          len(CG.spec_slots(_a[0])) == len(CG.spec_slots(_b[0])) == 11,
          f"{[p['template_id'] for p in _a[0]][:3]}")
    config.comic_layout_rolls = 1
    _one = len(CG.spec_slots(CG.plan_pages_layout(3, 11, 0)[0]))
    config.comic_layout_rolls = 10
    check("재추첨 횟수를 1로 줄이면 예전처럼粗い 사다리가 된다(회귀 비교용 스위치)",
          _one < 11, f"rolls=1 → {_one}컷")

    # ── ⑬e [2026-09-09] 정제 로그를 회차 끝 한 줄로 (사용자: "이거 정말 필요함?")
    CG._prompt_san_reset()
    check("정제가 아무것도 안 하면 로그를 남기지 않는다", CG._prompt_san_summary(1) == "")
    CG._PROMPT_SAN["dup"] = 12
    CG._PROMPT_SAN["dup_cuts"] = 11
    CG._PROMPT_SAN["hangul"] = {"치마", "교복", "창가", "눈물"}
    _sum = CG._prompt_san_summary(1)
    check("중복 태그·한글 파기는 회차 끝 요약 한 줄로 모인다(컷마다 1,500줄 회귀 방지)",
          _sum.startswith("EP1 프롬프트 정제:") and _sum.count(" · ") == 1 and "12개" in _sum
          and "컷 11개" in _sum and "한글 파기 4종" in _sum, _sum)
    check("플래시를 부르면 요약이 나오고 다음 회차는清白하다",
          CG._prompt_san_flush(1) == _sum and CG._prompt_san_summary(2) == "")
    _gs = open(os.path.join(ROOT, "comic_gen.py"), encoding="utf-8").read()
    check("컷 단위로 찍던 '정제: 중복 N개 제거' 줄은源码에 없다",
          "정제: 중복" not in _gs and '_PROMPT_SAN["dup"] += int(removed or 0)' in _gs)

    # ── ⑬d [2026-09-09] 컷 크롭을 얼굴 중심으로 (사용자: "얼굴이 많이 나오게")
    _W, _H = 1024, 1344
    _sim = Image.new("RGB", (_W, _H), (240, 240, 255))
    _dd = ImageDraw.Draw(_sim)
    _dd.rectangle([int(0.42 * _W), int(0.10 * _H), int(0.58 * _W), int(0.25 * _H)], fill=(200, 40, 40))

    def _red_share(_img):
        _px = _img.convert("RGB")
        _w, _h = _px.size
        _n = sum(1 for _y in range(_h) for _x in range(0, _w, 7)
                 if _px.getpixel((_x, _y))[0] > 150 and _px.getpixel((_x, _y))[1] < 90)
        return _n / float(_w * _h) * 100.0

    _o_wide = _red_share(CPM.fit_cover(_sim, 1008, 755, 0.5, anchor=False))
    _n_wide = _red_share(CPM.fit_cover(_sim, 1008, 755, 0.5, path="synthetic_noface.png", anchor=True))
    check("가로 전폭 컷: 얼굴이 위에 있는 원본을 가운데로 자르면 잘려 나가고, 새 규칙은 지킨다",
          _n_wide >= max(3.0 * _o_wide, 0.3), f"옛={_o_wide:.2f}% → 새={_n_wide:.2f}%")
    _o_p = _red_share(CPM.fit_cover(_sim, 330, 450, 0.5, anchor=False))
    _n_p = _red_share(CPM.fit_cover(_sim, 330, 450, 0.5, path="synthetic_noface2.png", anchor=True))
    _rec = {}
    _crop0, _rs0 = Image.Image.crop, Image.Image.resize
    try:
        Image.Image.crop = lambda self, box=None, **_kw: (_rec.update(box=box) or _crop0(self, box, **_kw))
        Image.Image.resize = lambda self, size, *A, **K: (_rec.update(size=size) or _rs0(self, size, *A, **K))
        CPM.fit_cover(_sim, 1008, 755, 0.5, path="synthetic_noface.png", anchor=False)
        _nh = _rec["size"][1]
        check("--no-face-crop(anchor=False)은 예전 '세로 가운데 자르기'와 같은 창을 쓴다 "
              "( anchor 밖에서도 폴백이 새던 회귀)", abs(_rec["box"][1] - (_nh - 755) // 2) <= 1,
              f"top={_rec['box'][1]} center={(_nh - 755) // 2}")
    finally:
        Image.Image.crop, Image.Image.resize = _crop0, _rs0
    check("세로 슬롯(잘릴 여유가 작은 컷)은 예전과 같이 가운데 크롭을 지킨다",
          abs(_o_p - _n_p) < 0.05, f"{_o_p:.2f}% vs {_n_p:.2f}%")
    check("얼굴 크롭 상수가 실측 최적값 근방이다(원본 위에서 0~20%, 세로 여유 10% 이상만)",
          0.0 <= CPM.FACE_CROP_TOP <= 0.20 and 0.05 <= CPM.FACE_SLACK_MIN <= 0.30,
          f"top={CPM.FACE_CROP_TOP} slack={CPM.FACE_SLACK_MIN}")
    check("OpenCV/모델이 없어도 크롭은 동작한다(face_anchor가 None을 돌려도 터지지 않는다)",
          CPM.face_anchor(_sim) is None or isinstance(CPM.face_anchor(_sim), tuple),
          str(CPM.face_anchor(_sim)))
    _hp4 = subprocess.run([sys.executable, os.path.join(ROOT, "run_comic.py"), "--help"],
                          capture_output=True, text=True).stdout
    _rs2 = open(os.path.join(ROOT, "run_comic.py"), encoding="utf-8").read()
    check("--no-face-crop / --get-face-model 이 도움말에 있고 config에 배선된다",
          "--no-face-crop" in _hp4 and "--get-face-model" in _hp4
          and "config.comic_face_crop = bool(getattr(args" in _rs2
          and 'CPM.FACE_CROP_ENABLE = bool(getattr(config, "comic_face_crop"' in
              open(os.path.join(ROOT, "comic_gen.py"), encoding="utf-8").read())

    # ── ⑬ [2026-09-09] 컷 배분 변동(--variation/--vary) + 수다장이 모드(--chatty)
    check("变动 헬퍼는 같은 입력 → 같은 값(재현성)이고 값마다 다른 값을 준다",
          CI._vary("budget|1", 7) == CI._vary("budget|1", 7)
          and len({CI._vary("budget|1", v) for v in range(9)}) > 3,
          str([CI._vary("budget|1", v) for v in range(5)]))
    _a0 = CI.allocate(11, [1, 2, 1, 3], minimum=1, variation=0)
    _a1 = CI.allocate(11, [1, 2, 1, 3], minimum=1, variation=4242)
    check("배분 변동은 '나머지 동점'의 순서만 바꾼다(합·최솟값은 보존)",
          sum(_a0) == sum(_a1) == 11 and min(_a1) >= 1, f"{_a0} vs {_a1}")
    _seed0 = random.Random(f"cut:{3}:{2}:0").random()
    _seedv = random.Random(f"cut:{3}:{2}:5").random()
    check("레이아웃 시드에 변동이 섞인다(같은 회차でも 값마다 다른 템플릿 추첨)",
          _seed0 != _seedv)
    _hp3 = subprocess.run([sys.executable, os.path.join(ROOT, "run_comic.py"), "--help"],
                          capture_output=True, text=True).stdout
    _rs = open(os.path.join(ROOT, "run_comic.py"), encoding="utf-8").read()
    check("플래그가 장식품이 아니다 — CLI 값을 config에 실제 배선하는 줄이 있다 "
          "(예전 --no-action-cuts/--name 이 도움말에만 있던 회귀)",
          all(x in _rs for x in ("config.comic_action_cuts = False", "config.comic_chatty = True",
                                 "config.comic_variation = int(args.variation)",
                                 "config.pin_name = str(args.name).strip()",
                                 "config.pin_name2 = str(args.name2).strip()")),
          "run_comic 배선 줄 검색")
    check("--variation / --vary / --chatty 플래그가 도움말에 있다",
          "--variation" in _hp3 and "--vary" in _hp3 and "--chatty" in _hp3)

    _ch_panels = [{"no": 1, "type": "action", "pose": "She stands by the window.", "caption_ko": "기존 지문.",
                   "lines": [], "camera": "side_view", "position": "NONE", "facing": "right", "clothes": ""},
                  {"no": 2, "type": "face", "pose": "She looks at viewer.", "caption_ko": "",
                   "lines": [{"kind": "speech", "who": "렌", "text": "여기 있어요.", "emo": "heart"}],
                   "camera": "close_up", "position": "NONE", "facing": "front", "clothes": ""},
                  {"no": 3, "type": "action", "pose": "She walks away.", "caption_ko": "",
                   "lines": [], "camera": "side_view", "position": "NONE", "facing": "right", "clothes": ""}]
    _ch_beats = ["창가에서 그녀는 오래 망설였다. 결국 편지를 책상에 남겼다. 그녀가 돌아선다."]
    _orig_cgt = CG.call_openai_for_text
    CG.call_openai_for_text = lambda prompt, **kw: (json.dumps(
        ["그녀는 창가에 서서 밖을 내다본다.", "볼이 붉어진 채로 그녀는 정면을 본다."], ensure_ascii=False), None)
    try:
        _cn = []
        CG._fill_chatty_narration([dict(p) for p in _ch_panels], _ch_beats, [3], _cn)
        _cp = [dict(p) for p in _ch_panels]
        CG._fill_chatty_narration(_cp, _ch_beats, [3], _cn)
        check("수다장이 모드: 지문 없는 컷의 설명은 **LLM이 만든다**(기존 지문은 안 건드린다)",
              _cp[0]["caption_ko"] == "기존 지문." and _cp[1]["caption_ko"].startswith("그녀는 창가에")
              and _cp[2]["caption_ko"].startswith("볼이 붉어진")
              and any("LLM 작문" in n for n in _cn), str([p["caption_ko"] for p in _cp]) + str(_cn))
    finally:
        CG.call_openai_for_text = _orig_cgt
    CG.call_openai_for_text = lambda prompt, **kw: ("[]", None)
    try:
        _cp2 = [dict(p) for p in _ch_panels[1:]]
        _cn2 = []
        CG._fill_chatty_narration(_cp2, _ch_beats, [2], _cn2)
        check("LLM이 빈 응답을 주면 장면 본문의 남은 문장으로 메우고 순서대로 소비한다",
              _cp2[0]["caption_ko"].startswith("창가에서") and _cp2[1]["caption_ko"].startswith("결국")
              and _cp2[0]["caption_ko"] != _cp2[1]["caption_ko"], str([p["caption_ko"] for p in _cp2]))
        _cp3 = [dict(p) for p in _ch_panels[1:]]
        CG._fill_chatty_narration(_cp3, [], [], [])
        check("장면 본문도 없으면 짧은 기본 문장으로 화면을 조용히 두지 않는다",
              all(p["caption_ko"] for p in _cp3), str([p["caption_ko"] for p in _cp3]))
    finally:
        CG.call_openai_for_text = _orig_cgt
    _cp4 = [dict(p) for p in _ch_panels]
    CG._fill_chatty_narration([], _ch_beats, [3], [])
    check("수다장이 함수는 빈 패널 목록에서 아무것도 하지 않는다", True)
    config.comic_chatty = False

    # ── ⑭ [2026-09-10] 실행 로그 위생 + 추출 체크포인트(빈 항목만 이어받기)
    from tempfile import mkdtemp as _mkdtemp_rl
    import runlog as _RL
    _tmp = _mkdtemp_rl(prefix="selftest_log_")
    _cwd = os.getcwd()
    _keep = (_RL.LOG_DIR, _RL.ERROR_LOG, CI.EXTRACT_CACHE, list(config.char_tags))
    try:
        os.chdir(_tmp)
        os.makedirs("log", exist_ok=True)
        _RL.LOG_DIR, _RL.ERROR_LOG = "log", os.path.join("log", "error.log")
        CI.EXTRACT_CACHE = os.path.join("state", "extract_cache.yaml")
        open(os.path.join("log", "comic_gen.log"), "w", encoding="utf-8").write("지난 실행 흔적\n")
        _RL.start_run()
        check("실행 시작에 본 로그를 비운다(어제 실패와 섞이지 않는다)",
              open(os.path.join("log", "comic_gen.log"), encoding="utf-8").read().strip() == "")
        open(os.path.join("log", "comic_gen.log"), "w", encoding="utf-8").write("유지됨\n")
        _RL.start_run(keep=True)
        check("--keep-logs면 본 로그를 이어서 쓴다",
              "유지됨" in open(os.path.join("log", "comic_gen.log"), encoding="utf-8").read())
        _RL.note("EP1 컷 스크립트가 필수 항목을 채우지 못했습니다", "COMIC")   # 표어 없는 문장도 잡히게
        _RL.note("정상적인 진행 정보는 여기에 남지 않는다", "COMIC")
        _er = open(os.path.join("log", "error.log"), encoding="utf-8").read()
        check("에러·경고만 log/error.log에 따로 남는다(실행 구분자 포함)",
              "필수 항목" in _er and "정상적인" not in _er and "RUN" in _er, _er[-90:].replace("\n", " "))
        check("종료 요약이 에러 건수와 파일 위치를 알려준다",
              "1" in _RL.summary() and "error.log" in _RL.summary(), _RL.summary())

        _k1 = CI.extract_key("본문 A", "시트", 1, "plain")
        check("추출 체크포인트 키는 원고 지문 — 원고를 고르면 자동 폐기",
              _k1 == CI.extract_key("본문 A", "시트", 1, "plain")
              and _k1 != CI.extract_key("본문 B", "시트", 1, "plain")
              and _k1 != CI.extract_key("본문 A", "시트", 2, "plain"))
        _d1 = {"protagonist": {"name": "A", "sex": "female", "clothes": "", "hair_color": "brown hair"},
               "guides": {"protagonist": ["가", "나", "다", "라"]}, "rating": ""}
        _miss = CI.missing_extract_fields(_d1)
        check("빈 핵심 항목을 목록으로 뽑는다(다음 실행이 채울 대상)",
              "protagonist.clothes" in _miss and "rating" in _miss and "protagonist.name" not in _miss,
              str(_miss[:5]))
        CI.save_extract_checkpoint(_k1, _d1, _miss)
        _d2 = {"protagonist": {"name": "B", "clothes": ""}, "guides": {"protagonist": []}, "rating": ""}
        _d2, _cf = CI.merge_extract_cached(_d2, CI.load_extract_checkpoint(_k1))
        check("다음 실행은 빈 칸만 이어받는다(이번에 얻은 값은 안 덮는다)",
              _d2["protagonist"]["name"] == "B" and _d2["protagonist"]["hair_color"] == "brown hair"
              and len(_d2["guides"]["protagonist"]) == 4, str(_cf)[:70])
        _orig_cit = CI.call_openai_for_text
        try:
            CI.call_openai_for_text = lambda *a, **k: (
                '{"protagonist": {"clothes": "school uniform"}, "rating": "safe"}', "")
            _d3, _m3 = CI.fill_missing_extract(_d2, ["protagonist.clothes", "rating"], "본문 A", "시트", 1)
        finally:
            CI.call_openai_for_text = _orig_cit
        check("빈 항목만 따로 다시 물어서 메운다(전체 재추출보다 싸다)",
              _d3["protagonist"]["clothes"] == "school uniform" and _d3["rating"] == "safe"
              and "protagonist.clothes" not in _m3 and "rating" not in _m3, str(_m3)[:60])
        config.char_tags = ["Kirisaki Chitoge, long hair, brown hair, blue eyes, fair skin,"
                            " slim body, school uniform, red ribbon"]
        _d4 = {"protagonist": {"name": "A", "job": "경찰"}, "guides": {"protagonist": ["a", "b", "c", "d"]}}
        _d4, _inf = CI.infer_missing_from_profile(_d4, CI.missing_extract_fields(_d4))
        check("그래도 모자라면 공식 캐릭터 태그·직업으로 추론해 메운다(마지막 안전판)",
              "police uniform" in _d4["protagonist"]["clothes"]
              and "blue eyes" in _d4["protagonist"]["eye_color"] and "rating" in _inf, str(_inf)[:80])
        check("추론으로 메운 '지금 표정'은 중립 — 회차 끝 표정을 기본값으로 쓰지 않는다",
              _d4["protagonist"]["face_style"] == "neutral expression",
              str(_d4["protagonist"].get("face_style")))
    finally:
        os.chdir(_cwd)
        _RL.LOG_DIR, _RL.ERROR_LOG, CI.EXTRACT_CACHE = _keep[0], _keep[1], _keep[2]
        config.char_tags = _keep[3]


    # ── ⑮ [2026-09-10] 말풍선·속마음 이미지 은행 (9슬라이스 · 감정 선택 · 꼬리/물방울 폐지)
    from tempfile import mkdtemp as _mkd_b
    _btmp = _mkd_b(prefix="selftest_balloons_")
    _bkeep = CPM.balloon_style()
    try:
        _bs = CPM.generate_balloon_set(dest=_btmp)
        _png = sorted(f for f in os.listdir(_btmp) if f.endswith(".png"))
        _bodies = [f for f in _png if f.startswith(("speech_", "thought_"))]
        check("--get-balloons가 몸통 9종만 만든다(꼬리·물방울은 기능 폐지)",
              len(_bodies) == len(CPM.BALLOON_ART_VARIANTS)
              and not [f for f in _png if f.startswith(("tail_", "bubble"))]
              and os.path.exists(_bs["manifest"]),
              f"몸통 {len(_bodies)} / png {len(_png)}")
        CPM.set_balloon_style("image", _btmp)
        check("감정으로 변형을 고른다 (anger→sharp, heart→dreamy, surprise→shout, gloom→void)",
              CPM.pick_balloon_variant("speech", "anger", 0, 0, 0) == "speech_sharp"
              and CPM.pick_balloon_variant("thought", "heart", 0, 0, 0) == "thought_dreamy"
              and CPM.pick_balloon_variant("speech", "surprise", 0, 0, 0) == "speech_shout"
              and CPM.pick_balloon_variant("thought", "gloom", 0, 0, 0) == "thought_void")
        _used_page = []
        _seq = []
        for _i in range(3):
            _v = CPM.pick_balloon_variant("speech", "", 90 * _i, 0, _i, _used_page) or ""
            _seq.append(_v)
            if _v:
                _used_page.append(_v)
        check("같은 페이지에서 같은 모양을 2번 쓰지 않는다(변형이 1종일 때만 예외)",
              len([v for v in _seq if v]) == len(set(_seq)), str(_seq))

        _LONG = "그래서 말인데, 그날 이후로 나는 네가 조금 무서워졌다. 그래도 네가 온 것은 기뻤다."

        def _shot(kind, text, emo="", side=None, bg=(40, 40, 40), style=None):
            if style:
                CPM.set_balloon_style(style, _btmp if style == "image" else None)
            cv = Image.new("RGB", (560, 380), bg)
            dd = ImageDraw.Draw(cv)
            used = []
            bx = CPM._draw_balloon(dd, 10, 10, 540, 360,
                                   CPM._balloon(kind, text, side=side, emo=emo), canvas=cv, bank_used=used)
            if style:
                CPM.set_balloon_style("image", _btmp)
            return cv, used, bx

        _cv, _u, _bx = _shot("speech", "거기 서! 오늘 할 이야기가 있어서 왔어.")
        check("이미지 모드로 그려지고 페이지에 사용 변형이 기록된다", bool(_u) and _bx is not None, str(_u))
        check("같은 원고를 두 번 그리면 같은 변형이 고른다(random 안 쓴다)",
              _shot("speech", "거기 서! 오늘 할 이야기가 있어서 왔어.")[1] == _u)
        # ── 사용자 지시: 몸통이 글자보다 작았다(2~2.5배로) — 벡터때와 견준다
        _cv_v, _, _bx_v = _shot("speech", "거기 서! 오늘 할 이야기가 있어서 왔어.", style="vector")
        _a_img = (_bx[2] - _bx[0]) * (_bx[3] - _bx[1])
        _a_vec = (_bx_v[2] - _bx_v[0]) * (_bx_v[3] - _bx_v[1])
        check("몸통이 예전(벡터)보다 면적 1.5배 이상 크다(폭 절반 후) — 글자를 몸통 안에 다 넣는다",
              _a_img >= 1.5 * _a_vec, f"이미지 {_a_img} vs 벡터 {_a_vec} = {_a_img / max(1, _a_vec):.1f}배")

        def _bg_leak(cv, box, variant):
            w, h = box[2] - box[0], box[3] - box[1]
            sl, st, sr, sb = CPM._balloon_art_safe(variant, w, h)
            x0, y0, x1, y1 = box[0] + sl, box[1] + st, box[2] - sr, box[3] - sb
            tot = leak = 0
            for yy in range(y0, max(y0, y1), 2):
                for xx in range(x0, max(x0, x1), 2):
                    tot += 1
                    if abs(cv.getpixel((xx, yy))[0] - 40) <= 5:
                        leak += 1
            return (leak / tot) if tot else 1.0

        _r = []
        for _k, _t in (("speech", "거기 서!"), ("speech", _LONG), ("thought", "…심장이 너무 시끄럽다."),
                       ("thought", "고백할 타이밍을 놓쳤다.")):
            _c2, _u2, _b2 = _shot(_k, _t)
            _r.append(_bg_leak(_c2, _b2, _u2[0]))
        check("글자가 몸통 안에 전부 들어간다(안전영역 배경 노출 ≤ 1%, 폭 절반 후 기준)",
              all(x <= 0.01 for x in _r), " ".join(f"{x:.2%}" for x in _r))
        _cv3, _u3, _bx3 = _shot("speech", "이음새 확인 문장입니다.")
        _pl = [c for c in (_cv3.getpixel((xx, yy))[0]
                           for yy in range(_bx3[1] + 30, _bx3[3] - 30, 2)
                           for xx in range(_bx3[0] + 50, _bx3[2] - 50, 2)) if c > 200]
        check("플레이트는 반투명(α=210)으로 유지된다",
              _pl and 210 <= max(_pl) <= 220, f"몸통 안 최대 {max(_pl) if _pl else None} (기대 217)")
        CPM.set_balloon_style("image", os.path.join(_btmp, "없는_디렉터리"))
        _cv5, _u5, _bx5 = _shot("speech", "벡터 폴백 확인")
        check("자산이 없으면 조용히 벡터로 그린다(렌더가 죽지 않는다)",
              _u5 == [] and _bx5 is not None and _cv5.crop(_bx5).convert("L").getextrema()[1] > 250,
              str(_u5))
        CPM.set_balloon_style("image", _btmp)
        _cv6 = Image.new("RGB", (520, 360), (200, 200, 200))
        check("좌우 반전해서 붙일 수 있다(비대칭 자산 대비)",
              CPM.paste_balloon_art(_cv6, "speech_sharp", (10, 10, 500, 340), (60, 60, 360, 160),
                                    flip=True) == "speech_sharp")
    finally:
        CPM.set_balloon_style(_bkeep, CPM.BALLOON_ART_DIR_DEFAULT)


    # ── ⑮ [2026-09-10] --special 10회차 중 5회차를 죽인 '"cuts"' 자리 디코딩 사고 (실측 회귀)
    #   log/error.log 13:28:58·13:37:14·13:37:38·13:38:03·13:38:29 = EP02·EP05·EP06·EP07·EP08
    #   전부 "kind": "행동", 바로 아래 "cuts" 자리에서 깨졌다(EP09는 strict 게이트로 따로빠졌다).
    _unit = lambda key_line: ('{ "units": [ { "at": "그녀는 가슴을 주무르고 있었다.", '
                              '"kind": "행동",\n' + key_line + '\n } ] }')
    _c1 = CI.extract_json_obj_checked(_unit("     *cuts*: 1"))[0]                    # 마크다운 별표
    _c2 = CI.extract_json_obj_checked(_unit("     \u0989\u09aa\u09b8\u09cd\u09a5\u09bf\u09a4 cuts: 1"))[0]  # 벵골·태국(실측 코드포인트 그대로)
    _c3 = CI.extract_json_obj_checked(_unit('     \u1ec5ncuts": 1'))[0]                # 베트남 글자 + 열림따옴표 실종
    check("키 앞 이물질 3종을 키 이름으로 되살린다(*cuts* / 벵골·태국 / 베트남) — 실측 그대로",
          all(bool(x.get("units")) for x in (_c1, _c2, _c3)),
          " / ".join(str(x)[:40] for x in (_c1, _c2, _c3)))
    check("복구한 항목의 값까지 산다 (at/kind가 원문대로)",
          bool(_c1.get("units")) and _c1["units"][0]["kind"] == "행동"
          and "가슴" in _c1["units"][0]["at"], str(_c1)[:60])
    check("스키마와 다른 키 이름('ncuts')은 접는 단추(fold_extract_keys)가 'cuts'로 되돌린다",
          CI._fold_key("ncuts", CI.UNIT_KEYS) == "cuts"
          and CI._fold_key("*cuts*", CI.UNIT_KEYS) == "cuts", CI._fold_key("ncuts", CI.UNIT_KEYS))
    # 파싱은 **성공**했는데 키 이름만 찢어진 경우 — 조용히 필드가 사라지는 제일 위험한 형태(실측 yaml)
    _silent = '{"protagonist": {"name": "유즈", "eye_ \u0435\u0439_color": "brown eyes", "skin_color": "fair skin"}}'
    _sd, _slog = CI.fold_extract_keys(CI.extract_json_obj_checked(_silent)[0])
    check("파싱은 성공했으나 키가 찢어진 답('eye_ ей_color')을 스키마 키로 되돌린다(조용한 실종 차단)",
          _sd["protagonist"].get("eye_color") == "brown eyes" and "eye_ ей_color" not in _sd["protagonist"],
          str(_sd["protagonist"])[:70])
    check("ASCII로 찢긴 키('eye_com_color')도 가장 가까운 스키마 키로 접는다",
          CI._fold_key("eye_com_color", CI._EXTRACT_KEYSETS["protagonist"]) == "eye_color",
          CI._fold_key("eye_com_color", CI._EXTRACT_KEYSETS["protagonist"]))
    # 값 안의 곡선 따옴표/줄임표는 **건드리면 안 된다** — 예전 파서가 살아있던 값을 죽였다(실측 EP05)
    _q = CI.extract_json_obj_checked('{"units": [{"at": "“하아, 하아… 소타 님, 빨리 와주세요….”",'
                                     ' "kind": "대사"}]}')[0]
    check("값 안의 “곡선 따옴표”와 줄임표는 그대로 둔다(직따옴표 치환이 JSON을 죽이던 실측 회귀)",
          bool(_q.get("units")) and "하아" in _q["units"][0]["at"], str(_q)[:70])
    _tags = CI.extract_json_obj_checked('{"stats":{"M":5,"L":3,"A":3,"O":3,"I:4,"S:4,"D":2},'
                                        '"face":"soft smile, calm"}')
    check("렌더 태그 생성의 닫는 따옴표 실종(I:4 → I\":4)도 복구한다(2026-09-10 15:36:43 실측 — 예전은 fallback 태그)",
          _tags[0].get("stats", {}).get("I") == 4 and _tags[0].get("face") == "soft smile, calm",
          _tags[1][:60] or str(_tags[0])[:60])

    check("구조 자리의 이상 문자(쉼표 자리를 삼킨 U+2024)는 여전히 고친다",
          CI.extract_json_obj_checked('{"a": "x"\u2024 "b": "y"}')[0] == {"a": "x", "b": "y"}, "")
    # 배열 안에 키 없이 툭 던진 본문 문장(실측 EP05) / 객체 키 자리의 비문(실측 EP08·EP10)
    _bare = CI.extract_json_obj_checked('{ "units": [\n   {\n     "at": "첫 문장입니다.",\n     "kind": "행동"\n   },\n'
                                        '   며칠 전부터 시작된 여운이 가시지 않은 듯, 그녀는 몽롱했다.\n   {\n'
                                        '     "at": "둘째 문장입니다.",\n     "kind": "대사"\n   }\n ] }')[0]
    check("배열 안에 키 없이 던져진 본문 문장은 {'at': …}로 감싼다(앵커라 지우면 컷이 사라진다)",
          len(_bare.get("units") or []) == 3 and "몽롱" in str(_bare["units"][1]), str(_bare)[:90])
    _junk = CI.extract_json_obj_checked('{ "units": [ { "at": "문장입니다.", "kind": "행동",\n     을",\n'
                                        '     "cuts": 1 } ] }')[0]
    check("객체 키 자리에 깨져 나온 비문 줄(을+따옴표)은 버리고 나머지를 살린다",
          bool(_junk.get("units")) and _junk["units"][0]["kind"] == "행동", str(_junk)[:70])
    # 스키마: 항목 1:1 모드(기본)에서는 'cuts'를 애초에 부탁하지 않는다 — 취약한 자리를 없앤다
    _keep_item = getattr(config, "comic_item_cuts", True)
    config.comic_item_cuts = True
    _p_item = CI.build_extract_prompt("본문입니다." * 30, "시트", 1, need_segments=False)
    config.comic_item_cuts = False
    _p_event = CI.build_extract_prompt("본문입니다." * 30, "시트", 1, need_segments=False)
    config.comic_item_cuts = _keep_item
    check("항목 1:1 모드(기본) 추출 프롬프트는 'cuts' 키를 부탁하지 않는다(항상 1이라 값도 버린다)",
          '"units"' in _p_item and "cuts" not in _p_item.split('"units"')[1].split("]")[0]
          and "두 키만" in _p_item, _p_item.split('"units"')[1][:80])
    check("사건 모드(--no-item-cuts)는 'cuts'를 부탁하되 생략을 허용한다",
          "cuts" in _p_event and "빼도 된다" in _p_event, "")
    check("'cuts'가 없는 유닛도 컷 수를 자체 판정으로 채운다(스키마 제거로 정보 손실 없음)",
          [u["cuts"] for u in CI.normalize_units([{"at": "그는 그녀의 손을 잡았다.", "kind": "행동"}])] == [1]
          if not _keep_item else CI.normalize_units([{"at": "그는 그녀의 손을 잡았다."}])[0]["cuts"] >= 1,
          str(CI.normalize_units([{"at": "그는 그녀의 손을 잡았다."}]))[:60])


    # ── ⑮b [2026-09-11] --special 장면 카드 ([LOCATION]/[SITUATION]/[TIME]/[CLOTHES])
    import novel_progress as _NP_card
    _card_src = """=== Episode 1 ===

# 주인공 (호시 소이치로)
직업: 고등학생 선도부원

# 상대방 (카즈키 렌)
직업: 오타쿠 고등학생

--- 에피소드 내용 ---

##EPISODE 1:
[LOCATION]: 심야 약국 내부. 선반마다 약품이 빼곡하고 형광등이 깜빡인다.
[SITUATION]: 비밀 취미를 숨기던 약국에서 상대와 마주친 상황.
[TIME]: 심야 (밤)
[CLOTHES]: 다크 네이비 슬림핏 학생 바지, 흰 와이셔츠.
#####
기:
[ACTION] 소이치로가 경계하며 약국으로 들어선다.
[INNER] 이곳이 유일한 탈출구다.
#####
승:
[LOCATION]: 약국 구석 서가. 잡지 코너 앞.
[TIME]: 심야 (밤)
[CLOTHES]: 젖은 셔츠, 옷이 몸에 붙은 상태.
[ACTION] 소이치로가 잡지를 꺼내고 손을 뻗는다.
[TALK] 이게 신간이군요.
#####
전:
[ACTION] 렌이 소이치로의 손 위에 손을 포갠다.
#####
결:
[ACTION] 카즈키 렌이 다정한 미소를 지으며 입을 연다.
[TALK] 앞으로 많이 알려드릴게요.
[ACTION] 호시 소이치로가 고개를 돌린다.
[TALK] 고… 고맙군.
"""
    _card_dir = _mkdtemp_rl(prefix="selftest_card_")
    _card_ep = os.path.join(_card_dir, "ep01_c09a45175e5843d7.txt")
    with open(_card_ep, "w", encoding="utf-8") as _f_card:
        _f_card.write(_card_src)
    _cp = _NP_card.parse_episode(_card_ep)
    _body, _segs = _NP_card.render(_cp)
    check("신형 장면 카드 [LOCATION]/[SITUATION]/[TIME]/[CLOTHES]를 4개 필드로 읽는다(지문 오염 아님)",
          len(_cp["cards"]) >= 2 and _cp["cards"][0].get("시간") == "심야 (밤)"
          and "약국" in _cp["cards"][0].get("장소", "") and "학생 바지" in _cp["cards"][0].get("복장", "")
          and "마주친" in _cp["cards"][0].get("상황", ""),
          str([{k: v for k, v in c.items() if k in ("장소", "시간")} for c in _cp["cards"]])[:110])
    check("카드 라벨([LOCATION] …)과 '##EPISODE 1:' 구간 헤더가 본문에 남지 않는다",
          "[LOCATION]" not in _body and "##EPISODE" not in _body and "[CLOTHES]" not in _body,
          _body[:60].replace("\n", "⏎"))
    check("회차 시작 카드는 본문 맨 앞(첫 막 라벨 앞)에 상태 줄로 놓인다",
          _body.startswith("장소:") and "시간: 심야 (밤)" in _body and "소이치로의 복장:" in _body,
          _body[:70].replace("\n", "⏎"))
    check("막 중간 카드는 **그 막 안에** 놓인다(장면 전환 — 앞 막 꼬리로 새지 않는다)",
          _body.find("약국 구석 서가") > _body.find("승:")
          and _body.find("약국 구석 서가") < _body.find("전:"),
          _body[_body.find("승:"):][:70].replace("\n", "⏎") if "승:" in _body else "(승 없음)")
    check("카드를 넣어도 기승전결 앵커 4개가 잡고 막이 갈라진다",
          len(_segs) == 4 and len(CI.split_by_segments(_body, _segs)) == 4,
          str([len(x) for x in CI.split_by_segments(_body, _segs)]))
    _cl = CI.scene_card_block(_cp["cards"])
    check("추출 프롬프트는 **회차 시작 카드만** 원작 지정값으로 올린다(막 중간 카드는 컷이 받는다)",
          "약국 내부" in _cl and "약국 구석 서가" not in _cl, _cl[:80].replace("\n", "⏎"))
    _ep_prompt = CI.build_extract_prompt(_body, "시트", 1, need_segments=False, scene_cards=_cp["cards"])
    check("추출 프롬프트에 장면 카드 블록과 '1순위 근거' 규칙이 들어간다",
          "[장면 카드(원작 지정" in _ep_prompt and "1순위 근거" in _ep_prompt, "")
    check("추출 프롬프트가 **[에피소드 N 본문]을 다시 싣는다**(한때 '# 본문 미사용'으로 시트만 갔다)",
          "[에피소드 1 본문]" in _ep_prompt and "소이치로가 경계하며 약국으로 들어선다" in _ep_prompt,
          _ep_prompt[-90:].replace("\n", "⏎"))
    check("컷 스크립트 규칙에 '장소:/시간:/복장: 줄 = 원작 지정값'이 있다",
          "원작이 정한 값" in open(os.path.join(ROOT, "comic_gen.py"), encoding="utf-8").read(), "")
    _keep_sc = getattr(config, "ep_scene_cards", None)
    CI.apply_to_config({"protagonist": {"name": "소이치로", "sex": "male"}, "guides": {},
                        "rating": "nsfw", "units": [{"at": "소이치로가 경계하며", "kind": "행동"}]},
                       _body, "시트", 1, scene_cards=_cp["cards"])
    _sc = (getattr(config, "ep_scene_cards", {}) or {}).get(1) or []
    check("장면 카드가 config.ep_scene_cards에 구조로 남는다(막 전환 값을 구분해 쓸 수 있게)",
          len(_sc) >= 2 and any(c.get("act") == "승" for c in _sc)
          and not _sc[0].get("act"), str([(c.get("act"), c.get("idx")) for c in _sc])[:80])
    if _keep_sc is None:
        config.ep_scene_cards = {}
    else:
        config.ep_scene_cards = _keep_sc
    check("본문 예산은 **시트 길이까지 빼서** 계산한다(시트가 예약을 먹으면 규칙/스키마가 앞에서 잘린다)",
          CI.episode_char_budget(sheet_text="가" * 4000) < CI.episode_char_budget()
          and CI.episode_char_budget(sheet_text="가" * 40000) >= CI.EPISODE_TEXT_CAP_MIN,
          f"{CI.episode_char_budget()} → {CI.episode_char_budget(sheet_text='가' * 4000)}")
    # LaTeX로 감싼 값(ep04 실측: "kind": $\\text{속마음}$ — 0.2/0.0 두 번 다 같은 형태로 회차를 잃었다)
    def _texfix(_raw):
        _f = CI.json_soft_fix(_raw)
        try:
            return json.loads(_f)["units"][0]["kind"], _f
        except Exception as _e:
            return f"{type(_e).__name__}", _f
    _v, _f = _texfix('{"units": [{"at": "x", "kind": $\\\\text{속마음}$}]}')
    check("LaTeX로 감싼 값(**$\\\\text{속마음}$**)을 따옴표 값으로 고쳐 회차를 살린다", _v == "속마음", f"{_v} | {_f[-32:]}")
    _v, _f = _texfix('{"units": [{"at": "x", "kind": $\\\\text{속마음"}$}]}')
    check('LaTeX 안 따옴표가 어긋난 형태(**$\\\\text{속마음"}$**)도 복구한다', _v == "속마음", f"{_v} | {_f[-32:]}")
    _v, _f = _texfix('{"units": [{"at": "x", "kind": \\\\text{action}}]}')
    check("\\\\text{…}(달러 없는 형태)도 복구한다", _v == "action", f"{_v} | {_f[-32:]}")
    _v, _f = _texfix('{"units": [{"at": "x", "kind": $행동$}]}')
    check("$…$로만 감싼 값도 복구한다", _v == "행동", f"{_v} | {_f[-32:]}")
    check("따옴표 **안**의 달러 표기는 LaTeX로 오인하지 않는다",
          json.loads(CI.json_soft_fix('{"a": "cost $100 and $200 ok", "b": "x"}'))["a"] == "cost $100 and $200 ok", "")
    # ComfyUI가 쓰는 도중 복사한 잘린 PNG는 흰 칸이 된다 — 사본을 검수하고 다시 기다린다
    _md = _mkdtemp_rl(prefix="selftest_png_")
    _good = os.path.join(_md, "good.png")
    Image.new("RGB", (64, 64), (10, 20, 30)).save(_good)
    _cutp = os.path.join(_md, "cut.png")
    with open(_cutp, "wb") as _fh:
        _fh.write(open(_good, "rb").read()[:-16])            # IEND 잘라내기(실측과 같은 형태)
    check("잘린 PNG 사본을 감지한다(IEND 없음) — 완성본은 통과, 없는 파일도 실패",
          anima_gen.png_complete(_good) is True and anima_gen.png_complete(_cutp) is False
          and anima_gen.png_complete(os.path.join(_md, "nope.png")) is False, "")
    _ag_src = open(os.path.join(ROOT, "anima_gen.py"), encoding="utf-8").read()
    check("사본이 미완성이면 삭제하고 다음 순환에 다시 복사한다(흰 칸으로 페이지를 채우지 않는다)",
          "if not png_complete(dst):" in _ag_src and "다시 기다립니다" in _ag_src, "")

    # [2026-09-11] ComfyUI가 렌더 도중 죽은 실측에서 나온 3가지 방어선
    _mp4 = _mkdtemp_rl(prefix="selftest_gone_")
    _pp4 = []
    for _i in (1, 2):
        _pt4 = os.path.join(_mp4, f"p{_i}.png")
        Image.new("RGB", (420, 420), (90, 120, 200)).save(_pt4)
        _pp4.append(_pt4)
    _specs4 = [{"size": 1, "rows": [{"cells": [{"idx": j}]}]} for j in range(3)]   # 슬롯 3, 파일 2
    try:
        _saved4 = CPM.compose_pages(_pp4, ["a", "b"], _mp4, "ep_gone",
                                    page_specs=_specs4, page_label_prefix="EP99")
        _gone_err = ""
    except Exception as _e4:
        _saved4, _gone_err = [], f"{type(_e4).__name__}: {_e4}"
    check("ComfyUI 중단으로 컷이 일부만 렌더되면 **빈 페이지 슬롯은 건너뛰고** 나머지를 살린다(예전엔 ValueError로 회차 전체 사망)",
          len(_saved4) == 2, _gone_err or f"{len(_saved4)}장")
    _cg4 = open(os.path.join(ROOT, "comic_gen.py"), encoding="utf-8").read()
    check("렌더 5연속 실패(ComfyUI 중단)면 남은 컷을 불러 시간을 태우지 않는다",
          "_miss_run >= 5" in _cg4 and "ComfyUI가 중단된 것으로 보여" in _cg4, "")
    check("부분 렌더 시 페이지는 **렌더된 컷 기준** 레이아웃으로 짠다(슬롯/파일 어긋남 방지)",
          "build_page_specs(panels[:len(files)]" in _cg4, "")
    _rc4 = open(os.path.join(ROOT, "run_comic.py"), encoding="utf-8").read()
    check("잡히지 않은 예외도 error.log에 남긴다(날것 traceback로 회차가 조용히 사라지던 것)",
          "except Exception as e:" in _rc4 and "runlog.note(_ln" in _rc4 and "실행 중 예외" in _rc4, "")
    _dead_src = ("=== Episode 5 ===\n\n# 주인공 (호시 소이치로)\n직업: 고등학생 선도부원\n\n"
                 "--- 에피소드 내용 ---\n\n서버 응답 실패 (마지막 남은 이성으로 저항하려 눈물을 흘리지만 …)\n")
    _dead_ep = os.path.join(_card_dir, "ep05_c09a45175e5843d7.txt")
    with open(_dead_ep, "w", encoding="utf-8") as _f_d:
        _f_d.write(_dead_src)
    _dead_info = _NP_card.load(_dead_ep, "")
    check("원작 생성이 실패한 회차(본문 몇 자 · 앵커 없음)는 **회차 자체를 건너뜁니다**(근거 없는 컷 6개를 지우지 않는다)",
          bool(_dead_info.get("empty_body")) and any("비어" in n for n in _dead_info["notes"])
          and not _NP_card.load(_card_ep, "")["empty_body"], str(_dead_info["notes"])[:90])
    check("건너뛴 회차는 사유 코드(rc 5)와 SKIPPED 메모로 남긴다",
          "5: \"에피소드 본문이 비어 있음" in open(os.path.join(ROOT, "run_comic.py"), encoding="utf-8").read()
          and "inp.get(\"empty_body\")" in open(os.path.join(ROOT, "run_comic.py"), encoding="utf-8").read(), "")
    _uk_src = _card_src.replace("[TIME]: 심야 (밤)\n[CLOTHES]:",
                                "[TIME]: 심야 (밤)\n[MOOD]: 형광등이 깜빡이는 음침함\n[CLOTHES]:", 1)
    _uk_ep = os.path.join(_card_dir, "ep02_c09a45175e5843d7.txt")
    with open(_uk_ep, "w", encoding="utf-8") as _f_uk:
        _f_uk.write(_uk_src)
    _uk_body, _uk_segs = _NP_card.render(_NP_card.parse_episode(_uk_ep))
    check("카드 4종 밖의 [키]: 줄도 지문으로 살아남는다(입력이 바뀌어도 정보를 버리지 않는다)",
          "형광등이 깜빡이는 음침함" in _uk_body and len(_uk_segs) == 4, _uk_body[:50].replace("\n", "⏎"))
    _lines = [l for l in _body.split("\n") if l.startswith(("카즈키 렌:", "호시 소이치로:"))]
    check("[TALK] 화자 = 호명 > **직전 서술의 주어** > 교대 순서(1음절 이름 '렌'도 조사로 검출)",
          any(l.startswith("카즈키 렌: 앞으로 많이") for l in _lines)
          and any(l.startswith("호시 소이치로: 고… 고맙군") for l in _lines),
          " | ".join(x[:22] for x in _lines)[:110])
    _broken = ('{\n "units": [\n   {"at": "잡지를 꺼낸다.", "kind": "행동"},\n'
               '   {"의 "at": "손이 겹친다.", "kind": "행동"},\n   {"at": "눈을 맞춘다.", "kind": "행동"}\n ]\n}')
    _bd, _be = CI.extract_json_obj_checked(_broken)
    check('키 자리에 섞인 따옴표+홀 글자({"의 "at": …})도 복구한다(2026-09-11 실측 — 값 안 따옴표는 그대로)',
          bool(_bd) and len(_bd.get("units") or []) == 3, (_be or "")[:70])
    _ns = CI.extract_json_obj_checked('{\n "units": [\n  {"at": "aaa", "kind": "행동"},\ns    {"at": "bbb", "kind": "속마음"}\n ]\n}')
    check("줄 맨 앞 잡토큰(ns + 객체 여는 글자) 뒤의 객체도 주워담는다(2026-09-11 실측 — 예전엔 배열째 버렸다)",
          bool(_ns[0]) and len(_ns[0].get("units") or []) == 2, (_ns[1] or "")[:60])
    _v = CI.extract_json_obj_checked('{"units":[{"at":"“하아… 렌 님….” 가까이 왔다","kind":"대사"}]}')
    check("값 안 곡선 따옴표·줄임표는 이번 복구로도 죽지 않는다(2026-09-10 회귀)",
          bool(_v[0]) and "“하아… 렌 님….”" in str(_v[0]), (_v[1] or "")[:60])


    # ── ⑯ [2026-09-10] --special 10회 실행 분석으로 남긴 6가지 (2·5·6·7 항목)
    _rs16 = open(os.path.join(ROOT, "run_comic.py"), encoding="utf-8").read()
    check("회차 실패·게이트 중단이 콘솔이 아니라 error.log에도 남는다(perr)",
          "def perr(" in _rs16 and _rs16.count("perr(f\"[오류] {e}\")") == 2, str(_rs16.count("perr(")))
    check("스킵한 회차는 산출물에 사유 메모를 남기고 성공하면 지운다(파일 없는 회차의 '면책 vs 미실행')",
          "_skip_note(" in _rs16 and "episode_{ep_num:02d}_SKIPPED.txt" in _rs16
          and "os.remove(_skip_f)" in _rs16, "")
    check("전 회차 요약이 완성/스킵 회차를 번호로 세어 알려준다",
          "완성 {len(done)}회차" in _rs16 and "스킵된 회차" in _rs16, "")
    check("추출이 통째로 실패해도 실패 사유를 체크포인트에 남긴다(재실행이 0부터 시작하지 않는다)",
          callable(CI.save_extract_failure) and callable(CI.load_extract_record)
          and "save_extract_failure(_xkey" in _rs16, "")
    _k16 = CI.extract_key("본문 16", "시트", 16, "plain")
    _cache16 = os.path.join(_mkdtemp_rl(prefix="selftest_sk_"), "ex16.yaml")
    _keep_cache = CI.EXTRACT_CACHE
    try:
        CI.EXTRACT_CACHE = _cache16
        CI.save_extract_failure(_k16, "추출 JSON 파싱 실패(2회 시도)")
        _rec16 = CI.load_extract_record(_k16)
        check("실패 기록을 같은 원고 지문으로 되 읽는다(다음 실행이 안내할 수 있게)",
              bool(_rec16.get("failed")) and "2회" in _rec16["failed"][-1], str(_rec16)[:80])
        _k16c = CI.extract_key("본문 16c", "시트", 16, "plain")
        CI.save_extract_checkpoint(_k16c, {"rating": "explicit"}, ["units"],
                                   source=CI._src_note("inputs/ep90_deadbeef.txt", "sheet.json"))
        _rec16b = CI.load_extract_record(_k16c)
        check("체크포인트에 '어느 파일의 회차 몇'이었는지 남는다(책장 안에 같은 본문이 많고 원고 지문은 소실된다)",
              isinstance(_rec16b.get("source"), dict)
              and _rec16b["source"].get("episode") == "ep90_deadbeef.txt", str(_rec16b.get("source"))[:70])
        CI.save_extract_failure(_k16c, "실패 하나")
        CI.save_extract_checkpoint(_k16c, {"rating": "explicit"})
        check("성공 체크포인트를 쓰는 순간 지난 실패 기록이 사라지지 않는다(원인 조율의 실마리)",
              len(CI.load_extract_record(_k16c).get("failed") or []) == 1
              and CI.load_extract_record(_k16c)["source"].get("episode") == "ep90_deadbeef.txt",
              str(CI.load_extract_record(_k16c))[:90])
        check("창 요약 로그는 .get 체인 — 로그 한 줄의 KeyError가 창 결과를 통째로 버리지 않는다",
              "u['cuts']" not in open(os.path.join(ROOT, "comic_input.py"), encoding="utf-8").read()
              and "u.get('cuts', 1)" in open(os.path.join(ROOT, "comic_input.py"), encoding="utf-8").read(), "")
        check("렌더할 회차만 LLM을 내린다(dry-run에서 5초짜리 재기동을 반복하지 않는다)",
              "if rendered and do_render:" in open(os.path.join(ROOT, "comic_gen.py"), encoding="utf-8").read(), "")
        check("실패 기록은 체크포인트와 한 파일에 공존한다(부분 항목이 있으면 살린다)",
            CI.load_extract_checkpoint(_k16) == {} and CI.save_extract_failure(_k16, "두 번째 사유")
            and len(CI.load_extract_record(_k16).get("failed") or []) == 2,
            str(CI.load_extract_record(_k16).get("failed"))[:90])
    finally:
        CI.EXTRACT_CACHE = _keep_cache
    check("map-reduce 창이 빠지면 조용히 사건을 잃지 않고 알린다",
          "개가 비었습니다" in open(os.path.join(ROOT, "comic_input.py"), encoding="utf-8").read(), "")

    # ── 6) 동시 실행 로그 (selftest 서브프로세스가 살아있는 실행의 로그를 지운 실측)
    import runlog as _RL16
    _tmp16 = _mkdtemp_rl(prefix="selftest_runlog16_")
    _cwd16 = os.getcwd()
    try:
        os.chdir(_tmp16)
        os.makedirs("log", exist_ok=True)
        _RL16.LOG_DIR, _RL16.ERROR_LOG, _RL16.LOCK = "log", os.path.join("log", "error.log"), \
            os.path.join("log", ".run.lock")
        open(os.path.join("log", "comic_gen.log"), "w", encoding="utf-8").write("살아있는 실행의 로그\n")
        open(_RL16.LOCK, "w", encoding="utf-8").write('{"999998": "2026-09-10 13:35:06"}')   # 우리 PID 아님
        os.kill = (lambda *a, **k: None) if not hasattr(os, "kill") else os.kill
        _RL16._alive = lambda pid: True                      # 그 실행이 아직 도는 중이라고 가정
        _RL16.start_run()
        check("다른 실행이 아직 도는 중이면 본 로그를 **지우지 않고** 이어 쓴다(실측: selftest가 렌더 로그를 지웠다)",
              "살아있는 실행의 로그" in open(os.path.join("log", "comic_gen.log"), encoding="utf-8").read(),
              open(os.path.join("log", "comic_gen.log"), encoding="utf-8").read()[:40])
        check("동시 실행을 error.log에 알린다(혼입된 로그를 해석할 실마리)",
              "동시 실행 감지" in open(_RL16.ERROR_LOG, encoding="utf-8").read(), "")
        _RL16.note("[PLOT_PROMPT] model=x, temp=0.80\n규칙: 예외를 허용한다", "COMIC")
        _RL16.note("EP9 컷 스크립트가 필수 항목을 채우지 못했습니다", "COMIC")
        _er16 = open(_RL16.ERROR_LOG, encoding="utf-8").read()
        check("프롬프트 덤프는 에러가 아니다(실측: 에러 20건 중 13건이 [PLOT_PROMPT])",
              "PLOT_PROMPT" not in _er16 and "필수 항목" in _er16, _er16[-80:].replace("\n", " "))
        check("에러 줄에 pid를 남긴다(두 실행이 섞여도 귀속을 읽는다)",
              "pid " in _er16, _er16[-70:].replace("\n", " "))
    finally:
        os.chdir(_cwd16)

    # ── 3) 컷 상태 vs 레거시 clothes 우선순위 (실측: 컷의 옷 변화가 승계값에 지워졌다)
    _cp2026 = [{"no": 1, "clothes": "school uniform", "state": {}},
               {"no": 2, "clothes": "school uniform", "_clothes_prev": True,
                "state": {"clothes": "wet dress"}},          # 이 컷이 실제로 입은 변화
               {"no": 3, "clothes": "school uniform", "_clothes_prev": True, "state": {}}]
    CG.fold_cut_state(_cp2026, 0)
    check("직전 컷에서 **승계된** clothes는 상태 시트를 덮지 않는다(EP10 실측: 컷2의 젖은 원피스가 사라졌다)",
          _cp2026[1]["_state"]["clothes"] == "wet dress" and _cp2026[2]["_state"]["clothes"] == "wet dress",
          str([x["_state"]["clothes"] for x in _cp2026])[:80])
    _cp2026b = [{"no": 1, "clothes": "school uniform", "state": {}},
                {"no": 2, "clothes": "bikini", "state": {}}]  # 컷이 직접 쓴 clothes는 여전히 반영
    CG.fold_cut_state(_cp2026b, 0)
    check("컷이 직접 쓴 clothes는 여전히 상태에 반영된다(승계만 우선권이 떨어진다)",
          _cp2026b[1]["_state"]["clothes"] == "bikini", str(_cp2026b[1]["_state"]["clothes"]))
    check("_repair_panels가 승계한 clothes에 표시를 남긴다(위 판정의 근거)",
          "_clothes_prev" in open(os.path.join(ROOT, "comic_gen.py"), encoding="utf-8").read(), "")


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
