"""python -m devlog_trade run prices.csv : 새 날짜의 종가로 전략을 돌려 모의 체결하고 devlog 매매 일지에 남긴다 (#243)."""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

from .broker import Fill, PaperBroker
from .journal import Journal, JournalError
from .strategy import MovingAverageCross, read_bars


def _fill_from(d: dict) -> Fill:
    return Fill(d["external_id"], d["symbol"], d["symbol_name"], d["side"], Decimal(d["quantity"]), Decimal(d["price"]),
                Decimal(d["fee"]), None if d["realized_pnl"] is None else Decimal(d["realized_pnl"]), d["reason"], d["executed_at"])


def _pending_path(state: Path) -> Path:
    return state.with_name(state.stem + ".pending.json")


def run(args: argparse.Namespace) -> int:
    if args.live:
        print("실계좌 주문은 이 예제에 없어요. 되돌릴 수 없는 주문이라 직접 코드를 더하고 직접 켜야 해요.", file=sys.stderr)
        return 2
    state = Path(args.state)
    broker = PaperBroker.load(state) if state.exists() else PaperBroker(
        cash=Decimal(args.cash), fee_rate=Decimal(args.fee_rate), sell_tax_rate=Decimal(args.sell_tax_rate))
    pending_file = _pending_path(state)
    pending = [_fill_from(d) for d in json.loads(pending_file.read_text(encoding="utf-8"))] if pending_file.exists() else []
    journal = None if args.dry_run else Journal(args.server, os.environ.get("DEVLOG_TOKEN", ""))
    strategy = MovingAverageCross(window=args.window, stop_loss=Decimal(args.stop_loss))

    def send_all() -> bool:
        while pending:
            fill = pending[0]
            if journal is None:
                print(f"[dry-run] {fill.executed_at} {fill.symbol_name} {fill.side} {fill.quantity}주 @ {fill.price} 손익 {fill.realized_pnl} · {fill.reason}")
            else:
                try:
                    print(journal.record(fill, "PAPER", args.strategy))
                except (JournalError, OSError) as e:
                    print(f"devlog에 남기지 못했어요 (다음 실행 때 다시 보내요): {e}", file=sys.stderr)
                    return False
            pending.pop(0)
        return True

    def save() -> None:
        broker.save(state)
        if pending:
            pending_file.write_text(json.dumps([asdict(f) for f in pending], default=str, ensure_ascii=False, indent=2), encoding="utf-8")
        elif pending_file.exists():
            pending_file.unlink()

    ok = send_all()
    for bar in read_bars(Path(args.prices)):
        fresh = bar.date > broker.last_date
        pos = broker.positions.get(bar.symbol)
        signal = strategy.on_bar(bar, pos is not None, pos.avg_price if pos else Decimal(0))
        if not fresh or signal is None:
            continue
        at = f"{bar.date}T{args.time}:00+09:00"
        if signal.side == "BUY":
            qty = (Decimal(args.budget) / bar.close).to_integral_value(rounding="ROUND_FLOOR")
            fill = broker.buy(bar.symbol, bar.name, qty, bar.close, signal.reason, at)
        else:
            fill = broker.sell(bar.symbol, bar.name, broker.holding(bar.symbol), bar.close, signal.reason, at)
        if fill:
            pending.append(fill)
    bars = read_bars(Path(args.prices))
    if bars:
        broker.last_date = max(broker.last_date, bars[-1].date)
    ok = send_all() and ok
    save()
    print(f"현금 {broker.cash:,.0f}원 · 보유 {', '.join(f'{s} {p.quantity}주' for s, p in broker.positions.items()) or '없음'}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="devlog_trade", description="모의 체결 봇 예제: 체결마다 devlog 매매 일지에 기록을 남겨요.")
    sub = p.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run", help="새 날짜의 종가로 전략을 돌려 모의 체결하고 devlog에 남겨요")
    r.add_argument("prices", help="date,symbol,name,close 열이 있는 CSV")
    r.add_argument("--state", default="trade-state.json", help="모의 계좌 상태 파일 (현금·보유 종목·처리한 날짜)")
    r.add_argument("--server", default="https://devlog.life", help="devlog 주소. 집에서 띄웠으면 http://localhost:8080")
    r.add_argument("--cash", default="10000000", help="처음 모의 현금 (원)")
    r.add_argument("--budget", default="1000000", help="한 번 살 때 쓸 금액 (원)")
    r.add_argument("--fee-rate", default="0", help="매매 수수료율 (예: 0.00015). 쓰는 증권사 기준으로 넣어요")
    r.add_argument("--sell-tax-rate", default="0", help="매도할 때 내는 세율. 그해 세율을 확인해 넣어요")
    r.add_argument("--window", type=int, default=5, help="이동평균 날 수")
    r.add_argument("--stop-loss", default="0.03", help="손절 비율 (0.03 = -3%)")
    r.add_argument("--strategy", default="ma-cross", help="매매 일지에 남길 전략 이름")
    r.add_argument("--time", default="15:20", help="체결 시각으로 남길 한국 시간 (종가 기준이라 장 마감 무렵)")
    r.add_argument("--dry-run", action="store_true", help="devlog에 보내지 않고 화면에만 보여요")
    r.add_argument("--live", action="store_true", help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
