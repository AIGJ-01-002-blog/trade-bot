"""세 겹 거름망 복리법(ladder v0.1) — 코인 모의매매 전용. 설계: 프로젝트 노트 trade-bot-compound-method-2026-10-15.md

판단 순서(1시간봉이 닫힐 때마다, 코인마다)
  ① 1일봉 방향: 종가 > SMA50이고 SMA50이 5일 전보다 높으면 롱만, 반대면 숏만(선물), 나머지는 새 진입 없음
  ② 4시간봉 자리: EMA20 > EMA50이고 최근 6봉 안에 저가가 EMA20 × 1.01 이하(되돌림). 숏은 뒤집은 것
  ③ 1시간봉 방아쇠: 종가가 직전 10봉 최고가 위(숏은 최저가 아래)
  ④ 과열: 1일 종가 > SMA200 × 1.5이거나 선물 펀딩비 연환산 30% 이상이면 롱 금지, 공포·탐욕 80 이상이면 롱 크기 절반
  ⑤ 크기: 내 기록으로 켈리(윌슨 하한 승률) × 1/4, 30건 전에는 위험 0.5% 고정, 단계 상한 0.5 → 0.75 → 1%.
     켈리가 0 이하이면 돈을 넣지 않고 그림자 거래로 R만 기록해 다시 플러스가 되면 돌아온다
  ⑥ 손절 1.5 × ATR14(4시간)을 1.5~6%로 자름(6% 넘게 흔들리면 건너뜀). +1R에 1/3 익절 뒤 손절 본전, +2R에 1/3 익절,
     나머지는 1시간 종가가 EMA20 아래(숏은 위)면 정리. 1일 방향이 바뀌면 전부 정리, 선물은 72시간이면 정리
  ⑦ 하루 -3%·한 주 -6%면 새 진입 없음, 낙폭 10%면 크기 절반, 달마다 트레이딩 수익 30%를 코어로 옮김(모의 장부)

백테스트(research/ladder.py, PR #9)와 같은 규칙이다. 모의 계좌에서만 돈다(coin --rule ladder, --live 거절).
뉴스 거부권(#15)과 AI 판단 형식(#16)은 아직 없다.
"""
from __future__ import annotations

import json
import math
import sys
import urllib.parse
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Callable

from .exchanges import Candle, Exchange, ExchangeError, http_get_json

SATELLITE = {"XRP", "SOL", "DOGE"}
COIN_STEP = Decimal("0.00000001")
VERSION = "ladder-v0.1"


@dataclass(frozen=True)
class Params:
    regime_sma: int = 50
    regime_slope_days: int = 5
    ema_fast: int = 20
    ema_slow: int = 50
    pullback_bars: int = 6
    pullback_band: float = 0.01
    breakout_bars: int = 10
    atr_len: int = 14
    atr_mult: float = 1.5
    stop_min: float = 0.015
    stop_max: float = 0.06
    overheat_sma: int = 200
    overheat_ratio: float = 1.5
    funding_overheat_apr: float = 0.30
    greed_level: int = 80
    kelly_fraction: float = 0.25
    kelly_min_samples: int = 30
    kelly_window: int = 50
    wilson_z: float = 1.2816  # 80% 신뢰구간 아래쪽
    fixed_risk: float = 0.005
    risk_floor: float = 0.0025
    risk_stages: tuple = ((50, 0.005), (100, 0.0075), (10**9, 0.01))  # (실제 거래 수 미만, 상한)
    max_notional_pct: float = 0.20
    max_open_risk: float = 0.03
    max_same_side: int = 3
    drawdown_cut: float = 0.10
    daily_stop: float = 0.03
    weekly_stop: float = 0.06
    margin_cap_pct: float = 0.10
    leverage_stages: tuple = ((30, 2), (100, 3), (10**9, 5))
    liq_buffer: float = 3.0
    maint_margin: float = 0.005
    time_exit_hours: int = 72
    short_size: float = 0.5
    satellite_size: float = 0.5
    core_transfer: float = 0.30


# ---------------------------------------------------------------- 지표 (pandas 없이, research/ladder.py와 같은 정의)
def sma(xs: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(xs)
    run = 0.0
    for i, x in enumerate(xs):
        run += x
        if i >= n:
            run -= xs[i - n]
        if i >= n - 1:
            out[i] = run / n
    return out


def ema(xs: list[float], n: int) -> list[float | None]:
    """pandas ewm(span=n, adjust=False, min_periods=n)와 같다: 첫 값부터 쌓고 n번째부터 보인다"""
    out: list[float | None] = [None] * len(xs)
    a = 2 / (n + 1)
    e = None
    for i, x in enumerate(xs):
        e = x if e is None else a * x + (1 - a) * e
        if i >= n - 1:
            out[i] = e
    return out


def atr(c: list[Candle], n: int) -> list[float | None]:
    """와일더 방식(ewm alpha=1/n, adjust=False, min_periods=n)"""
    out: list[float | None] = [None] * len(c)
    e = None
    for i, k in enumerate(c):
        hi, lo = float(k.high), float(k.low)
        tr = hi - lo if i == 0 else max(hi - lo, abs(hi - float(c[i - 1].close)), abs(lo - float(c[i - 1].close)))
        e = tr if e is None else tr / n + (1 - 1 / n) * e
        if i >= n - 1:
            out[i] = e
    return out


def wilson_low(wins: int, n: int, z: float) -> float:
    if n == 0:
        return 0.0
    ph = wins / n
    den = 1 + z * z / n
    return (ph + z * z / (2 * n) - z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n))) / den


@dataclass
class View:
    """1시간봉 하나가 닫힌 시점에 보이는 판정 (미래 값 없음)"""
    regime: int  # 1 롱만, -1 숏만, 0 새 진입 없음
    long_ok: bool
    short_ok: bool
    overheat: bool
    stop_raw: float | None  # 손절 거리(비율), ATR 기준
    ema1: float | None


def view(h1: list[Candle], i: int, h4: list[Candle], d1: list[Candle], p: Params) -> View:
    """h1[i]가 닫힌 시각까지 닫힌 4시간·1일봉만 쓴다"""
    t = h1[i].close_time
    d = [k for k in d1 if k.close_time <= t]
    q = [k for k in h4 if k.close_time <= t]
    dc = [float(k.close) for k in d]
    s = sma(dc, p.regime_sma)
    regime = 0
    if len(d) > p.regime_slope_days and s[-1] is not None and s[-1 - p.regime_slope_days] is not None:
        if dc[-1] > s[-1] > s[-1 - p.regime_slope_days]:
            regime = 1
        elif dc[-1] < s[-1] < s[-1 - p.regime_slope_days]:
            regime = -1
    long_sma = sma(dc, p.overheat_sma)
    overheat = bool(d) and long_sma[-1] is not None and dc[-1] > long_sma[-1] * p.overheat_ratio
    qc = [float(k.close) for k in q]
    ef, es = ema(qc, p.ema_fast), ema(qc, p.ema_slow)
    setup_long = setup_short = False
    stop_raw = None
    if q and ef[-1] is not None and es[-1] is not None:
        recent = range(max(0, len(q) - p.pullback_bars), len(q))
        near_low = any(ef[j] is not None and float(q[j].low) <= ef[j] * (1 + p.pullback_band) for j in recent)
        near_high = any(ef[j] is not None and float(q[j].high) >= ef[j] * (1 - p.pullback_band) for j in recent)
        setup_long = ef[-1] > es[-1] and near_low
        setup_short = ef[-1] < es[-1] and near_high
        a = atr(q, p.atr_len)[-1]
        stop_raw = None if a is None else p.atr_mult * a / qc[-1]
    hc = [float(k.close) for k in h1[: i + 1]]
    trig_long = trig_short = False
    if i >= p.breakout_bars:
        prev = h1[i - p.breakout_bars: i]
        trig_long = hc[-1] > max(float(k.high) for k in prev)
        trig_short = hc[-1] < min(float(k.low) for k in prev)
    return View(regime, regime == 1 and setup_long and trig_long, regime == -1 and setup_short and trig_short, overheat,
                stop_raw, ema(hc, p.ema_fast)[-1])


# ---------------------------------------------------------------- 장부 (상태 파일 옆 <state>.ladder.json)
@dataclass
class Leg:
    """기법이 들고 있는 포지션 하나. 실제 수량은 브로커에 있고 여기는 손절·익절 자리와 R 계산용 값을 둔다"""
    side: int
    entry: float
    stop: float
    stop_pct: float
    qty0: float  # 처음 수량(그림자는 1/entry)
    risk_amt: float  # 손절에 닿으면 잃는 돈(그림자는 stop_pct)
    opened: str  # 봉이 닫힌 시각 ISO
    tp1_done: bool = False
    tp2_done: bool = False
    pnl: float = 0.0  # 수수료 뺀 실현 손익(그림자는 R 단위 계산용)
    shadow: bool = False
    lev: float = 1.0

    @property
    def tp1(self) -> float:
        return self.entry * (1 + self.side * self.stop_pct)

    @property
    def tp2(self) -> float:
        return self.entry * (1 + self.side * 2 * self.stop_pct)


@dataclass
class Book:
    legs: dict[str, Leg] = field(default_factory=dict)
    history: list[dict] = field(default_factory=list)  # 닫힌 거래 {symbol, side, r, shadow, closed}
    real_trades: int = 0
    last_px: dict[str, float] = field(default_factory=dict)
    peak: float = 0.0
    day: str = ""
    day_start: float = 0.0
    week: str = ""
    week_start: float = 0.0
    month: str = ""
    month_start: float = 0.0
    core: float = 0.0  # 코어로 옮긴 돈(모의 장부에서 빼 둔 금액)
    blocked_until: str = ""  # 하루·주 손실 브레이크가 풀리는 날(UTC 날짜)

    @staticmethod
    def path_for(state: Path) -> Path:
        return state.with_name(state.stem + ".ladder.json")

    @classmethod
    def load(cls, state: Path) -> "Book":
        p = cls.path_for(state)
        if not p.exists():
            return cls()
        d = json.loads(p.read_text(encoding="utf-8"))
        d["legs"] = {s: Leg(**v) for s, v in d.get("legs", {}).items()}
        return cls(**d)

    def save(self, state: Path) -> None:
        p = self.path_for(state)
        tmp = p.with_suffix(".tmp")
        data = asdict(self)
        data["history"] = data["history"][-200:]
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)


# ---------------------------------------------------------------- 바깥 자료 (실패하면 없는 것으로 본다)
def fear_greed(fetch: Callable[[str], object] = http_get_json) -> float | None:
    try:
        d = fetch("https://api.alternative.me/fng/?limit=1")
        return float(d["data"][0]["value"])  # type: ignore[index]
    except (OSError, ValueError, KeyError, IndexError, TypeError, ExchangeError):
        return None


def funding_apr(symbol: str, fetch: Callable[[str], object] = http_get_json) -> float:
    """바이낸스 선물의 마지막 펀딩비 × 하루 3번 × 365. 받지 못하면 0(과열로 보지 않음)"""
    try:
        d = fetch("https://fapi.binance.com/fapi/v1/premiumIndex?" + urllib.parse.urlencode({"symbol": symbol}))
        return float(d["lastFundingRate"]) * 3 * 365  # type: ignore[index]
    except (OSError, ValueError, KeyError, TypeError, ExchangeError):
        return 0.0


# ---------------------------------------------------------------- 매매
class Ladder:
    def __init__(self, session, ex: Exchange, book: Book, p: Params = Params(), fng: float | None = None,
                 funding: Callable[[str], float] | None = None, lev_cap: int = 5):
        self.s, self.ex, self.book, self.p = session, ex, book, p
        self.b = session.broker
        self.futures = self.b.market == "futures"
        self.fng = fng
        self.funding = funding or (lambda _s: 0.0)
        self.lev_cap = lev_cap

    # 잔고
    def equity(self) -> float:
        px = self.book.last_px
        if self.futures:
            return float(self.b.cash) + sum(float(pos.unrealized(Decimal(str(px.get(s, float(pos.entry))))))
                                            for s, pos in self.b.positions.items())
        return float(self.b.cash) + sum(float(pos.quantity) * px.get(s, float(pos.avg_price)) for s, pos in self.b.positions.items())

    def _roll(self, closed_at: datetime) -> None:
        """하루·주·달이 바뀌면 기준 잔고를 새로 잡고, 달이 바뀌면 지난달 수익의 30%를 코어로 옮긴다"""
        bk, eq = self.book, self.equity()
        day = closed_at.strftime("%Y-%m-%d")
        if bk.day == day:
            return
        bk.day, bk.day_start = day, eq
        bk.peak = max(bk.peak, eq)
        week = (closed_at - timedelta(days=closed_at.weekday())).strftime("%Y-%m-%d")
        if bk.week != week:
            bk.week, bk.week_start = week, eq
        month = closed_at.strftime("%Y-%m")
        if bk.month != month:
            gain = eq - bk.month_start if bk.month else 0.0
            if gain > 0:
                free = float(self.b.available()) if self.futures else float(self.b.cash)
                amt = Decimal(str(max(0.0, min(gain * self.p.core_transfer, free)))).quantize(
                    Decimal(1) if self.b.currency == "KRW" else COIN_STEP, rounding=ROUND_FLOOR)
                if amt > 0:
                    self.b.cash -= amt
                    bk.core += float(amt)
                    eq -= float(amt)
                    bk.day_start = eq
                    print(f"[ladder] 지난달 트레이딩 수익의 30% {amt:f} {self.b.currency}를 코어로 옮겼어요 (모의 장부)")
            bk.month, bk.month_start = month, eq

    def _blocked(self, closed_at: datetime) -> bool:
        bk, eq = self.book, self.equity()
        day = closed_at.strftime("%Y-%m-%d")
        if bk.blocked_until and day < bk.blocked_until:
            return True
        if bk.day_start and eq <= bk.day_start * (1 - self.p.daily_stop):
            bk.blocked_until = (closed_at + timedelta(days=1)).strftime("%Y-%m-%d")
            return True
        if bk.week_start and eq <= bk.week_start * (1 - self.p.weekly_stop):
            bk.blocked_until = (closed_at + timedelta(days=7 - closed_at.weekday())).strftime("%Y-%m-%d")
            return True
        return False

    # 크기
    def _risk(self, side: int, coin: str) -> float | None:
        p, bk = self.p, self.book
        cap = next(c for lim, c in p.risk_stages if bk.real_trades < lim)
        same = [t["r"] for t in bk.history if t["side"] == side][-p.kelly_window:]
        pool = same if len(same) >= p.kelly_min_samples else [t["r"] for t in bk.history][-p.kelly_window:]
        if len(pool) >= p.kelly_min_samples:
            wins = [r for r in pool if r > 0]
            losses = [-r for r in pool if r <= 0]
            w_low = wilson_low(len(wins), len(pool), p.wilson_z)
            avg_loss = sum(losses) / len(losses) if losses else 0.0
            rr = (sum(wins) / len(wins)) / avg_loss if wins and losses and avg_loss > 0 else (10.0 if wins else 0.0)
            k_raw = w_low - (1 - w_low) / rr if rr > 0 else -1
            if k_raw <= 0:
                return None
            base = min(k_raw * p.kelly_fraction, cap)
        else:
            base = min(p.fixed_risk, cap)
        mult = 1.0
        if side < 0:
            mult *= p.short_size
        if coin in SATELLITE:
            mult *= p.satellite_size
        if side > 0 and self.fng is not None and self.fng >= p.greed_level:
            mult *= 0.5
        if bk.day_start and bk.day_start < bk.peak * (1 - p.drawdown_cut):
            mult *= 0.5
        return max(p.risk_floor, base * mult)

    # 정리
    def _exit(self, symbol: str, leg: Leg, frac: float, price: float, reason: str, at: str) -> None:
        """처음 수량 중 frac만큼(1이면 남은 것 전부) price에 정리한다. 다 정리되면 R을 기록한다"""
        last = frac >= 1
        if leg.shadow:  # 그림자: 돈은 움직이지 않고 진입가 대비 비율로만 손익을 쌓는다
            part = self._left(leg) if last else frac
            leg.pnl += part * (leg.side * (price - leg.entry) / leg.entry - float(self.b.fee_rate))
            if last:
                self._close(symbol, leg, at)
            return
        held = self.b.positions.get(symbol)
        if held is None:  # 손으로 정리했거나 MCP 주문으로 없어진 경우
            self._close(symbol, leg, at)
            return
        qty = held.quantity if last else min(held.quantity, Decimal(str(leg.qty0 * frac)).quantize(COIN_STEP, rounding=ROUND_FLOOR))
        px = Decimal(str(price))
        if self.futures:
            fill = self.b.close(symbol, px, reason, at) if qty >= held.quantity else self.b.reduce(symbol, qty, px, reason, at)
        else:
            fill = self.b.sell(symbol, self.ex.coin_name(symbol), qty, px, reason, at)
        if fill is not None:
            self.s._add(fill)
            leg.pnl += float(fill.realized_pnl or 0)
        if symbol not in self.b.positions:
            self._close(symbol, leg, at)

    @staticmethod
    def _left(leg: Leg) -> float:
        return 1 - (1 / 3 if leg.tp1_done else 0) - (1 / 3 if leg.tp2_done else 0)

    def _close(self, symbol: str, leg: Leg, at: str) -> None:
        r = leg.pnl / leg.stop_pct if leg.shadow else (leg.pnl / leg.risk_amt if leg.risk_amt else 0.0)
        self.book.history.append({"symbol": symbol, "side": leg.side, "r": round(r, 4), "shadow": leg.shadow, "closed": at})
        if not leg.shadow:
            self.book.real_trades += 1
        self.book.legs.pop(symbol, None)

    def manage(self, symbol: str, k: Candle, v: View, at: str) -> None:
        leg = self.book.legs.get(symbol)
        if leg is None:
            return
        s = leg.side
        lo, hi, op, cl = float(k.low), float(k.high), float(k.open), float(k.close)
        hit = (lo <= leg.stop) if s > 0 else (hi >= leg.stop)
        if hit:
            gap = (op < leg.stop) if s > 0 else (op > leg.stop)
            self._exit(symbol, leg, 1, op if gap else leg.stop, "본전 정리" if leg.tp1_done else f"손절 (-{leg.stop_pct * 100:.1f}%)", at)
            return
        reach = (lambda lvl: hi >= lvl) if s > 0 else (lambda lvl: lo <= lvl)
        if not leg.tp1_done and reach(leg.tp1):
            self._exit(symbol, leg, 1 / 3, leg.tp1, "+1R 1/3 익절, 손절을 본전으로", at)
            leg.tp1_done, leg.stop = True, leg.entry
        elif leg.tp1_done and not leg.tp2_done and reach(leg.tp2):
            self._exit(symbol, leg, 1 / 3, leg.tp2, "+2R 1/3 익절", at)
            leg.tp2_done = True
        if symbol not in self.book.legs:
            return
        if v.regime != s:
            self._exit(symbol, leg, 1, cl, "1일 방향이 바뀜", at)
        elif leg.tp1_done and v.ema1 is not None and ((cl < v.ema1) if s > 0 else (cl > v.ema1)):
            self._exit(symbol, leg, 1, cl, "1시간 종가가 EMA20을 넘어 되돌아옴", at)
        elif self.futures and k.close_time - datetime.fromisoformat(leg.opened) >= timedelta(hours=self.p.time_exit_hours):
            self._exit(symbol, leg, 1, cl, f"{self.p.time_exit_hours}시간 지남", at)

    # 진입
    def enter(self, symbol: str, name: str, k: Candle, v: View, at: str) -> None:
        p, bk = self.p, self.book
        if symbol in bk.legs or symbol in self.b.positions or v.stop_raw is None or v.stop_raw > p.stop_max:
            return
        side = 0
        if v.long_ok and not v.overheat and not (self.futures and self.funding(symbol) >= p.funding_overheat_apr):
            side = 1
        elif self.futures and v.short_ok:
            side = -1
        if not side:
            return
        if sum(1 for leg in bk.legs.values() if leg.side == side and not leg.shadow) >= p.max_same_side:
            return
        coin = self.ex.base_asset(symbol)
        stop_pct = min(max(v.stop_raw, p.stop_min), p.stop_max)
        close = float(k.close)
        risk = self._risk(side, coin)
        if risk is None:  # 켈리 0 이하: 돈 없이 따라가며 R만 기록
            bk.legs[symbol] = Leg(side, close, close * (1 - side * stop_pct), stop_pct, 1 / close, stop_pct, k.close_time.isoformat(),
                                  pnl=-float(self.b.fee_rate), shadow=True)
            print(f"[ladder] {symbol} {'롱' if side > 0 else '숏'} 신호: 켈리 0 이하라 그림자 거래로 기록만 해요")
            return
        eq = self.equity()
        open_risk = sum(leg.risk_amt * self._left(leg) for leg in bk.legs.values() if not leg.shadow and not leg.tp1_done) / max(eq, 1e-9)
        risk = min(risk, p.max_open_risk - open_risk)
        if risk < p.risk_floor:
            return
        size_eq = bk.day_start or eq
        notional = min(size_eq * risk / stop_pct, eq * p.max_notional_pct)
        lev = 1
        if self.futures:
            margin_cap = eq * p.margin_cap_pct
            lev_cap = min(self.lev_cap, next(c for lim, c in p.leverage_stages if bk.real_trades < lim))
            lev = max(1, math.ceil(notional / margin_cap))
            if lev > lev_cap:
                lev, notional = lev_cap, margin_cap * lev_cap
            while lev > 1 and (1 / lev - p.maint_margin) < p.liq_buffer * stop_pct:
                lev -= 1
                notional = min(notional, margin_cap * lev)
            if (1 / lev - p.maint_margin) < p.liq_buffer * stop_pct:
                return
        else:
            notional = min(notional, float(self.b.cash) / (1 + float(self.b.fee_rate)))
        qty = (Decimal(str(notional)) / k.close).quantize(COIN_STEP, rounding=ROUND_FLOOR)
        if qty <= 0:
            return
        reason = (f"[{VERSION}] 1일 {'상승' if side > 0 else '하락'} 방향 · 4시간 되돌림 · 1시간 {p.breakout_bars}봉 "
                  f"{'최고가 돌파' if side > 0 else '최저가 이탈'} · 위험 {risk * 100:.2f}% · 손절 {stop_pct * 100:.1f}%")
        if self.futures:
            keep = self.b.leverage
            self.b.leverage = Decimal(lev)
            fill = self.b.open(symbol, name, "LONG" if side > 0 else "SHORT", qty, k.close, reason, at)
            self.b.leverage = keep
        else:
            fill = self.b.buy(symbol, name, qty, k.close, reason, at)
        if fill is None:
            return
        self.s._add(fill)
        bk.legs[symbol] = Leg(side, close, close * (1 - side * stop_pct), stop_pct, float(qty), float(qty) * close * stop_pct,
                              k.close_time.isoformat(), pnl=-float(fill.fee), lev=float(lev))

    # 한 종목
    def trade(self, symbol: str, name: str, h1: list[Candle], h4: list[Candle], d1: list[Candle],
              executed_at: Callable[[Candle], str]) -> None:
        seen = self.b.last_seen.get(symbol, "")
        keys = [c.open_time.strftime("%Y-%m-%dT%H:%M:%SZ") for c in h1]
        if not seen and len(h1) >= 2:
            seen = keys[-2]  # 처음 보는 종목은 지난 봉으로 거래하지 않고 마지막 닫힌 봉부터 본다
        for i, k in enumerate(h1):
            if keys[i] <= seen or k.high is None:
                continue
            at = executed_at(k)
            self._roll(k.close_time)
            v = view(h1, i, h4, d1, self.p)
            self.manage(symbol, k, v, at)
            self.book.last_px[symbol] = float(k.close)
            if self.futures and symbol in self.b.positions and self.b.liquidation_due(symbol, k.close):
                leg = self.book.legs.get(symbol)
                fill = self.b.close(symbol, k.close, "손실이 증거금을 다 씀", at, liquidated=True)
                self.s._add(fill)
                if leg:
                    leg.pnl += float(fill.realized_pnl or 0) if fill else 0.0
                    self._close(symbol, leg, at)
            if not self._blocked(k.close_time):
                self.enter(symbol, name, k, v, at)
        if keys:
            self.b.last_seen[symbol] = max(self.b.last_seen.get(symbol, ""), keys[-1])

    def describe(self) -> str:
        real = [t for t in self.book.history if not t["shadow"]]
        shadow = [t for t in self.book.history if t["shadow"]]
        held = ", ".join(f"{s}{'(그림자)' if leg.shadow else ''}" for s, leg in self.book.legs.items())
        return (f"{VERSION} · 실제 거래 {self.book.real_trades}건 · 그림자 {len(shadow)}건(최근 기록) · 평균 "
                f"{(sum(t['r'] for t in real) / len(real)) if real else 0:.2f}R · 코어 {self.book.core:g} · 들고 있는 것 {held or '없음'}")


def run(session, ex: Exchange, symbols: list[str], limit: int, lev_cap: int,
        executed_at: Callable[[Candle], str], p: Params = Params()) -> tuple[bool, list[str]]:
    """coin --rule ladder 한 번: 종목마다 1시간·4시간·1일봉을 받아 판단한다. (모두 받았는지, 받은 것 요약)"""
    state = Path(session.state)
    book = Book.load(state)
    lad = Ladder(session, ex, book, p, fear_greed(ex.fetch), (lambda s: funding_apr(s, ex.fetch)) if ex.market == "futures" else None,
                 lev_cap)
    ok, seen = True, []
    for symbol in symbols:
        try:
            h1 = ex.fetch_candles(symbol, "1h", max(limit, 200))
            h4 = ex.fetch_candles(symbol, "4h", 200)
            d1 = ex.fetch_candles(symbol, "1d", 250)
        except (ExchangeError, OSError, ValueError, KeyError) as e:
            print(f"{ex.code} {symbol} 시세를 받지 못했어요 (다음 실행 때 다시 해요): {e}", file=sys.stderr)
            ok = False
            continue
        lad.trade(symbol, ex.coin_name(symbol), h1, h4, d1, executed_at)
        seen.append(f"{symbol} {len(h1)}/{len(h4)}/{len(d1)}개")
    book.save(state)
    print(lad.describe())
    return ok, seen
