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
# 실거래(LIVE.md): 업비트 키도 키체인(devlog-trade-bot-upbit-access·devlog-trade-bot-upbit-secret)에서 읽는다
KEY="$(security find-generic-password -a "$USER" -s devlog-trade-bot-upbit-access -w 2>/dev/null || true)"
[ -n "$KEY" ] && export UPBIT_ACCESS_KEY="$KEY"
KEY="$(security find-generic-password -a "$USER" -s devlog-trade-bot-upbit-secret -w 2>/dev/null || true)"
[ -n "$KEY" ] && export UPBIT_SECRET_KEY="$KEY"
# 사고팔 때 디스코드 알림: 채널 웹후크 주소도 키처럼 키체인(devlog-trade-bot-discord-live·-paper)에서 읽는다
KEY="$(security find-generic-password -a "$USER" -s devlog-trade-bot-discord-live -w 2>/dev/null || true)"
[ -n "$KEY" ] && export DISCORD_WEBHOOK_LIVE="$KEY"
KEY="$(security find-generic-password -a "$USER" -s devlog-trade-bot-discord-paper -w 2>/dev/null || true)"
[ -n "$KEY" ] && export DISCORD_WEBHOOK_PAPER="$KEY"
unset KEY
# 실거래소 주문은 이 폴더에 trade-live.on 파일이 있을 때만 켠다(DEVLOG_TRADE_LIVE=1). 지우면 다음 바퀴부터 꺼진다.
# jobs.conf 줄의 --live와 둘 다 있어야 주문이 나간다.
[ -f "$BOT_DIR/trade-live.on" ] && export DEVLOG_TRADE_LIVE=1
export PYTHONUTF8=1
echo "=== $(date '+%Y-%m-%d %H:%M:%S')"
PY="$("$BOT_DIR/macos/find-python.sh")" || { echo "Python 3.10 이상을 찾지 못했어요. brew install python 또는 python.org 설치 뒤 다시 돌려요."; exit 1; }
exec /usr/bin/caffeinate -i "$PY" -m devlog_trade jobs "${JOBS_FILE:-$BOT_DIR/jobs.conf}"
