@echo off
rem =====================================================================
rem run_ollama_win.bat — Windows 엔트리 (1줄 래퍼, 더블클릭 가능)
rem
rem   run_ollama_win.bat --episode inputs\ep01.txt --sheet inputs\sheet01.txt --dry-run
rem   run_ollama_win.bat --episode inputs\ep01.txt --sheet inputs\sheet01.txt --start-comfy
rem   run_ollama_win.bat stop                 rem ollama 모델+서버 종료
rem   run_ollama_win.bat plan                 rem OS 분기 결과(경로/명령)만 출력
rem
rem 하는 일: venv\Scripts\activate → `python run_comic.py --start-llm <인자...>`
rem   ollama 바이너리 탐색/기동/포트 대기/taskkill /T/메모리 반납은 전부 run_comic.py가 한다.
rem   셸 트윈(run_ollama_win.sh/.ps1)은 만들지 않는다 — OS 분기는 run_comic.py 안에 있다.
rem
rem 사전 준비(Windows):
rem   1) Python 3.11+ 설치 후 `python -m venv venv` + `venv\Scripts\pip install -r requirements.txt`
rem   2) ollama 바이너리: 설치형이면 그만, 포터블은 압축 풀고 set OLLAMA_BIN=C:\tools\ollama\ollama.exe
rem   3) plot.json 의 comfyuidir 를 실 설치 경로로 (예: D:\ComfyUI_aki)
rem 환경변수(선택): OLLAMA_BIN, OLLAMA_HOST_URL(예: 127.0.0.1:11455), OLLAMA_MODELS_DIR
rem =====================================================================
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8:replace

if exist "venv\Scripts\activate.bat" call "venv\Scripts\activate.bat"
where python >nul 2>nul || (echo ! python 이 없습니다. Python 3.11+ 설치 후 venv 를 만들어 주세요.&exit /b 1)

rem OLLAMA_BIN / OLLAMA_MODELS_DIR 는 그대로 자식 프로세스에 상속된다. HOST 만 이름 변환한다.
if not "%OLLAMA_HOST_URL%"=="" set COMIC_OLLAMA_HOST=%OLLAMA_HOST_URL%

if /i "%~1"=="stop" (
  shift
  python run_comic.py --stop-llm %*
  goto :eof
)
if /i "%~1"=="plan" (
  shift
  python run_comic.py --llm-plan %*
  goto :eof
)
python run_comic.py --start-llm %*
