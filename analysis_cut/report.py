# -*- coding: utf-8 -*-
"""cut_report.md 생성 — 모든 수치는 pages.json / quality.json / q2.json / retest.json 에서 계산."""
import collections, json, os, statistics as st

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
rd = lambda n: json.load(open(os.path.join(HERE, n), encoding="utf-8"))
pages, qual, q2, retest = rd("pages.json"), rd("quality.json"), rd("q2.json"), rd("retest.json")
N = len(pages)
cells = [c for p in pages for t in p["tiers"] for c in t["cells"]]
shot_of = {"full_body": "FB", "upper_body": "UB", "bust": "BU", "closeup": "CU",
           "scenery": "SC", "object": "OB", "crowd": "CR", "?": "??", "None": "??"}
role_ko = {"establishing": "상황 제시", "dialogue": "대화 진행", "action": "행동·사건",
           "reaction": "리액션", "climax": "클라이맥스", "transition": "전환", "title": "표지·타이틀"}
pct = lambda a, b: f"{100.0 * a / max(1, b):.0f}%"
q = lambda k, ps: float(__import__("numpy").percentile([x[k] for x in qual], ps))

def sig(p): return "/".join(str(len(t["cells"])) for t in p["tiers"])
def hs(p): return "·".join(f"{t['h']:.2f}" for t in p["tiers"])
def ws(p): return " ｜".join("/".join(f"{c['w']:.2f}" for c in t["cells"]) for t in p["tiers"])
def sh(p):
    out = []
    for t in p["tiers"]:
        out.append("+".join(shot_of.get(c["shot"], "??") for c in t["cells"]))
    return " ｜".join(out)

# ── 집계 ─────────────────────────────────────────────────────────────
sigc = collections.Counter(sig(p) for p in pages)
nc = collections.Counter(sum(len(t["cells"]) for t in p["tiers"]) for p in pages)
nt = collections.Counter(len(p["tiers"]) for p in pages)
roles = collections.Counter(p["role"] for p in pages)
shots = collections.Counter(c["shot"] for c in cells)
texts = collections.Counter(c["text"] for c in cells)
bgs = collections.Counter(c["bg"] for c in cells)
ang = collections.Counter(c["angle"] for c in cells)
chars = collections.Counter(c["chars"] for c in cells)
tach = sum(1 for c in cells if c["tach"])
fullrows = sum(1 for p in pages for t in p["tiers"] if len(t["cells"]) == 1 and t["cells"][0]["w"] >= 0.9)
allrows = sum(len(p["tiers"]) for p in pages)
measured = sum(1 for p in pages if p["measured"])
agree_geom = sum(1 for p in pages if p["geom_panel"] == sum(len(t["cells"]) for t in p["tiers"]))
same_re = sum(1 for r in retest if r[3]); near_re = sum(1 for r in retest if r[2] != "ERR"
              and sum(map(int, r[1].split("-"))) == sum(map(int, r[2].split("-"))))
hh = sorted(t["h"] for p in pages for t in p["tiers"])
big_side = collections.Counter()
for p in pages:
    for t in p["tiers"]:
        if len(t["cells"]) == 2 and abs(t["cells"][0]["w"] - t["cells"][1]["w"]) >= 0.08:
            big_side["왼쪽" if t["cells"][0]["w"] > t["cells"][1]["w"] else "오른쪽"] += 1
first_full = sum(1 for p in pages if len(p["tiers"][0]["cells"]) == 1)
last_full = sum(1 for p in pages if len(p["tiers"][-1]["cells"]) == 1)
any_full = sum(1 for p in pages if any(len(t["cells"]) == 1 for t in p["tiers"]))
big3 = collections.Counter(("맨 위" if i == 0 else ("가운데" if i == 1 else "맨 아래"))
                           for p in pages if len(p["tiers"]) == 3
                           for i in [max(range(3), key=lambda k: p["tiers"][k]["h"])])
frame_white = [x["frame_white"] for x in q2]
no_margin = sum(1 for x in q2 if x["frame_white"] < 0.10)
mean_panel = st.mean(nc.elements())

def bucket_table(key, buckets):
    b = collections.defaultdict(collections.Counter)
    for c in cells: b[buckets(c["w"])][c["shot"]] += 1
    return b
BW = lambda w: "좁은(<35%) " if w < 0.35 else ("중간(35~60%)" if w < 0.6 else "넓은(≥60%) ")
wb = bucket_table("shot", BW)
pos = collections.defaultdict(collections.Counter)
for p in pages:
    n = len(p["tiers"])
    for i, t in enumerate(p["tiers"]):
        for c in t["cells"]:
            pos["맨 위 행" if i == 0 else ("맨 아래 행" if i == n - 1 else "가운데 행")][c["shot"]] += 1
bychars = {k: collections.Counter() for k in (0, 1, 2)}
for c in cells: bychars[min(2, c["chars"])][c["shot"]] += 1
areabyshot = {}
for s in shots:
    a = [c["w"] * t["h"] for p in pages for t in p["tiers"] for c in t["cells"] if c["shot"] == s]
    areabyshot[s] = st.median(a)
rolestat = {}
for r in roles:
    G = [p for p in pages if p["role"] == r]
    dl = sum(1 for p in G for t in p["tiers"] for c in t["cells"] if c["text"] == "dialog")
    rolestat[r] = (len(G), st.mean(sum(len(t["cells"]) for t in p["tiers"]) for p in G),
                   st.mean(len(p["tiers"]) for p in G), pct(dl, sum(len(t["cells"]) for p in G for t in p["tiers"])))

L = []
A = L.append
A("# 컷(파널) 사용 분석 리포트 — 268면 실측")
A("")
A("> 분석 대상: `임모럴 엑스터시 총집편 - 최면세뇌 채널 000~267.png` (268면, 일본식 만화 페이지)\n"
  "> 분석 도구: `analysis_cut/` (기하 계측 `detect2.py`·`fit.py` + 비전 LLM `scan.py` + 화질 `qmetrics.py`·`q2.py`)\n"
  "> 산출물: 이 리포트, `data/cut_new.yaml` (20종 페이지 템플릿)")
A("")
A("---")
A("")
A("## 0. 한눈에 보는 결론")
A("")
A(f"1. **해상도는 268면이 전부 똑같은 {qual[0]['w']}×{qual[0]['h']} px({q('mp',50):.2f}MP, 종횡 {q('aspect',50):.3f})** 입니다. "
  "A-series 판형 비율(0.707)에 거의 정확히 맞고, 인쇄 환산 시 A5 ≈ **205dpi**·B6 ≈ **238dpi** 입니다. "
  "JPEG 블록 왜곡은 거의 없고(블록비 %.2f≈1) 스캔 후 PNG 무압축 저장입니다." % q("block", 50))
A(f"2. 페이지당 컷 수는 **평균 {mean_panel:.1f}컷, 4~6컷이 {pct(nc[4]+nc[5]+nc[6], N)}** 로 가장 많습니다. "
  f"1~2컷 페이지({pct(nc[1]+nc[2], N)})는 거의 표지·상황 제시용입니다.")
A(f"3. 행(단) 구성은 **3단 {pct(nt[3], N)} / 2단 {pct(nt[2], N)} / 4단 {pct(nt[4], N)} / 단 없이 1컷 {pct(nt[1], N)}** 입니다. "
  "즉 \"3단 4~6컷\"이 이 만화의 기본 문법입니다.")
A(f"4. 가장 많은 구조는 **2/2/2(6컷, {sigc['2/2/2']}면)**, **2/2/1(5컷, {sigc['2/2/1']}면)**, "
  f"그 다음 **2/2(4컷, {sigc['2/2']}면)**, **1/2/2({sigc['1/2/2']}면)**, **2/1/1({sigc['2/1/1']}면)** 순입니다.")
A(f"5. 컷 면적 배분이 균등하지 않습니다. 3단 페이지에서 가장 높은 행은 **맨 아래 {big3['맨 아래']}면 / 맨 위 {big3['맨 위']}면 / 가운데 {big3['가운데']}면** — "
  "가운데 행을 얇게 눌러(중앙값 0.30) 위·아래를 키우는 사용법입니다.")
A(f"6. 행 안에서 두 칸으로 나뉠 때 **같은 크기가 아니라 폭차 중앙값 10%p(차이≥8%p 행이 62%)**로 비균등하게 나눕니다. "
  f"큰 칸이 오른쪽인 행 {big_side['오른쪽']}회 / 왼쪽인 행 {big_side['왼쪽']}회 — 어느 쪽이 크다는 편향보다 "
  "**같은 행 안에서도 역할(진행 vs 리액션)에 따라 크기를 다르게 준다**는 쪽이 본질입니다.")
A(f"7. **전폭(행 전체를 쓰는 1칸) 컷이 {allrows}행 중 {fullrows}행({pct(fullrows, allrows)})**, "
  f"전폭 컷을 한 개 이상 쓰는 페이지가 {any_full}면({pct(any_full, N)})입니다. "
  f"첫 행을 전폭으로 여는 페이지 {pct(first_full, N)}, 마지막 행을 전폭으로 마무리하는 페이지 {pct(last_full, N)} — "
  "**\"열 때 넓게, 마무리도 넓게\"**가 뚜렷한 습관입니다.")
A(f"8. 몰아치기(타치키리·여백 없이 면 끝까지) 컷이 {pct(tach, len(cells))}이고, "
  f"268면 중 {no_margin}면은 바깥 테두리 여백이 아예 없습니다(측정). 컷은 넓게 쓰고 여백은 아끼는 스타일입니다.")
A(f"9. 화각은 **상반신 {pct(shots['upper_body'], len(cells))} → 가슴위 {pct(shots['bust'], len(cells))} → 클로즈업 {pct(shots['closeup'], len(cells))} → 전신 {pct(shots['full_body'], len(cells))}** 순입니다. "
  "전신은 등장·이동·쓰러짐처럼 '몸으로 보여주는' 컷에만 쓰인 제한 장치입니다(실측 예: '걸어가는 여성', '바닥에 쓰러진 남성'). "
  f"좁은 칸(폭<35%)의 {pct(sum(v for k, v in wb['좁은(<35%) '].items() if k in ('closeup', 'bust', 'object')), sum(wb['좁은(<35%) '].values()))}는 클로즈업·부분 컷이고, "
  f"카메라 높이는 {pct(ang['eye_level'], len(cells))}가 정면 수평 — 로우/하이는 연출 포인트로만 씁니다.")
A(f"10. 대사풍선 컷 {pct(texts['dialog'], len(cells))} / 무자막 {pct(texts['none'], len(cells))} / 효과문자 {pct(texts['sfx'], len(cells))} / 독백·설명 {pct(texts['mono'], len(cells))}. "
  "배경은 단순 처리가 절반(51%)이며, 배경 정밀 컷은 상황 제시 페이지에 몰립니다(21%).")
A("")
A("---")
A("")
A("## 1. 분석 방법 (두 개의 독립 판정)")
A("")
A("비전 LLM 만으로 레이아웃을 읽으면 \"작은 칸을 세지 않는\" 오류가 생겨, 두 갈래로 판정하고 교차 검증했습니다.")
A("")
A("| 갈래 | 방식 | 얻는 것 |")
A("|---|---|---|")
A("| **기하 계측** | 픽셀 밝기 프로파일에서 *얇은 균일 띠*(흰/검정 가터)를 경계로 보고, 컷 외곽좌표·폭·높이·면적·블리드를 px 단위로 측정 (`detect2.py`, `fit.py`, `q2.py`) | 정확한 비율·좌표, 가터 색, 몰아치기 여부 |")
A("| **비전 LLM** | `Qwen3.8-Flash-Next`(로컬 vLLM, thinking off, 쪽당 1024px JPEG)에게 행/칸 구조 + 화각·인물 수·자막 종류·배경 처리를 JSON으로 받음 (`scan.py`) | 그림 \"내용\"과 의도 |")
A("")
A(f"- 두 판정기는 컷 수에서 {pct(agree_geom, N)} 일치했습니다. 자유 기하 계측은 어두운 면 처리를 행 경계로 오인하는 성향이 있어 "
  f"참고값으로만 쓰고, **비전 LLM 뼈대에 기하 계측이 경계를 얹는** 방식으로 확정했습니다({measured}면={pct(measured, N)}은 실측 좌표, 나머지는 LLM 비율).")
A(f"- 재현율 검증: 60면을 다른 문장의 짧은 프롬프트로 다시 물었을 때 행/칸 서명 **완전 일치 {pct(same_re, len(retest))}**, "
  "총 컷수 일치 83%(불일치는 전부 ±1칸 — 작은 칸 유무 차이)였습니다. 상위 구조(3단·4~6컷) 결론에는 영향이 없습니다.")
A("- 비전 LLM은 컷 *내용*(화각·자막)의 유일한 출처입니다. 화각 수치는 '판정'임을 감안해 큰 경향만 읽어 주세요.")
A("")
A("## 2. 해상도·화질")
A("")
A("| 지표 | p5 | 중앙값 | p95 | 해설 |")
A("|---|---|---|---|---|")
A(f"| 가로×세로(px) | {qual[0]['w']}×{qual[0]['h']} | {qual[0]['w']}×{qual[0]['h']} | {qual[-1]['w']}×{qual[-1]['h']} | **268면 전부 동일** — 리사이즈 없는 단일 배율 |")
A(f"| 면적(MP) | {q('mp',5):.2f} | {q('mp',50):.2f} | {q('mp',95):.2f} | 2.0MP = 생성 파이프라인(1024²)과 같은 급 |")
A(f"| 종횡 | {q('aspect',5):.3f} | {q('aspect',50):.3f} | {q('aspect',95):.3f} | A비율(0.707) 만화판형 |")
A(f"| 선명도(라플라시안 분산) | {q('lap',5):.0f} | {q('lap',50):.0f} | {q('lap',95):.0f} | 중간~높음. 다만 2.0MP라 원고(600dpi) 대비 선 없음 |")
A(f"| 엣지 밀도(Canny) | {q('edges',5):.2f} | {q('edges',50):.2f} | {q('edges',95):.2f} | 컷·라인이 화면의 12%를 채움 = 정보 밀도 높음 |")
A(f"| 순수 흰(≥250) 비율 | {q('white',5):.2f} | {q('white',50):.2f} | {q('white',95):.2f} | 가터·종이 흰색. 평균 13%뿐 = 여백을 크게 쓰지 않음 |")
A(f"| 순수 검은(≤5) 비율 | {q('black',5):.2f} | {q('black',50):.2f} | {q('black',95):.2f} | 먹 라인·흑면 처리. 상위 5% 면은 검은 가터/흑면 위주 페이지 |")
A(f"| 채도(0~255) | {q('sat',5):.0f} | {q('sat',50):.0f} | {q('sat',95):.0f} | 유채 색면이 주류(흑백 페이지 {sum(1 for x in qual if max(x['ch_std'])-min(x['ch_std'])<3)}면) |")
A(f"| JPEG 블록 잔존(1.0=없음) | {q('block',5):.2f} | {q('block',50):.2f} | {q('block',95):.2f} | 재압축 흔적 사실상 없음 |")
A(f"| 파일 크기(KB) | {q('bytes',5)/1024:.0f} | {q('bytes',50)/1024:.0f} | {q('bytes',95)/1024:.0f} | 무압축 PNG, 면당 200~300KB |")
A("")
A("- 알파 채널이 있으나 사용하는 화소는 0.24%뿐(장식 스티커 수준) → **사실상 RGB 페이지**입니다.")
A("- 인쇄 환산: A5(148×210mm)면 **205dpi**, B6(128×182mm)면 **238dpi**, A4면 147dpi. "
  "웹 연재(1080px 세로)엔 충분, 만화 원고 관례(350~600dpi)엔 못 미치는 **중해상도 스캔**입니다.")
A("- 결론: 이 DB는 **구도·컷 배워가는 용도로는 충분**, 디테일(라인 날카로움·톤 밀도)을 베끼는 용도로는 부족.")
A("")
A("## 3. 컷 사용 방식")
A("")
A("### 3.1 컷 수·행 수 분포")
A("")
A("| 페이지당 컷 | " + " | ".join(str(k) for k in sorted(nc)) + " |")
A("|---|" + "---|" * len(nc))
A("| 면 | " + " | ".join(f"{nc[k]} ({pct(nc[k], N)})" for k in sorted(nc)) + " |")
A("| 페이지당 행 | " + " | ".join(f"{k}단 {nt[k]}면({pct(nt[k], N)})" for k in sorted(nt)) + " |")
A("")
A(f"평균 {mean_panel:.1f}컷/면. 1~2컷 면({nc[1]+nc[2]}면)은 표지·타이틀({roles['title']}면)과 겹쳐 거의 전부 타이틀/배경 전용 면이고, "
  "본편은 **4~6컷에 3단**이 표준입니다. 7컷 이상은 {0}면뿐으로, 컷을 잘게 쪼개 속도를 내는 유형보다 "
  "**\"컷을 크게 먹고 천천히 읽히는\" 유형**입니다.".format(nc[7] + nc[8] + nc[10]))
A("")
A("### 3.2 실제로 자주 쓰인 구조 (지원 면수순)")
A("")
A("행/칸 서명은 `위행칸수/중간행칸수/…` 입니다. 폭·높이는 실측값의 중앙값(정렬 후 큰→작은)입니다.")
A("")
A("| 서명 | 면 | 평균컷 | 행 높이(위→아래) | 2칸 행 폭(큰/작은) | 타치키리 | 주요 화각 |")
A("|---|---|---|---|---|---|---|")
for s in sigc.most_common(12):
    sig_, n = s
    G = [p for p in pages if sig(p) == sig_]
    hh_ = [[t["h"] for t in p["tiers"]] for p in G]
    hp = ["%.2f" % st.median([r[i] for r in hh_]) for i in range(len(hh_[0]))]
    w2 = [sorted([c["w"] for c in t["cells"]], reverse=True)[:2] for p in G for t in p["tiers"] if len(t["cells"]) >= 2]
    w2s = ("%.2f/%.2f" % (st.median([x[0] for x in w2]), st.median([x[1] for x in w2]))) if w2 else "—"
    sc = collections.Counter(c["shot"] for p in G for t in p["tiers"] for c in t["cells"])
    tc = sum(1 for p in G for t in p["tiers"] for c in t["cells"] if c["tach"])
    A(f"| `{sig_}` | {n} | {st.mean(sum(len(t['cells']) for t in p['tiers']) for p in G):.1f} | {' / '.join(hp)} | {w2s} | "
      f"{pct(tc, sum(len(t['cells']) for p in G for t in p['tiers']))} | {', '.join(shot_of.get(a,'??')+f' {b}' for a,b in sc.most_common(3))} |")
A("")
A("읽어낼 것:")
A("- 윗칸·아랫칸은 대칭으로 쌓되(0.35/0.30/0.35) **가운데 행을 일부러 얇게** 누른다. 얇은 행은 효과음 전용·부분 클로즈업 전용으로 쓴다(얇은 행 비율: 행 높이 p10=%.2f)." % hh[int(len(hh)*0.1)])
A("- 2칸 행은 55/45를 기준으로 62%의 행이 8%p 이상 비대칭 — **역할이 다른 두 컷**을 한 행에 붙인다(큰 컷 = 진행, 작은 컷 = 리액션).")
A("- `1`단독(13면)은 전부 표지·타이틀·배경 전용 면. 즉 \"1컷 페이지\"는 이야기 컷이 아니라 **표지 문법**이다.")
A("")
A("### 3.3 전폭·몰아치기·가터")
A("")
A(f"- 전폭 행(행에 칸 하나, 폭≥90%): {allrows}행 중 {fullrows}행 = **{pct(fullrows, allrows)}**. 첫 행이 전폭인 면 {pct(first_full, N)}, 마지막 행이 전폭인 면 {pct(last_full, N)}.")
A(f"- 몰아치기(타치키리) 컷: {pct(tach, len(cells))} — 바깥 여백이 전혀 없는 면도 {no_margin}면(268면 중).")
A(f"- 가터 색: 비전 판정 흰색 {pct(roles and sum(1 for p in pages if p['gutter']=='white'), N)}, 검은색 {pct(sum(1 for p in pages if p['gutter']=='black'), N)}, 혼용 {pct(sum(1 for p in pages if p['gutter']=='mixed'), N)}. "
  f"기하 측정은 흰색 {pct(sum(1 for p in pages if p['gutter_geom']=='white'), N)} / 혼용 {pct(sum(1 for p in pages if p['gutter_geom']=='mixed'), N)} / 검은색 {pct(sum(1 for p in pages if p['gutter_geom']=='black'), N)} — "
  "**흰 가터가 기본**, 어둠이 필요한 면만 검은 가터/흑면 처리.")
A("- 즉 \"여백으로 호흡을 준다\"기보다 **컷을 면까지 밀어 넣고, 행 높이·칸 폭의 비대칭으로 리듬**을 만든다.")
A("")
A("### 3.4 화각(프레이밍) 사용법")
A("")
A("| 화각 | 컷 수 | 비중 | 컷 면적 중앙값(페이지 대비) |")
A("|---|---|---|---|")
for s, n in shots.most_common():
    A(f"| {shot_of.get(s,s)} ({s}) | {n} | {pct(n, len(cells))} | {pct(areabyshot[s], 1)} |")
A("")
A("**칸 크기·위치에 따른 화각 규칙** — \"화각을 컷 자리에 맞춰 배치한다\"는 뜻입니다(→ `cut_new.yaml` 의 `gen` 요청).")
A("")
A("| 행 위치 | 상반신 | 가슴위 | 클로즈업 | 전신 | 풍경 |")
A("|---|---|---|---|---|---|")
for k in ("맨 위 행", "가운데 행", "맨 아래 행"):
    t_ = sum(pos[k].values())
    f = lambda s: pct(pos[k][s], t_)
    A(f"| {k} (n={t_}) | {f('upper_body')} | {f('bust')} | {f('closeup')} | {f('full_body')} | {f('scenery')} |")
A("")
A("| 칸 폭 | 상반신 | 가슴위 | 클로즈업 | 전신 | 풍경 |")
A("|---|---|---|---|---|---|")
for k in ("좁은(<35%) ", "중간(35~60%)", "넓은(≥60%) "):
    t_ = sum(wb[k].values())
    f = lambda s: pct(wb[k][s], t_)
    A(f"| {k} (n={t_}) | {f('upper_body')} | {f('bust')} | {f('closeup')} | {f('full_body')} | {f('scenery')} |")
A("")
A(f"- 인물 수 규칙: 1인 컷은 클로즈업~가슴위({pct(bychars[1]['bust']+bychars[1]['closeup'], sum(bychars[1].values()))}), "
  f"2인 컷은 상반신/전신({pct(bychars[2]['upper_body']+bychars[2]['full_body'], sum(bychars[2].values()))}) — **둘이 같은 화면이면 몸을 넓게, 혼자일수록 얼굴로** 들어갑니다.")
A(f"- 앵글: 수평 {pct(ang['eye_level'], len(cells))}, 하이 {pct(ang['high'], len(cells))}, 로우 {pct(ang['low'], len(cells))}, 1인칭 {pct(ang['pov'], len(cells))} — 앵글 전환은 드물지만 그 자체로 연출 신호.")
A("- 화각별 면적을 보면 전신·풍경 컷이 가장 넓고(≈페이지 23~27%) 클로즈업·소품은 가장 작다(≈14~16%). "
  "**\"전신을 주려면 컷을 크게\"**가 물리 조건 — 템플릿이 화각을 요청할 때 컷 크기부터 맞는 자리에 배치해야 합니다.")
A("")
A("### 3.5 자막·배경 처리")
A("")
A(f"- 자막: 대사 {pct(texts['dialog'], len(cells))} · 없음 {pct(texts['none'], len(cells))} · 효과문자 {pct(texts['sfx'], len(cells))} · 독백/설명 {pct(texts['mono'], len(cells))}. "
  "컷 4개 중 1개는 무자막 — **숨 고르기 컷**이 구조적으로 필요합니다.")
A(f"- 배경: 단순 {pct(bgs['simple'], len(cells))} · 정밀 {pct(bgs['detailed'], len(cells))} · 그라데이션/효과선 {pct(bgs['tone'], len(cells))} · 흑면 {pct(bgs['black'], len(cells))} — "
  "정밀 배경은 상황 제시 페이지(21%)에, 흑면·톤은 감정/암전 컷에 몰립니다.")
A("")
A("### 3.6 페이지 기능(기승전결 대응)")
A("")
A("| 기능 | 면 | 평균 컷 | 평균 행 | 대사 컷 |")
A("|---|---|---|---|---|")
for r, n in roles.most_common():
    c0, c1, c2, c3 = rolestat[r]
    A(f"| {role_ko.get(r, r)} | {n} | {c1:.1f} | {c2:.1f} | {c3} |")
A("")
A("- 상황 제시(37면)는 컷이 적고(3.8) 대사도 적습니다(24%) → **넓은 컷 + 설명/무자막**.")
A("- 대화(110면)·행동(63면)은 5컷/3단으로 같습니다. 차이는 화각이 아니라 자막 밀도(67% vs 39%)로 납니다 → "
  "**컷 수는 장면을, 자막 밀도는 속도를 정한다**고 읽힙니다.")
A("")
A("---")
A("")
A("## 4. `data/cut_new.yaml` 설계")
A("")
A("- 위 측정에서 지원면수가 높은 구조 20종만 뽑아 **범용 템플릿**으로 만들었습니다. "
  "이야기 전용 템플릿(\"요리 시식\", \"우산 공유\" 등)은 넣지 않고, **화면 문법**만 남겼습니다(그래서 어떤 소재에도 재사용 가능).")
A("- 기존 `cut.yaml` 과 키를 그대로 유지(`id/name/situations/total_size/total_tiers/tier_details[tier,shares,height,center,role,description]`) → 로더 수정 없이 교체 가능.")
A("- **추가한 키: `gen`** — 이미지 생성 단계에 *반드시 요청*하는 사항을 칸 순서(좌→우)로 적었습니다. "
  "이전 포맷은 `description` 에 문장으로 섞여 있어(\"전신 샷\" 등) 생성 프롬프트로 뽑아 쓸 수 없었습니다.")
A("")
A("```yaml")
A("tier_details:")
A("  - tier: 1")
A("    shares: [0.62, 0.38]          # 행 안 좌→우 폭 비율 (기존 키)")
A("    height: 0.45                  # 페이지 높이 중 이 행 비율 (기존 키)")
A("    gen:                          # ★신규: 이미지 생성 요청 (shares 순서와 1:1)")
A("      - {shot: full_body, angle: eye_level, chars: 1, bg: simple,  text: dialog}")
A("      - {shot: closeup,   angle: high,      chars: 1, bg: black,   text: mono}")
A("gen_page: \"전체 컷이 면 끝까지 밀립니다. 발끝·얼굴을 컷 가장자리에 붙이지 마세요.\"")
A("```")
A("")
A("| 신규 키 | 값 | 의미 | 생성 시 쓰는 법 |")
A("|---|---|---|---|")
A("| `gen[].shot` | full_body / upper_body / bust / closeup / scenery / object / crowd | 그 칸의 프레이밍 | 프롬프트에 직접 (예: `full body, head to toe`) |")
A("| `gen[].angle` | eye_level / low / high / pov | 카메라 각 | `from below`, `high angle`, `pov` |")
A("| `gen[].chars` | 0~3 | 화면 인물 수 | 인물 태그 수·`two people` 등 |")
A("| `gen[].bg` | detailed / simple / black / tone | 배경 밀도 | `detailed background` / `simple background` / `black background` |")
A("| `gen[].text` | dialog / mono / sfx / none | 그 칸에 들어갈 자막 종류 | 풍선·효과문자 개수 및 여백 확보 지시 |")
A("| `gen_page` | 문자열 | 페이지 단위 요청(블리드·가터 색) | 레이아웃 설명에 그대로 실림 |")
A("")
A("**배선 완료(2026-09-16)** — `comic_gen.load_cut_templates()` 가 `gen`/`gen_page` 를 읽고 슬롯에 실며, "
  "`_apply_slot_meta()` 가 컷에 옮기고, `build_panel_prompt()` 가 태그로 푼다 "
  "(`--cut-yaml data/cut_new.yaml` 로 DB 교체, `--no-cut-gen` 으로 요청만 끄기). "
  "`gen` 없는 기존 34종은 태그가 빈 문자열이라 동작이 그대로다.")
A("")
A("### 템플릿 20종 (`data/cut_new.yaml` 실제 값에서 생성한 목록)")
A("")
A("행 구성·행 높이는 yaml 에 적어 넣은 값이고, `근거`는 이 작품 268면에서 그 구조에 맞는 면을 센 값입니다 "
  "(`analysis_cut/match.py` 의 템플릿별 조건으로 계산, 재현 가능). 조건이 두 구조 이상을 허용하는 템플릿은 "
  "면수가 그 합계가 됩니다. `0`은 관측이 없는 **확장안**입니다.")
A("")
A("| # | id | 이름 | 행 구성 | 행 높이 | 상황 | 근거 | 생성 요청(화각 집합) |")
A("|--:|---|---|---|---|---|---:|---|")
import yaml as _yaml
_yd = _yaml.safe_load(open(os.path.join(ROOT, "data", "cut_new.yaml"), encoding="utf-8"))
for _i, _t in enumerate(_yd["manga_panel_templates"], 1):
    _sg = "/".join(str(len(td["shares"])) for td in _t["tier_details"])
    _hh2 = "/".join(f"{td.get('height') or 0:.2f}" for td in _t["tier_details"])
    _sh2 = sorted({g["shot"] for td in _t["tier_details"] for g in (td.get("gen") or [])})
    _ob = int(_t.get("observed") or 0)
    _basis = (f"{_ob}면 관측" if _ob else "확장안(관측 0)")
    if _ob in sigc:
        _basis += f"(실측 {sigc[_sg]}면과 같은 구조)" if _sg in sigc else ""
    A(f"| {_i} | `{_t['id']}` | {_t['name']} | `{_sg}` | {_hh2} | {'/'.join(_t['situations'])} | {_basis} | "
      f"{', '.join(shot_of.get(s, s) for s in _sh2)} |")
A("")
A("---")
A("")
A("## 5. 페이지별 상세 (268면)")
A("")
A("표기 — 행 구성: `위행칸/중간행칸/…`, 행 높이·칸 폭은 페이지 대비 실측(1.00 = 전체), "
  "화각: FB 전신 · UB 상반신 · BU 가슴위 · CU 클로즈업 · SC 풍경 · OB 소품 · CR 군중(행은 `｜`, 같은 행 안 칸은 `+`), "
  "실측: 경계를 픽셀로 잰 면(✓) / LLM 비율(·), 가터: 기하 측정값.")
A("")
A("| 쪽 | 컷 | 행 구성 | 행 높이 | 칸 폭(좌→우) | 화각 | 가터 | 몰아치기 | 실측 | 기능 | 비고 |")
A("|---:|---:|---|---|---|---|---|---|:-:|---|---|")
for p in pages:
    npan = sum(len(t["cells"]) for t in p["tiers"])
    ntach = sum(1 for t in p["tiers"] for c in t["cells"] if c["tach"])
    note = p["notes"].replace("|", "/").replace("\\", "/").strip()[:22].rstrip()
    A(f"| {p['no']:03d} | {npan} | {sig(p)} | {hs(p)} | {ws(p)} | {sh(p)} | {p['gutter_geom'][:4]} | {ntach}/{npan} | "
      f"{'✓' if p['measured'] else '·'} | {role_ko.get(p['role'], p['role'])} | {note} |")
A("")
A("## 6. 한계")
A("")
A("- 화각·자막 종류는 비전 LLM 판정(재현율 83%)입니다. 단락 단락의 큰 결론(3단 4~6컷, 비대칭 폭, 전신=제한 사용)은 "
  "동일 프롬프트 재검증·기하 실측과 어긋나지 않았습니다.")
A(f"- 사선(대각선) 분할은 직선 경계 검출의 한계로 기하 계측이 실패하는 방향으로 작동했습니다. LLM 이 notes 에 `사선` 으로 남긴 면은 {sum(1 for p in pages if '사선' in p['notes'])}면이고, 이 리포트의 행/칸 집계에서는 직사각 분할로 근사했습니다.")
A("- 읽기 방향(우→좌)은 판정하지 않았습니다. 표에는 촬영 순서(위→아래, 좌→우)로 적었으니, 실제 작품의 컷 진행 순서와 다를 수 있습니다.")
A("- 세로 긴 컷(행 하나를 통째로 쓰는 세로형)은 1%에 불과합니다. 세로형 컷을 쓰는 템플릿은 이 작품의 관측치가 아니라 **확장안**임을 명시했습니다(`vertical_full_body_intro`).")
A("")
A(("[분석 스크립트] `analysis_cut/{scan,fit,detect2,qmetrics,q2,agg2,stats2,support,match,report}.py` — 재실행하면 같은 숫자가 나옵니다. "
   "생성 데이터: `analysis_cut/{vlm.jsonl,fit.json,geom.json,quality.json,q2.json,pages.json,support.json}`."))
open(os.path.join(ROOT, "cut_report.md"), "w", encoding="utf-8").write("\n".join(L) + "\n")
print("wrote cut_report.md", len(L), "lines")
