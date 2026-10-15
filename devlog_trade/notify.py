"""사고팔 때마다 디스코드 채널로 보내는 알림. 실거래와 모의(테스트넷 포함)를 다른 채널로 나눈다.

- 주소는 환경 변수 DISCORD_WEBHOOK_LIVE·DISCORD_WEBHOOK_PAPER로만 받는다. 맥은 run-bot.sh가 키체인에서 읽는다.
  주소 자체가 비밀이라 로그·오류에도 남기지 않는다.
- 없으면 아무것도 안 한다. 보내지 못해도 매매와 종료 코드는 그대로다(다음 체결 알림은 따로 간다).
- devlog 서버의 디스코드 연결(078)과 같은 웹후크 방식이지만, 실계좌 체결을 서버에 보내지 않으려고 봇이 직접 보낸다.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable

from .broker import Fill

ENV = {"LIVE": "DISCORD_WEBHOOK_LIVE", "PAPER": "DISCORD_WEBHOOK_PAPER"}
LABEL = {"LIVE": "🔴 실거래", "TESTNET": "🧪 테스트넷", "PAPER": "📝 모의"}
# 사는 쪽(매수·롱)은 초록, 파는 쪽(매도·숏·정리)은 빨강 띠로 보인다
GREEN, RED = 0x2ECC71, 0xE74C3C
# 디스코드는 기본 urllib User-Agent를 막을 때가 있어 봇 이름을 준다
AGENT = "DiscordBot (https://github.com/AIGJ-01-002-blog/trade-bot, 1)"

Post = Callable[[str, bytes], None]


def _post(url: str, body: bytes) -> None:
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": AGENT})
    with urllib.request.urlopen(req, timeout=5) as r:
        r.read()


@dataclass
class Plan:
    """체결 알림에 붙일 계좌 규칙. 모르면 0(목표가·손절가를 안 보인다)"""
    stop_loss: Decimal = Decimal(0)
    take_profit: Decimal = Decimal(0)


def _money(v: Decimal, currency: str) -> str:
    if currency == "KRW":
        return f"{v:,.0f}원"
    return f"{v.quantize(Decimal('0.0001')).normalize():f} {currency}"


def _price(v: Decimal, currency: str) -> str:
    return f"{v:,.0f}원" if currency == "KRW" else f"{v.normalize():f} {currency}"


def color(fill: Fill) -> int:
    return GREEN if fill.side == "BUY" else RED


def _exit_kind(reason: str) -> str:
    if "손절" in reason:
        return "🛑 **손절**"
    if "익절" in reason:
        return "🎯 **익절**"
    return ""


def text(mode: str, account: str, market: str, fill: Fill, plan: Plan, balance: str, day_pnl: Decimal | None) -> str:
    cur = fill.currency or "KRW"
    entry = fill.realized_pnl is None
    if market == "futures":
        what = ("롱 진입" if fill.side == "BUY" else "숏 진입") if entry else "포지션 정리"
    else:
        what = "매수" if fill.side == "BUY" else "매도"
    lines = [f"**[{LABEL.get(mode, mode)}] {what} {fill.symbol_name or fill.symbol}** ({fill.symbol})",
             f"가격 {_price(fill.price, cur)} · 수량 {fill.quantity.normalize():f} · 금액 {_money(fill.price * fill.quantity, cur)}"]
    kind = "" if entry else _exit_kind(fill.reason)
    if kind:
        lines.insert(0, kind)
    if entry:
        short = market == "futures" and fill.side == "SELL"
        if plan.take_profit > 0:
            lines.append(f"**익절가 {_price(fill.price * (1 - plan.take_profit if short else 1 + plan.take_profit), cur)} "
                         f"(+{(plan.take_profit * 100).normalize():f}%)**")
        if plan.stop_loss > 0:
            lines.append(f"**손절가 {_price(fill.price * (1 + plan.stop_loss if short else 1 - plan.stop_loss), cur)} "
                         f"(-{(plan.stop_loss * 100).normalize():f}%)**")
    else:
        pnl = fill.realized_pnl
        cost = fill.price * fill.quantity - fill.fee - pnl  # 매수에 쓴 돈 (pnl = 판 돈 - 산 돈 - 수수료)
        pct = f" ({pnl / cost * 100:+.2f}%)" if market != "futures" and cost > 0 else ""
        lines.append(f"이번 손익 {'+' if pnl >= 0 else ''}{_money(pnl, cur)}{pct}")
        if day_pnl is not None:
            lines.append(f"오늘 손익 {'+' if day_pnl >= 0 else ''}{_money(day_pnl, cur)}")
    lines.append(f"잔액 {balance}")
    lines.append(f"이유: {fill.reason[:300]} · 계좌 {account}")
    return "\n".join(lines)


def send(mode: str, message: str, post: Post | None = None, colour: int | None = None) -> bool:
    """실거래는 LIVE 채널, 모의·테스트넷은 PAPER 채널로. 채널 주소가 없으면 False.
    색을 주면 왼쪽에 색 띠가 붙는 임베드로 보낸다(본문 4096자까지)"""
    url = os.environ.get(ENV["LIVE" if mode == "LIVE" else "PAPER"], "").strip()
    if not url:
        return False
    try:
        body = {"username": "devlog 매매 봇", "allowed_mentions": {"parse": []}}
        if colour is None:
            body["content"] = message[:1900]
        else:
            body["embeds"] = [{"description": message[:4000], "color": colour}]
        (post or _post)(url, json.dumps(body, ensure_ascii=False).encode("utf-8"))
        return True
    except Exception as e:  # noqa: BLE001 - 알림 실패가 매매를 멈추면 안 된다. 주소가 섞일 수 있어 종류만 남긴다
        print(f"디스코드 알림을 보내지 못했어요 ({type(e).__name__}). 매매는 그대로 진행해요.", file=sys.stderr)
        return False
