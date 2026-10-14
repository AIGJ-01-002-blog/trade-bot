"""모의 체결 (#243). 실제 주문을 내지 않고, 받은 가격에 바로 체결된 것으로 보고 평균 단가와 실현 손익을 계산한다.

실계좌 주문은 이 예제에 없다. 되돌릴 수 없는 일이라, 실계좌 주문을 붙이려면 직접 코드를 더하고 직접 켜야 한다.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path


@dataclass
class Position:
    quantity: Decimal = Decimal(0)
    avg_price: Decimal = Decimal(0)


@dataclass
class Fill:
    external_id: str
    symbol: str
    symbol_name: str
    side: str  # BUY | SELL
    quantity: Decimal
    price: Decimal
    fee: Decimal
    realized_pnl: Decimal | None
    reason: str
    executed_at: str  # ISO-8601, 시간대 포함


@dataclass
class PaperBroker:
    """현금·보유 종목을 들고 있는 모의 계좌. 수수료율·매도 세율은 쓰는 증권사 기준으로 넣는다."""

    cash: Decimal
    fee_rate: Decimal = Decimal(0)
    sell_tax_rate: Decimal = Decimal(0)
    positions: dict[str, Position] = field(default_factory=dict)
    last_date: str = ""  # 이미 처리한 마지막 날짜. 이 날짜까지는 다시 체결하지 않는다

    @staticmethod
    def _won(v: Decimal) -> Decimal:
        return v.quantize(Decimal(1), rounding=ROUND_HALF_UP)

    def buy(self, symbol: str, name: str, quantity: Decimal, price: Decimal, reason: str, at: str) -> Fill | None:
        cost = quantity * price
        fee = self._won(cost * self.fee_rate)
        if quantity <= 0 or cost + fee > self.cash:
            return None
        pos = self.positions.setdefault(symbol, Position())
        total = pos.quantity * pos.avg_price + cost
        pos.quantity += quantity
        pos.avg_price = total / pos.quantity
        self.cash -= cost + fee
        return Fill(f"paper-{uuid.uuid4()}", symbol, name, "BUY", quantity, price, fee, None, reason, at)

    def sell(self, symbol: str, name: str, quantity: Decimal, price: Decimal, reason: str, at: str) -> Fill | None:
        pos = self.positions.get(symbol)
        if pos is None or quantity <= 0 or quantity > pos.quantity:
            return None
        proceeds = quantity * price
        fee = self._won(proceeds * self.fee_rate) + self._won(proceeds * self.sell_tax_rate)
        pnl = self._won(proceeds - quantity * pos.avg_price - fee)
        pos.quantity -= quantity
        if pos.quantity == 0:
            del self.positions[symbol]
        self.cash += proceeds - fee
        return Fill(f"paper-{uuid.uuid4()}", symbol, name, "SELL", quantity, price, fee, pnl, reason, at)

    def holding(self, symbol: str) -> Decimal:
        pos = self.positions.get(symbol)
        return pos.quantity if pos else Decimal(0)

    # 상태 파일: 다음 실행이 처리한 마지막 날짜까지 건너뛰고 보유 종목을 이어 간다
    def save(self, path: Path) -> None:
        data = {
            "cash": str(self.cash),
            "fee_rate": str(self.fee_rate),
            "sell_tax_rate": str(self.sell_tax_rate),
            "positions": {s: {k: str(v) for k, v in asdict(p).items()} for s, p in self.positions.items()},
            "last_date": self.last_date,
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> "PaperBroker":
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            cash=Decimal(data["cash"]),
            fee_rate=Decimal(data["fee_rate"]),
            sell_tax_rate=Decimal(data["sell_tax_rate"]),
            positions={s: Position(Decimal(p["quantity"]), Decimal(p["avg_price"])) for s, p in data["positions"].items()},
            last_date=data.get("last_date", ""),
        )
