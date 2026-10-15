"""매매 봇 MCP 서버. 대화하는 AI(Claude Desktop·Claude Code·Codex CLI)가 봇의 시세·계좌를 보고 주문을 낼 수 있게 한다.

python -m devlog_trade.mcp --dir ~/trade-bot          : 모의 주문만 (기본)
python -m devlog_trade.mcp --dir ~/trade-bot --record : 모의 체결도 devlog 매매 일지에 남긴다 (DEVLOG_TOKEN 필요)

- 표준 입출력으로 JSON-RPC를 주고받는 MCP stdio 서버다. 표준 라이브러리만 쓴다.
- 도구 이름은 trade_bot_으로 시작한다. devlog의 MCP(record_trade 등 매매 일지)와 겹치지 않게, 이 서버는 일지를 직접 쓰지 않고
  봇이 체결을 남기는 같은 길(Session.send_all)로만 보낸다.
- 주문은 기본이 모의다. 실계좌 주문은 서버를 --live와 상한(--max-order·--max-order-loss·--max-daily-loss)으로 띄우고,
  실거래소면 DEVLOG_TRADE_LIVE=1까지 있어야 나간다. 도구 호출에서 live=true를 줘도 서버 설정을 넘지 못한다.
- 대화는 사람이 말을 걸 때만 움직인다. 봉마다 혼자 판단하는 자동 매매는 그대로 jobs.conf와 AI 판단(ai.py)이 맡는다.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Callable

from . import __version__, ai, report, rules
from . import live as live_orders
from .__main__ import COIN_STEP, KST, Session
from .broker import PaperBroker, state_lock
from .exchanges import COINS, EXCHANGES, INTERVALS, Exchange, ExchangeError
from .futures import FuturesBroker

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
ACCOUNT_FILE = re.compile(r"^trade-(state|live)-[A-Za-z0-9_.-]+\.json$")
STRATEGY = "mcp-manual"  # 매매 일지에서 대화로 낸 주문을 봇의 자동 주문과 나눈다


class ToolError(Exception):
    """도구 호출이 잘못됐거나 막혔을 때. 대화하는 AI에게 isError로 이유를 돌려준다"""


def _num(v: Decimal) -> str:
    return f"{v.quantize(COIN_STEP).normalize():f}"


def _mode(path: Path) -> str:
    """상태 파일이 어떤 돈인지. 실계좌 파일은 테스트넷으로 돌린 기록(testnet)이 있으면 TESTNET이다"""
    if not path.name.startswith("trade-live-"):
        return "PAPER"
    try:
        return "TESTNET" if json.loads(path.read_text(encoding="utf-8")).get("testnet") else "LIVE"
    except (OSError, ValueError):
        return "LIVE"


def _sma(closes: list[Decimal], n: int) -> str | None:
    return _num(sum(closes[-n:], Decimal(0)) / n) if len(closes) >= n else None


class TradeBotTools:
    """도구 하나가 메서드 하나다. 봇 폴더(상태 파일·사용량·켜고 끄기 파일이 있는 곳)를 기준으로 움직인다"""

    def __init__(self, root: Path, record: bool = False, live_args: argparse.Namespace | None = None,
                 exchange_for: Callable[[str, str], Exchange] | None = None, now: Callable[[], datetime] = lambda: datetime.now(KST)):
        self.root, self.record, self.live_args, self.now = root, record, live_args, now
        self.exchange_for = exchange_for or (lambda exchange, market: EXCHANGES[(exchange, market)]())

    # ---- 도구 목록 (MCP tools/list)
    def specs(self) -> list[dict]:
        coin = {"type": "string", "description": f"코인 이름 ({', '.join(COINS)}) 또는 마켓 코드(BTCUSDT, KRW-BTC)"}
        exchange = {"type": "string", "enum": sorted({e for e, _ in EXCHANGES}), "default": "binance"}
        market = {"type": "string", "enum": ["spot", "futures"], "default": "spot", "description": "선물은 바이낸스만"}
        return [
            {"name": "trade_bot_prices", "title": "시세와 지표",
             "description": "거래소 공개 캔들로 최근 종가와 이동평균(5·20봉), 등락률, 고가·저가를 봅니다. 다 만들어진 봉만 씁니다.",
             "inputSchema": {"type": "object", "properties": {
                 "coin": coin, "exchange": exchange, "market": market,
                 "interval": {"type": "string", "enum": list(INTERVALS), "default": "1h"},
                 "count": {"type": "integer", "minimum": 5, "maximum": 200, "default": 30, "description": "돌려줄 봉 수"}},
                 "required": ["coin"], "additionalProperties": False},
             "annotations": {"readOnlyHint": True, "openWorldHint": True}},
            {"name": "trade_bot_accounts", "title": "계좌·포지션·손익",
             "description": "봇 폴더의 모의·실계좌 상태 파일마다 현금, 보유 종목·포지션, 평균 단가, 지금 가격 기준 평가 손익을 봅니다. "
                            "AI 판단 켜짐 여부와 오늘 AI 호출 수·이번 달 추정 비용도 함께 줍니다. 실현 손익 기록은 devlog 매매 일지에 있습니다.",
             "inputSchema": {"type": "object", "properties": {
                 "with_prices": {"type": "boolean", "default": True, "description": "지금 가격을 받아 평가 손익을 계산할지"}},
                 "additionalProperties": False},
             "annotations": {"readOnlyHint": True, "openWorldHint": True}},
            {"name": "trade_bot_order", "title": "주문 (기본 모의)",
             "description": "계좌 하나에 시장가 주문을 냅니다. 현물은 BUY·SELL, 선물은 LONG·SHORT·CLOSE. 가격은 방금 닫힌 1분봉 종가입니다. "
                            "계좌는 trade_bot_accounts의 account 이름(trade-state-…json)을 씁니다. 체결은 봇과 같은 방식으로 기록되고 "
                            f"전략 이름은 {STRATEGY}입니다. 실계좌(trade-live-…json)는 서버가 --live로 떠 있고 live=true일 때만 나가며 "
                            "봇의 주문당·하루 손실 상한을 그대로 거칩니다.",
             "inputSchema": {"type": "object", "properties": {
                 "account": {"type": "string", "description": "상태 파일 이름, 예: trade-state-binance-spot-1h.json"},
                 "coin": coin,
                 "action": {"type": "string", "enum": ["BUY", "SELL", "LONG", "SHORT", "CLOSE"]},
                 "amount": {"type": "string", "description": "BUY·LONG·SHORT에 쓸 금액(계좌 통화, 선물은 증거금). 빼면 봇 기본값"},
                 "fraction": {"type": "string", "default": "1", "description": "SELL할 비율 (0~1, 기본 전부)"},
                 "reason": {"type": "string", "description": "매매 일지에 남길 이유 한 줄"},
                 "live": {"type": "boolean", "default": False, "description": "실계좌 주문임을 확인. 실계좌 상태 파일에만 씁니다"}},
                 "required": ["account", "coin", "action", "reason"], "additionalProperties": False},
             "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True}},
            {"name": "trade_bot_ai",
             "title": "AI 판단 켜고 끄기",
             "description": "봇의 자동 매매에서 AI 판단을 켜거나 끕니다(get은 지금 상태만). 봇 폴더의 trade-control.json에 남고, "
                            "jobs.conf의 --ai 설정보다 먼저 적용됩니다. clear는 파일을 지워 jobs.conf 설정으로 돌립니다.",
             "inputSchema": {"type": "object", "properties": {
                 "action": {"type": "string", "enum": ["get", "on", "off", "clear"], "default": "get"}},
                 "additionalProperties": False},
             "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
            {"name": "trade_bot_rules",
             "title": "매매 기준 정하기",
             "description": "모의 계좌마다 봇이 따를 기준(코인·이동평균 봉 수·손절·익절·하루 수익 멈춤·한 번 금액·멈춤)을 봅니다(get), 바꿉니다(set), "
                            "jobs.conf대로 되돌립니다(clear). 판단은 이 대화의 AI가 하고, 주문은 봇이 5분마다 이 기준과 상한 안에서 냅니다. "
                            "다음 바퀴부터 적용되고 devlog 자동매매 화면에 '기준을 정한 곳'으로 보입니다. 실계좌(trade-live-…)는 바꾸지 않습니다. "
                            "세 겹 거름망 계좌(trade-state-ladder-…)는 코인과 멈춤만 됩니다. 손절에 걸렸을 때 잃는 돈(금액 × 레버리지 × 손절)이 "
                            "처음 현금의 1%를 넘는 값은 거절합니다.",
             "inputSchema": {"type": "object", "properties": {
                 "action": {"type": "string", "enum": ["get", "set", "clear"], "default": "get"},
                 "account": {"type": "string", "description": "모의 계좌 상태 파일 이름, 예: trade-state-binance-spot-1h.json"},
                 "coins": {"type": "string", "description": f"쉼표로 1~{rules.MAX_COINS}개, 예: BTC,ETH"},
                 "window": {"type": "integer", "minimum": rules.WINDOW[0], "maximum": rules.WINDOW[1],
                            "description": "이동평균에 쓰는 봉 수"},
                 "stop_loss": {"type": "string", "description": "손절 비율, 0.005~0.2 (0.03 = -3%)"},
                 "take_profit": {"type": "string", "description": "익절 비율, 0(끔) 또는 0.005~0.5 (0.04 = +4%에서 정리)"},
                 "daily_profit_stop": {"type": "string",
                                       "description": "하루 실현 수익이 처음 현금의 이 비율에 닿으면 그날 새 진입을 쉼, 0(끔) 또는 0.005~0.2"},
                 "budget": {"type": "string", "description": "한 번 들어갈 금액(계좌 통화, 선물은 증거금). 봇 기본값 이하로 줄이기만 됩니다"},
                 "paused": {"type": "boolean", "description": "true면 이 계좌는 새로 사고팔지 않고 건너뜁니다"},
                 "reason": {"type": "string", "description": "왜 이렇게 정했는지 한 줄. set에 꼭 넣습니다"}},
                 "additionalProperties": False},
             "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
        ]

    def call(self, name: str, arguments: dict) -> str:
        handler = {"trade_bot_prices": self.prices, "trade_bot_accounts": self.accounts, "trade_bot_order": self.order,
                   "trade_bot_ai": self.ai_switch, "trade_bot_rules": self.rules}.get(name)
        if handler is None:
            raise ToolError(f"없는 도구예요: {name}")
        return json.dumps(handler(**(arguments or {})), ensure_ascii=False, indent=2)

    # ---- 시세
    def _exchange(self, exchange: str, market: str) -> Exchange:
        if (exchange, market) not in EXCHANGES:
            raise ToolError(f"{exchange}에는 {market} 시세가 없어요. 선물은 바이낸스만 돼요.")
        return self.exchange_for(exchange, market)

    @staticmethod
    def _symbol(ex: Exchange, coin: str) -> str:
        coin = coin.strip().upper()
        if not re.fullmatch(r"[A-Z0-9-]{2,20}", coin):
            raise ToolError(f"코인 이름이 이상해요: {coin}")
        return coin if coin not in COINS and (coin.endswith(("USDT", "BTC")) or "-" in coin) else ex.symbol_for(coin)

    def _last_price(self, ex: Exchange, symbol: str) -> Decimal:
        try:
            candles = ex.fetch_candles(symbol, "1m", 3)
        except (ExchangeError, OSError, ValueError, KeyError) as e:
            raise ToolError(f"{ex.code} {symbol} 시세를 받지 못했어요: {e}") from e
        if not candles:
            raise ToolError(f"{ex.code} {symbol} 닫힌 1분봉이 없어요.")
        return candles[-1].close

    def prices(self, coin: str, exchange: str = "binance", market: str = "spot", interval: str = "1h", count: int = 30) -> dict:
        if interval not in INTERVALS:
            raise ToolError(f"봉 간격은 {', '.join(INTERVALS)} 중 하나예요.")
        count = max(5, min(int(count), 200))
        ex = self._exchange(exchange, market)
        symbol = self._symbol(ex, coin)
        try:
            candles = ex.fetch_candles(symbol, interval, max(count, 21) + 1)
        except (ExchangeError, OSError, ValueError, KeyError) as e:
            raise ToolError(f"{ex.code} {symbol} 시세를 받지 못했어요: {e}") from e
        if not candles:
            raise ToolError(f"{ex.code} {symbol} 닫힌 {interval} 봉이 아직 없어요.")
        closes = [c.close for c in candles]
        shown = candles[-count:]
        first, last = shown[0].close, shown[-1].close
        return {"exchange": ex.code, "market": ex.market, "symbol": symbol, "name": ex.coin_name(symbol), "currency": ex.currency,
                "interval": interval, "last_close": _num(last), "last_close_at": shown[-1].close_time.astimezone(KST).isoformat(timespec="seconds"),
                "sma5": _sma(closes, 5), "sma20": _sma(closes, 20),
                "change_pct": _num((last - first) / first * 100) if first else None,
                "high": _num(max(c.close for c in shown)), "low": _num(min(c.close for c in shown)),
                "closes": [{"at": c.open_time.astimezone(KST).isoformat(timespec="minutes"), "close": _num(c.close)} for c in shown]}

    # ---- 계좌
    def _account_path(self, account: str) -> Path:
        if not ACCOUNT_FILE.match(account or ""):
            raise ToolError("account는 trade_bot_accounts에 나온 상태 파일 이름(trade-state-…json 또는 trade-live-…json)이에요.")
        path = self.root / account
        if not path.exists():
            raise ToolError(f"{account} 계좌가 없어요. 봇을 그 설정으로 한 번 돌리면 만들어져요.")
        return path

    def _load(self, path: Path) -> PaperBroker | FuturesBroker:
        data = json.loads(path.read_text(encoding="utf-8"))
        return FuturesBroker.load(path) if data.get("market") == "futures" else PaperBroker.load(path)

    def accounts(self, with_prices: bool = True) -> dict:
        """mode: PAPER(모의), TESTNET(거래소 테스트넷, 가짜 돈), LIVE(실거래소)"""
        out, prices = [], {}
        for path in sorted(self.root.glob("trade-*-*.json")):
            if not ACCOUNT_FILE.match(path.name) or path.name.endswith((".pending.json", ".ladder.json")):
                continue
            try:
                b = self._load(path)
            except (OSError, ValueError, KeyError) as e:
                out.append({"account": path.name, "error": f"읽지 못했어요: {e}"})
                continue
            exchange = (b.exchange or "").lower()
            acc = {"account": path.name, "mode": _mode(path), "exchange": b.exchange,
                   "market": b.market, "currency": b.currency, "cash": _num(b.cash), "positions": []}
            if b.market == "futures":
                acc["available"], acc["leverage"] = _num(b.available()), _num(b.leverage)
            pending = path.with_name(path.stem + ".pending.json")
            if pending.exists():
                acc["unsent_fills"] = len(json.loads(pending.read_text(encoding="utf-8")))
            total = Decimal(0)
            for symbol, p in b.positions.items():
                row = {"symbol": symbol}
                if b.market == "futures":
                    row.update(side=p.side, quantity=_num(p.quantity), entry=_num(p.entry), margin=_num(p.margin))
                else:
                    row.update(quantity=_num(p.quantity), avg_price=_num(p.avg_price))
                if with_prices and (exchange, b.market) in EXCHANGES:
                    key = (exchange, b.market, symbol)
                    try:
                        if key not in prices:
                            prices[key] = self._last_price(self._exchange(exchange, b.market), symbol)
                        price = prices[key]
                        pnl = p.unrealized(price) if b.market == "futures" else (price - p.avg_price) * p.quantity
                        row.update(price=_num(price), unrealized_pnl=_num(pnl))
                        total += pnl
                    except ToolError as e:
                        row["price_error"] = str(e)
                acc["positions"].append(row)
            if with_prices and acc["positions"]:
                acc["unrealized_pnl"] = _num(total)
            out.append(acc)
        calls, cost = ai.Budget(self.root / "trade-ai-usage.json", 0, Decimal(0)).used()
        return {"accounts": out, "ai": {"switch": ai.switch() or "jobs.conf 설정을 따름", "calls_today": calls,
                                         "cost_krw_this_month": f"{cost:.0f}"},
                "note": "실현 손익과 지난 체결은 devlog 매매 일지에서 봐요." if out else "아직 계좌가 없어요. 봇을 한 번 돌리면 생겨요."}

    # ---- 주문
    def _live_session_args(self, path: Path, leverage: Decimal) -> argparse.Namespace:
        la = self.live_args
        if la is None or not la.live:
            raise ToolError("실계좌 주문은 MCP 서버를 --live와 상한(--max-order·--max-order-loss·--max-daily-loss)으로 띄웠을 때만 돼요.")
        if not la.testnet and not self.record:
            raise ToolError("실거래소 주문은 devlog에 기록해야 해서 서버를 --record로 띄워야 해요.")
        return argparse.Namespace(state=str(path), dry_run=not self.record, server=la.server, strategy=STRATEGY, live=True,
                                  testnet=la.testnet, max_order=la.max_order, max_order_loss=la.max_order_loss,
                                  max_daily_loss=la.max_daily_loss, leverage=str(leverage))

    def order(self, account: str, coin: str, action: str, reason: str, amount: str | None = None, fraction: str = "1",
              live: bool = False) -> dict:
        path = self._account_path(account)
        action = action.upper()
        reason = " ".join(str(reason).split())[:200]
        if not reason:
            raise ToolError("reason에 이유 한 줄을 넣어 주세요. 매매 일지에 남아요.")
        is_live = path.name.startswith("trade-live-")
        if is_live != bool(live):
            raise ToolError("실계좌(trade-live-…json)에는 live=true를, 모의 계좌에는 live=false를 주세요.")
        data = json.loads(path.read_text(encoding="utf-8"))
        market, exchange = data.get("market", "spot"), str(data.get("exchange", "")).lower()
        allowed = ("LONG", "SHORT", "CLOSE") if market == "futures" else ("BUY", "SELL")
        if action not in allowed:
            raise ToolError(f"{market} 계좌는 {', '.join(allowed)}만 돼요.")
        ex = self._exchange(exchange, market)
        symbol = self._symbol(ex, coin)
        price = self._last_price(ex, symbol)
        at = self.now().astimezone(KST).isoformat(timespec="seconds")
        log = io.StringIO()
        try:
            with state_lock(path), redirect_stdout(log), redirect_stderr(log):
                if is_live:
                    args = self._live_session_args(path, Decimal(data.get("leverage", "1")))
                    s = Session(args, None, live_orders.open_live(args, exchange, market, path))
                else:
                    args = argparse.Namespace(state=str(path), dry_run=not self.record, server=os.environ.get("DEVLOG_SERVER", "https://devlog.life"),
                                              strategy=STRATEGY)
                    s = Session(args, None)
                fill = self._place(s.broker, ex, symbol, action, price, amount, fraction, f"[대화 주문] {reason}", at)
                s._add(fill)  # 매매 일지 보내기와 자동매매 화면 알림(report)을 봇 체결과 같은 길로
                sent = s.send_all()
                s.save()
        except TimeoutError as e:
            raise ToolError(str(e)) from e
        except (live_orders.LiveOrderError, ValueError, ArithmeticError) as e:
            raise ToolError(f"주문하지 못했어요: {e}") from e
        result = {"account": account, "mode": _mode(path), "symbol": symbol, "action": action,
                  "reference_price": _num(price), "filled": fill is not None, "account_after": s.broker.describe(),
                  "recorded": ("devlog에 남겼어요" if sent else "devlog에 못 남겨 다음 봇 실행 때 다시 보내요") if self.record and fill
                  else "devlog에 남기지 않았어요(서버가 --record 없이 떠 있음)" if fill else None}
        if fill:
            result["fill"] = {"side": fill.side, "quantity": _num(fill.quantity), "price": _num(fill.price), "fee": _num(fill.fee),
                              "realized_pnl": None if fill.realized_pnl is None else _num(fill.realized_pnl), "reason": fill.reason}
        else:
            result["why_not"] = "돈이 모자라거나, 팔 수량·정리할 포지션이 없거나, 이미 같은 종목 포지션이 있거나, 실계좌 상한에 걸렸어요."
        if log.getvalue().strip():
            result["log"] = log.getvalue().strip()[-1500:]
        return result

    @staticmethod
    def _place(b, ex: Exchange, symbol: str, action: str, price: Decimal, amount: str | None, fraction: str, reason: str, at: str):
        name = ex.coin_name(symbol)
        budget = Decimal(amount) if amount else ex.default_budget
        if budget <= 0:
            raise ValueError("amount는 0보다 커야 해요")
        if action == "BUY":
            spend = min(budget, b.cash)
            qty = (spend / (price * (1 + b.fee_rate))).quantize(COIN_STEP, rounding=ROUND_FLOOR)
            return b.buy(symbol, name, qty, price, reason, at)
        if action == "SELL":
            part = Decimal(fraction or "1")
            if not Decimal(0) < part <= 1:
                raise ValueError("fraction은 0보다 크고 1 이하예요")
            qty = (b.holding(symbol) * part).quantize(COIN_STEP, rounding=ROUND_FLOOR)
            return b.sell(symbol, name, qty, price, reason, at)
        if action == "CLOSE":
            return b.close(symbol, price, reason, at)
        return b.open(symbol, name, action, b.quantity_for(budget, price), price, reason, at)

    # ---- AI 켜고 끄기
    def ai_switch(self, action: str = "get") -> dict:
        path = ai.control_path()
        if action in ("on", "off"):
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps({"ai": action, "changed_at": self.now().isoformat(timespec="seconds")}), encoding="utf-8")
            tmp.replace(path)
        elif action == "clear":
            path.unlink(missing_ok=True)
        elif action != "get":
            raise ToolError("action은 get·on·off·clear 중 하나예요.")
        state = ai.switch()
        note = "다음 봉부터 적용돼요. 15분봉 이상에서만 AI가 판단해요."
        if state == "on" and not any(os.environ.get(k) for k in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY")):
            note += " 이 서버에는 AI 키가 안 보여요. 봇(launchd)에 키가 없으면 그 실행은 매매하지 않고 멈춰요."
        return {"ai": state or "jobs.conf 설정을 따름", "file": str(path), "note": note}

    # ---- 매매 기준 (규칙형: 판단은 대화의 AI, 주문은 봇 코드)
    def rules(self, action: str = "get", account: str = "", reason: str = "", **values) -> dict:
        limits = {"coins": f"1~{rules.MAX_COINS}개", "window": f"{rules.WINDOW[0]}~{rules.WINDOW[1]}봉",
                  "stop_loss": f"{rules.STOP_LOSS[0]}~{rules.STOP_LOSS[1]}",
                  "take_profit": f"0(끔) 또는 {rules.TAKE_PROFIT[0]}~{rules.TAKE_PROFIT[1]}",
                  "daily_profit_stop": f"0(끔) 또는 {rules.DAILY_PROFIT_STOP[0]}~{rules.DAILY_PROFIT_STOP[1]}", "budget": "봇 기본값 이하(줄이기만)",
                  "한 번 손실": "금액 × 레버리지 × 손절 ≤ 처음 현금의 1%", "레버리지": "대화로 바꾸지 않음"}
        if action == "get":
            try:
                applied = json.loads((self.root / report.OUTBOX).read_text(encoding="utf-8")).get("rules", {})
            except (OSError, ValueError):
                applied = {}
            return {"chosen": rules.load()["accounts"], "applied_last_round": applied, "limits": limits,
                    "note": "chosen은 대화에서 정한 값, applied_last_round는 봇이 지난 바퀴에 실제로 쓴 기준이에요."}
        if action not in ("set", "clear"):
            raise ToolError("action은 get·set·clear 중 하나예요.")
        if not rules.ACCOUNT.match(account or ""):
            raise ToolError("기준은 모의 계좌(trade-state-…json)만 바꿀 수 있어요. 실계좌는 대화로 바꾸지 않아요.")
        path = self._account_path(account)
        if action == "clear":
            rules.save(account, {}, "", self.now())
            return {"account": account, "chosen": {}, "note": "jobs.conf 기준으로 돌아가요. 다음 바퀴부터 적용돼요."}
        if not " ".join(str(reason).split()):
            raise ToolError("reason에 왜 이렇게 정했는지 한 줄을 넣어 주세요. 자동매매 화면에 남아요.")
        data = json.loads(path.read_text(encoding="utf-8"))
        ex = self._exchange(str(data.get("exchange", "")).lower(), data.get("market", "spot"))
        ladder = account.startswith("trade-state-ladder-")
        try:
            cleaned = rules.clean(account, values, ladder, ex.default_budget)
            if not ladder:  # 이미 정한 값과 합쳐 한 번 손실 1% 상한을 본다
                merged = {**rules.for_account(account), **cleaned}
                rules.check_risk(Decimal(merged.get("budget") or ex.default_budget), Decimal(merged.get("stop_loss") or "0.03"),
                                 Decimal(data.get("leverage", "1")), ex.default_cash)
        except rules.RuleError as e:
            raise ToolError(str(e)) from e
        if not cleaned:
            raise ToolError("바꿀 값(coins·window·stop_loss·budget·paused)을 하나 이상 주세요.")
        return {"account": account, "chosen": rules.save(account, cleaned, reason, self.now()), "limits": limits,
                "note": "다음 바퀴(5분 안)부터 이 기준으로 주문해요. coins에서 뺀 코인을 들고 있으면 봇이 더는 손절·매도하지 않으니 "
                        "trade_bot_order로 정리해 주세요."}


# ---- JSON-RPC over stdio
class Server:
    def __init__(self, tools: TradeBotTools):
        self.tools = tools

    def handle(self, msg: dict) -> dict | None:
        method, mid = msg.get("method"), msg.get("id")
        if mid is None:  # 알림(notifications/initialized 등)에는 답하지 않는다
            return None
        try:
            if method == "initialize":
                asked = (msg.get("params") or {}).get("protocolVersion")
                result = {"protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                          "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": {"name": "devlog-trade-bot", "title": "devlog 매매 봇", "version": __version__},
                          "instructions": "코인 매매 봇의 시세·계좌를 보고 주문을 냅니다. 주문은 기본이 모의매매예요. "
                                          "매매 일지 조회·기록은 devlog MCP가 맡고, 이 서버는 봇 계좌만 다룹니다."}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": self.tools.specs()}
            elif method == "tools/call":
                params = msg.get("params") or {}
                try:
                    text = self.tools.call(params.get("name", ""), params.get("arguments") or {})
                    result = {"content": [{"type": "text", "text": text}], "isError": False}
                except (ToolError, TypeError, ValueError, ArithmeticError) as e:
                    result = {"content": [{"type": "text", "text": str(e)}], "isError": True}
            else:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"모르는 메서드: {method}"}}
        except Exception as e:  # noqa: BLE001 (서버가 죽지 않고 이유를 돌려준다)
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)[:500]}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def serve(self, stdin=None, stdout=None) -> None:
        stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "JSON이 아니에요"}}
            else:
                reply = self.handle(msg) if isinstance(msg, dict) else \
                    {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "묶음 요청은 받지 않아요"}}
            if reply is not None:
                stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
                stdout.flush()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="devlog_trade.mcp", description="매매 봇 MCP 서버(stdio). 대화하는 AI가 시세·계좌를 보고 주문해요.")
    p.add_argument("--dir", default=".", help="봇 폴더(상태 파일·trade-ai-usage.json·trade-control.json이 있는 곳)")
    p.add_argument("--record", action="store_true", help="대화로 낸 체결도 devlog 매매 일지에 남겨요 (DEVLOG_TOKEN 필요)")
    p.add_argument("--server", default="https://devlog.life", help="devlog 주소")
    p.add_argument("--live", action="store_true", help=f"실계좌 주문을 허용해요. 실거래소는 {live_orders.LIVE_ENV}=1과 --record도 있어야 해요")
    live_orders.add_live_args(p)
    args = p.parse_args(argv)
    root = Path(args.dir).expanduser().resolve()
    os.chdir(root)  # 봇과 같은 상대 경로(trade-ai-usage.json·trade-control.json)를 쓴다
    os.environ.setdefault("DEVLOG_SERVER", args.server)
    Server(TradeBotTools(root, record=args.record, live_args=args)).serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
