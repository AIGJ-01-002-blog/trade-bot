#!/bin/bash
# launchd가 몇 분마다 부르는 한 번 실행. 도는 동안만 맥이 잠들지 않게 caffeinate로 감싼다.
# devlog 토큰은 파일에 적지 않고 맥 키체인(서비스 이름 devlog-trade-bot)에서 읽는다.
set -u
BOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$BOT_DIR" || exit 1
DEVLOG_TOKEN="$(security find-generic-password -a "$USER" -s devlog-trade-bot -w 2>/dev/null || true)"
export DEVLOG_TOKEN
# AI 매매 판단(#263)을 켤 때만: Gemini 키도 키체인(서비스 이름 devlog-trade-bot-gemini)에서 읽는다
KEY="$(security find-generic-password -a "$USER" -s devlog-trade-bot-gemini -w 2>/dev/null || true)"
[ -n "$KEY" ] && export GEMINI_API_KEY="$KEY"  # 키체인에 없으면 이미 받은 환경 변수를 그대로 둔다
# 상위 모델(AI_PROVIDER=anthropic·openai)을 고르면 그 키도 키체인에서 읽는다
KEY="$(security find-generic-password -a "$USER" -s devlog-trade-bot-anthropic -w 2>/dev/null || true)"
[ -n "$KEY" ] && export ANTHROPIC_API_KEY="$KEY"  # 키체인에 없으면 이미 받은 환경 변수를 그대로 둔다
KEY="$(security find-generic-password -a "$USER" -s devlog-trade-bot-openai -w 2>/dev/null || true)"
[ -n "$KEY" ] && export OPENAI_API_KEY="$KEY"  # 키체인에 없으면 이미 받은 환경 변수를 그대로 둔다
unset KEY
export PYTHONUTF8=1
echo "=== $(date '+%Y-%m-%d %H:%M:%S')"
PY="$("$BOT_DIR/macos/find-python.sh")" || { echo "Python 3.10 이상을 찾지 못했어요. brew install python 또는 python.org 설치 뒤 다시 돌려요."; exit 1; }
exec /usr/bin/caffeinate -i "$PY" -m devlog_trade jobs "${JOBS_FILE:-$BOT_DIR/jobs.conf}"
