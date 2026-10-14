# SPDX-License-Identifier: GPL-3.0-or-later
"""세 겹 거름망 복리법(ladder v0.1)을 Freqtrade 전략으로 옮긴 것. 바이낸스 현물·USDT-M 선물 백테스트 전용.

이 폴더(research/freqtrade)는 Freqtrade(GPL-3.0) 위에서 도는 코드라 GPL-3.0이다. trade-bot 봇 코드(devlog_trade)는
이 파일을 읽지 않고, 이 파일도 봇 코드를 읽지 않는다. 같은 규칙의 기준 구현은 research/ladder.py에 있다.

옮기지 못한 것: 달마다 수익 30%를 코어로 옮기기, 공포·탐욕 지수, 펀딩비 과열 필터(Freqtrade 백테스트는 펀딩비를 손익에는
반영하지만 진입 판단용 자료로 주지 않는다), 그림자 거래(켈리 0 이하일 때 돈 없이 R만 기록). 또 백테스트에서 order_filled가 거래가 닫히기 전에
불려 r_multiple이 비어 있으면 켈리 대신 위험 0.5% 고정으로 돈다. 그래서 research/ladder.py 결과와 다를 수 있다.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

import numpy as np
import talib.abstract as ta
from pandas import DataFrame

from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy, informative, stoploss_from_absolute

SATELLITE = {"XRP", "SOL", "DOGE"}


class LadderStrategy(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "1h"
    can_short = True  # 현물 설정에서는 __init__이 끈다
    stoploss = -0.99  # 실제 손절은 custom_stoploss(ATR)로
    use_custom_stoploss = True
    position_adjustment_enable = True
    max_entry_position_adjustment = 0  # 물타기 없음, 분할 익절만
    process_only_new_candles = True
    startup_candle_count = 2400  # 100일. 바이낸스 선물 한도(2,494개) 안이라 1일 SMA200(과열 필터)은 구간 첫 100일쯤 비어 있다

    # 설계 문서 기본값
    breakout_bars = 10
    atr_mult = 1.5
    stop_min, stop_max = 0.015, 0.06
    risk_stages = ((50, 0.005), (100, 0.0075), (10**9, 0.01))
    leverage_stages = ((30, 2), (100, 3), (10**9, 5))
    fixed_risk, risk_floor = 0.005, 0.0025
    max_notional_pct, margin_cap_pct = 0.20, 0.10
    maint_margin, liq_buffer = 0.005, 3.0

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.can_short = config.get("trading_mode") == "futures"
        self.peak = 0.0

    # ------------------------------------------------------------ 지표
    @informative("1d")
    def populate_indicators_1d(self, df: DataFrame, metadata: dict) -> DataFrame:
        df["sma50"] = ta.SMA(df, timeperiod=50)
        df["sma200"] = ta.SMA(df, timeperiod=200)
        up = (df["close"] > df["sma50"]) & (df["sma50"] > df["sma50"].shift(5))
        down = (df["close"] < df["sma50"]) & (df["sma50"] < df["sma50"].shift(5))
        df["regime"] = np.where(up, 1, np.where(down, -1, 0))
        df["overheat"] = (df["close"] > df["sma200"] * 1.5).astype(int)
        return df

    @informative("4h")
    def populate_indicators_4h(self, df: DataFrame, metadata: dict) -> DataFrame:
        df["ema20"] = ta.EMA(df, timeperiod=20)
        df["ema50"] = ta.EMA(df, timeperiod=50)
        near_low = (df["low"] <= df["ema20"] * 1.01).rolling(6, min_periods=1).max()
        near_high = (df["high"] >= df["ema20"] * 0.99).rolling(6, min_periods=1).max()
        df["setup_long"] = ((df["ema20"] > df["ema50"]) & (near_low > 0)).astype(int)
        df["setup_short"] = ((df["ema20"] < df["ema50"]) & (near_high > 0)).astype(int)
        df["atr_pct"] = ta.ATR(df, timeperiod=14) / df["close"]
        return df

    def populate_indicators(self, df: DataFrame, metadata: dict) -> DataFrame:
        df["hi_prev"] = df["high"].rolling(self.breakout_bars).max().shift(1)
        df["lo_prev"] = df["low"].rolling(self.breakout_bars).min().shift(1)
        df["ema20"] = ta.EMA(df, timeperiod=20)
        df["stop_pct"] = (self.atr_mult * df["atr_pct_4h"]).clip(lower=self.stop_min)
        df["stop_raw"] = self.atr_mult * df["atr_pct_4h"]
        return df

    def populate_entry_trend(self, df: DataFrame, metadata: dict) -> DataFrame:
        calm = df["stop_raw"] <= self.stop_max
        df.loc[(df["regime_1d"] == 1) & (df["setup_long_4h"] == 1) & (df["close"] > df["hi_prev"])
               & (df["overheat_1d"] == 0) & calm, ["enter_long", "enter_tag"]] = (1, "ladder")
        df.loc[(df["regime_1d"] == -1) & (df["setup_short_4h"] == 1) & (df["close"] < df["lo_prev"]) & calm,
               ["enter_short", "enter_tag"]] = (1, "ladder")
        return df

    def populate_exit_trend(self, df: DataFrame, metadata: dict) -> DataFrame:
        return df  # 정리는 custom_exit·손절·분할 익절로

    # ------------------------------------------------------------ 크기·레버리지
    def _row(self, pair: str):
        df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        return df.iloc[-1] if len(df) else None

    def _closed(self) -> list:
        return Trade.get_trades_proxy(is_open=False)

    def _risk(self, pair: str, side: str) -> float | None:
        closed = self._closed()
        cap = next(c for lim, c in self.risk_stages if len(closed) < lim)
        rs = [t.get_custom_data("r_multiple") for t in closed]
        rs = [r for r in rs if r is not None][-50:]
        if len(rs) >= 30:
            wins = [r for r in rs if r > 0]
            losses = [-r for r in rs if r <= 0]
            n, z = len(rs), 1.2816
            ph = len(wins) / n
            w_low = (ph + z * z / (2 * n) - z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n))) / (1 + z * z / n)
            rr = (np.mean(wins) / np.mean(losses)) if wins and losses else (10.0 if wins else 0.0)
            k_raw = w_low - (1 - w_low) / rr if rr > 0 else -1
            if k_raw <= 0:
                return None
            base = min(k_raw * 0.25, cap)
        else:
            base = min(self.fixed_risk, cap)
        mult = (0.5 if side == "short" else 1.0) * (0.5 if pair.split("/")[0] in SATELLITE else 1.0)
        equity = self.wallets.get_total_stake_amount()
        self.peak = max(self.peak, equity)
        if equity < self.peak * 0.9:
            mult *= 0.5
        return max(self.risk_floor, base * mult)

    def _notional(self, pair: str, side: str) -> tuple[float, float] | None:
        row = self._row(pair)
        risk = self._risk(pair, side)
        if row is None or risk is None or not row["stop_pct"] == row["stop_pct"]:
            return None
        stop = min(row["stop_pct"], self.stop_max)
        equity = self.wallets.get_total_stake_amount()
        return min(equity * risk / stop, equity * self.max_notional_pct), stop

    def leverage(self, pair: str, current_time: datetime, current_rate: float, proposed_leverage: float,
                 max_leverage: float, entry_tag: str | None, side: str, **kwargs) -> float:
        got = self._notional(pair, side)
        if got is None:
            return 1.0
        notional, stop = got
        margin_cap = self.wallets.get_total_stake_amount() * self.margin_cap_pct
        lev_cap = next(c for lim, c in self.leverage_stages if len(self._closed()) < lim)
        lev = min(max(1, math.ceil(notional / margin_cap)), lev_cap, max_leverage)
        while lev > 1 and (1 / lev - self.maint_margin) < self.liq_buffer * stop:
            lev -= 1
        return float(lev)

    def custom_stake_amount(self, pair: str, current_time: datetime, current_rate: float, proposed_stake: float,
                            min_stake: float | None, max_stake: float, leverage: float, entry_tag: str | None,
                            side: str, **kwargs) -> float:
        got = self._notional(pair, side)
        if got is None:
            return 0.0
        notional, _ = got
        margin_cap = self.wallets.get_total_stake_amount() * self.margin_cap_pct * max(leverage, 1)
        stake = min(notional, margin_cap) / max(leverage, 1)
        return min(stake, max_stake)

    def order_filled(self, pair: str, trade: Trade, order, current_time: datetime, **kwargs) -> None:
        if trade.nr_of_successful_entries == 1 and order.ft_order_side == trade.entry_side and trade.get_custom_data("stop") is None:
            row = self._row(pair)
            stop = min(row["stop_pct"], self.stop_max) if row is not None else self.stop_max
            sign = -1 if trade.is_short else 1
            trade.set_custom_data("stop_pct", float(stop))
            trade.set_custom_data("stop", float(trade.open_rate * (1 - sign * stop)))
            trade.set_custom_data("risk_amt", float(trade.amount * trade.open_rate * stop))
        if not trade.is_open:
            risk = trade.get_custom_data("risk_amt") or 0
            trade.set_custom_data("r_multiple", float(trade.realized_profit / risk) if risk else 0.0)

    # ------------------------------------------------------------ 손절·익절·정리
    def custom_stoploss(self, pair: str, trade: Trade, current_time: datetime, current_rate: float,
                        current_profit: float, after_fill: bool, **kwargs) -> float | None:
        stop = trade.get_custom_data("stop")
        if stop is None:
            return None
        if trade.nr_of_successful_exits >= 1:  # 1R 익절 뒤 본전
            stop = trade.open_rate
        return stoploss_from_absolute(stop, current_rate, is_short=trade.is_short, leverage=trade.leverage)

    def adjust_trade_position(self, trade: Trade, current_time: datetime, current_rate: float, current_profit: float,
                              min_stake: float | None, max_stake: float, current_entry_rate: float,
                              current_exit_rate: float, current_entry_profit: float, current_exit_profit: float,
                              **kwargs):
        stop_pct = trade.get_custom_data("stop_pct")
        if not stop_pct:
            return None
        move = (current_exit_rate / trade.open_rate - 1) * (-1 if trade.is_short else 1)
        exits = trade.nr_of_successful_exits
        third = trade.stake_amount / (3 - exits) if exits < 2 else None
        if exits == 0 and move >= stop_pct:
            return -third, "TP1"
        if exits == 1 and move >= 2 * stop_pct:
            return -third, "TP2"
        return None

    def custom_exit(self, pair: str, trade: Trade, current_time: datetime, current_rate: float, current_profit: float,
                    **kwargs):
        row = self._row(pair)
        if row is None:
            return None
        side = -1 if trade.is_short else 1
        if row["regime_1d"] != side:
            return "REGIME"
        if trade.nr_of_successful_exits >= 1 and (row["close"] - row["ema20"]) * side < 0:
            return "TRAIL"
        if self.config.get("trading_mode") == "futures" and current_time - trade.open_date_utc >= timedelta(hours=72):
            return "TIME"
        return None

    def confirm_trade_entry(self, pair: str, order_type: str, amount: float, rate: float, time_in_force: str,
                            current_time: datetime, entry_tag: str | None, side: str, **kwargs) -> bool:
        """하루 -3%·한 주 -6%(실현 손익 기준)면 새 진입 없음, 같은 방향 3코인까지"""
        equity = self.wallets.get_total_stake_amount()
        day = current_time.replace(hour=0, minute=0, second=0, microsecond=0)
        week = day - timedelta(days=day.weekday())
        closed = self._closed()
        lost_day = sum(t.realized_profit for t in closed if t.close_date_utc and t.close_date_utc >= day)
        lost_week = sum(t.realized_profit for t in closed if t.close_date_utc and t.close_date_utc >= week)
        if lost_day <= -0.03 * equity or lost_week <= -0.06 * equity:
            return False
        same = [t for t in Trade.get_trades_proxy(is_open=True) if t.is_short == (side == "short")]
        return len(same) < 3
