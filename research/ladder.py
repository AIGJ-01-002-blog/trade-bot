"""세 겹 거름망 복리법(ladder v0.1) 백테스트 엔진. 설계: notes/trade-bot-compound-method-2026-10-15.md

봇 코드(devlog_trade)와 따로 두는 검증 전용 코드다. 봇은 이 파일을 읽지 않고, 이 파일도 봇을 읽지 않는다.
pandas·numpy만 쓰고, 현물 계좌 장부는 run_backtest.py가 vectorbt로 한 번 더 맞춰 본다.

판단 순서 (1시간봉이 닫힐 때마다, 코인마다)
  ① 1일봉 방향: 종가 > SMA50이고 SMA50이 5일 전보다 높으면 UP, 반대면 DOWN, 나머지 FLAT(새 진입 없음)
  ② 4시간봉 자리: UP이면 EMA20 > EMA50이고 최근 6봉 안에 저가가 EMA20 × 1.01 이하(되돌림). DOWN은 뒤집은 것
  ③ 1시간봉 방아쇠: 종가가 직전 10봉 최고가 위(숏은 최저가 아래)
  ⑤ 과열: 1일 종가 > SMA200 × 1.5이거나 선물 펀딩비 연환산 30% 이상이면 롱 금지, 공포·탐욕 80 이상이면 롱 크기 절반
  ⑥ 크기: 내 거래 기록으로 켈리(윌슨 하한 승률) × 1/4, 30건 전에는 위험 0.5% 고정, 단계 상한 0.5→0.75→1%
  ⑦ 손절 1.5 × ATR14(4시간), 1.5~6%로 자름(6% 넘게 흔들리면 건너뜀). +1R 1/3 익절 후 손절 본전, +2R 1/3 익절,
     나머지는 1시간 종가 < EMA20이면 정리. 1일 방향이 바뀌면 전부 정리, 선물은 72시간이면 정리
⑧ 뉴스와 AI는 과거 자료가 없어 백테스트에서 뺀다(AI 확신도 1.0, 뉴스 배수 1.0). 모의 4주에서 본다.
⑨ 크기를 정하는 잔고는 매일 0시 UTC(한국 9시) 잔고, 낙폭 10%면 크기 절반, 하루 -3%·한 주 -6%면 그날·그 주 새 진입 없음,
   달마다 트레이딩 수익의 30%를 코어로 옮긴다(옮긴 돈은 잃지 않는 것으로 보고 총수익에 더한다).

한 봉 안에서 손절과 익절이 같이 닿으면 손절이 먼저라고 본다(나쁜 쪽으로). 진입은 신호가 난 봉의 종가에 미끄러짐을 더한 값이다.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

import numpy as np
import pandas as pd

SATELLITE = {"XRP", "SOL", "DOGE"}
HOUR = pd.Timedelta(hours=1)


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
    risk_stages: tuple = ((50, 0.005), (100, 0.0075), (10**9, 0.01))  # (거래 수 미만, 상한)
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


# ---------------------------------------------------------------- 지표
def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def atr(df: pd.DataFrame, n: int) -> pd.Series:
    prev = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()  # 와일더 방식


def resample(h1: pd.DataFrame, rule: str) -> pd.DataFrame:
    """1시간봉(인덱스 = 봉 시작 시각, UTC)을 묶는다. 시간이 빠진 묶음도 그대로 쓴다"""
    g = h1.resample(rule, label="left", closed="left")
    out = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                        "close": g["close"].last(), "volume": g["volume"].sum(), "n": g["close"].count()})
    return out[out["n"] > 0]


def _on_close(feat: pd.DataFrame, span: pd.Timedelta, h1_index: pd.DatetimeIndex) -> pd.DataFrame:
    """큰 봉 지표를 '그 봉이 닫힌 뒤'부터 1시간봉에 붙인다. 1시간봉 t(시작)는 t+1h에 닫히므로 그 시각까지 닫힌 큰 봉만 본다"""
    feat = feat.copy()
    feat.index = feat.index + span  # 닫힌 시각
    closes = h1_index + HOUR
    out = feat.reindex(feat.index.union(closes)).ffill().reindex(closes)
    out.index = h1_index
    return out


def features(h1: pd.DataFrame, p: Params, funding: pd.Series | None = None, fng: pd.Series | None = None) -> pd.DataFrame:
    """1시간봉마다 ①②③ 판정과 손절 거리 등 판단에 쓰는 값을 모두 붙인다. 미래 값은 쓰지 않는다"""
    d1 = resample(h1, "1D")
    sma = d1["close"].rolling(p.regime_sma, min_periods=p.regime_sma).mean()
    up = (d1["close"] > sma) & (sma > sma.shift(p.regime_slope_days))
    down = (d1["close"] < sma) & (sma < sma.shift(p.regime_slope_days))
    d_feat = pd.DataFrame({"regime": np.where(up, 1, np.where(down, -1, 0)),
                           "overheat": d1["close"] > d1["close"].rolling(p.overheat_sma, min_periods=p.overheat_sma).mean() * p.overheat_ratio,
                           "d_close": d1["close"], "d_sma": sma}, index=d1.index)
    d_feat.loc[sma.isna(), "regime"] = 0

    h4 = resample(h1, "4h")
    e_fast, e_slow = ema(h4["close"], p.ema_fast), ema(h4["close"], p.ema_slow)
    near_low = (h4["low"] <= e_fast * (1 + p.pullback_band)).rolling(p.pullback_bars, min_periods=1).max().astype(bool)
    near_high = (h4["high"] >= e_fast * (1 - p.pullback_band)).rolling(p.pullback_bars, min_periods=1).max().astype(bool)
    h4_feat = pd.DataFrame({"setup_long": (e_fast > e_slow) & near_low, "setup_short": (e_fast < e_slow) & near_high,
                            "atr4": atr(h4, p.atr_len), "c4": h4["close"]}, index=h4.index)

    f = h1[["open", "high", "low", "close"]].copy()
    f = f.join(_on_close(d_feat, pd.Timedelta(days=1), h1.index)).join(_on_close(h4_feat, pd.Timedelta(hours=4), h1.index))
    f["regime"] = f["regime"].fillna(0).astype(int)
    for c in ("overheat", "setup_long", "setup_short"):
        f[c] = f[c].astype("boolean").fillna(False).astype(bool)
    f["hi_prev"] = h1["high"].rolling(p.breakout_bars).max().shift(1)
    f["lo_prev"] = h1["low"].rolling(p.breakout_bars).min().shift(1)
    f["ema1"] = ema(h1["close"], p.ema_fast)
    f["stop_raw"] = p.atr_mult * f["atr4"] / f["c4"]
    f["trig_long"] = f["close"] > f["hi_prev"]
    f["trig_short"] = f["close"] < f["lo_prev"]
    if funding is not None and len(funding):
        # 마지막으로 정해진 펀딩비 × 하루 3번 × 365 = 연환산
        apr = funding.reindex(funding.index.union(h1.index + HOUR)).ffill().reindex(h1.index + HOUR) * 3 * 365
        f["funding_apr"] = apr.to_numpy()
    else:
        f["funding_apr"] = 0.0
    if fng is not None and len(fng):
        f["fng"] = _on_close(fng.to_frame("fng"), pd.Timedelta(days=1), h1.index)["fng"].to_numpy()
    else:
        f["fng"] = np.nan
    f["long_ok"] = (f["regime"] == 1) & f["setup_long"] & f["trig_long"]
    f["short_ok"] = (f["regime"] == -1) & f["setup_short"] & f["trig_short"]
    return f


# ---------------------------------------------------------------- 장부
@dataclass
class Pos:
    coin: str
    side: int  # 1 롱, -1 숏
    entry: float
    qty: float
    qty0: float
    stop: float
    stop_pct: float
    risk_amt: float
    opened: pd.Timestamp
    tp1: float
    tp2: float
    lev: float = 1.0
    margin: float = 0.0
    liq: float = 0.0
    tp1_done: bool = False
    tp2_done: bool = False
    pnl: float = 0.0  # 수수료·펀딩비 뺀 실현 손익 누계
    fees: float = 0.0
    funding: float = 0.0
    shadow: bool = False  # 그림자 거래: 돈을 넣지 않고 R만 기록(켈리가 0 이하일 때)


@dataclass
class Result:
    name: str
    equity: pd.Series  # 옮긴 돈까지 더한 총액(시간마다)
    trades: pd.DataFrame
    orders: list = field(default_factory=list)  # (시각, 코인, 수량(+사기/-팔기), 가격) — vectorbt 대조용
    transfers: list = field(default_factory=list)  # (시각, 금액) 코어로 옮긴 돈
    fees: float = 0.0
    funding: float = 0.0
    liquidations: int = 0
    shadow: pd.DataFrame = field(default_factory=pd.DataFrame)  # 그림자 거래 기록


def wilson_low(wins: int, n: int, z: float) -> float:
    if n == 0:
        return 0.0
    ph = wins / n
    den = 1 + z * z / n
    return (ph + z * z / (2 * n) - z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n))) / den


class Ladder:
    """한 계좌(현물 또는 선물)를 코인 여럿으로 돌린다"""

    def __init__(self, feats: dict[str, pd.DataFrame], market: str, fee: float, slip: float, cash: float, p: Params,
                 funding: dict[str, pd.Series] | None = None, name: str = "ladder"):
        self.f, self.market, self.fee, self.slip, self.p, self.name = feats, market, fee, slip, p, name
        self.futures = market == "futures"
        self.funding = funding or {}
        self.cash = cash  # 현물: 현금, 선물: 지갑(실현 손익이 쌓인다)
        self.pos: dict[str, Pos] = {}
        self.closed: list[dict] = []
        self.orders: list = []
        self.transfers: list = []
        self.fees = self.funding_paid = 0.0
        self.liqs = 0

    # 잔고
    def equity(self, px: dict[str, float]) -> float:
        if self.futures:
            return self.cash + sum(p.side * (px[c] - p.entry) * p.qty for c, p in self.pos.items() if not p.shadow)
        return self.cash + sum(p.qty * px[c] for c, p in self.pos.items() if not p.shadow)

    def _real(self) -> int:
        return sum(1 for t in self.closed if not t["shadow"])

    def _result(self, equity: pd.Series) -> Result:
        real = pd.DataFrame([t for t in self.closed if not t["shadow"]])
        shadow = pd.DataFrame([t for t in self.closed if t["shadow"]])
        return Result(self.name, equity, real, self.orders, self.transfers, self.fees, self.funding_paid, self.liqs, shadow)

    def _fill(self, price: float, side: int) -> float:
        return price * (1 + self.slip * side)

    def _trade(self, ts, pos: Pos, qty: float, price: float, why: str) -> None:
        """qty만큼 정리(price는 미끄러짐 반영 전)"""
        px = self._fill(price, -pos.side)
        fee = qty * px * self.fee
        gross = pos.side * (px - pos.entry) * qty
        if pos.shadow:
            pos.pnl += gross - fee
            pos.qty -= qty
            if pos.qty <= pos.qty0 * 1e-9:
                self._close(ts, pos, why)
            return
        if self.futures:
            self.cash += gross - fee
        else:
            self.cash += qty * px - fee
        pos.pnl += gross - fee
        pos.fees += fee
        pos.qty -= qty
        self.fees += fee
        self.orders.append((ts, pos.coin, -pos.side * qty, px))
        if pos.qty <= pos.qty0 * 1e-9:
            self._close(ts, pos, why)

    def _close(self, ts, pos: Pos, why: str) -> None:
        del self.pos[pos.coin]
        self.closed.append({"coin": pos.coin, "side": pos.side, "opened": pos.opened, "closed": ts, "entry": pos.entry,
                            "pnl": pos.pnl, "r": pos.pnl / pos.risk_amt if pos.risk_amt else 0.0, "exit": why,
                            "lev": pos.lev, "fees": pos.fees, "funding": pos.funding, "stop_pct": pos.stop_pct,
                            "shadow": pos.shadow})

    def _liquidate(self, ts, pos: Pos) -> None:
        loss = pos.margin * pos.qty / pos.qty0
        self.cash -= loss
        pos.pnl -= loss
        self.liqs += 1
        self.orders.append((ts, pos.coin, -pos.side * pos.qty, pos.liq))
        pos.qty = 0
        self._close(ts, pos, "LIQ")

    # 크기
    def _risk(self, side: int, coin: str, row, eq_peak: float, eq_size: float) -> float | None:
        p = self.p
        n_closed = self._real()
        cap = next(c for lim, c in p.risk_stages if n_closed < lim)
        same = [t["r"] for t in self.closed if t["side"] == side][-p.kelly_window:]
        pool = same if len(same) >= p.kelly_min_samples else [t["r"] for t in self.closed][-p.kelly_window:]
        if len(pool) >= p.kelly_min_samples:
            wins = [r for r in pool if r > 0]
            losses = [-r for r in pool if r <= 0]
            w_low = wilson_low(len(wins), len(pool), p.wilson_z)
            rr = (np.mean(wins) / np.mean(losses)) if wins and losses and np.mean(losses) > 0 else (10.0 if wins else 0.0)
            k_raw = w_low - (1 - w_low) / rr if rr > 0 else -1
            if k_raw <= 0:
                return None  # 기대값이 마이너스면 돈은 넣지 않는다(그림자 거래로 R만 쌓아 다시 플러스가 되면 돌아온다)
            base = min(k_raw * p.kelly_fraction, cap)
        else:
            base = min(p.fixed_risk, cap)
        mult = 1.0
        if side < 0:
            mult *= p.short_size
        if coin in SATELLITE:
            mult *= p.satellite_size
        if side > 0 and not np.isnan(row.fng) and row.fng >= p.greed_level:
            mult *= 0.5
        if eq_size < eq_peak * (1 - p.drawdown_cut):
            mult *= 0.5
        return max(p.risk_floor, base * mult)

    def _enter(self, ts, coin: str, side: int, row, eq: float, eq_size: float, eq_peak: float, px: dict) -> None:
        p = self.p
        if row.stop_raw != row.stop_raw or row.stop_raw > p.stop_max:
            return
        if sum(1 for q in self.pos.values() if q.side == side and not q.shadow) >= p.max_same_side:
            return
        risk = self._risk(side, coin, row, eq_peak, eq_size)
        if risk is None:
            self._enter_shadow(ts, coin, side, row)
            return
        open_risk = sum(q.risk_amt * q.qty / q.qty0 for q in self.pos.values()
                        if not q.tp1_done and not q.shadow) / max(eq, 1e-9)
        risk = min(risk, p.max_open_risk - open_risk)
        if risk < p.risk_floor:
            return
        stop_pct = min(max(row.stop_raw, p.stop_min), p.stop_max)
        notional = min(eq_size * risk / stop_pct, eq * p.max_notional_pct)
        entry = self._fill(row.close, side)
        lev, margin, liq = 1.0, 0.0, 0.0
        if self.futures:
            margin_cap = eq * p.margin_cap_pct
            lev_cap = next(c for lim, c in p.leverage_stages if self._real() < lim)
            lev = max(1, math.ceil(notional / margin_cap))
            if lev > lev_cap:
                lev, notional = lev_cap, margin_cap * lev_cap
            while lev > 1 and (1 / lev - p.maint_margin) < p.liq_buffer * stop_pct:
                lev -= 1
                notional = min(notional, margin_cap * lev)
            if (1 / lev - p.maint_margin) < p.liq_buffer * stop_pct:
                return
            margin = notional / lev
            free = self.cash - sum(q.margin * q.qty / q.qty0 for q in self.pos.values())
            if margin + notional * self.fee > free:
                return
            liq = entry * (1 - side * (1 / lev - p.maint_margin))
        else:
            notional = min(notional, self.cash / (1 + self.fee))
        qty = notional / entry
        if qty <= 0 or notional < 1e-6:
            return
        fee = notional * self.fee
        self.cash -= fee if self.futures else notional + fee
        self.fees += fee
        r = stop_pct * entry
        pos = Pos(coin, side, entry, qty, qty, entry - side * r, stop_pct, qty * r, ts,
                  entry + side * r, entry + side * 2 * r, lev, margin, liq)
        pos.pnl -= fee
        pos.fees += fee
        self.pos[coin] = pos
        self.orders.append((ts, coin, side * qty, entry))

    def _enter_shadow(self, ts, coin: str, side: int, row) -> None:
        """켈리가 0 이하라 돈을 넣지 않는 신호도 같은 규칙으로 따라가 R을 기록한다. 이게 없으면 기록이 멈춰 거래가 영영 재개되지 않는다"""
        stop_pct = min(max(row.stop_raw, self.p.stop_min), self.p.stop_max)
        entry = self._fill(row.close, side)
        qty = 1.0 / entry  # 1만큼 산 셈. R배수는 크기와 무관하다
        r = stop_pct * entry
        pos = Pos(coin, side, entry, qty, qty, entry - side * r, stop_pct, qty * r, ts,
                  entry + side * r, entry + side * 2 * r, shadow=True)
        fee = self.fee
        pos.pnl -= fee
        self.pos[coin] = pos

    # 봉 하나
    def _manage(self, ts, pos: Pos, row) -> None:
        s = pos.side
        # 펀딩비 (이 봉 안에 정해진 것)
        if self.futures and pos.coin in self.funding:
            fr = self.funding[pos.coin]
            for rate in fr[(fr.index >= ts) & (fr.index < ts + HOUR)].to_numpy():
                cost = s * pos.qty * row.open * rate
                if not pos.shadow:
                    self.cash -= cost
                    self.funding_paid += cost
                pos.pnl -= cost
                pos.funding += cost
        lo, hi = (row.low, row.high) if s > 0 else (row.high, row.low)
        hit_stop = (lo <= pos.stop) if s > 0 else (lo >= pos.stop)
        if hit_stop:
            gap = (row.open < pos.stop) if s > 0 else (row.open > pos.stop)
            fill = row.open if gap else pos.stop
            if self.futures and not pos.shadow and ((fill <= pos.liq) if s > 0 else (fill >= pos.liq)):
                self._liquidate(ts, pos)
            else:
                self._trade(ts, pos, pos.qty, fill, "STOP" if not pos.tp1_done else "BE")
            return
        if self.futures and not pos.shadow and ((lo <= pos.liq) if s > 0 else (lo >= pos.liq)):
            self._liquidate(ts, pos)
            return
        reach = lambda lvl: (hi >= lvl) if s > 0 else (hi <= lvl)  # noqa: E731
        if not pos.tp1_done and reach(pos.tp1):
            self._trade(ts, pos, pos.qty0 / 3, pos.tp1, "TP1")
            pos.tp1_done, pos.stop = True, pos.entry
        elif pos.tp1_done and not pos.tp2_done and reach(pos.tp2):
            self._trade(ts, pos, pos.qty0 / 3, pos.tp2, "TP2")
            pos.tp2_done = True
        if pos.coin not in self.pos:
            return
        if row.regime != s:
            self._trade(ts, pos, pos.qty, row.close, "REGIME")
        elif pos.tp1_done and ((row.close < row.ema1) if s > 0 else (row.close > row.ema1)):
            self._trade(ts, pos, pos.qty, row.close, "TRAIL")
        elif self.futures and ts + HOUR - pos.opened >= pd.Timedelta(hours=self.p.time_exit_hours):
            self._trade(ts, pos, pos.qty, row.close, "TIME")

    def run(self, start: pd.Timestamp, end: pd.Timestamp) -> Result:
        p = self.p
        coins = list(self.f)
        idx = self.f[coins[0]].index
        for c in coins[1:]:
            idx = idx.union(self.f[c].index)
        idx = idx[(idx >= start) & (idx < end)]
        frames = {c: self.f[c].reindex(idx) for c in coins}
        cols = list(frames[coins[0]].columns)
        arr = {c: frames[c].itertuples(index=False, name="Row") for c in coins}
        last_px: dict[str, float] = {}
        eq_curve = np.empty(len(idx))
        transferred = 0.0
        init = self.cash
        eq_size = eq_peak = day_start = week_start = month_start = init
        day_block = week_block = False
        prev = None
        for i, ts in enumerate(idx):
            rows = {c: next(arr[c]) for c in coins}
            # 하루·주·달 경계 (0시 UTC = 한국 9시)
            if prev is None or ts.normalize() != prev.normalize():
                eq_now = self.equity(last_px) if last_px else self.cash
                eq_size = eq_now
                eq_peak = max(eq_peak, eq_now)
                day_start, day_block = eq_now, False
                if prev is None or ts.weekday() == 0:
                    week_start, week_block = eq_now, False
                if prev is not None and ts.month != prev.month:
                    gain = eq_now - month_start
                    if gain > 0:
                        amt = min(gain * p.core_transfer, self.cash if not self.futures else max(0.0, self.cash - sum(
                            q.margin for q in self.pos.values())))
                        self.cash -= amt
                        transferred += amt
                        self.transfers.append((ts, amt))
                        eq_now -= amt
                        eq_size, day_start, week_start = eq_now, eq_now, min(week_start, eq_now)
                    month_start = eq_now
            for c in coins:
                row = rows[c]
                if row.close != row.close:
                    continue
                if c in self.pos:
                    self._manage(ts, self.pos[c], row)
                last_px[c] = row.close
            eq = self.equity(last_px)
            if eq <= day_start * (1 - p.daily_stop):
                day_block = True
            if eq <= week_start * (1 - p.weekly_stop):
                week_block = True
            if not (day_block or week_block):
                for c in coins:
                    row = rows[c]
                    if c in self.pos or row.close != row.close:
                        continue
                    if row.long_ok and not row.overheat and not (self.futures and row.funding_apr >= p.funding_overheat_apr):
                        self._enter(ts, c, 1, row, eq, eq_size, eq_peak, last_px)
                    elif self.futures and row.short_ok:
                        self._enter(ts, c, -1, row, eq, eq_size, eq_peak, last_px)
            eq_curve[i] = self.equity(last_px) + transferred
            prev = ts
        # 끝에 남은 포지션은 마지막 종가로 정리해 거래 기록에 넣는다
        for c, pos in list(self.pos.items()):
            self._trade(idx[-1], pos, pos.qty, last_px[c], "END")
        eq_curve[-1] = self.equity(last_px) + transferred
        del cols
        return self._result(pd.Series(eq_curve, index=idx + HOUR))


class Baseline(Ladder):
    """지금 봇 규칙: 1시간봉 종가가 5봉 평균선을 위로 뚫으면 진입(선물은 아래로 뚫으면 숏), 반대로 뚫거나 종가 손절 3%면 정리.
    한 번에 처음 잔고의 10%(봇 기본 100 USDT / 1,000 USDT)를 쓰고, 선물은 3배(jobs.conf 기본)"""

    def __init__(self, h1: dict[str, pd.DataFrame], market: str, fee: float, slip: float, cash: float,
                 funding: dict[str, pd.Series] | None = None, window: int = 5, stop: float = 0.03, lev: float = 3.0,
                 budget_pct: float = 0.10):
        feats = {}
        for c, df in h1.items():
            f = df[["open", "high", "low", "close"]].copy()
            ma = f["close"].rolling(window).mean()
            above = f["close"] > ma
            f["up"] = above & ~above.shift(1, fill_value=False) & ma.notna() & ma.shift(1).notna()
            f["down"] = ~above & above.shift(1, fill_value=False) & ma.notna()
            feats[c] = f
        super().__init__(feats, market, fee, slip, cash, Params(), funding, name="baseline")
        self.stop_pct, self.lev = stop, (lev if market == "futures" else 1.0)
        self.budget = cash * budget_pct

    def _open(self, ts, c, side, row) -> None:
        entry = self._fill(row.close, side)
        notional = self.budget * self.lev if self.futures else min(self.budget, self.cash / (1 + self.fee))
        if self.futures:
            free = self.cash - sum(q.margin for q in self.pos.values())
            if self.budget + notional * self.fee > free:
                return
        if notional <= 0:
            return
        qty = notional / entry
        fee = notional * self.fee
        self.cash -= fee if self.futures else notional + fee
        self.fees += fee
        liq = entry * (1 - side * (1 / self.lev - self.p.maint_margin)) if self.futures else 0.0
        pos = Pos(c, side, entry, qty, qty, 0.0, self.stop_pct, qty * entry * self.stop_pct, ts, 0.0, 0.0, self.lev,
                  self.budget if self.futures else 0.0, liq)
        pos.pnl -= fee
        pos.fees += fee
        self.pos[c] = pos
        self.orders.append((ts, c, side * qty, entry))

    def run(self, start: pd.Timestamp, end: pd.Timestamp) -> Result:
        coins = list(self.f)
        idx = self.f[coins[0]].index
        for c in coins[1:]:
            idx = idx.union(self.f[c].index)
        idx = idx[(idx >= start) & (idx < end)]
        frames = {c: self.f[c].reindex(idx) for c in coins}
        arr = {c: frames[c].itertuples(index=False, name="Row") for c in coins}
        last_px: dict[str, float] = {}
        eq_curve = np.empty(len(idx))
        for i, ts in enumerate(idx):
            for c in coins:
                row = next(arr[c])
                if row.close != row.close:
                    continue
                last_px[c] = row.close
                pos = self.pos.get(c)
                if pos is not None and self.futures:
                    if c in self.funding:
                        fr = self.funding[c]
                        for rate in fr[(fr.index >= ts) & (fr.index < ts + HOUR)].to_numpy():
                            cost = pos.side * pos.qty * row.open * rate
                            self.cash -= cost
                            pos.pnl -= cost
                            pos.funding += cost
                            self.funding_paid += cost
                    lo = row.low if pos.side > 0 else row.high
                    if (lo <= pos.liq) if pos.side > 0 else (lo >= pos.liq):
                        self._liquidate(ts, pos)
                        pos = None
                if pos is not None and pos.side * (row.close - pos.entry) / pos.entry <= -self.stop_pct:
                    self._trade(ts, pos, pos.qty, row.close, "STOP")
                    pos = None
                if pos is not None and ((pos.side > 0 and row.down) or (pos.side < 0 and row.up)):
                    self._trade(ts, pos, pos.qty, row.close, "CROSS")
                    pos = None
                if pos is None and c not in self.pos:
                    if row.up:
                        self._open(ts, c, 1, row)
                    elif row.down and self.futures:
                        self._open(ts, c, -1, row)
            eq_curve[i] = self.equity(last_px)
        for c, pos in list(self.pos.items()):
            self._trade(idx[-1], pos, pos.qty, last_px[c], "END")
        eq_curve[-1] = self.equity(last_px)
        return self._result(pd.Series(eq_curve, index=idx + HOUR))


# ---------------------------------------------------------------- 성적표
def metrics(r: Result, init: float) -> dict:
    eq = r.equity
    dd = (eq / eq.cummax() - 1).min() if len(eq) else 0.0
    daily = eq.resample("1D").last().dropna()
    day_ret = daily.pct_change().dropna()
    t = r.trades
    n = len(t)
    return {
        "name": r.name,
        "return_pct": (eq.iloc[-1] / init - 1) * 100 if len(eq) else 0.0,
        "max_dd_pct": dd * 100,
        "worst_day_pct": day_ret.min() * 100 if len(day_ret) else 0.0,
        "trades": n,
        "win_rate_pct": (t["pnl"] > 0).mean() * 100 if n else 0.0,
        "avg_r": t["r"].mean() if n else 0.0,
        "liquidations": r.liquidations,
        "fees": r.fees,
        "funding": r.funding,
        "to_core": sum(a for _, a in r.transfers),
        "shadow_trades": len(r.shadow),
    }


def passes(m: dict) -> dict:
    """설계 문서 8장 통과 기준"""
    checks = {"수익 플러스": m["return_pct"] > 0, "최대 낙폭 15% 이하": m["max_dd_pct"] >= -15,
              "청산 0건": m["liquidations"] == 0, "최악의 하루 -3% 이내": m["worst_day_pct"] >= -3,
              "거래 30건 이상": m["trades"] >= 30}
    return {"all": all(checks.values()), **checks}


def monte_carlo_dd(r_multiples: np.ndarray, risk: float, n: int = 1000, seed: int = 7) -> float:
    """거래 순서를 n번 섞어 최대 낙폭의 95% 지점(%). 거래마다 잔고의 risk만큼 R배수로 벌고 잃는다고 본다"""
    if len(r_multiples) == 0:
        return 0.0
    rng = np.random.default_rng(seed)
    worst = []
    for _ in range(n):
        eq = np.cumprod(1 + rng.permutation(r_multiples) * risk)
        worst.append((eq / np.maximum.accumulate(eq) - 1).min())
    return float(np.percentile(worst, 5) * 100)


def with_params(p: Params, **kw) -> Params:
    return replace(p, **kw)
