#!/usr/bin/env bash
# =====================================================================
# run_ollama.sh — Linux/macOS 엔트리 (1줄 래퍼)
#
#   ./run_ollama.sh --episode inputs/ep01.txt --sheet inputs/sheet01.txt --dry-run
#   ./run_ollama.sh --episode inputs/ep01.txt --sheet inputs/sheet01.txt --start-comfy
#   ./run_ollama.sh stop                     # ollama 모델+서버 종료
#
# 하는 일: venv 활성화 → `python3 run_comic.py --start-llm <인자...>`
#   ollama 기동/포트 대기/kill, plot.json 임시 전환, 메모리 반납은 **전부 run_comic.py**가 한다.
#   (예전에 이 스크립트가持有했던 규칙 4개 — 모델명/포트/num_ctx/kill — 를 셸에 두면
#    Windows 트윈(.sh/.ps1)과 반드시 어긋난다 → Python으로 이관, OS 분기도 그 안에만 있다.)
#
# 환경변수(선택):
#   OLLAMA_BIN        ollama 바이너리 경로 (미지정 시 PATH → ~/AI/ollama/bin/ollama)
#   OLLAMA_HOST_URL   127.0.0.1:11455 처럼 지정하면 설치형 ollama(11434)와 충돌하지 않는다
#   OLLAMA_MODELS_DIR 모델을 repo/앱 폴더 안으로 격리(ollama 서버 기본 경로 오염 방지)
#   num_ctx는 plot.json `ollama_num_ctx`(기본 8192)에서 읽는다 — 이 스크립트에 값이 없다.
# =====================================================================
set -euo pipefail
cd "$(dirname "$0")"

if [ -f venv/bin/activate ]; then
  . venv/bin/activate
fi

exec python3 run_comic.py "$@"
