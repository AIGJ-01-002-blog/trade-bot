#!/bin/bash
# 3.10 이상인 python3 경로를 찾아 출력한다. 맥 기본 /usr/bin/python3는 3.9라 봇이 돌지 않는다.
# launchd는 PATH가 짧아서 Homebrew·python.org 설치 경로를 직접 본다. 못 찾으면 종료 코드 1.
for p in "${DEVLOG_TRADE_PYTHON:-}" /opt/homebrew/bin/python3 /usr/local/bin/python3 \
         /Library/Frameworks/Python.framework/Versions/Current/bin/python3 "$(command -v python3 2>/dev/null)"; do
  [ -n "$p" ] && [ -x "$p" ] || continue
  if "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    echo "$p"
    exit 0
  fi
done
exit 1
