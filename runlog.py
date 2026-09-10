#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
runlog.py — 실행 로그 위생 (2026-09-10)

1) **실행 시작에 본 로그를 비웁니다.** 로그가 append라 지난 실패와 섞여, 로그를 덮어쓰는
   방식은 '어제 실패'를 오늘 실패로 읽게 합니다(실제로 사용자가 06:08 로그를 짚고 물었습니다).
2) **에러·경고는 log/error.log에 따로 남깁니다.** 본 로그는 다음 실행에 지워지니 실패 이력이
   사라집니다. 에러 로그는 지우지 않고 실행 헤더(`===== RUN … =====`)만 구분자로 쌓습니다.
3) `summary()` — 실행 종료 시 "에러 N건 → log/error.log" 한 줄로 알려줍니다.

사용: run_comic.py가 가장 먼저 start_run()을 부르고, 각 모듈의 로거가 note()를 통과시킵니다.
"""

import os
import sys
import time

LOG_DIR = "log"
MAIN_LOGS = ("comic_gen.log", "comic_input.log", "anima_gen.log", "tag_out.txt", "tag_out.json")
ERROR_LOG = os.path.join(LOG_DIR, "error.log")
# 로그 문장에 이 표어가 있으면 에러 로그에 복제합니다 (수작업 지점이 아니라 로거 한 곳만 보면 됩니다)
MARKS = ("[오류]", "✗", "ERROR", "WARNING", "경고", "오류", "에러", "실패", "예외", "누락", "미달",
         "포기", "Traceback", "Exception", "하지 못", "못했습니다")

_error_count = 0


def start_run(keep: bool = False) -> str:
    """실행 시작: 본 로그를 비우고 에러 로그에 실행 구분자를 남긴다 (keep=True: 유지)"""
    global _error_count
    _error_count = 0
    os.makedirs(LOG_DIR, exist_ok=True)
    if not keep:
        for n in MAIN_LOGS:
            try:
                open(os.path.join(LOG_DIR, n), "w", encoding="utf-8").close()
            except Exception:
                pass
    argv = " ".join(sys.argv[1:])[:400]
    try:
        with open(ERROR_LOG, "a", encoding="utf-8") as f:
            f.write(f"\n===== RUN {time.strftime('%Y-%m-%d %H:%M:%S')}  argv: {argv} =====\n")
    except Exception:
        pass
    return ERROR_LOG


def note(msg, source: str = "") -> bool:
    """로그 문장이 에러/경고 계열이면 error.log에 복제한다. (복제 여부를 반환)"""
    global _error_count
    m = str(msg)
    if not any(k in m for k in MARKS):
        return False
    # 프롬프트 덤프처럼 긴 블록은 규칙 문장에 '실패/미달'이 들어가도 통째로 옮기지 않는다(첫 줄만)
    m = m.split("\n", 1)[0] if len(m) > 1200 else " ⏎ ".join(x.strip() for x in m.strip().split("\n"))
    _error_count += 1
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(ERROR_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}]"
                    + (f" [{source}]" if source else "") + " " + m.strip()[:400] + "\n")
    except Exception:
        pass
    return True


def summary() -> str:
    if _error_count:
        return f"에러/경고 {_error_count}건 → {ERROR_LOG} (본 로그는 다음 실행에 초기화됩니다)"
    return f"에러·경고 없음 — 이력: {ERROR_LOG}"
