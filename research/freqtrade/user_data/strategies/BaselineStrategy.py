# SPDX-License-Identifier: GPL-3.0-or-later
"""지금 trade-bot 규칙을 Freqtrade로 옮긴 비교 기준: 1시간봉 종가가 5봉 평균선을 위로 뚫으면 롱(선물은 아래로 뚫으면 숏),
반대로 뚫으면 정리, 손절 3%. 한 번에 stake_amount(설정 파일, 처음 잔고의 10%)를 쓰고 선물은 3배"""
from __future__ import annotations

from datetime import datetime

from pandas import DataFrame

from freqtrade.strategy import IStrategy


class BaselineStrategy(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "1h"
    can_short = True
    stoploss = -0.03
    process_only_new_candles = True
    startup_candle_count = 10
    use_exit_signal = True

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.can_short = config.get("trading_mode") == "futures"
        if self.can_short:  # Freqtrade 손절은 증거금 기준이라 가격 3%를 3배로 맞춘다
            self.stoploss = -0.09

    def populate_indicators(self, df: DataFrame, metadata: dict) -> DataFrame:
        df["ma"] = df["close"].rolling(5).mean()
        above = df["close"] > df["ma"]
        df["up"] = (above & ~above.shift(1, fill_value=False)).astype(int)
        df["down"] = (~above & above.shift(1, fill_value=False)).astype(int)
        return df

    def populate_entry_trend(self, df: DataFrame, metadata: dict) -> DataFrame:
        df.loc[df["up"] == 1, "enter_long"] = 1
        df.loc[df["down"] == 1, "enter_short"] = 1
        return df

    def populate_exit_trend(self, df: DataFrame, metadata: dict) -> DataFrame:
        df.loc[df["down"] == 1, "exit_long"] = 1
        df.loc[df["up"] == 1, "exit_short"] = 1
        return df

    def leverage(self, pair: str, current_time: datetime, current_rate: float, proposed_leverage: float,
                 max_leverage: float, entry_tag: str | None, side: str, **kwargs) -> float:
        return min(3.0, max_leverage)
