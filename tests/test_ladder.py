"""세 겹 거름망 기법(ladder): 지표, 크기, 분할 익절·본전 손절, 그림자 거래, 브레이크, coin --rule ladder를 확인한다."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

from devlog_trade import __main__ as cli
from devlog_trade import ladder
from devlog_trade.broker import PaperBroker
from devlog_trade.exchanges import Binance, BinanceFutures, Candle
from devlog_trade.futures import FuturesBroker

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


def candle(i: int, o: float, h: float, lo: float, c: float, hours: int = 1) -> Candle:
    start = T0 + timedelta(hours=i * hours)
    return Candle(start, start + timedelta(hours=hours), Decimal(str(c)), Decimal(str(o)), Decimal(str(h)), Decimal(str(lo)))


class FakeSession:
    def __init__(self, broker, state: Path):
        self.broker, self.state, self.fills = broker, state, []

    def _add(self, fill):
        if fill:
            self.fills.append(fill)


def up_view(stop: float = 0.02) -> ladder.View:
    return ladder.View(regime=1, long_ok=True, short_ok=False, overheat=False, stop_raw=stop, ema1=None)


class IndicatorTest(unittest.TestCase):
    def test_sma_ema_follow_pandas_definitions(self):
        self.assertEqual(ladder.sma([1, 2, 3, 4], 2), [None, 1.5, 2.5, 3.5])
        e = ladder.ema([1.0, 2.0, 3.0], 2)  # alpha 2/3, 첫 값부터 쌓고 2번째부터 보인다
        self.assertIsNone(e[0])
        self.assertAlmostEqual(e[1], 2 / 3 * 2 + 1 / 3 * 1)
        self.assertAlmostEqual(e[2], 2 / 3 * 3 + 1 / 3 * e[1])

    def test_wilson_lower_bound_is_below_win_rate(self):
        self.assertLess(ladder.wilson_low(18, 30, 1.2816), 0.6)
        self.assertGreater(ladder.wilson_low(18, 30, 1.2816), 0.45)

    def test_view_uses_only_closed_big_candles_and_breakout(self):
        p = ladder.Params()
        d1 = [candle(i, 100 + i, 101 + i, 99 + i, 100.5 + i, hours=24) for i in range(70)]  # 꾸준히 오름
        h4 = [candle(i, 160 + i * 0.2, 161 + i * 0.2, 159 + i * 0.2, 160.2 + i * 0.2, hours=4) for i in range(400)]
        base = 70 * 24  # 1일봉이 다 닫힌 뒤
        h1 = [candle(base + i, 170, 170.5, 169.5, 170) for i in range(12)] + [candle(base + 12, 170, 172, 169.9, 171.8)]
        v = ladder.view(h1, len(h1) - 1, h4, d1, p)
        self.assertEqual(v.regime, 1)
        self.assertTrue(v.long_ok)
        self.assertFalse(v.short_ok)
        self.assertIsNotNone(v.stop_raw)
        v_early = ladder.view(h1, 11, h4, d1, p)  # 돌파 전 봉
        self.assertFalse(v_early.long_ok)


class LadderSpotTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = Path(self.dir.name) / "s.json"
        self.b = PaperBroker(cash=Decimal(1000), fee_rate=Decimal("0.001"), currency="USDT", exchange="BINANCE")
        self.s = FakeSession(self.b, self.state)
        self.lad = ladder.Ladder(self.s, Binance(fetch=lambda u: []), ladder.Book())

    def tearDown(self):
        self.dir.cleanup()

    def test_sizes_by_fixed_risk_before_30_trades(self):
        self.lad.enter("BTCUSDT", "비트코인", candle(0, 100, 100, 100, 100), up_view(0.02), "t")
        leg = self.lad.book.legs["BTCUSDT"]
        # 위험 0.5% ÷ 손절 2% = 잔고의 25% → 명목 상한 20%(200 USDT) → 2개
        self.assertEqual(self.b.holding("BTCUSDT"), Decimal(2))
        self.assertAlmostEqual(leg.risk_amt, 2 * 100 * 0.02)
        self.assertIn("ladder-v0.1", self.s.fills[0].reason)

    def test_take_profit_thirds_then_breakeven_stop_records_r(self):
        self.lad.enter("BTCUSDT", "비트코인", candle(0, 100, 100, 100, 100), up_view(0.02), "t")
        self.lad.manage("BTCUSDT", candle(1, 100, 102.5, 100.5, 102), up_view(), "t1")  # +1R(102) 닿음
        leg = self.lad.book.legs["BTCUSDT"]
        self.assertTrue(leg.tp1_done)
        self.assertEqual(leg.stop, 100)
        self.assertEqual(self.b.holding("BTCUSDT"), Decimal("1.33333334"))
        self.lad.manage("BTCUSDT", candle(2, 101, 101, 99.5, 100), up_view(), "t2")  # 본전에서 정리
        self.assertNotIn("BTCUSDT", self.b.positions)
        self.assertEqual(self.lad.book.real_trades, 1)
        r = self.lad.book.history[-1]["r"]
        self.assertGreater(r, 0)  # 1/3을 +1R에 팔았으니 수수료를 빼도 플러스
        self.assertLess(r, 1 / 3)

    def test_stop_gap_fills_at_open(self):
        self.lad.enter("BTCUSDT", "비트코인", candle(0, 100, 100, 100, 100), up_view(0.02), "t")
        self.lad.manage("BTCUSDT", candle(1, 95, 96, 94, 95), up_view(), "t1")
        self.assertEqual(self.s.fills[-1].price, Decimal(95))
        self.assertLess(self.lad.book.history[-1]["r"], -2)

    def test_regime_change_closes_everything(self):
        self.lad.enter("BTCUSDT", "비트코인", candle(0, 100, 100, 100, 100), up_view(0.02), "t")
        flat = ladder.View(0, False, False, False, 0.02, None)
        self.lad.manage("BTCUSDT", candle(1, 100, 101, 99.5, 100.5), flat, "t1")
        self.assertNotIn("BTCUSDT", self.b.positions)
        self.assertIn("1일 방향", self.s.fills[-1].reason)

    def test_negative_kelly_takes_shadow_trades_and_comes_back(self):
        self.lad.book.history = [{"symbol": "X", "side": 1, "r": -1.0, "shadow": False, "closed": "t"}] * 30
        self.lad.enter("BTCUSDT", "비트코인", candle(0, 100, 100, 100, 100), up_view(0.02), "t")
        self.assertEqual(self.s.fills, [])  # 돈은 넣지 않는다
        self.assertTrue(self.lad.book.legs["BTCUSDT"].shadow)
        self.lad.manage("BTCUSDT", candle(1, 100, 106, 100, 106), up_view(), "t1")  # +1R, +2R
        self.lad.manage("BTCUSDT", candle(2, 106, 110, 105, 109), ladder.View(0, False, False, False, 0.02, None), "t2")
        self.assertNotIn("BTCUSDT", self.lad.book.legs)
        last = self.lad.book.history[-1]
        self.assertTrue(last["shadow"])
        self.assertGreater(last["r"], 2)
        self.assertEqual(self.lad.book.real_trades, 0)
        # 그림자 수익이 쌓이면 켈리가 플러스로 돌아온다
        self.lad.book.history = [{"symbol": "X", "side": 1, "r": 2.0, "shadow": True, "closed": "t"}] * 30
        self.assertIsNotNone(self.lad._risk(1, "BTC"))

    def test_daily_loss_brake_blocks_new_entries(self):
        self.lad.book.day_start = 1100.0  # 오늘 아침보다 9% 적다
        self.assertTrue(self.lad._blocked(T0))
        self.assertTrue(self.lad._blocked(T0 + timedelta(hours=3)))
        self.lad.book.day = T0.strftime("%Y-%m-%d")
        self.lad.book.week_start = 1000.0
        self.lad._roll(T0 + timedelta(days=1))  # 다음 날 아침 잔고로 다시 잡는다
        self.assertEqual(self.lad.book.day_start, 1000.0)
        self.assertFalse(self.lad._blocked(T0 + timedelta(days=1)))

    def test_month_rollover_moves_30_percent_of_gain_to_core(self):
        self.lad.book.month, self.lad.book.month_start, self.lad.book.day = "2026-09", 900.0, "2026-09-30"
        self.lad._roll(T0)  # 2026-10-01
        self.assertEqual(self.lad.book.core, 30.0)
        self.assertEqual(self.b.cash, Decimal(970))

    def test_book_survives_save_and_load(self):
        self.lad.enter("BTCUSDT", "비트코인", candle(0, 100, 100, 100, 100), up_view(0.02), "t")
        self.lad.book.save(self.state)
        again = ladder.Book.load(self.state)
        self.assertEqual(again.legs["BTCUSDT"].stop, self.lad.book.legs["BTCUSDT"].stop)


class LadderFuturesTest(unittest.TestCase):
    def test_short_uses_staged_leverage_and_partial_close(self):
        b = FuturesBroker(cash=Decimal(1000), fee_rate=Decimal("0.0005"), leverage=Decimal(1))
        s = FakeSession(b, Path("unused.json"))
        lad = ladder.Ladder(s, BinanceFutures(fetch=lambda u: []), ladder.Book())
        down = ladder.View(-1, False, True, False, 0.02, None)
        lad.enter("BTCUSDT", "비트코인 선물", candle(0, 100, 100, 100, 100), down, "t")
        pos = b.positions["BTCUSDT"]
        # 숏은 위험 절반(0.25%) ÷ 2% = 125 USDT 명목 → 증거금 상한 100 USDT라 2배
        self.assertEqual(pos.side, "SHORT")
        self.assertEqual(pos.quantity, Decimal("1.25"))
        self.assertIn("2x", s.fills[0].reason)
        self.assertEqual(b.leverage, Decimal(1))  # 계좌 기본 레버리지는 그대로
        lad.manage("BTCUSDT", candle(1, 100, 100, 97.9, 98), down, "t1")  # -2% = +1R
        self.assertEqual(b.positions["BTCUSDT"].quantity, Decimal("0.83333334"))
        self.assertIn("일부 정리", s.fills[-1].reason)


def kline_rows(series, hours):
    return [[int((T0 + timedelta(hours=i * hours)).timestamp() * 1000), str(o), str(h), str(lo), str(c), "0", 0]
            for i, (o, h, lo, c) in enumerate(series)]


class LadderCliTest(unittest.TestCase):
    def fake(self):
        d1 = [(100 + i, 101 + i, 99 + i, 100.5 + i) for i in range(70)]
        h4 = [(160 + i * 0.2, 161 + i * 0.2, 159 + i * 0.2, 160.2 + i * 0.2) for i in range(420)]
        h1 = [(170, 170.5, 169.5, 170)] * 12 + [(170, 172, 169.9, 171.8)]
        base = 70 * 24

        def fetch(url):
            if "interval=1d" in url:
                return kline_rows(d1, 24)
            if "interval=4h" in url:
                return kline_rows(h4, 4)
            rows = kline_rows([(0, 0, 0, 0)] * base + h1, 1)
            return rows[base:]
        ex = Binance(fetch=fetch)
        real = ex.fetch_candles
        now = T0 + timedelta(hours=base + len(h1))
        ex.fetch_candles = lambda symbol, interval, limit: real(symbol, interval, limit, now=now)
        return ex

    def test_enters_on_breakout_and_keeps_book_next_to_state(self):
        ex = self.fake()
        out, err = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as d, mock.patch.dict("os.environ", {}, clear=True), redirect_stdout(out), redirect_stderr(err), \
                mock.patch.dict(cli.EXCHANGES, {("binance", "spot"): lambda: ex}):
            state = Path(d) / "lad.json"
            code = cli.main(["coin", "--rule", "ladder", "--symbol", "BTCUSDT", "--state", str(state), "--dry-run"])
            book = json.loads((Path(d) / "lad.ladder.json").read_text(encoding="utf-8"))
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("BUY", out.getvalue())
        self.assertIn("ladder-v0.1", out.getvalue())
        self.assertIn("BTCUSDT", book["legs"])

    def test_refuses_live_and_other_intervals(self):
        for extra in (["--interval", "4h"], ["--live"]):
            err = io.StringIO()
            with redirect_stderr(err), redirect_stdout(io.StringIO()), mock.patch.dict("os.environ", {}, clear=True):
                code = cli.main(["coin", "--rule", "ladder", "--dry-run", *extra])
            self.assertEqual(code, 2)
            self.assertIn("1시간봉 모의 계좌에서만", err.getvalue())


if __name__ == "__main__":
    unittest.main()
