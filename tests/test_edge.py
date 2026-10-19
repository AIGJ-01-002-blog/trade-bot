"""우위 스캔 규칙(breakout·volbreak, #40): 신호, 하루 한 번, 절반 비중, 모의 전용을 확인한다."""

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

from devlog_trade import __main__ as cli
from devlog_trade import edge
from devlog_trade.broker import PaperBroker
from devlog_trade.exchanges import Binance, BinanceFutures, Candle
from devlog_trade.futures import FuturesBroker

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


def bar(start: datetime, o: float, h: float, lo: float, c: float, hours: int = 1) -> Candle:
    return Candle(start, start + timedelta(hours=hours), Decimal(str(c)), Decimal(str(o)), Decimal(str(h)), Decimal(str(lo)))


class FakeSession:
    def __init__(self, broker):
        self.broker, self.state, self.fills = broker, Path("unused.json"), []

    def _add(self, fill):
        if fill:
            self.fills.append(fill)


def rising_days(n: int = 60) -> list[Candle]:
    return [bar(T0 - timedelta(days=n - i), 100 + i, 101 + i, 99 + i, 100.5 + i, 24) for i in range(n)]


class BreakoutTest(unittest.TestCase):
    def setUp(self):
        self.h1 = [bar(T0 + timedelta(hours=i), 100, 100.5, 99.5, 100) for i in range(100)]
        self.b = PaperBroker(cash=Decimal(1000), fee_rate=Decimal("0.001"), exchange="BINANCE", currency="USDT")
        self.s = FakeSession(self.b)
        self.e = edge.Edge("breakout", self.s, Binance(fetch=lambda u: []), edge.Book(), n_symbols=5)

    def test_buys_half_share_on_breakout_in_uptrend(self):
        self.h1.append(bar(T0 + timedelta(hours=100), 100, 102, 100, 102))
        self.e.step("BTCUSDT", "비트코인", self.h1, 100, rising_days(), "t")
        pos = self.b.positions["BTCUSDT"]
        self.assertAlmostEqual(float(pos.quantity * pos.avg_price), 100 / 1.001, places=4)  # 1000 ÷ 5 × 0.5
        self.assertIn("breakout-v0.1", self.s.fills[0].reason)

    def test_no_buy_in_downtrend_and_sells_on_exit_low(self):
        falling = [bar(T0 - timedelta(days=60 - i), 200 - i, 201 - i, 199 - i, 199.5 - i, 24) for i in range(60)]
        self.h1.append(bar(T0 + timedelta(hours=100), 100, 102, 100, 102))
        self.e.step("BTCUSDT", "비트코인", self.h1, 100, falling, "t")
        self.assertNotIn("BTCUSDT", self.b.positions)
        self.e.step("BTCUSDT", "비트코인", self.h1, 100, rising_days(), "t")
        self.h1.append(bar(T0 + timedelta(hours=101), 100, 100, 98, 99))
        self.e.step("BTCUSDT", "비트코인", self.h1, 101, rising_days(), "t2")
        self.assertNotIn("BTCUSDT", self.b.positions)
        self.assertIn("최저가", self.s.fills[-1].reason)


class VolbreakTest(unittest.TestCase):
    def test_enters_once_a_day_and_exits_on_last_bar(self):
        b = FuturesBroker(cash=Decimal(1000), fee_rate=Decimal("0.0005"), leverage=Decimal(3))
        s = FakeSession(b)
        e = edge.Edge("volbreak", s, BinanceFutures(fetch=lambda u: []), edge.Book(), n_symbols=1)
        d1 = [bar(T0 - timedelta(days=1), 100, 110, 100, 105, 24)]  # 어제 폭 10 → 목표 100 + 7 = 107
        closes = [101, 108, 106, 109] + [106] * 19 + [104]
        h1 = [bar(T0 + timedelta(hours=i), 100 if i == 0 else 105, 110, 99, c) for i, c in enumerate(closes)]
        for i in range(len(h1)):
            e.step("BTCUSDT", "비트코인 선물", h1, i, d1, f"t{i}")
            if i == 1:
                self.assertEqual(b.positions["BTCUSDT"].side, "LONG")
                self.assertIn("1x", s.fills[0].reason)  # 계좌 레버리지와 상관없이 1배
        self.assertEqual(len(s.fills), 2)  # 3시 봉에서 다시 넘어도 하루 한 번만
        self.assertNotIn("BTCUSDT", b.positions)
        self.assertIn("마지막 봉", s.fills[-1].reason)
        self.assertEqual(b.leverage, Decimal(3))


class EdgeCliTest(unittest.TestCase):
    def test_refuses_live_and_other_intervals(self):
        for rule in edge.RULES:
            for extra in (["--interval", "4h"], ["--live"]):
                err = io.StringIO()
                with redirect_stderr(err), redirect_stdout(io.StringIO()), mock.patch.dict("os.environ", {}, clear=True):
                    code = cli.main(["coin", "--rule", rule, "--dry-run", *extra])
                self.assertEqual(code, 2)
                self.assertIn("1시간봉 모의 계좌에서만", err.getvalue())

    def test_runs_and_keeps_book_next_to_state(self):
        def fetch(url):
            rows = []
            if "interval=1d" in url:
                rows = [bar(T0 - timedelta(days=60 - i), 100 + i, 101 + i, 99 + i, 100.5 + i, 24) for i in range(60)]
            else:
                rows = [bar(T0 + timedelta(hours=i), 100, 100.5, 99.5, 100) for i in range(100)] + \
                       [bar(T0 + timedelta(hours=100), 100, 102, 100, 102)]
            return [[int(k.open_time.timestamp() * 1000), str(k.open), str(k.high), str(k.low), str(k.close), "0", 0] for k in rows]
        ex = Binance(fetch=fetch)
        real = ex.fetch_candles
        ex.fetch_candles = lambda symbol, interval, limit: real(symbol, interval, limit, now=T0 + timedelta(hours=101))
        out, err = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as d, mock.patch.dict("os.environ", {}, clear=True), redirect_stdout(out), \
                redirect_stderr(err), mock.patch.dict(cli.EXCHANGES, {("binance", "spot"): lambda: ex}):
            state = Path(d) / "bo.json"
            code = cli.main(["coin", "--rule", "breakout", "--symbol", "BTCUSDT", "--state", str(state), "--dry-run"])
            self.assertTrue((Path(d) / "bo.edge.json").exists())
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("breakout-v0.1", out.getvalue())
        self.assertIn("BUY", out.getvalue())


if __name__ == "__main__":
    unittest.main()
