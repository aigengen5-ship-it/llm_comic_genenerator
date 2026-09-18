#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
캐릭터 태그 프로브 — "태그를 빌리면 정말 같은 인물로 보이는가"를 실렌더로 잰다 (2026-09-16).

같은 시트·같은 시드·같은 4구도로 세 조건을 뽑는다:
  off          속성 태그만 (캐릭터 태그 없음)
  auto         시트 속성으로 고른 캐릭터 태그 1개
  auto_series  캐릭터 태그 + 작품(저작권) 태그 — 화풍이 그 작품으로 끌려가는지 보는 팔

그리고 VLM에게 4장을 한 번에 보여주고 "같은 인물인가"를 묻는다. 문장 재배열이 재현률 0.6pt였던
audit를 감안하면, 이 표가 캐릭터 태그를 켤지 말지의 유일한 근거가 된다.

  venv/bin/python analysis_chara/probe.py                      # 렌더 + 판정
  venv/bin/python analysis_chara/probe.py --render-only        # 판정 없이 뽑기만
  COMIC_COMFY_API=http://host:8080 venv/bin/python analysis_chara/probe.py --milf
"""
import argparse
import datetime
import json
import os
import re
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(HERE, "probe")

# 시트 두 벌 — prose(# 머리말 금지: #태그#로 오인된다)와 그 시트에서 나와야 할 JSON 속성.
#   실전에서는 이 JSON을 LLM이 만드는데, 프로브는 재현성을 위해 손으로 같은 스키마로 적는다.
SHEET_PROSE = """주인공은 아리무라 나쓰미. 스물여덟 살 여자 회사원이다.
머리는 갈색이고 허리까지 내려오며 옆으로 넘긴 앞머리가 있다. 눈은 갈색, 피부는 하얗다.
옷은 흰 블라우스에 감청 원피스, 차분한 구두다.
"""
SHEET = {
    "protagonist": {"name": "아리무라 나쓰미", "sex": "female", "hair_color": "brown hair",
                    "hair_style": "long hair, side bangs", "eye_color": "brown eyes",
                    "skin_color": "fair skin", "face_style": "smiling",
                    "clothes": "white blouse, blue dress", "body_shape": "slim", "job": "회사원",
                    "breasts_size": "medium breasts", "hip_size": "wide hips"},
    "partner": {"name": "상대", "sex": "male", "clothes": "shirt"},
    "guides": {"protagonist": ["책상에서 일한다", "창가로 부름을 받는다", "놀라 돌아본다", "진정한다"],
               "partner": [], "sub": []},
    "actions": [], "rating": "SAFE",
}
SHEET_MILF_PROSE = """주인공은 쿠로스 나츠요. 서른여덟 살 여자 의사다. 성숙한 체격에 기혼 여성이다.
머리는 검은색이고 목 아래로 내려오며 항상 단정하게 올려 묶는다. 눈은 갈색, 피부는 하얗다.
옷은 흰 가운에 검정 롱스커트, 얇은 프레임 안경이다.
"""
SHEET_MILF = {
    "protagonist": {"name": "쿠로스 나츠요", "sex": "female", "hair_color": "black hair",
                    "hair_style": "very long hair, updo", "eye_color": "brown eyes",
                    "skin_color": "fair skin", "face_style": "smiling",
                    "clothes": "white coat, black long skirt, glasses",
                    "body_shape": "mature female", "job": "의사",
                    "breasts_size": "large breasts", "hip_size": "wide hips"},
    "partner": {"name": "상대", "sex": "male", "clothes": "shirt"},
    "guides": {"protagonist": ["진료실에서 서류를 본다", "창밖을 본다", "놀라 돌아본다", "미소 짓는다"],
               "partner": [], "sub": []},
    "actions": [], "rating": "SAFE",
}
SHEET_HARD_PROSE = """주인공은 사쿠라기 아야. 마흔둘 여자 변호사다. 기혼에 두 아이를 키운다.
머리는 검은색 웨이브가 어깨 아래로 흘러내리고, 늘 얇은 금테 안경을 쓴다. 눈은 검은색에 가깝고 피부는 하얗다.
옷은 회양복 재킷에 블라우스, 진주 귀걸이.
"""
SHEET_HARD = {
    "protagonist": {"name": "사쿠라기 아야", "sex": "female", "hair_color": "black hair",
                    "hair_style": "long hair, wavy hair, glasses", "eye_color": "brown eyes",
                    "skin_color": "fair skin", "face_style": "smiling",
                    "clothes": "grey business suit, blouse, pearl earrings",
                    "body_shape": "mature female", "job": "변호사",
                    "breasts_size": "large breasts", "hip_size": "wide hips"},
    "partner": {"name": "상대", "sex": "male", "clothes": "shirt"},
    "guides": {"protagonist": ["서류를 본다", "창가로 간다", "놀라 돌아본다", "미소 짓는다"],
               "partner": [], "sub": []},
    "actions": [], "rating": "SAFE",
}

# 성숙/노년 시트의 '너무 어리지 않은가' 마지노선 — 러브코미디 화풍은 원래 젊게 나오므로
# 30대 중반을 30대로 뽑자는 기준이 아니라, 20대 중반 이하로 밀리면政策이 죽었다고 본다.
AGE_FLOOR = {"plain": 0, "milf": 25, "hard": 25}

EYE_ASK = """각 그림에서 주인공의 **눈동자 색**만 보세요. 그림 {n}장을 순서대로 봅니다.
조명·음영이 아니라 문양 자체의 색을 봅니다.
모두 같은 색이면 그 색을 하나만 쓰세요. 다르면 순서대로 쉼표로 {n}개 쓰세요.
색은 갈색/파랑/초록/보라/주황/빨강/회색/검은 중 하나로 답합니다.
답은 반드시:
EYES: 색[, 색…]"""

AGE_ASK = """각 그림의 주인공 여성이 몇 살로 보이는지 추정하세요. 그림 {n}장을 순서대로 봅니다.
나이는 실제 나이대보다 젊게 그려지기 쉬우니, 화풍이 어려 보여도 얼굴·몸·차림새의 나이대를 보세요.
답은 반드시 이 형식만:
AGE: 숫자, 숫자, … ({n}개, 쉼표 구분, 세는 단위 없이)
NOTE: 18자 안쪽으로 인상 한 마디"""

# 4구도 — 얼굴·상반신·전신·측면 (구도가 바뀌어도 같은 인물이어야 한다)
PANELS = [
    {"no": 1, "type": "face", "caption_ko": "", "dialog": [], "lines": [], "sfx": "",
     "pose": "She looks at the viewer with a calm smile.",
     "camera": "front_view", "position": "NONE", "climax": "", "facing": "front",
     "wide": False, "clothes": "", "emotion": "", "_state": None},
    {"no": 2, "type": "action", "caption_ko": "", "dialog": [], "lines": [], "sfx": "",
     "pose": "She is reading a book at a wooden desk, upper body.",
     "camera": "front_view", "position": "NONE", "climax": "", "facing": "front",
     "wide": False, "clothes": "", "emotion": "", "_state": None},
    {"no": 3, "type": "action", "caption_ko": "", "dialog": [], "lines": [], "sfx": "",
     "pose": "She is standing by a tall window, full body.",
     "camera": "front_view", "position": "NONE", "climax": "", "facing": "front",
     "wide": False, "clothes": "", "emotion": "", "_state": None},
    {"no": 4, "type": "action", "caption_ko": "", "dialog": [], "lines": [], "sfx": "",
     "pose": "She is turning her head to the side, surprised.",
     "camera": "side_view", "position": "NONE", "climax": "", "facing": "right",
     "wide": False, "clothes": "", "emotion": "", "_state": None},
]

JUDGE_MULTI = """이 {n}장의 그림은 모두 **같은 한 명의 인물**을 그린 것입니까?
첫 장을 기준 삼아, 나머지 장들이 같은 인물로 보이는지 보세요. 판단 재료는 머리색·머리 길이·
앞머리 모양·눈동자 색·나이대·분위기입니다. 배경·구도·의상이 다른 것은 같은 인물 판정에 불리하게 보지 마세요.
답은 반드시 이 형식으로:
SAME: YES 또는 SAME: NO
LOOK: 머리색/눈동자/나이대를 20자 안쪽으로
WHY: 근거 한 줄"""

# 같은 컷을 **시드 두 번** 뽑아 비교한다 — 회차가 바뀌면 시드도 바뀌므로, 이것이
# "회차를 넘어 같은 얼굴인가"의 대리 시험이다. (구도를 섞으면 구도 차이가 노이즈가 된다)
JUDGE_PAIR = """이 두 장은 같은 한 인물을 그린 것입니까? 같은 시드에 다시 생성한 두 장입니다.
판단 재료는 얼굴형·머리색·앞머리 모양·눈동자 색·나이대입니다. 배경/구도 차이는 보지 마세요.
답은 반드시:
SAME: YES 또는 SAME: NO
SAME_HAIR: YES/NO     SAME_EYES: YES/NO     SAME_AGE: YES/NO
SIM: 0~10 정수 (10 = 같은 인물 확정, 5 = 닮긴 했지만 남, 0 = 완전히 다른 사람)
WHY: 근거 한 줄"""


def log(m):
    print(time.strftime("%H:%M:%S ") + m, flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=118800)
    ap.add_argument("--milf", action="store_true", help="성숙/미시 시트로 돌린다")
    ap.add_argument("--hard", action="store_true", help="흑발·안경·성숙(속성으로 덜 잡히는 생김새)으로 돌린다")
    ap.add_argument("--arms", default="off,voice,auto",
                    help="off(기준선) / voice(나이대 발화만) / voice_prose / voice_tag / auto(캐릭터 태그) /"
                         " auto_series / eye(눈 색 태그 기본) / eye_plain(무가중) / eye_w18 / eye_w25 / eye_prose")
    ap.add_argument("--render-only", action="store_true")
    ap.add_argument("--keep", action="store_true", help="image/ 에 두고 복사하지 않음")
    ap.add_argument("--wait", type=int, default=300, help="ComfyUI 기동 대기 초")
    ap.add_argument("--prefix", default="", help="결과 파일 접두어(예: milf_) — 두 시트를 한 폴더에 둘 때")
    ap.add_argument("--reps", type=int, default=1, help="교차-시드 쌍을 여러 번 만들어 평균냅니다")
    ap.add_argument("--noun", default="", help="나이대 산문 문구를 덮어씁니다(A/B용): 'mature=문구,elder=문구'")
    ap.add_argument("--judge-only", action="store_true",
                    help="렌더를 건너뛰고 이미 뽑힌 그림으로 판정·인지 나이만 다시 합니다")
    ap.add_argument("--no-cross", action="store_true",
                    help="교차-시드 검사(같은 컷을 시드 두 번)를 건너뜁니다 — 회차를 넘어 얼굴이 유지되는지의 대리 시험")
    args = ap.parse_args()

    os.chdir(ROOT)
    sys.path.insert(0, ROOT)
    import config
    import anima_gen
    import comic_gen as CG
    import comic_input as CI

    os.makedirs(OUT, exist_ok=True)
    sheet, data = (SHEET_HARD if args.hard else (SHEET_MILF if args.milf else SHEET)), \
                  (SHEET_HARD if args.hard else (SHEET_MILF if args.milf else SHEET))
    prose = (SHEET_HARD_PROSE if args.hard else (SHEET_MILF_PROSE if args.milf else SHEET_PROSE))
    data = dict(data)
    CI.apply_to_config(data, "probe body. " * 120, prose, ep_num=1, panels_per_page=4, book_num=1)
    CG.load_cut_templates()
    auto_tags = list(config.char_tags or [])
    picked = auto_tags[0] if auto_tags else ""

    db = {}
    try:
        import chara_match
        db = chara_match.load_db().get(picked, {}) or {}
    except Exception:
        pass
    series = str(db.get("series") or "")
    log(f"시트 → 자동 선택: {picked or '(없음)'}  작품 태그: {series or '(없음)'}")
    if args.noun:
        for kv in args.noun.split(","):
            k, _, v = kv.partition("=")
            if k.strip() and v:
                anima_gen.AGE_VOICE_NOUN[k.strip()] = v.strip()
        log(f"문구 덮어쓰기: {anima_gen.AGE_VOICE_NOUN}")

    json_value = config.get_json_value()          # plot.json + 로컬 오버레이가 다 반영된 값
    if not args.render_only:
        import run_comic
        if not run_comic.start_comfyui(wait_seconds=int(args.wait)):
            log("ComfyUI를 켤 수 없어 렌더만 건너뜁니다 (--render-only 로 프롬프트 확인은 가능)")
            return 2
    # 팔 = (캐릭터 태그 목록, 나이대 발화 정책 (산문, 태그))
    eyes = {}
    arms, voice = {}, {}
    for a in str(args.arms).split(","):
        a = a.strip()
        if not a:
            continue
        if a == "off":
            arms[a], voice[a], eyes[a] = [], (False, False), dict(enable=False)
        elif a.startswith("eye"):
            arms[a], voice[a] = ([picked] if picked else []), (True, True)
            if a == "eye_plain":
                eyes[a] = dict(weight=0, prose=False, enable=True)
            elif a == "eye_w18":
                eyes[a] = dict(weight=1.8, prose=False, enable=True)
            elif a == "eye_w25":
                eyes[a] = dict(weight=2.5, prose=False, enable=True)
            else:                                    # eye(기본 1.4) / eye_prose
                eyes[a] = dict(weight=1.4, prose=(a == "eye_prose"), enable=True)
        elif a == "voice":
            arms[a], voice[a] = [], (True, True)
        elif a == "voice_prose":
            arms[a], voice[a] = [], (True, False)
        elif a == "voice_tag":
            arms[a], voice[a] = [], (False, True)
        elif a == "auto":
            arms[a], voice[a] = ([picked] if picked else []), (True, True)
        elif a == "auto_series":
            arms[a], voice[a] = (([picked] if picked else []) + ([series] if series else [])), (True, True)

    band = "hard" if args.hard else ("milf" if args.milf else "plain")
    mf = os.path.join(OUT, "manifest" + (("_" + args.prefix.rstrip("_")) if args.prefix else "") + ".json")
    manifest = {"generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
                "seed": args.seed, "picked": picked, "series": series, "arms": {}}
    if args.judge_only and os.path.isfile(mf):
        manifest = json.load(open(mf, encoding="utf-8"))
        for arm in list(arms):
            arms[arm] = [r for r in manifest.get("arms", {}).get(arm, [])]
            arms[arm] = ([picked] if picked and arm in ("auto", "auto_series") else [])
        log("렌더 생략 — 기존 그림으로 판정만 합니다")
    for arm, tags in arms.items():
        if args.judge_only:
            continue
        config.char_tags = list(tags)
        anima_gen.set_age_voice(*(voice.get(arm) or (True, True)))
        anima_gen.set_eye_voice(**(eyes.get(arm) or dict(weight=1.4, prose=False, enable=True)))
        rows = []
        for i, base in enumerate(PANELS):
            panel = dict(base)
            panel["_neg_extra"] = ""
            seed = args.seed + i            # 구도마다 같은 시드 → 팔 간 차이는 태그뿐
            prompt = CG.build_panel_prompt(0, panel, "safe")
            t0 = time.time()
            path = CG.render_panel(0, panel, seed, "safe", json_value, prompt=prompt)
            if not path:
                log(f"  {arm:12s} 컷{i+1} 렌더 실패")
                continue
            dest = os.path.join(OUT, f"{args.prefix}{arm}_{i+1}.png")
            if not args.keep:
                shutil.copy(path, dest)
            # 시트/설정의 태그는 최종 프롬프트에서 `_`→공백이 된다(_sanitize_prompt_underscores)
            # → 검사도 두 표기 모두 본다. 없으면 진짜 빠진 것이다.
            _want = picked.replace("_", " ") if picked else ""
            _in = bool(_want) and (_want in prompt or picked in prompt)
            rows.append({"no": i + 1, "seed": seed, "png": dest, "src": path,
                         "chars_in_prompt": _in, "prompt": prompt})
            log(f"  {arm:12s} 컷{i+1} {os.path.basename(dest)} ({time.time()-t0:.0f}s) "
                f"태그포함={'O' if rows[-1]['chars_in_prompt'] else '-'}")
        manifest["arms"][arm] = rows
        json.dump(manifest, open(mf, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    if args.render_only:
        log("렌더만 끝냈습니다 (판정 생략)")
        return 0

    # ── 교차-시드 검사: 같은 컷을 시드 두 번 → 회차를 넘어 얼굴이 유지되는가의 대리 시험
    cross = {}
    if not args.no_cross:
        import image_eval as IE0
        for arm, tags in arms.items():
            config.char_tags = list(tags)
            anima_gen.set_age_voice(*(voice.get(arm) or (True, True)))
            anima_gen.set_eye_voice(**(eyes.get(arm) or dict(weight=1.4, prose=False, enable=True)))
            votes = []
            for rep in range(max(1, args.reps)):
              pair = []
              for k, sd in enumerate((args.seed + 900 + rep * 7000, args.seed + 1900 + rep * 7000)):
                panel = dict(PANELS[1])
                panel["_neg_extra"] = ""
                pr = CG.build_panel_prompt(0, panel, "safe")
                pth = CG.render_panel(0, panel, sd, "safe", json_value, prompt=pr)
                if not pth:
                    continue
                dest = os.path.join(OUT, f"{args.prefix}{arm}_x{rep+1}_{k+1}.png")
                if not args.keep:
                    shutil.copy(pth, dest)
                pair.append(dest)
              if len(pair) < 2:
                continue
              msg = [{"role": "user", "content": [{"type": "text", "text": JUDGE_PAIR}]}]
              for f in pair:
                msg[0]["content"].append({"type": "image_url",
                                          "image_url": {"url": IE0.image_data_url(f)}})
              try:
                  ans = IE0.ask_image_raw(msg) or "(응답 없음)"
              except Exception as e:
                  ans = f"판정 실패: {e}"
              up = ans.upper()
              def grab(key):
                  return ("YES" if f"{key}: YES" in up else ("NO" if f"{key}: NO" in up else "?"))
              import re as _re
              m = _re.search(r"SIM[^0-9]{0,4}([0-9]{1,2})", up)
              sim = max(0, min(10, int(m.group(1)))) if m else None
              votes.append({"same": grab("SAME"), "hair": grab("SAME_HAIR"), "eyes": grab("SAME_EYES"),
                            "age": grab("SAME_AGE"), "sim": sim, "raw": ans, "pngs": pair})
            yes = sum(1 for v in votes if v["same"] == "YES")
            sims = [v["sim"] for v in votes if v["sim"] is not None]
            cross[arm] = {"votes": votes, "same_yes": yes, "reps": len(votes),
                          "sim_avg": round(sum(sims) / len(sims), 1) if sims else None,
                          "hair_yes": sum(1 for v in votes if v["hair"] == "YES"),
                          "eyes_yes": sum(1 for v in votes if v["eyes"] == "YES")}
            log(f"  교차-시드 {arm:12s} 같은 인물 {yes}/{len(votes)} · 유사도 평균 "
                f"{cross[arm]['sim_avg']} · 머리 {cross[arm]['hair_yes']}/{len(votes)} · 눈 {cross[arm]['eyes_yes']}/{len(votes)}")
        manifest["cross_seed"] = cross
        json.dump(manifest, open(mf, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    import image_eval as IE
    import re as _re2

    def perceived_age(rows, tag):
        """그림 무더기를 VLM에 보여주고 인지 나이를 받는다 → (list, 평균)"""
        imgs = [r["png"] for r in rows if os.path.isfile(r.get("png", ""))]
        if len(imgs) < 2:
            return [], None
        msg = [{"role": "user", "content": [{"type": "text", "text": AGE_ASK.format(n=len(imgs))}]}]
        for p_ in imgs:
            msg[0]["content"].append({"type": "image_url", "image_url": {"url": IE.image_data_url(p_)}})
        try:
            ans = IE.ask_image_raw(msg) or ""
        except Exception as e:
            log(f"  인지 나이 판정 실패({tag}): {e}")
            return [], None
        m = _re2.search(r"AGE[^0-9]{0,8}([0-9][0-9,\s]*)", ans.upper())
        ages = [int(x) for x in _re2.findall(r"\d+", m.group(1))][:len(imgs)] if m else []
        ages = [a for a in ages if 8 <= a <= 90]
        return ages, (round(sum(ages) / len(ages), 1) if ages else None)

    # 눈 색: 시트가 요구한 색과 컷마다 같은 색이 나왔는지 (이 항목은政策 도입 전 아예 미주입이었다)
    want_eye = str((SHEET_MILF if args.milf else (SHEET_HARD if args.hard else SHEET))
                   ["protagonist"].get("eye_color") or "").split()[0]
    KO = {"brown": "갈색", "blue": "파랑", "green": "초록", "purple": "보라", "red": "빨강",
          "grey": "회색", "gray": "회색", "black": "검은", "amber": "주황", "yellow": "주황"}
    want_ko = KO.get(want_eye.lower(), want_eye)
    for arm, rows in manifest.get("arms", {}).items():
        imgs = [r["png"] for r in rows if os.path.isfile(r.get("png", ""))]
        if len(imgs) < 2:
            continue
        msg = [{"role": "user", "content": [{"type": "text", "text": EYE_ASK.format(n=len(imgs))}]}]
        for p_ in imgs:
            msg[0]["content"].append({"type": "image_url", "image_url": {"url": IE.image_data_url(p_)}})
        try:
            ans = IE.ask_image_raw(msg) or ""
        except Exception as e:
            ans = ""
        m2 = re.search(r"EYES[^\n]*[:\s]+(.*)", ans, flags=re.I)
        seen = [x.strip() for x in re.split(r"[,，]", m2.group(1)) if x.strip()][:len(imgs)] if m2 else []
        seen = [s_ for s_ in seen if s_][:len(imgs)]
        if len(seen) == 1:                       # "모두 같으면 하나만" 이라고 했으므로 전부로 간주
            seen = seen * len(imgs)
        hit = sum(1 for x in seen if want_ko in x)
        manifest.setdefault("eye_color", {})[arm] = {"asked": want_eye, "want_ko": want_ko,
                                                   "seen": seen, "hit": hit, "of": len(imgs)}
        log(f"  눈 색 {arm:12s} 요구 {want_eye}({want_ko}) → 본 것 {seen} 적중 {hit}/{len(imgs)}")

    floor = AGE_FLOOR.get(band, 0)
    for arm, rows in manifest.get("arms", {}).items():
        ages, avg = perceived_age(rows, arm)
        if avg is None:
            continue
        verdict = "PASS" if avg >= floor else "FAIL"
        manifest.setdefault("perceived_age", {})[arm] = {"ages": ages, "avg": avg,
                                                        "floor": floor, "verdict": verdict}
        log(f"  인지 나이 {arm:12s} {ages} 평균 {avg}세 · 마지노선 {floor}세 → {verdict}"
            + ("" if floor else " (이 시트는 나이대 요구 없음)"))
    json.dump(manifest, open(mf, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    log("VLM 판정 시작")
    for arm, rows in manifest["arms"].items():
        if len(rows) < 2:
            continue
        msg = [{"role": "user", "content": [{"type": "text", "text": JUDGE_MULTI.format(n=len(rows))}]}]
        for r in rows:
            msg[0]["content"].append({"type": "image_url",
                                      "image_url": {"url": IE.image_data_url(r["png"])}})
        try:
            ans = IE.ask_image_raw(msg) or "(응답 없음)"
        except Exception as e:
            ans = f"판정 실패: {e}"
        manifest.setdefault("judge", {})[arm] = ans
        same = "YES" if "SAME: YES" in ans.upper() else ("NO" if "SAME: NO" in ans.upper() else "?")
        look = [l for l in ans.splitlines() if l.strip().upper().startswith("LOOK")]
        why = [l for l in ans.splitlines() if l.strip().upper().startswith("WHY")]
        log(f"  {arm:12s} 같은 인물: {same:>3}  {look[0][:46] if look else ''} "
            f"｜ {why[0][:44] if why else ''}")
        json.dump(manifest, open(mf, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n결과: {mf} — 그림은 {OUT}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
