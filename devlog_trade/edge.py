"""우위 스캔(#34)을 통과한 두 규칙을 모의 계좌로 돌린다 (#40). 실계좌에는 쓰지 않는다.

- breakout: 1시간봉 종가가 앞선 100봉 최고가를 넘고, 닫힌 일봉 종가가 50일 평균선 위면 산다. 앞선 50봉 최저가 아래로 닫히면 판다.
- volbreak: 1시간봉 종가가 오늘(UTC) 시가 + 0.7 × 어제 폭을 넘으면 산다. 그날 마지막 봉(UTC 23시 봉)이 닫힐 때 판다. 하루 한 번.

둘 다 롱만, 레버리지 1배, 코인마다 (계좌 ÷ 코인 수) × 0.5만 쓴다. 백테스트는 research/edge_scan.py와 같은 규칙이다.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Callable

from .exchanges import Candle, Exchange, ExchangeError

RULES = ("breakout", "volbreak")
VERSION = {"breakout": "breakout-v0.1", "volbreak": "volbreak-v0.1"}
SHARE = Decimal("0.5")  # 코인마다 몫의 절반만 쓴다 (스캔의 '절반 비중')
COIN_STEP = Decimal("0.00000001")


@dataclass
class Params:
    breakout_bars: int = 100
    exit_bars: int = 50
    regime_sma: int = 50
    k: float = 0.7


@dataclass
class Book:
    """규칙이 기억할 것: 변동성 돌파는 하루 한 번만 들어가므로 마지막으로 들어간 날"""
    entered_day: dict[str, str] = field(default_factory=dict)
    last_px: dict[str, float] = field(default_factory=dict)

    @staticmethod
    def path(state: Path) -> Path:
        return state.with_name(state.stem + ".edge.json")

    @classmethod
    def load(cls, state: Path) -> Book:
        p = cls.path(state)
        if not p.exists():
            return cls()
        raw = json.loads(p.read_text(encoding="utf-8"))
        return cls(raw.get("entered_day", {}), raw.get("last_px", {}))

    def save(self, state: Path) -> None:
        self.path(state).write_text(json.dumps({"entered_day": self.entered_day, "last_px": self.last_px},
                                               ensure_ascii=False, indent=2), encoding="utf-8")


def breakout_signal(h1: list[Candle], i: int, d1: list[Candle], p: Params) -> str | None:
    """i번째(닫힌) 1시간봉에서 BUY·SELL·None. 들고 있는지는 부르는 쪽이 본다"""
    k = h1[i]
    if i >= p.exit_bars and k.close < min(c.low for c in h1[i - p.exit_bars:i]):
        return "SELL"
    if i < p.breakout_bars:
        return None
    if k.close <= max(c.high for c in h1[i - p.breakout_bars:i]):
        return None
    closed = [c.close for c in d1 if c.close_time <= k.close_time]  # 이 1시간봉이 닫힐 때까지 닫힌 일봉만
    if len(closed) < p.regime_sma:
        return None
    return "BUY" if closed[-1] > sum(closed[-p.regime_sma:]) / p.regime_sma else None


def volbreak_target(h1: list[Candle], i: int, d1: list[Candle], p: Params) -> Decimal | None:
    """오늘(UTC) 시가 + k × 어제 폭. 오늘 시가는 오늘 첫 1시간봉 시가, 어제 폭은 어제 일봉(닫힌 것)"""
    k = h1[i]
    day = k.open_time.replace(hour=0, minute=0, second=0, microsecond=0)
    first = next((c for c in h1[:i + 1] if c.open_time == day), None)
    prev = next((c for c in d1 if c.open_time == day - timedelta(days=1) and c.close_time <= k.close_time), None)
    if first is None or first.open is None or prev is None or prev.high is None or prev.low is None:
        return None
    return first.open + Decimal(str(p.k)) * (prev.high - prev.low)


class Edge:
    def __init__(self, rule: str, session, ex: Exchange, book: Book, n_symbols: int, p: Params = Params()):
        self.rule, self.s, self.ex, self.book, self.p = rule, session, ex, book, p
        self.b = session.broker
        self.futures = self.b.market == "futures"
        self.n = max(1, n_symbols)

    def equity(self) -> Decimal:
        px = self.book.last_px
        if self.futures:
            return self.b.cash + sum((pos.unrealized(Decimal(str(px.get(s, float(pos.entry))))) for s, pos in self.b.positions.items()),
                                     Decimal(0))
        return self.b.cash + sum((pos.quantity * Decimal(str(px.get(s, float(pos.avg_price)))) for s, pos in self.b.positions.items()),
                                 Decimal(0))

    def _buy(self, symbol: str, name: str, k: Candle, reason: str, at: str) -> None:
        budget = self.equity() / self.n * SHARE
        free = self.b.available() if self.futures else self.b.cash
        spend = min(budget, free) / (1 + self.b.fee_rate)
        qty = (spend / k.close).quantize(COIN_STEP, rounding=ROUND_FLOOR)
        if qty <= 0:
            return
        reason = f"[{VERSION[self.rule]}] {reason}"
        if self.futures:
            keep = self.b.leverage
            self.b.leverage = Decimal(1)
            fill = self.b.open(symbol, name, "LONG", qty, k.close, reason, at)
            self.b.leverage = keep
        else:
            fill = self.b.buy(symbol, name, qty, k.close, reason, at)
        if fill is not None:
            self.s._add(fill)

    def _sell(self, symbol: str, k: Candle, reason: str, at: str) -> None:
        reason = f"[{VERSION[self.rule]}] {reason}"
        if self.futures:
            fill = self.b.close(symbol, k.close, reason, at)
        else:
            pos = self.b.positions[symbol]
            fill = self.b.sell(symbol, self.ex.coin_name(symbol), pos.quantity, k.close, reason, at)
        if fill is not None:
            self.s._add(fill)

    def step(self, symbol: str, name: str, h1: list[Candle], i: int, d1: list[Candle], at: str) -> None:
        k, p = h1[i], self.p
        held = symbol in self.b.positions
        if self.rule == "breakout":
            sig = breakout_signal(h1, i, d1, p)
            if held and sig == "SELL":
                self._sell(symbol, k, f"종가가 앞선 {p.exit_bars}봉 최저가 아래로 닫힘", at)
            elif not held and sig == "BUY":
                self._buy(symbol, name, k, f"종가가 앞선 {p.breakout_bars}봉 최고가를 넘음 · 일봉 {p.regime_sma}일 평균선 위", at)
            return
        day = k.open_time.strftime("%Y-%m-%d")
        if held and k.open_time.hour == 23:
            self._sell(symbol, k, "그날(UTC) 마지막 봉이 닫힘", at)
            return
        if held or k.open_time.hour == 23 or self.book.entered_day.get(symbol) == day:
            return
        target = volbreak_target(h1, i, d1, p)
        if target is not None and k.close > target:
            self._buy(symbol, name, k, f"종가가 오늘 시가 + {p.k} × 어제 폭({target:f})을 넘음", at)
            if symbol in self.b.positions:
                self.book.entered_day[symbol] = day

    def trade(self, symbol: str, name: str, h1: list[Candle], d1: list[Candle], executed_at: Callable[[Candle], str]) -> None:
        seen = self.b.last_seen.get(symbol, "")
        keys = [c.open_time.strftime("%Y-%m-%dT%H:%M:%SZ") for c in h1]
        if not seen and len(h1) >= 2:
            seen = keys[-2]  # 처음 보는 종목은 지난 봉으로 거래하지 않고 마지막 닫힌 봉부터 본다
        for i, k in enumerate(h1):
            if keys[i] <= seen or k.high is None:
                continue
            self.book.last_px[symbol] = float(k.close)
            self.step(symbol, name, h1, i, d1, executed_at(k))
        if keys:
            self.b.last_seen[symbol] = max(self.b.last_seen.get(symbol, ""), keys[-1])

    def describe(self) -> str:
        held = ", ".join(self.b.positions) or "없음"
        return f"{VERSION[self.rule]} · 계좌 {self.equity():.2f} {self.b.currency} · 들고 있는 것 {held}"


def run(rule: str, session, ex: Exchange, symbols: list[str], limit: int,
        executed_at: Callable[[Candle], str], p: Params = Params()) -> tuple[bool, list[str]]:
    """coin --rule breakout|volbreak 한 번: 종목마다 1시간봉과 일봉을 받아 판단한다. (모두 받았는지, 받은 것 요약)"""
    state = Path(session.state)
    book = Book.load(state)
    edge = Edge(rule, session, ex, book, len(symbols), p)
    ok, seen = True, []
    for symbol in symbols:
        try:
            h1 = ex.fetch_candles(symbol, "1h", max(limit, p.breakout_bars + 2))
            d1 = ex.fetch_candles(symbol, "1d", p.regime_sma + 10)
        except (ExchangeError, OSError, ValueError, KeyError) as e:
            print(f"{ex.code} {symbol} 시세를 받지 못했어요 (다음 실행 때 다시 해요): {e}", file=sys.stderr)
            ok = False
            continue
        edge.trade(symbol, ex.coin_name(symbol), h1, d1, executed_at)
        seen.append(f"{symbol} {len(h1)}/{len(d1)}개")
    book.save(state)
    print(edge.describe())
    return ok, seen

