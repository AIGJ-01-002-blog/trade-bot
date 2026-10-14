"""선물 모의 계좌 (#255). 바이낸스 USDT-M 무기한 선물을 흉내 낸다. 실제 주문은 내지 않는다.

- 롱·숏 모두 들어가고, 증거금 = 명목 금액 / 레버리지(교차 아님, 종목마다 따로)다.
- 수수료는 진입·정리 때 명목 금액에 붙는다. 실현 손익은 (정리가 - 진입가) × 수량 × 방향 - 정리 수수료로, 현물과 같은 기준이다.
- 봉 종가에서 손실이 증거금을 다 쓰면 그 값에 강제 청산한다. 유지 증거금·펀딩비는 아직 계산하지 않는다.

devlog 매매 일지에는 진입 롱 BUY, 진입 숏 SELL, 정리는 반대 방향으로 남긴다.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from .broker import Fill

STEP = Decimal("0.00000001")


@dataclass
class FuturesPosition:
    side: str  # LONG | SHORT
    quantity: Decimal
    entry: Decimal
    margin: Decimal
    name: str = ""

    @property
    def direction(self) -> int:
        return 1 if self.side == "LONG" else -1

    def unrealized(self, price: Decimal) -> Decimal:
        return (price - self.entry) * self.quantity * self.direction


@dataclass
class FuturesBroker:
    cash: Decimal  # 지갑 잔고(USDT). 증거금은 여기서 묶인다
    fee_rate: Decimal = Decimal("0.0005")
    leverage: Decimal = Decimal(1)
    positions: dict[str, FuturesPosition] = field(default_factory=dict)
    last_date: str = ""
    last_seen: dict[str, str] = field(default_factory=dict)  # 종목마다 처리한 마지막 봉 시작 시각
    currency: str = "USDT"
    exchange: str = "BINANCE"
    market: str = "futures"

    @staticmethod
    def _money(v: Decimal) -> Decimal:
        return v.quantize(STEP, rounding=ROUND_HALF_UP)

    def available(self) -> Decimal:
        return self.cash - sum((p.margin for p in self.positions.values()), Decimal(0))

    def quantity_for(self, budget: Decimal, price: Decimal) -> Decimal:
        """증거금 budget으로 들어갈 수 있는 수량. 수수료까지 남게 소수 8자리에서 버린다"""
        spend = min(budget, self.available())
        if spend <= 0 or price <= 0:
            return Decimal(0)
        return (spend * self.leverage / (price * (1 + self.fee_rate * self.leverage))).quantize(STEP, rounding="ROUND_FLOOR")

    def open(self, symbol: str, name: str, side: str, quantity: Decimal, price: Decimal, reason: str, at: str) -> Fill | None:
        if symbol in self.positions or quantity <= 0 or side not in ("LONG", "SHORT"):
            return None
        notional = quantity * price
        margin = self._money(notional / self.leverage)
        fee = self._money(notional * self.fee_rate)
        if margin + fee > self.available():
            return None
        self.cash -= fee
        self.positions[symbol] = FuturesPosition(side, quantity, price, margin, name)
        label = "롱" if side == "LONG" else "숏"
        return Fill(f"paper-{uuid.uuid4()}", symbol, name, "BUY" if side == "LONG" else "SELL", quantity, price, fee, None,
                    f"[선물 {label} 진입 {self.leverage:f}x] {reason}", at, self.exchange, self.currency)

    def close(self, symbol: str, price: Decimal, reason: str, at: str, liquidated: bool = False) -> Fill | None:
        pos = self.positions.pop(symbol, None)
        if pos is None:
            return None
        fee = self._money(pos.quantity * price * self.fee_rate)
        gross = pos.unrealized(price)
        if liquidated:  # 증거금까지만 잃는다
            gross = max(gross, -pos.margin)
        pnl = self._money(gross - fee)
        self.cash += pnl
        label = "롱" if pos.side == "LONG" else "숏"
        tag = "강제 청산" if liquidated else "정리"
        return Fill(f"paper-{uuid.uuid4()}", symbol, pos.name, "SELL" if pos.side == "LONG" else "BUY", pos.quantity, price, fee, pnl,
                    f"[선물 {label} {tag}] {reason}", at, self.exchange, self.currency)

    def liquidation_due(self, symbol: str, price: Decimal) -> bool:
        pos = self.positions.get(symbol)
        return pos is not None and pos.unrealized(price) <= -pos.margin

    def describe(self) -> str:
        held = ", ".join(f"{s} {p.side} {p.quantity:f}" for s, p in self.positions.items())
        return f"지갑 {self.cash.quantize(STEP).normalize():f} {self.currency} · 포지션 {held or '없음'}"

    def save(self, path: Path) -> None:
        data = {
            "market": self.market,
            "cash": str(self.cash),
            "fee_rate": str(self.fee_rate),
            "leverage": str(self.leverage),
            "positions": {s: {"side": p.side, "quantity": str(p.quantity), "entry": str(p.entry), "margin": str(p.margin), "name": p.name}
                          for s, p in self.positions.items()},
            "last_seen": self.last_seen,
            "currency": self.currency,
            "exchange": self.exchange,
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> "FuturesBroker":
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            cash=Decimal(data["cash"]),
            fee_rate=Decimal(data["fee_rate"]),
            leverage=Decimal(data["leverage"]),
            positions={s: FuturesPosition(p["side"], Decimal(p["quantity"]), Decimal(p["entry"]), Decimal(p["margin"]), p.get("name", ""))
                       for s, p in data["positions"].items()},
            last_seen=data.get("last_seen", {}),
            currency=data.get("currency", "USDT"),
            exchange=data.get("exchange", "BINANCE"),
        )
