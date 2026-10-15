"""devlog 매매 일지 봇 예제 (#243, #255). 새 가격으로 전략을 돌려 모의 체결하고 devlog 매매 일지에 남긴다.

python -m devlog_trade run prices.csv                       : 종가 CSV (증권사 종목, 원화)
python -m devlog_trade coin --exchange binance --interval 1h : 거래소 공개 캔들 (코인, API 키 없음)
python -m devlog_trade coin --market futures --leverage 3     : 바이낸스 USDT-M 선물 (롱·숏)
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Callable

from . import __version__, ai, ladder, live, notify, report, rules
from .broker import Fill, PaperBroker, state_lock
from .exchanges import COINS, EXCHANGES, INTERVALS, Exchange, ExchangeError
from .futures import FuturesBroker
from .journal import Journal, JournalError
from .strategy import Bar, MovingAverageCross, read_bars

KST = timezone(timedelta(hours=9))
MAX_LEVERAGE = Decimal(live.LEVERAGE_CAP)  # 민서님 결정: 선물은 5배까지(2026-10-15). 실계좌(live.py)와 같은 값
COIN_STEP = Decimal("0.00000001")  # 코인 수량·USDT 금액은 소수 8자리까지
LIVE_REFUSED = "실계좌 주문은 coin 명령에서만 돼요(바이낸스 현물·선물, 업비트 현물). 종가 CSV 모드는 모의 체결만 해요."


def _fill_from(d: dict) -> Fill:
    return Fill(d["external_id"], d["symbol"], d["symbol_name"], d["side"], Decimal(d["quantity"]), Decimal(d["price"]),
                Decimal(d["fee"]), None if d["realized_pnl"] is None else Decimal(d["realized_pnl"]), d["reason"], d["executed_at"],
                d.get("exchange", ""), d.get("currency", "KRW"))


def _pending_path(state: Path) -> Path:
    return state.with_name(state.stem + ".pending.json")


def _amount(v: Decimal, currency: str) -> str:
    return f"{v:,.0f}원" if currency == "KRW" else f"{v.quantize(COIN_STEP).normalize():f} {currency}"


class Session:
    """상태 파일·보내지 못한 체결·devlog 보내기를 한곳에서 다룬다. CSV·현물·선물 모드가 함께 쓴다"""

    day, day_pnl = "", Decimal(0)
    profit_stop = Decimal(0)  # 하루 실현 손익이 이 금액 이상이면 그날 새 진입을 쉰다(0이면 끔)
    plan = notify.Plan()  # 디스코드 체결 알림에 보일 손절·익절. 코인 이동평균 모드가 채운다

    def __init__(self, args: argparse.Namespace, new_broker: Callable[[], PaperBroker | FuturesBroker] | None,
                 broker: PaperBroker | FuturesBroker | None = None):
        self.args = args
        self.state = Path(args.state)
        if broker is not None:  # 실계좌 브로커는 live.open_live가 상태 파일까지 열어서 준다
            self.broker = broker
        elif self.state.exists():
            market = json.loads(self.state.read_text(encoding="utf-8")).get("market", "spot")
            self.broker = FuturesBroker.load(self.state) if market == "futures" else PaperBroker.load(self.state)
        else:
            self.broker = new_broker()
        self.pending_file = _pending_path(self.state)
        self.pending = [_fill_from(d) for d in json.loads(self.pending_file.read_text(encoding="utf-8"))] \
            if self.pending_file.exists() else []
        self.journal = None if args.dry_run else Journal(args.server, os.environ.get("DEVLOG_TOKEN", ""))
        # 하루 수익 멈춤용 한국 날짜별 실현 손익. 상태 파일에 함께 남아 5분마다 새로 뜨는 실행이 이어 센다
        saved = json.loads(self.state.read_text(encoding="utf-8")).get("day_profit", {}) if self.state.exists() else {}
        self.day, self.day_pnl = saved.get("day", ""), Decimal(saved.get("pnl", "0"))

    def send_all(self) -> bool:
        while self.pending:
            fill = self.pending[0]
            if self.journal is None:
                unit = "주" if fill.currency == "KRW" and not fill.exchange else " " + fill.symbol
                pnl = "" if fill.realized_pnl is None else f" 손익 {_amount(fill.realized_pnl, fill.currency)}"
                print(f"[dry-run] {fill.executed_at} {fill.symbol_name} {fill.side} {fill.quantity:f}{unit} @ "
                      f"{_amount(fill.price, fill.currency)}{pnl} · {fill.reason}")
            else:
                try:
                    print(self.journal.record(fill, getattr(self.broker, "mode", "PAPER"), self.args.strategy))
                except (JournalError, OSError) as e:
                    print(f"devlog에 남기지 못했어요 (다음 실행 때 다시 보내요): {e}", file=sys.stderr)
                    return False
            self.pending.pop(0)
        return True

    def _add(self, fill: Fill | None) -> None:
        if fill:
            self.pending.append(fill)
            if fill.realized_pnl is not None:
                self._day(fill.executed_at)
                self.day_pnl += fill.realized_pnl
            # devlog 자동매매 화면(/trade)용. 실계좌 체결은 report가 빼고, 매매 일지 보내기와 따로 다음 알림 때 나간다
            report.add_event(self.state.parent, report.fill_event(self.state.name, self.broker.market, fill))
            self._notify(fill)

    def _notify(self, fill: Fill) -> None:
        """사고팔 때마다 디스코드로. 실거래와 모의(테스트넷 포함)는 다른 채널이다"""
        mode = getattr(self.broker, "mode", "PAPER")
        trader = getattr(self.broker, "trader", None)
        if mode == "LIVE" and getattr(trader, "testnet", False):
            mode = "TESTNET"
        notify.send(mode, notify.text(mode, self.state.name, self.broker.market, fill, self.plan, self.broker.describe(),
                                      self.day_pnl if fill.realized_pnl is not None else None), colour=notify.color(fill))

    def _day(self, at: str) -> None:
        day = datetime.fromisoformat(at.replace("Z", "+00:00")).astimezone(KST).date().isoformat()
        if day != self.day:
            self.day, self.day_pnl = day, Decimal(0)

    def _entry_blocked(self, at: str) -> bool:
        """하루 수익 멈춤: 그날 번 돈이 목표에 닿았으면 새 진입만 막는다. 들고 있는 것의 손절·익절·정리는 그대로 나간다"""
        if self.profit_stop <= 0:
            return False
        self._day(at)
        if self.day_pnl < self.profit_stop:
            return False
        print(f"{self.state.name}: 오늘({self.day}) 실현 수익이 {_amount(self.day_pnl, self.broker.currency)}로 "
              f"하루 목표 {_amount(self.profit_stop, self.broker.currency)}에 닿아 새 진입을 쉬어요.")
        return True

    def trade(self, strategy: MovingAverageCross, bars: list[Bar], quantity: Callable[[Decimal], Decimal],
              executed_at: Callable[[Bar], str]) -> None:
        """종가 CSV: bars 전체로 전략을 데우고, 상태 파일의 마지막 날짜보다 뒤인 날에서만 체결한다"""
        broker = self.broker
        for bar in bars:
            fresh = bar.date > broker.last_date
            pos = broker.positions.get(bar.symbol)
            signal = strategy.on_bar(bar, pos is not None, pos.avg_price if pos else Decimal(0))
            if not fresh or signal is None:
                continue
            if signal.side == "BUY":
                self._add(broker.buy(bar.symbol, bar.name, quantity(bar.close), bar.close, signal.reason, executed_at(bar)))
            else:
                self._add(broker.sell(bar.symbol, bar.name, broker.holding(bar.symbol), bar.close, signal.reason, executed_at(bar)))
        if bars:
            broker.last_date = max(broker.last_date, bars[-1].date)

    def trade_coin(self, strategy: MovingAverageCross, symbol: str, bars: list[Bar], budget: Decimal,
                   executed_at: Callable[[Bar], str]) -> None:
        """코인 한 종목: 받은 봉 전체로 전략을 데우고, 그 종목의 마지막 봉 시각보다 뒤인 봉에서만 체결한다"""
        broker = self.broker
        seen = broker.last_seen.get(symbol, "")
        if not seen and getattr(broker, "mode", "PAPER") == "LIVE" and bars:
            # 실계좌는 처음 보는 종목의 지난 봉으로 주문하지 않는다. 받은 봉으로 전략만 데우고 다음 봉부터 거래한다
            seen = bars[-1].date
        for bar in bars:
            fresh = bar.date > seen
            at = executed_at(bar)
            if broker.market == "futures":  # 모의 선물·실계좌 선물 브로커가 같은 open/close 모양을 쓴다
                if fresh and broker.liquidation_due(symbol, bar.close):
                    self._add(broker.close(symbol, bar.close, "손실이 증거금을 다 씀", at, liquidated=True))
                pos = broker.positions.get(symbol)
                signals = strategy.on_bar_futures(bar, pos.side if pos else None, pos.entry if pos else Decimal(0))
                for sig in signals if fresh else []:
                    if sig.side == "CLOSE":
                        self._add(broker.close(symbol, bar.close, sig.reason, at))
                    elif not self._entry_blocked(at):
                        qty = broker.quantity_for(budget, bar.close)
                        self._add(broker.open(symbol, bar.name, sig.side, qty, bar.close, sig.reason, at))
            else:
                pos = broker.positions.get(symbol)
                signal = strategy.on_bar(bar, pos is not None, pos.avg_price if pos else Decimal(0))
                if not fresh or signal is None:
                    continue
                if signal.side == "BUY":
                    if self._entry_blocked(at):
                        continue
                    spend = min(budget, broker.cash)
                    qty = (spend / (bar.close * (1 + broker.fee_rate))).quantize(COIN_STEP, rounding=ROUND_FLOOR)
                    self._add(broker.buy(symbol, bar.name, qty, bar.close, signal.reason, at))
                else:
                    self._add(broker.sell(symbol, bar.name, broker.holding(symbol), bar.close, signal.reason, at))
        if bars:
            broker.last_seen[symbol] = max(seen, bars[-1].date)

    def save(self) -> None:
        self.broker.save(self.state)
        if self.day:
            data = json.loads(self.state.read_text(encoding="utf-8"))
            data["day_profit"] = {"day": self.day, "pnl": str(self.day_pnl)}
            tmp = self.state.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.state)
        if self.pending:
            self.pending_file.write_text(json.dumps([asdict(f) for f in self.pending], default=str, ensure_ascii=False, indent=2),
                                         encoding="utf-8")
        elif self.pending_file.exists():
            self.pending_file.unlink()


def run(args: argparse.Namespace) -> int:
    """종가 CSV 모드 (#243)"""
    if args.live:
        print(LIVE_REFUSED, file=sys.stderr)
        return 2
    s = Session(args, lambda: PaperBroker(cash=Decimal(args.cash), fee_rate=Decimal(args.fee_rate),
                                          sell_tax_rate=Decimal(args.sell_tax_rate)))
    ok = s.send_all()
    s.trade(MovingAverageCross(window=args.window, stop_loss=Decimal(args.stop_loss)), read_bars(Path(args.prices)),
            lambda close: (Decimal(args.budget) / close).to_integral_value(rounding=ROUND_FLOOR),
            lambda bar: f"{bar.date}T{args.time}:00+09:00")
    ok = s.send_all() and ok
    s.save()
    print(s.broker.describe())
    return 0 if ok else 1


def coin(args: argparse.Namespace) -> int:
    """거래소 공개 캔들 모드 (#255). 다 만들어진 봉의 종가로만 판단하고, 그 봉이 닫힌 시각을 체결 시각으로 남긴다"""
    adapter = EXCHANGES.get((args.exchange, args.market))
    if adapter is None:
        print(f"{args.exchange}에는 {args.market} 시세가 없어요. 선물은 바이낸스만 돼요.", file=sys.stderr)
        return 2
    ex: Exchange = adapter()
    if args.state is None:
        args.state = live.live_state_name(args.exchange, ex.market, args.interval) if args.live else \
            f"trade-state-{'ladder-' if args.rule == 'ladder' else ''}{args.exchange}-{ex.market}-{args.interval}.json"
    chosen = {} if args.live else _chosen_rules(args, ex)
    args.chosen_by = f"대화(AI) · {chosen.get('reason') or '이유 없음'}"[:200] if chosen else ""
    if chosen.get("paused"):
        print(f"{Path(args.state).name}: 대화에서 멈춘 계좌라 이번 바퀴는 건너뛰어요 ({chosen.get('reason') or '이유 없음'}).")
        return 0
    symbols = [s.strip().upper() for s in args.symbol.split(",")] if args.symbol else \
        [ex.symbol_for(c) for c in (args.coins.split(",") if args.coins else COINS) if c.strip()]
    fee_rate = Decimal(args.fee_rate) if args.fee_rate is not None else ex.fee_rate
    cash = Decimal(args.cash) if args.cash else ex.default_cash
    leverage = Decimal(args.leverage)
    if ex.market == "futures" and not Decimal(1) <= leverage <= MAX_LEVERAGE:
        print(f"레버리지는 1~{MAX_LEVERAGE}배로 주세요.", file=sys.stderr)
        return 2
    if args.rule == "ladder" and (args.live or args.interval != "1h"):
        print("세 겹 거름망 기법(--rule ladder)은 1시간봉 모의 계좌에서만 돌아요.", file=sys.stderr)
        return 2
    if args.rule == "ladder" and ai.enabled(args):
        print("세 겹 거름망 기법은 아직 AI 판단 없이 돌아요 (AI 판단 형식은 이슈 #16).", file=sys.stderr)
    if args.live and args.dry_run and not args.testnet:  # 테스트넷은 돈이 오가지 않아 화면에만 볼 수 있다
        print("실거래소 --live는 --dry-run과 함께 쓸 수 없어요. 실계좌 체결은 devlog에 남겨야 해요.", file=sys.stderr)
        return 2
    try:
        with state_lock(Path(args.state)):  # MCP 주문(devlog_trade.mcp)과 같은 상태 파일을 번갈아 쓴다
            return _coin_session(args, ex, symbols, fee_rate, cash, leverage)
    except TimeoutError as e:
        print(e, file=sys.stderr)
        return 1


def _chosen_rules(args: argparse.Namespace, ex: Exchange) -> dict:
    """대화에서 정한 계좌 기준(trade-rules.json)을 덮어 쓴다. 상한을 넘는 값이면 무시하고 jobs.conf대로 돈다"""
    account = Path(args.state).name
    row = rules.for_account(account)
    if not row:
        return {}
    try:
        rules.clean(account, {k: row.get(k) for k in rules.FIELDS if k in row}, args.rule == "ladder", ex.default_budget)
        if args.rule != "ladder":
            leverage = Decimal(args.leverage) if ex.market == "futures" else Decimal(1)
            rules.check_risk(Decimal(row.get("budget") or args.budget or ex.default_budget),
                             Decimal(row.get("stop_loss") or args.stop_loss), leverage, Decimal(args.cash) if args.cash else ex.default_cash)
    except rules.RuleError as e:
        print(f"{account}: 대화에서 정한 기준을 쓰지 않아요 ({e}). jobs.conf대로 돌아요.", file=sys.stderr)
        return {}
    return rules.apply(args, account)


def _coin_session(args: argparse.Namespace, ex: Exchange, symbols: list[str], fee_rate: Decimal, cash: Decimal,
                  leverage: Decimal) -> int:
    def new_broker() -> PaperBroker | FuturesBroker:
        if ex.market == "futures":
            return FuturesBroker(cash=cash, fee_rate=fee_rate, leverage=leverage, currency=ex.currency, exchange=ex.code)
        return PaperBroker(cash=cash, fee_rate=fee_rate, currency=ex.currency, exchange=ex.code)

    if args.live:  # 되돌릴 수 없는 주문. 켜는 조건·상한·키·출금 권한을 live.open_live가 모두 확인한다
        try:
            s = Session(args, None, live.open_live(args, args.exchange, ex.market, Path(args.state)))
        except (live.LiveOrderError, OSError, ValueError) as e:
            print(e, file=sys.stderr)
            return 2
    else:
        s = Session(args, new_broker)
    b = s.broker
    if b.exchange != ex.code or b.currency != ex.currency or b.market != ex.market:
        print(f"상태 파일 {s.state}은 {b.exchange or '종가 CSV'} {b.market}({b.currency}) 계좌예요. --state로 다른 파일을 주세요.",
              file=sys.stderr)
        return 2
    ok = s.send_all()
    if args.rule == "ladder":
        if args.strategy == "ma-cross":
            args.strategy = ladder.VERSION
        report.set_rules(s.state.parent, s.state.name, {
            "전략": f"{ladder.VERSION} 세 겹 거름망", "봉": "1h", "코인": ", ".join(symbols), "레버리지 상한": f"{MAX_LEVERAGE}배",
            "모드": "모의(--dry-run)" if args.dry_run else "모의",
            **({"기준을 정한 곳": args.chosen_by} if getattr(args, "chosen_by", "") else {})})
        got, seen = ladder.run(s, ex, symbols, args.limit, int(MAX_LEVERAGE),
                               lambda k: k.close_time.astimezone(KST).isoformat(timespec="seconds"))
        ok = s.send_all() and got and ok
        s.save()
        print(f"{ex.code} {ex.market} 1h봉 · {', '.join(seen) or '받은 시세 없음'} · {b.describe()}")
        return 0 if ok else 1
    budget = Decimal(args.budget) if args.budget else ex.default_budget
    take_profit = Decimal(args.take_profit or 0)
    s.plan = notify.Plan(Decimal(args.stop_loss), take_profit)
    rule = MovingAverageCross(window=args.window, stop_loss=Decimal(args.stop_loss), period=f"개 {args.interval}봉",
                              take_profit=take_profit)
    if args.daily_profit_stop:
        # 모의는 처음 현금, 실계좌는 이번 바퀴를 시작할 때 계좌 크기(주문 가능 현금 + 들고 있는 코인의 산 값)를 기준으로 한다
        base = sum((getattr(p, "margin", None) or p.quantity * p.avg_price for p in b.positions.values()), b.cash) \
            if args.live else cash
        s.profit_stop = base * Decimal(args.daily_profit_stop)
    closed_at: dict[str, datetime] = {}  # 봉 시작 시각 → 닫힌 시각. 종목마다 아래에서 채운다

    def verdict(bar: Bar, verdict: str, side: str, reason: str) -> None:  # AI 진입 판단을 자동매매 화면에 남긴다
        at = closed_at.get(bar.symbol + bar.date)
        report.add_event(s.state.parent, report.ai_event(
            s.state.name, bar.symbol, bar.date, verdict, side, reason,
            at.astimezone(KST).isoformat(timespec="seconds") if at else datetime.now(KST).isoformat(timespec="seconds")))

    try:
        strategy, args.strategy = ai.wrap(args, rule, args.interval, INTERVALS, b, verdict)  # AI_DECISION=on일 때만 (#263)
    except ai.AiError as e:
        print(e, file=sys.stderr)
        s.save()
        return 2
    model = strategy.client.label if isinstance(strategy, ai.AiStrategy) else ""
    shown = {"전략": f"{args.window}봉 이동평균 교차", "봉": args.interval, "코인": ", ".join(symbols),
             "한 번 금액": _amount(budget, ex.currency), "손절": f"{(Decimal(args.stop_loss) * 100).normalize():f}%",
             "익절": f"{(take_profit * 100).normalize():f}%" if take_profit > 0 else "없음 (평균선이 꺾일 때 정리)",
             "하루 수익 멈춤": _amount(s.profit_stop, ex.currency) if s.profit_stop > 0 else "없음",
             "AI 판단": f"켜짐 ({model})" if model else "꺼짐", "모드": "모의(--dry-run)" if args.dry_run else "모의"}
    if ex.market == "futures":
        shown["레버리지"] = f"{b.leverage:f}배"
    if getattr(args, "chosen_by", ""):
        shown["기준을 정한 곳"] = args.chosen_by
    report.set_rules(s.state.parent, s.state.name, shown, model)
    seen = []
    for symbol in symbols:
        try:
            candles = ex.fetch_candles(symbol, args.interval, args.limit)
        except (ExchangeError, OSError, ValueError, KeyError) as e:
            print(f"{ex.code} {symbol} 시세를 받지 못했어요 (다음 실행 때 다시 해요): {e}", file=sys.stderr)
            ok = False
            continue
        name = ex.coin_name(symbol)
        # 봉 시작 시각(UTC ISO)을 순서 키로 쓴다. 상태 파일에 종목마다 남아 같은 봉을 두 번 거래하지 않는다
        bars = [Bar(c.open_time.strftime("%Y-%m-%dT%H:%M:%SZ"), symbol, name, c.close) for c in candles]
        closed_at.update({symbol + bar.date: c.close_time for bar, c in zip(bars, candles)})
        s.trade_coin(strategy, symbol, bars, budget,
                     lambda bar: closed_at[bar.symbol + bar.date].astimezone(KST).isoformat(timespec="seconds"))
        seen.append(f"{symbol} {len(candles)}개")
    ok = s.send_all() and ok
    s.save()
    print(f"{ex.code} {ex.market} {args.interval}봉 · {', '.join(seen) or '받은 시세 없음'} · {b.describe()}")
    return 0 if ok else 1


def jobs(args: argparse.Namespace) -> int:
    """작업 파일의 줄마다 coin 명령을 돌린다. --every를 주면 그 간격(초)으로 계속 돈다(Docker·집 PC용)"""
    path = Path(args.file)
    while True:
        worst = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            print(f"--- coin {line}", flush=True)
            try:
                code = main(["coin", *shlex.split(line)])
            except SystemExit as e:  # 작업 파일에 잘못 적은 옵션
                code = int(e.code or 2)
            worst = max(worst, code)
            sys.stdout.flush()
        if not args.no_report and report.enabled():  # 화면 알림이 실패해도 종료 코드는 매매 결과만 따른다
            from .mcp import TradeBotTools  # mcp가 이 모듈을 불러서 여기서 늦게 부른다
            report.report(Path("."), TradeBotTools(Path(".")).accounts, os.environ.get("DEVLOG_SERVER", "https://devlog.life"),
                          os.environ.get("DEVLOG_TOKEN", ""))
            sys.stdout.flush()
        if not args.every:
            return worst
        time.sleep(args.every)


def _common(r: argparse.ArgumentParser) -> None:
    r.add_argument("--server", default="https://devlog.life", help="devlog 주소. 집에서 띄웠으면 http://localhost:8080")
    r.add_argument("--window", type=int, default=5, help="이동평균에 쓰는 봉(날) 수")
    r.add_argument("--stop-loss", default="0.03", help="손절 비율 (0.03 = -3%%)")
    r.add_argument("--strategy", default="ma-cross", help="매매 일지에 남길 전략 이름")
    r.add_argument("--dry-run", action="store_true", help="devlog에 보내지 않고 화면에만 보여요")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="devlog_trade", description="모의 체결 봇 예제: 체결마다 devlog 매매 일지에 기록을 남겨요.")
    p.add_argument("--version", action="version", version=f"devlog_trade {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="종가 CSV로 전략을 돌려 모의 체결하고 devlog에 남겨요")
    r.add_argument("prices", help="date,symbol,name,close 열이 있는 CSV")
    r.add_argument("--state", default="trade-state.json", help="모의 계좌 상태 파일 (현금·보유 종목·처리한 날짜)")
    r.add_argument("--cash", default="10000000", help="처음 모의 현금 (원)")
    r.add_argument("--budget", default="1000000", help="한 번 살 때 쓸 금액 (원)")
    r.add_argument("--fee-rate", default="0", help="매매 수수료율 (예: 0.00015). 쓰는 증권사 기준으로 넣어요")
    r.add_argument("--sell-tax-rate", default="0", help="매도할 때 내는 세율. 그해 세율을 확인해 넣어요")
    r.add_argument("--time", default="15:20", help="체결 시각으로 남길 한국 시간 (종가 기준이라 장 마감 무렵)")
    _common(r)
    r.add_argument("--live", action="store_true", help=argparse.SUPPRESS)  # 종가 CSV 모드는 실계좌가 없어 멈춘다

    c = sub.add_parser("coin", help="거래소 공개 캔들로 코인 모의매매하고 devlog에 남겨요 (API 키 없음)")
    c.add_argument("--exchange", choices=sorted({e for e, _ in EXCHANGES}), default="binance", help="시세를 받을 거래소")
    c.add_argument("--market", choices=["spot", "futures"], default="spot", help="현물(spot) 또는 USDT-M 선물(futures, 바이낸스만)")
    c.add_argument("--coins", help=f"거래할 코인, 쉼표로 (기본 {','.join(COINS)})")
    c.add_argument("--symbol", help="마켓 코드를 직접 줄 때, 쉼표로 (예: BTCUSDT 또는 KRW-BTC). 주면 --coins는 무시해요")
    c.add_argument("--rule", choices=["ma-cross", "ladder"], default="ma-cross",
                   help="매매 규칙. ma-cross: 이동평균선 교차(예제), ladder: 세 겹 거름망 복리법(1시간봉 모의 전용)")
    c.add_argument("--interval", choices=list(INTERVALS), default="1h", help="봉 간격")
    c.add_argument("--limit", type=int, default=200, help="종목마다 한 번에 받을 봉 수 (전략을 데우는 데 씀, 업비트는 200까지)")
    c.add_argument("--state", help="모의 계좌 상태 파일. 기본 trade-state-<거래소>-<시장>-<봉>.json")
    c.add_argument("--cash", help="처음 모의 현금. 기본 바이낸스 1000 USDT, 업비트 1,000,000원")
    c.add_argument("--budget", help="한 번 들어갈 때 쓸 금액(선물은 증거금). 기본 바이낸스 100 USDT, 업비트 100,000원")
    c.add_argument("--fee-rate", help="수수료율. 기본 바이낸스 현물 0.001, 선물 0.0005, 업비트 0.0005")
    c.add_argument("--take-profit", help="익절 비율 (0.04 = 들어간 값보다 4%% 유리해지면 정리). 안 주면 평균선이 꺾일 때 정리해요")
    c.add_argument("--daily-profit-stop", help="하루(한국 시간) 실현 수익이 계좌의 이 비율에 닿으면 그날 새 진입을 쉬어요 (0.02 = 2%%). 모의는 처음 현금, 실계좌는 그 바퀴의 현금 + 보유 코인 산 값 기준")
    c.add_argument("--leverage", default="1", help=f"선물 레버리지 (1~{live.LEVERAGE_CAP}배, 기본 1배)")
    _common(c)
    c.add_argument("--live", action="store_true",
                   help=f"실계좌 주문 (기본 꺼짐). 실거래소는 {live.LIVE_ENV}=1도 있어야 하고, --max-order·--max-order-loss·--max-daily-loss가 필요해요")
    live.add_live_args(c)
    ai.add_args(c)

    j = sub.add_parser("jobs", help="작업 파일(줄마다 coin 옵션)을 차례로 돌려요. 맥 launchd·Docker가 써요")
    j.add_argument("file", help="예: jobs.conf")
    j.add_argument("--every", type=int, default=0, help="이 간격(초)으로 계속 돌아요. 안 주면 한 번만")
    j.add_argument("--no-report", action="store_true",
                   help="한 바퀴 뒤 devlog 자동매매 화면(/trade)에 계좌·체결을 알리지 않아요 (환경 변수 TRADE_REPORT=off와 같아요)")

    args = p.parse_args(argv)
    return {"run": run, "coin": coin, "jobs": jobs}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
