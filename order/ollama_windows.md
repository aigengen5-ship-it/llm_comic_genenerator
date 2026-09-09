ComfyUI의 커스텀 노드들이 외부 실행 파일(FFmpeg, 각종 포터블 서버 등)을 다루는 방식처럼, 윈도우 환경에서 파이썬 앱에 Ollama를 내장하여 **'포터블(Portable)'** 형태로 자동 실행하고 배포하는 아주 좋은 방법이 있습니다.

사용자가 따로 Ollama를 설치할 필요 없이, **프로그램이 알아서 바이너리를 다운로드(또는 앱에 동봉)하고 백그라운드에서 실행**하도록 구성하는 방법입니다.

다음은 이를 구현하기 위한 핵심 단계와 파이썬 코드입니다.

---

### 1. 핵심 개념
* **독립 실행형 바이너리 (`ollama.exe`)**: Ollama는 인스톨러 없이 `ollama.exe` 파일 하나만 있으면 동작합니다.
* **환경 변수 통제 (`OLLAMA_MODELS`)**: 시스템의 기본 경로(`C:\Users\...`)를 오염시키지 않도록, 앱 폴더 내부에 모델이 다운로드되도록 환경 변수를 설정합니다.
* **백그라운드 실행 (`subprocess`)**: 콘솔 창이 뜨지 않도록 숨김 처리하여 백그라운드 서버(`ollama serve`)로 띄웁니다.

---

### 2. 구현 코드 (ComfyUI 스타일의 매니저 스크립트)

아래 코드는 **1) Ollama가 없으면 자동 다운로드, 2) 포터블 경로 설정, 3) 백그라운드 실행, 4) 종료 시 자동 정리**를 모두 수행합니다.

```python
import os
import sys
import time
import zipfile
import urllib.request
import subprocess
import atexit
import requests

# 기본 경로 설정
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OLLAMA_DIR = os.path.join(BASE_DIR, "ollama_bin")
OLLAMA_EXE = os.path.join(OLLAMA_DIR, "ollama.exe")
MODELS_DIR = os.path.join(BASE_DIR, "ollama_models") # 모델이 저장될 포터블 경로

# 다운로드 URL (공식 GitHub Release의 윈도우용 zip 파일)
OLLAMA_ZIP_URL = "https://github.com/ollama/ollama/releases/latest/download/ollama-windows-amd64.zip"

ollama_process = None

def download_and_extract_ollama():
    """Ollama 바이너리가 없으면 다운로드 및 압축 해제"""
    if os.path.exists(OLLAMA_EXE):
        return

    print("Ollama 바이너리를 다운로드 중입니다... (최초 1회)")
    os.makedirs(OLLAMA_DIR, exist_ok=True)
    zip_path = os.path.join(OLLAMA_DIR, "ollama.zip")
    
    urllib.request.urlretrieve(OLLAMA_ZIP_URL, zip_path)
    
    print("압축 해제 중...")
    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
        zip_ref.extractall(OLLAMA_DIR)
        
    os.remove(zip_path)
    print("Ollama 준비 완료!")

def start_ollama_server():
    """Ollama를 백그라운드 서버로 실행"""
    global ollama_process
    
    # 환경 변수 설정 (중요: 포터블화)
    env = os.environ.copy()
    env["OLLAMA_MODELS"] = MODELS_DIR  # 모델 저장 경로를 내 앱 폴더로 지정
    env["OLLAMA_HOST"] = "127.0.0.1:11434" # 포트 충돌 방지를 위해 필요시 변경 가능 (예: 11435)

    os.makedirs(MODELS_DIR, exist_ok=True)

    # 윈도우에서 콘솔 창을 숨기기 위한 플래그
    CREATE_NO_WINDOW = 0x08000000

    print("Ollama 서버 시작 중...")
    ollama_process = subprocess.Popen(
        [OLLAMA_EXE, "serve"],
        env=env,
        stdout=subprocess.DEVNULL, # 로그를 숨김 (필요시 파일로 리다이렉트)
        stderr=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW
    )

    # 서버가 켜질 때까지 대기
    for _ in range(10):
        try:
            res = requests.get("http://127.0.0.1:11434/")
            if res.status_code == 200:
                print("Ollama 서버가 성공적으로 실행되었습니다.")
                return
        except requests.exceptions.ConnectionError:
            time.sleep(1)
            
    print("경고: Ollama 서버 응답 없음")

def cleanup():
    """앱 종료 시 Ollama 프로세스 종료"""
    global ollama_process
    if ollama_process:
        print("\nOllama 서버 종료 중...")
        ollama_process.terminate()
        ollama_process.wait()

# 앱이 종료될 때 cleanup 함수 자동 실행
atexit.register(cleanup)


if __name__ == "__main__":
    # 1. Ollama 준비 (설치)
    download_and_extract_ollama()
    
    # 2. 서버 실행
    start_ollama_server()
    
    # 3. 이후 로직 (예: 모델 다운로드 및 추론)
    # 파이썬 공식 ollama 라이브러리(`pip install ollama`)를 사용하거나 REST API 호출
    print("\n--- 파이썬 앱 메인 로직 실행 ---")
    
    try:
        import ollama
        
        # 클라이언트 초기화 (사용자 정의 호스트를 썼다면 명시해야 함)
        client = ollama.Client(host='http://127.0.0.1:11434')
        
        # 모델 풀링 (최초 실행 시)
        model_name = "qwen2.5:0.5b" # 테스트용 가벼운 모델
        print(f"{model_name} 모델을 가져옵니다. (시간이 걸릴 수 있습니다)")
        client.pull(model_name)
        
        # 추론
        print("추론 시작...")
        response = client.chat(model=model_name, messages=[
            {'role': 'user', 'content': '안녕? 넌 누구야?'}
        ])
        print("답변:", response['message']['content'])
        
        # 메인 프로그램 유지 (GUI 창이나 웹서버 구동 시 이 부분 대체)
        while True:
            time.sleep(1)
            
    except KeyboardInterrupt:
        print("프로그램을 종료합니다.")
```

### 3. 배포 시 팁 (실무 적용)

1. **용량 최적화 (바이너리 포함 vs 런타임 다운로드)**
   * 앱 용량을 줄이고 싶다면 위 코드처럼 **런타임 다운로드 방식**을 사용하세요. (ComfyUI Manager 방식)
   * 오프라인 환경에서도 작동해야 한다면 릴리즈 파일(`ollama.exe`)을 프로젝트 폴더에 미리 넣어서 PyInstaller 등으로 같이 패키징하면 됩니다.
2. **포트 충돌 방지 (`OLLAMA_HOST`)**
   * 사용자의 PC에 이미 Ollama가 설치되어 실행 중일 수 있습니다.
   * `env["OLLAMA_HOST"] = "127.0.0.1:11555"` 처럼 기본 포트(11434)가 아닌 **독자적인 포트**를 부여하고, 통신할 때 해당 포트를 사용하면 사용자 PC의 기존 Ollama와 완벽하게 독립적으로 동작합니다.
3. **콘솔 창 숨기기 (`CREATE_NO_WINDOW`)**
   * 윈도우 환경에서 `subprocess`를 그냥 쓰면 `cmd.exe` 창이 번쩍하고 뜹니다. 위 코드에 포함된 `creationflags=0x08000000`를 사용하면 백그라운드에 완전히 숨겨집니다.
4. **PyInstaller 배포**
   * 코드를 `pyinstaller --onedir --noconsole main.py`로 빌드하면, 일반 사용자들은 윈도우 앱처럼 더블클릭만으로 LLM이 구동되는 파이썬 프로그램을 사용할 수 있습니다.
