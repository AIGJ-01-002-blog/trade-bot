#!/bin/bash
# launchd가 몇 분마다 부르는 한 번 실행. 도는 동안만 맥이 잠들지 않게 caffeinate로 감싼다.
# devlog 토큰은 파일에 적지 않고 맥 키체인(서비스 이름 devlog-trade-bot)에서 읽는다.
set -u
BOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$BOT_DIR" || exit 1
DEVLOG_TOKEN="$(security find-generic-password -a "$USER" -s devlog-trade-bot -w 2>/dev/null || true)"
export DEVLOG_TOKEN
export PYTHONUTF8=1
echo "=== $(date '+%Y-%m-%d %H:%M:%S')"
exec /usr/bin/caffeinate -i /usr/bin/env python3 -m devlog_trade jobs "${JOBS_FILE:-$BOT_DIR/jobs.conf}"
