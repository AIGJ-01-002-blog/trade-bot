#!/bin/bash
# 맥에 매매 봇을 등록한다: ./macos/install.sh [몇 초마다, 기본 300]
# 1분봉을 돌리면 60으로 주세요. 지우기: ./macos/install.sh --uninstall
set -eu
BOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/life.devlog.trade-bot.plist"
LOG="$HOME/Library/Logs/devlog-trade-bot.log"
DOMAIN="gui/$(id -u)"
launchctl bootout "$DOMAIN" "$PLIST" 2>/dev/null || true
if [ "${1:-}" = "--uninstall" ]; then
  rm -f "$PLIST"
  echo "봇을 내렸어요. 상태 파일(trade-state-*.json)은 그대로 있어요."
  exit 0
fi
INTERVAL="${1:-300}"
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
sed -e "s#__BOT_DIR__#$BOT_DIR#g" -e "s#__INTERVAL__#$INTERVAL#g" -e "s#__LOG__#$LOG#g" \
  "$BOT_DIR/macos/life.devlog.trade-bot.plist" > "$PLIST"
launchctl bootstrap "$DOMAIN" "$PLIST"
echo "등록했어요: ${INTERVAL}초마다 $BOT_DIR/jobs.conf 를 돌려요. 로그: $LOG"
