# -*- coding: utf-8 -*-
"""로컬 비전 LLM(pi가 쓰는 그 서버)에 그림 한 장 + 질문을 보내는 최소 클라이언트."""
import base64, io, json, os, sys, time, urllib.request
from PIL import Image

BASE = os.environ.get("VLM_BASE", "http://192.168.1.162:8000/v1")
MODEL = os.environ.get("VLM_MODEL", "Qwen/Qwen3.8-Flash-Next")
KEY = os.environ.get("VLM_KEY", "local-dummy-key")

def data_url(path, max_side=1024, q=82):
    with Image.open(path) as im:
        im = im.convert("RGB")
        if max(im.size) > max_side:
            im.thumbnail((max_side, max_side), Image.LANCZOS)
        b = io.BytesIO(); im.save(b, "JPEG", quality=q)
    return "data:image/jpeg;base64," + base64.b64encode(b.getvalue()).decode()

def ask(image_paths, prompt, system=None, max_tokens=1200, temperature=0.0, max_side=1024, retries=3):
    content = [{"type": "text", "text": prompt}]
    for p in image_paths:
        content.append({"type": "image_url", "image_url": {"url": data_url(p, max_side)}})
    msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": content}]
    body = json.dumps({"model": MODEL, "messages": msgs, "max_tokens": max_tokens,
                       "temperature": temperature}).encode()
    req = urllib.request.Request(BASE + "/chat/completions", data=body,
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + KEY})
    last = None
    for i in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                d = json.loads(r.read().decode())
            return d["choices"][0]["message"]["content"]
        except Exception as e:
            last = e; time.sleep(2 + 3 * i)
    raise RuntimeError(f"VLM 호출 실패: {last}")

if __name__ == "__main__":
    fs = sys.argv[1:-1] or [sys.argv[1]]
    print(ask(fs, sys.argv[-1]))
