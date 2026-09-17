# -*- coding: utf-8 -*-
"""268장 전 페이지를 비전 LLM으로 훑어 컷 구조를 JSON으로 뽑는다(재개 지원, 6스레드)."""
import glob, json, os, sys, threading, time
import urllib.request
import vlm

DIR = "/run/media/chrisyeo/AIDATA/AI/temp/182856022"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vlm.jsonl")
LOCK = threading.Lock()
WORKERS = int(os.environ.get("WORKERS", "6"))

SYS = ("너는 만화(일본식 코마) 페이지 레이아웃을 수치로 읽어내는 분석가다. "
       "이미지 한 장만 보고 요청 규격에 따라 **JSON 하나만** 출력한다. "
       "markdown 코드펜스·설명문·해설을 붙이지 않는다. 숫자는 페이지 전체를 기준으로 한 비율(0~1)이다.")

PROMPT = """이 만화 페이지의 **컷(파널) 레이아웃**만 분석해서 아래 규격의 JSON 하나로 답하세요.

용어
- 행(tier): 세로로 쌓인 가로 띠. 위에서 아래 순서.
- 칸(cell): 한 행 안에서 왼쪽→오른쪽 순서로 나뉜 컷.
- 가터: 컷 사이/주위의 여백 띠.
- 타치키리: 컷이 페이지 면 끝까지 여백 없이 밀려 있음.

규격
{
 "n_panel": 총 컷 수(정수),
 "n_tier": 행 수(정수),
 "tiers": [                      // 위→아래 순서
   {"h": 행이 페이지 세로에서 차지하는 비율(0~1, 행들 합 ≈ 1),
    "cells": [                   // 왼쪽→오른쪽 순서
      {"w": 칸이 페이지 가로에서 차지하는 비율(한 행 안 합 ≈ 1),
       "shot": "full_body(머리~발끝) | upper_body(허리~위) | bust(가슴~위) | closeup(눈·입·손 등 부분) | scenery(인물 없는 배경/풍경) | object(소품만) | crowd(3인 이상)",
       "angle": "eye_level | low(낮은 카메라) | high(높은 카메라/조감) | pov(1인칭) | dutch(기울어진 구도)",
       "chars": 화면 속 인물 수(0~9),
       "focus": "화면 주제를 8-word 이내 한국어 명사구",
       "tachikiri": 이 칸이 페이지 외곽 면에 붙어 여백이 있는지 true/false,
       "text": "dialog(대사풍선) | mono(독백·내레이션 박스) | sfx(의성어/효과문자) | none(무자막)",
       "bg": "detailed(배경 정밀) | simple(단순/스크린톤) | black(흑백 면 처리) | tone(그라데이션·효과선)"
      }
    ]}
 ],
 "gutter": "white | black | mixed",
 "density": "low(1~2컷) | mid(3~4컷) | high(5컷 이상)",
 "page_role": "establishing(장소·상황 제시) | dialogue(대화 진행) | action(행동·사건) | climax(가장 높은 텐션) | reaction(표정·리액션 위주) | transition(시간·장면 전환) | title(표지·타이틀·텍스트만)",
 "notes": "레이아웃 관점에서 한 줄(30자 이내)"
}

주의
· 컷 경계는 가터(흰 띠 또는 검은 띠)로 판단하세요. 컷 내부의 검은 배경과 혼동하지 마세요.
· 사선(대각선)으로 나뉜 컷은 두 칸으로 계산하고 notes 에 '사선' 이라 적으세요.
· 한 컷이 행 2개를 세로로 차지하는처럼 복잡하면, 더 크게 보이는 쪽 행에 소속시키고 notes 에 적으세요.
· 그림만 있는 컷은 text: "none", 인물 없는 풍경 컷은 shot: "scenery", chars: 0."""

def call(path, max_side=1024, max_tokens=1600):
    content = [{"type": "text", "text": PROMPT},
               {"type": "image_url", "image_url": {"url": vlm.data_url(path, max_side)}}]
    body = json.dumps({"model": vlm.MODEL,
                       "messages": [{"role": "system", "content": SYS},
                                    {"role": "user", "content": content}],
                       "max_tokens": max_tokens, "temperature": 0.0,
                       "chat_template_kwargs": {"enable_thinking": False}}).encode()
    req = urllib.request.Request(vlm.BASE + "/chat/completions", data=body,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + vlm.KEY})
    with urllib.request.urlopen(req, timeout=300) as r:
        d = json.loads(r.read().decode())
    m = d["choices"][0]["message"]
    return (m.get("content") or m.get("reasoning") or "").strip()

def parse(txt):
    s = txt.find("{"); e = txt.rfind("}")
    if s < 0 or e < 0:
        return None
    try:
        return json.loads(txt[s:e + 1])
    except Exception:
        return None

def main():
    fs = sorted(glob.glob(os.path.join(DIR, "*.png")))
    done = set()
    if os.path.exists(OUT):
        for ln in open(OUT, encoding="utf-8"):
            try:
                done.add(json.loads(ln)["file"])
            except Exception:
                pass
    todo = [f for f in fs if os.path.basename(f) not in done]
    print(f"total={len(fs)} done={len(done)} todo={len(todo)}", flush=True)
    fh = open(OUT, "a", encoding="utf-8")
    _ctr = {"n": 0, "err": 0}

    def work(f):
        name = os.path.basename(f)
        for attempt in range(3):
            try:
                txt = call(f)
                j = parse(txt)
                if j is None:
                    raise ValueError("unparsed:" + txt[:120].replace("\n", " "))
                rec = {"file": name, "raw_ok": True, "vlm": j}
            except Exception as e:
                if attempt == 2:
                    rec = {"file": name, "raw_ok": False, "error": str(e)[:200]}
                    with LOCK:
                        _ctr["err"] += 1
                else:
                    time.sleep(2 + 3 * attempt); continue
            with LOCK:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n"); fh.flush()
                _ctr["n"] += 1
                if _ctr["n"] % 20 == 0:
                    print(f"  {_ctr['n']}/{len(todo)} (err {_ctr['err']})", flush=True)

    idx = list(range(len(todo))); pos = [0]
    def runner():
        while True:
            with LOCK:
                if pos[0] >= len(idx):
                    return
                i = idx[pos[0]]; pos[0] += 1
            work(todo[i])
    ts = [threading.Thread(target=runner) for _ in range(WORKERS)]
    [t.start() for t in ts]; [t.join() for t in ts]
    fh.close()
    print("FINISHED", _ctr)

main()
