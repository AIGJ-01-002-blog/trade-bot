"""모의 체결 (#243). 실제 주문을 내지 않고, 받은 가격에 바로 체결된 것으로 보고 평균 단가와 실현 손익을 계산한다.
코인(#255)은 소수 수량을 쓰고, 금액은 계좌 통화(KRW는 원 단위, USDT 등은 소수 8자리)로 맞춘다.

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
    exchange: str = ""  # BINANCE·UPBIT… 증권사 CSV면 비운다
    currency: str = "KRW"


@dataclass
class PaperBroker:
    """현금·보유 종목을 들고 있는 모의 계좌. 수수료율·매도 세율은 쓰는 증권사 기준으로 넣는다."""

    cash: Decimal
    fee_rate: Decimal = Decimal(0)
    sell_tax_rate: Decimal = Decimal(0)
    positions: dict[str, Position] = field(default_factory=dict)
    last_date: str = ""  # 이미 처리한 마지막 날짜(CSV)나 봉 시작 시각(코인). 여기까지는 다시 체결하지 않는다
    currency: str = "KRW"
    exchange: str = ""
    last_seen: dict[str, str] = field(default_factory=dict)  # 코인: 종목마다 처리한 마지막 봉 시작 시각
    market: str = "spot"

    def _money(self, v: Decimal) -> Decimal:
        unit = Decimal(1) if self.currency == "KRW" else Decimal("0.00000001")
        return v.quantize(unit, rounding=ROUND_HALF_UP)

    def buy(self, symbol: str, name: str, quantity: Decimal, price: Decimal, reason: str, at: str) -> Fill | None:
        cost = quantity * price
        fee = self._money(cost * self.fee_rate)
        if quantity <= 0 or cost + fee > self.cash:
            return None
        pos = self.positions.setdefault(symbol, Position())
        total = pos.quantity * pos.avg_price + cost
        pos.quantity += quantity
        pos.avg_price = total / pos.quantity
        self.cash -= cost + fee
        return Fill(f"paper-{uuid.uuid4()}", symbol, name, "BUY", quantity, price, fee, None, reason, at, self.exchange, self.currency)

    def sell(self, symbol: str, name: str, quantity: Decimal, price: Decimal, reason: str, at: str) -> Fill | None:
        pos = self.positions.get(symbol)
        if pos is None or quantity <= 0 or quantity > pos.quantity:
            return None
        proceeds = quantity * price
        fee = self._money(proceeds * self.fee_rate) + self._money(proceeds * self.sell_tax_rate)
        pnl = self._money(proceeds - quantity * pos.avg_price - fee)
        pos.quantity -= quantity
        if pos.quantity == 0:
            del self.positions[symbol]
        self.cash += proceeds - fee
        return Fill(f"paper-{uuid.uuid4()}", symbol, name, "SELL", quantity, price, fee, pnl, reason, at, self.exchange, self.currency)

    def holding(self, symbol: str) -> Decimal:
        pos = self.positions.get(symbol)
        return pos.quantity if pos else Decimal(0)

    def describe(self) -> str:
        unit = "" if self.exchange else "주"
        held = ", ".join(f"{s} {p.quantity:f}{unit}" for s, p in self.positions.items())
        cash = f"{self.cash:,.0f}원" if self.currency == "KRW" else f"{self.cash.quantize(Decimal('0.00000001')).normalize():f} {self.currency}"
        return f"현금 {cash} · 보유 {held or '없음'}"

    # 상태 파일: 다음 실행이 처리한 마지막 날짜까지 건너뛰고 보유 종목을 이어 간다
    def save(self, path: Path) -> None:
        data = {
            "cash": str(self.cash),
            "fee_rate": str(self.fee_rate),
            "sell_tax_rate": str(self.sell_tax_rate),
            "positions": {s: {k: str(v) for k, v in asdict(p).items()} for s, p in self.positions.items()},
            "last_date": self.last_date,
            "currency": self.currency,
            "exchange": self.exchange,
            "last_seen": self.last_seen,
            "market": self.market,
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
            currency=data.get("currency", "KRW"),
            exchange=data.get("exchange", ""),
            last_seen=data.get("last_seen", {}),
        )
