#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
runlog.py — 실행 로그 위생 (2026-09-10)

1) **실행 시작에 본 로그를 비웁니다.** 로그가 append라 지난 실패와 섞여, 로그를 덮어쓰는
   방식은 '어제 실패'를 오늘 실패로 읽게 합니다(실제로 사용자가 06:08 로그를 짚고 물었습니다).
2) **에러·경고는 log/error.log에 따로 남깁니다.** 본 로그는 다음 실행에 지워지니 실패 이력이
   사라집니다. 에러 로그는 지우지 않고 실행 헤더(`===== RUN … =====`)만 구분자로 쌓습니다.
3) `summary()` — 실행 종료 시 "에러 N건 → log/error.log" 한 줄로 알려줍니다.
4) **동시 실행을 인식합니다(2026-09-10).** 본 로그를 비우는 일은 '지금 다른 실행이 없는'
   경우에만 합니다. 실측: `--special --all-eps`가 렌더 중이는데 selftest가 서브프로세스로
   `run_comic.py --list-templates`를 띄워, 살아있는 실행의 comic_gen.log를 통째로 지웠습니다
   (EP01~03 렌더 이력이 사라졌고 error.log는 두 실행이 섞여 어느 실행 실패인지 못 읽었다).
   → `log/.run.lock`에 PID를 적어 두고, 살아있는 PID가 있으면 **이어 씁니다**.
5) **줄마다 PID를 남깁니다.** 섞여도 어느 실행의 실패인지 읽을 수 있습니다(실측 요구).
6) **프롬프트·전송 잡음은 에러가 아닙니다(2026-09-10).** 프롬프트 규칙 문장에 '예외/실패'라는 단어가 들어간다는 이유로 `[PLOT_PROMPT]` 덤프가 에러로 복제되어, 20건 중 13건이 가짜였습니다.

사용: run_comic.py가 가장 먼저 start_run()을 부르고, 각 모듈의 로거가 note()를 통과시킵니다.
"""

import atexit
import json
import os
import sys
import time

LOG_DIR = "log"
MAIN_LOGS = ("comic_gen.log", "comic_input.log", "anima_gen.log", "tag_out.txt", "tag_out.json")
ERROR_LOG = os.path.join(LOG_DIR, "error.log")
LOCK = os.path.join(LOG_DIR, ".run.lock")
# 로그 문장에 이 표어가 있으면 에러 로그에 복제합니다 (수작업 지점이 아니라 로거 한 곳만 보면 됩니다)
MARKS = ("[오류]", "✗", "ERROR", "WARNING", "경고", "오류", "에러", "실패", "예외", "누락", "미달",
         "포기", "Traceback", "Exception", "하지 못", "못했습니다")
# 이 헤더로 시작하는 줄은 **진단이 아니라 덤프**입니다 — 규칙 문장에 '예외'가 보여도 에러가 아니다
#   (실측: --special 실행의 '에러 20건' 중 13건이 [PLOT_PROMPT] 한 줄이었다)
NOT_ERRORS = ("[PLOT_PROMPT]", "[PLOT_RESULT]", "[TOKEN]", "[ELAPSED]", "[TIMEOUT]", "[LLM release]")

_error_count = 0
_pid = os.getpid()


def _alive(pid: int) -> bool:
    """그 PID가 아직 살아 있는지 (Windows 포함 안전)"""
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def _read_lock() -> dict:
    try:
        d = json.loads(open(LOCK, encoding="utf-8").read() or "{}")
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write_lock(d: dict):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(LOCK, "w", encoding="utf-8") as f:
            json.dump(d, f)
    except Exception:
        pass


def active_runs() -> list:
    """지금 로그를 쓰고 있는 다른 실행의 PID 목록 (내 것·죽은 것은 제외)"""
    d = _read_lock()
    return sorted(int(k) for k, v in d.items() if str(k) != str(_pid) and _alive(k))


def start_run(keep: bool = False) -> str:
    """실행 시작: 본 로그를 비우고 에러 로그에 실행 구분자를 남긴다 (keep=True: 유지)

    다른 실행이 아직 살아 있으면 **초기화하지 않고 이어 씁니다** — 예전에 남의 실행 로그를
    지워 실패 원인을 읽을 수 없게 만들었습니다(모듈 docstring 4번).
    """
    global _error_count
    _error_count = 0
    os.makedirs(LOG_DIR, exist_ok=True)
    others = active_runs()
    lock = _read_lock()
    lock = {k: v for k, v in lock.items() if _alive(k)}          # 죽은 PID 정리
    lock[str(_pid)] = time.strftime("%Y-%m-%d %H:%M:%S")
    _write_lock(lock)
    atexit.register(end_run)
    truncate = not keep and not others
    if truncate:
        for n in MAIN_LOGS:
            try:
                open(os.path.join(LOG_DIR, n), "w", encoding="utf-8").close()
            except Exception:
                pass
    argv = " ".join(sys.argv[1:])[:400]
    try:
        with open(ERROR_LOG, "a", encoding="utf-8") as f:
            f.write(f"\n===== RUN {time.strftime('%Y-%m-%d %H:%M:%S')}  pid={_pid}  argv: {argv} =====\n")
            if others:
                f.write(f"----- 동시 실행 감지 pid={','.join(str(x) for x in others)} — "
                        f"본 로그를 초기화하지 않고 이어 씁니다 -----\n")
    except Exception:
        pass
    return ERROR_LOG


def end_run():
    """이 PID를 락에서 뺀다(atexit) — 마지막 실행이 빠진 뒤 새 실행은 다시 초기화합니다."""
    lock = _read_lock()
    if str(_pid) in lock:
        lock.pop(str(_pid), None)
        _write_lock({k: v for k, v in lock.items() if _alive(k)})


def note(msg, source: str = "") -> bool:
    """로그 문장이 에러/경고 계열이면 error.log에 복제한다. (복제 여부를 반환)"""
    global _error_count
    m = str(msg)
    head = m.lstrip().split("\n", 1)[0]
    if any(head.startswith(k) for k in NOT_ERRORS):
        return False                                            # 덤프는 에러가 아니다
    if not any(k in m for k in MARKS):
        return False
    # 프롬프트 덤프처럼 긴 블록은 규칙 문장에 '실패/미달'이 들어가도 통째로 옮기지 않는다(첫 줄만)
    m = m.split("\n", 1)[0] if len(m) > 1200 else " ⏎ ".join(x.strip() for x in m.strip().split("\n"))
    _error_count += 1
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(ERROR_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}][pid {_pid}]"
                    + (f" [{source}]" if source else "") + " " + m.strip()[:400] + "\n")
    except Exception:
        pass
    return True


def summary() -> str:
    if _error_count:
        return f"에러/경고 {_error_count}건 → {ERROR_LOG} (본 로그는 다음 실행에 초기화됩니다)"
    return f"에러·경고 없음 — 이력: {ERROR_LOG}"
