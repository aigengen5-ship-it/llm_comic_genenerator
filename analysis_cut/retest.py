# 같은 페이지를 다른 문장의 짧은 프롬프트로 다시 물어 판정 재현율을 잰다(60장 표본).
import json, os, random, re, threading, urllib.request
import vlm
HERE = os.path.dirname(os.path.abspath(__file__))
P = [{"type": "text", "text": "이 페이지를 행(세로로 쌓인 가로 띠) 단위로 쪼갤 때, 각 행의 컷 개수를 위→아래 순서대로 "
                              "`1|2|1` 같은 문자열 하나로만 답하세요. 다른 글자 없이."},
     {"type": "image_url", "image_url": {"url": None}}]
def ask(path):
    import copy
    msg = copy.deepcopy(P); msg[1]["image_url"]["url"] = vlm.data_url(path, 896)
    body = json.dumps({"model": vlm.MODEL, "messages": [{"role": "user", "content": msg}],
                       "max_tokens": 40, "temperature": 0.0,
                       "chat_template_kwargs": {"enable_thinking": False}}).encode()
    req = urllib.request.Request(vlm.BASE + "/chat/completions", data=body,
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + vlm.KEY})
    with urllib.request.urlopen(req, timeout=180) as r:
        d = json.loads(r.read().decode())
    m = d["choices"][0]["message"]
    return ((m.get("content") or m.get("reasoning") or "")).strip()

pages = json.load(open(os.path.join(HERE, "pages.json"), encoding="utf-8"))
random.seed(7); samp = random.sample(pages, 60)
lock = threading.Lock(); res = []
def work(p):
    try:
        s = ask(os.path.join("/run/media/chrisyeo/AIDATA/AI/temp/182856022", p["file"]))
        s2 = re.sub(r"[^0-9]", "-", s).strip("-")
        s2 = re.sub(r"-+", "-", s2)
        sig = "-".join(str(len(t["cells"])) for t in p["tiers"])
        with lock: res.append((p["file"], sig, s2, sig == s2))
    except Exception as e:
        with lock: res.append((p["file"], "-".join(str(len(t["cells"])) for t in p["tiers"]), "ERR", False))
idx = list(range(len(samp))); pos = [0]
def runner():
    while True:
        with lock:
            if pos[0] >= len(idx): return
            i = idx[pos[0]]; pos[0] += 1
        work(samp[i])
ts = [threading.Thread(target=runner) for _ in range(6)]
[t.start() for t in ts]; [t.join() for t in ts]
json.dump(res, open(os.path.join(HERE, "retest.json"), "w"), ensure_ascii=False, indent=1)
same = sum(1 for r in res if r[3]); near = sum(1 for r in res if r[2] != "ERR" and sum(map(int, r[1].split("-"))) == sum(map(int, r[2].split("-"))))
print(f"재검증 {len(res)}장 — 행/칸 서명 완전 일치 {same}장({100*same/len(res):.0f}%), 총 컷수 일치 {near}장({100*near/len(res):.0f}%)")
print("불일치 예:", [r[1] + " vs " + r[2] for r in res if not r[3]][:12])
