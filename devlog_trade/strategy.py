"""예제 전략 (#243): 종가가 이동평균선을 위로 뚫으면 사고, 아래로 내려가거나 손절선에 닿으면 판다.
선물(#255)은 위로 뚫으면 롱, 아래로 뚫으면 숏으로 들어간다.
수익을 내는 전략이 아니라 기록 흐름을 보여 주는 예제다. 자기 전략으로 바꿔 쓴다.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path


@dataclass
class Bar:
    date: str  # 2026-10-14
    symbol: str
    name: str
    close: Decimal


@dataclass
class Signal:
    side: str  # BUY | SELL
    reason: str


def read_bars(path: Path) -> list[Bar]:
    """date,symbol,name,close 열이 있는 CSV. 날짜 순으로 돌려준다"""
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = [Bar(r["date"].strip(), r["symbol"].strip(), r["name"].strip(), Decimal(r["close"])) for r in csv.DictReader(f)]
    return sorted(rows, key=lambda b: (b.date, b.symbol))


class MovingAverageCross:
    def __init__(self, window: int = 5, stop_loss: Decimal = Decimal("0.03"), period: str = "일"):
        """period: 신호 이유에 쓰는 기간 이름. 종가 CSV는 "5일", 코인 1시간봉은 "5개 1h봉"처럼 읽힌다"""
        self.window = window
        self.period = period
        self.stop_loss = stop_loss
        self.closes: dict[str, list[Decimal]] = {}

    def _cross(self, bar: Bar) -> tuple[bool, str | None]:
        """종가를 쌓고 (데워졌는지, 교차 방향 UP·DOWN·None)을 돌려준다"""
        history = self.closes.setdefault(bar.symbol, [])
        prev_above = len(history) >= self.window and history[-1] > sum(history[-self.window:]) / self.window
        history.append(bar.close)
        if len(history) <= self.window:
            return False, None
        above = bar.close > sum(history[-self.window:]) / self.window
        return True, "UP" if above and not prev_above else "DOWN" if not above and prev_above else None

    def on_bar(self, bar: Bar, holding: bool, avg_price: Decimal) -> Signal | None:
        """현물: 사고 팔기만 한다(공매도 없음)"""
        ready, cross = self._cross(bar)
        if not ready:
            return None
        if holding and avg_price > 0 and bar.close <= avg_price * (1 - self.stop_loss):
            return Signal("SELL", f"손절선 -{self.stop_loss * 100:.0f}% 도달")
        if not holding and cross == "UP":
            return Signal("BUY", f"종가가 {self.window}{self.period} 이동평균선 위로 올라섬")
        if holding and cross == "DOWN":
            return Signal("SELL", f"종가가 {self.window}{self.period} 이동평균선 아래로 내려감")
        return None

    def on_bar_futures(self, bar: Bar, side: str | None, entry: Decimal) -> list[Signal]:
        """선물: 위로 뚫으면 롱, 아래로 뚫으면 숏. 반대 신호나 손절이면 먼저 정리한다.
        돌려주는 Signal.side는 CLOSE·LONG·SHORT다"""
        ready, cross = self._cross(bar)
        if not ready:
            return []
        out: list[Signal] = []
        if side == "LONG" and bar.close <= entry * (1 - self.stop_loss) or side == "SHORT" and bar.close >= entry * (1 + self.stop_loss):
            return [Signal("CLOSE", f"손절선 -{self.stop_loss * 100:.0f}% 도달")]
        if cross == "UP":
            if side == "SHORT":
                out.append(Signal("CLOSE", f"종가가 {self.window}{self.period} 이동평균선 위로 올라섬"))
            if side != "LONG":
                out.append(Signal("LONG", f"종가가 {self.window}{self.period} 이동평균선 위로 올라섬"))
        elif cross == "DOWN":
            if side == "LONG":
                out.append(Signal("CLOSE", f"종가가 {self.window}{self.period} 이동평균선 아래로 내려감"))
            if side != "SHORT":
                out.append(Signal("SHORT", f"종가가 {self.window}{self.period} 이동평균선 아래로 내려감"))
        return out
