"""모의 계좌·예제 전략·devlog 기록 보내기를 확인한다. 가짜 devlog 서버로 보내기와 다시 보내기를 본다."""

import io
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from devlog_trade import __main__ as cli
from datetime import datetime, timedelta, timezone

from devlog_trade.broker import PaperBroker
from devlog_trade.exchanges import COINS, Binance, BinanceFutures, ExchangeError, Upbit
from devlog_trade.futures import FuturesBroker
from devlog_trade.journal import Journal, JournalError, payload
from devlog_trade.strategy import Bar, MovingAverageCross

SAMPLE = Path(__file__).resolve().parent.parent / "sample-prices.csv"


class BrokerTest(unittest.TestCase):
    def test_average_price_fee_tax_and_realized_pnl(self):
        b = PaperBroker(cash=Decimal(1_000_000), fee_rate=Decimal("0.001"), sell_tax_rate=Decimal("0.002"))
        b.buy("A", "가", Decimal(10), Decimal(10_000), "", "t")  # 수수료 100
        b.buy("A", "가", Decimal(10), Decimal(12_000), "", "t")  # 수수료 120, 평균 11,000
        self.assertEqual(b.cash, Decimal(1_000_000 - 100_000 - 100 - 120_000 - 120))
        sell = b.sell("A", "가", Decimal(20), Decimal(12_000), "", "t")
        # 240,000 - 220,000 - 수수료 240 - 세금 480
        self.assertEqual(sell.fee, Decimal(720))
        self.assertEqual(sell.realized_pnl, Decimal(19_280))
        self.assertEqual(b.holding("A"), 0)
        self.assertNotIn("A", b.positions)

    def test_refuses_what_it_cannot_do(self):
        b = PaperBroker(cash=Decimal(1000))
        self.assertIsNone(b.buy("A", "가", Decimal(1), Decimal(2000), "", "t"))
        self.assertIsNone(b.sell("A", "가", Decimal(1), Decimal(100), "", "t"))
        self.assertIsNone(b.buy("A", "가", Decimal(0), Decimal(10), "", "t"))

    def test_state_survives_save_and_load(self):
        b = PaperBroker(cash=Decimal(50_000), fee_rate=Decimal("0.00015"))
        b.buy("A", "가", Decimal(3), Decimal(10_000), "", "t")
        b.last_date = "2026-10-13"
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s.json"
            b.save(path)
            again = PaperBroker.load(path)
        self.assertEqual(again.cash, b.cash)
        self.assertEqual(again.positions["A"].quantity, Decimal(3))
        self.assertEqual(again.last_date, "2026-10-13")


class CoinBrokerTest(unittest.TestCase):
    def test_fractional_quantity_and_usdt_amounts(self):
        b = PaperBroker(cash=Decimal(1000), fee_rate=Decimal("0.001"), currency="USDT", exchange="BINANCE")
        buy = b.buy("BTCUSDT", "비트코인", Decimal("0.00123456"), Decimal("65000.12"), "", "t")
        self.assertEqual(buy.fee, Decimal("0.08024655"))  # 0.0802465481… → 소수 8자리
        self.assertEqual((buy.exchange, buy.currency), ("BINANCE", "USDT"))
        sell = b.sell("BTCUSDT", "비트코인", Decimal("0.00123456"), Decimal("66000"), "", "t")
        self.assertEqual(sell.realized_pnl, Decimal("1.15293089"))
        self.assertEqual(b.holding("BTCUSDT"), 0)

    def test_krw_amounts_stay_in_won(self):
        b = PaperBroker(cash=Decimal(1_000_000), fee_rate=Decimal("0.0005"), currency="KRW", exchange="UPBIT")
        buy = b.buy("KRW-BTC", "비트코인", Decimal("0.001"), Decimal(95_000_000), "", "t")
        self.assertEqual(buy.fee, Decimal(48))  # 47.5원 → 48원


def binance_rows(closes, start):
    """바이낸스 klines 모양: [여는 시각(ms), 시가, 고가, 저가, 종가, …]"""
    return [[int((start + timedelta(hours=i)).timestamp() * 1000), "0", "0", "0", str(c), "0", 0] for i, c in enumerate(closes)]


def upbit_rows(closes, start):
    """업비트 분봉 모양. 최신 봉이 먼저 온다"""
    rows = [{"market": "KRW-BTC", "candle_date_time_utc": (start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%S"),
             "trade_price": c} for i, c in enumerate(closes)]
    return list(reversed(rows))


START = datetime(2026, 10, 13, 0, 0, tzinfo=timezone.utc)
# 내려가다 올라서고(매수) 다시 꺾이는(매도) 시세. 마지막 값은 아직 닫히지 않은 봉
CLOSES = [100, 99, 98, 97, 96, 95, 101, 104, 106, 99, 97, 300]


class ExchangeTest(unittest.TestCase):
    def test_binance_uses_public_klines_and_drops_the_open_candle(self):
        urls = []
        ex = Binance(fetch=lambda url: urls.append(url) or binance_rows(CLOSES, START))
        now = START + timedelta(hours=len(CLOSES) - 1, minutes=30)
        candles = ex.fetch_candles("BTCUSDT", "1h", 200, now=now)
        self.assertIn("data-api.binance.vision/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=200", urls[0])
        self.assertEqual(len(candles), len(CLOSES) - 1)
        self.assertEqual(candles[0].close, Decimal(100))
        self.assertEqual(candles[0].close_time, START + timedelta(hours=1))

    def test_upbit_sorts_newest_first_rows(self):
        urls = []
        ex = Upbit(fetch=lambda url: urls.append(url) or upbit_rows(CLOSES, START))
        candles = ex.fetch_candles("KRW-BTC", "1h", 500, now=START + timedelta(hours=len(CLOSES) - 1, minutes=1))
        self.assertIn("api.upbit.com/v1/candles/minutes/60?market=KRW-BTC&count=200", urls[0])
        self.assertEqual([c.close for c in candles][:3], [Decimal(100), Decimal(99), Decimal(98)])
        self.assertEqual(ex.coin_name("KRW-BTC"), "비트코인")

    def test_region_block_is_explained(self):
        import urllib.error
        from devlog_trade import exchanges

        def blocked(*args, **kwargs):
            raise urllib.error.HTTPError("https://fapi.binance.com", 451, "", {}, None)
        with mock.patch("urllib.request.urlopen", blocked), self.assertRaisesRegex(ExchangeError, "이 지역에서는"):
            exchanges.http_get_json("https://fapi.binance.com/fapi/v1/klines")

    def test_wrong_market_or_interval_is_refused(self):
        with self.assertRaises(ExchangeError):
            Binance(fetch=lambda url: []).fetch_candles("KRW-BTC", "1h", 10)
        with self.assertRaises(ExchangeError):
            Upbit(fetch=lambda url: []).fetch_candles("BTCUSDT", "1h", 10)
        with self.assertRaises(ExchangeError):
            Upbit(fetch=lambda url: []).fetch_candles("KRW-BTC", "2h", 10)


class FuturesBrokerTest(unittest.TestCase):
    def test_long_and_short_pnl_with_fees(self):
        b = FuturesBroker(cash=Decimal(1000), fee_rate=Decimal("0.0005"), leverage=Decimal(3))
        long = b.open("BTCUSDT", "비트코인 선물", "LONG", Decimal("0.01"), Decimal(60000), "신호", "t")
        self.assertEqual((long.side, long.fee), ("BUY", Decimal("0.3")))  # 명목 600 × 0.05%
        self.assertEqual(b.positions["BTCUSDT"].margin, Decimal(200))
        self.assertEqual(b.available(), Decimal("799.7"))
        close = b.close("BTCUSDT", Decimal(61000), "신호", "t")
        self.assertEqual((close.side, close.realized_pnl), ("SELL", Decimal("9.695")))  # 10 - 0.305
        short = b.open("ETHUSDT", "이더리움 선물", "SHORT", Decimal(1), Decimal(3000), "신호", "t")
        self.assertEqual(short.side, "SELL")
        self.assertIn("[선물 숏 진입 3x]", short.reason)
        cover = b.close("ETHUSDT", Decimal(2900), "신호", "t")
        self.assertEqual((cover.side, cover.realized_pnl), ("BUY", Decimal("98.55")))  # 100 - 1.45

    def test_margin_limits_and_liquidation(self):
        b = FuturesBroker(cash=Decimal(100), leverage=Decimal(3), fee_rate=Decimal(0))
        self.assertIsNone(b.open("A", "가", "LONG", Decimal(1), Decimal(400), "", "t"))  # 증거금 133 > 100
        self.assertEqual(b.quantity_for(Decimal(50), Decimal(100)), Decimal("1.5"))
        b.open("A", "가", "LONG", Decimal(3), Decimal(100), "", "t")  # 증거금 100
        self.assertFalse(b.liquidation_due("A", Decimal(70)))
        self.assertTrue(b.liquidation_due("A", Decimal("66.6")))
        fill = b.close("A", Decimal(50), "", "t", liquidated=True)
        self.assertEqual(fill.realized_pnl, Decimal(-100))  # 증거금까지만 잃는다
        self.assertIn("강제 청산", fill.reason)

    def test_state_survives_save_and_load(self):
        b = FuturesBroker(cash=Decimal(1000), leverage=Decimal(2))
        b.open("BTCUSDT", "비트코인 선물", "SHORT", Decimal("0.01"), Decimal(60000), "", "t")
        b.last_seen["BTCUSDT"] = "2026-10-14T00:00:00Z"
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "f.json"
            b.save(path)
            again = FuturesBroker.load(path)
        self.assertEqual(again.positions["BTCUSDT"].side, "SHORT")
        self.assertEqual((again.leverage, again.cash, again.last_seen), (Decimal(2), b.cash, b.last_seen))


class StrategyTest(unittest.TestCase):
    def feed(self, s, closes, holding=False, avg=Decimal(0)):
        out = None
        for i, c in enumerate(closes):
            out = s.on_bar(Bar(f"2026-09-{i + 1:02d}", "A", "가", Decimal(c)), holding, avg)
        return out

    def test_buys_on_cross_up_and_sells_on_cross_down(self):
        self.assertEqual(self.feed(MovingAverageCross(window=3), [100, 99, 98, 97, 105]).side, "BUY")
        self.assertEqual(self.feed(MovingAverageCross(window=3), [100, 101, 102, 103, 90], holding=True).side, "SELL")

    def test_stop_loss_wins_over_trend(self):
        signal = self.feed(MovingAverageCross(window=3, stop_loss=Decimal("0.03")), [100, 101, 102, 103, 104],
                           holding=True, avg=Decimal(108))
        self.assertEqual(signal.side, "SELL")
        self.assertIn("손절선 -3%", signal.reason)

    def test_futures_flips_between_long_and_short(self):
        s = MovingAverageCross(window=3)
        sig = None
        for i, c in enumerate([100, 99, 98, 97, 105]):
            sig = s.on_bar_futures(Bar(f"d{i}", "A", "가", Decimal(c)), None, Decimal(0))
        self.assertEqual([x.side for x in sig], ["LONG"])
        for i, c in enumerate([106, 107, 90]):
            sig = s.on_bar_futures(Bar(f"e{i}", "A", "가", Decimal(c)), "LONG", Decimal(80))
        self.assertEqual([x.side for x in sig], ["CLOSE", "SHORT"])

    def test_futures_short_stop_loss(self):
        s = MovingAverageCross(window=3, stop_loss=Decimal("0.03"))
        sig = None
        for i, c in enumerate([100, 101, 102, 103, 104]):
            sig = s.on_bar_futures(Bar(f"d{i}", "A", "가", Decimal(c)), "SHORT", Decimal(100))
        self.assertEqual([x.side for x in sig], ["CLOSE"])
        self.assertIn("손절선", sig[0].reason)

    def test_waits_until_enough_history(self):
        self.assertIsNone(self.feed(MovingAverageCross(window=5), [1, 2, 3, 4, 5]))


class FakeDevlog:
    """record_trade만 받는 가짜 devlog. fail이면 도구 오류를 돌려준다"""

    def __init__(self, fail=False):
        self.calls = []
        self.auth = []
        self.fail = fail
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode("utf-8"))
                outer.calls.append(body)
                outer.auth.append(self.headers.get("Authorization"))
                text = "하루 상한이에요" if outer.fail else "체결을 남겼어요"
                data = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {
                    "content": [{"type": "text", "text": text}], "isError": outer.fail}}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class JournalTest(unittest.TestCase):
    def test_payload_sends_numbers_as_text_and_pnl_only_on_sell(self):
        b = PaperBroker(cash=Decimal(100_000))
        buy = b.buy("005930", "삼성전자", Decimal(1), Decimal("70000.5"), "신호", "2026-10-14T15:20:00+09:00")
        args = payload(buy, strategy="ma")["params"]["arguments"]
        self.assertEqual(args["price"], "70000.5")
        self.assertNotIn("realized_pnl", args)
        self.assertEqual(args["mode"], "PAPER")
        sell = b.sell("005930", "삼성전자", Decimal(1), Decimal(71_000), "", "2026-10-15T15:20:00+09:00")
        self.assertIn("realized_pnl", payload(sell)["params"]["arguments"])

    def test_coin_payload_carries_exchange_currency_and_plain_decimals(self):
        b = PaperBroker(cash=Decimal(1000), fee_rate=Decimal("0.001"), currency="USDT", exchange="BINANCE")
        buy = b.buy("BTCUSDT", "비트코인", Decimal("0.00001"), Decimal("65000"), "신호", "2026-10-14T10:00:00+09:00")
        args = payload(buy)["params"]["arguments"]
        self.assertEqual((args["exchange"], args["currency"]), ("BINANCE", "USDT"))
        self.assertEqual(args["quantity"], "0.00001")  # 1E-5가 아니다
        self.assertEqual(args["fee"], "0.00065000")

    def test_stock_payload_has_no_exchange(self):
        buy = PaperBroker(cash=Decimal(100_000)).buy("005930", "삼성전자", Decimal(1), Decimal(70_000), "", "t")
        args = payload(buy)["params"]["arguments"]
        self.assertNotIn("exchange", args)
        self.assertEqual(args["currency"], "KRW")

    def test_needs_token(self):
        with self.assertRaises(JournalError):
            Journal("https://devlog.life", "")

    def test_non_json_response_is_a_journal_error(self):
        class Html(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                body = b"<html>proxy error</html>"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Html)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            fill = PaperBroker(cash=Decimal(100)).buy("A", "가", Decimal(1), Decimal(10), "", "t")
            with self.assertRaisesRegex(JournalError, "응답을 읽지 못했어요"):
                Journal(f"http://127.0.0.1:{server.server_address[1]}", "secret").record(fill)
        finally:
            server.shutdown()
            server.server_close()

    def test_tool_error_is_raised(self):
        fake = FakeDevlog(fail=True)
        try:
            fill = PaperBroker(cash=Decimal(100)).buy("A", "가", Decimal(1), Decimal(10), "", "t")
            with self.assertRaisesRegex(JournalError, "하루 상한"):
                Journal(fake.url, "secret").record(fill)
            self.assertEqual(fake.auth, ["Bearer secret"])
        finally:
            fake.close()


class CliTest(unittest.TestCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_live_orders_are_refused(self):
        code, _, err = self.run_cli("run", str(SAMPLE), "--live")
        self.assertEqual(code, 2)
        self.assertIn("실계좌 주문은 이 예제에 없어요", err)

    def test_sends_new_days_once_and_retries_failed_sends(self):
        with tempfile.TemporaryDirectory() as d:
            state = str(Path(d) / "state.json")
            fake = FakeDevlog(fail=True)
            try:
                with mock.patch.dict("os.environ", {"DEVLOG_TOKEN": "secret"}):
                    code, _, err = self.run_cli("run", str(SAMPLE), "--state", state, "--server", fake.url)
                    self.assertEqual(code, 1)
                    self.assertIn("다음 실행 때 다시 보내요", err)
                    self.assertTrue((Path(d) / "state.pending.json").exists())

                    fake.fail = False
                    code, _, _ = self.run_cli("run", str(SAMPLE), "--state", state, "--server", fake.url)
                    self.assertEqual(code, 0)
                    self.assertFalse((Path(d) / "state.pending.json").exists())
                    sent = [c["params"]["arguments"] for c in fake.calls[1:]]
                    ids = [a["external_id"] for a in sent]
                    self.assertEqual([a["side"] for a in sent][-2:], ["BUY", "SELL"])

                    # 같은 가격 파일로 다시 돌리면 이미 본 날짜라 새 체결이 없다
                    before = len(fake.calls)
                    code, out, _ = self.run_cli("run", str(SAMPLE), "--state", state, "--server", fake.url)
                    self.assertEqual(code, 0)
                    self.assertEqual(len(fake.calls), before)
                    self.assertIn("보유 없음", out)
            finally:
                fake.close()
        # 실패한 첫 시도와 다시 보낸 기록은 같은 체결 번호라 devlog에 한 번만 남는다
        self.assertEqual(ids[0], fake.calls[0]["params"]["arguments"]["external_id"])

    def test_dry_run_needs_no_token(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict("os.environ", {}, clear=True):
            code, out, _ = self.run_cli("run", str(SAMPLE), "--state", str(Path(d) / "s.json"), "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("[dry-run]", out)


class CoinCliTest(unittest.TestCase):
    def run_coin(self, ex, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), mock.patch.dict(cli.EXCHANGES, {("binance", "spot"): lambda: ex, ("upbit", "spot"): lambda: ex,
                                                                                         ("binance", "futures"): lambda: ex}):
            code = cli.main(["coin", "--symbol", ex.symbol_for("BTC"), *argv])
        return code, out.getvalue(), err.getvalue()

    def binance(self, closes, hours_open):
        now = START + timedelta(hours=hours_open)
        ex = Binance(fetch=lambda url: binance_rows(closes, START))
        real = ex.fetch_candles
        ex.fetch_candles = lambda symbol, interval, limit, now=now: real(symbol, interval, limit, now=now)
        return ex

    def test_live_orders_are_refused(self):
        code, _, err = self.run_coin(Binance(fetch=lambda url: []), "--live")
        self.assertEqual(code, 2)
        self.assertIn("실계좌 주문은 이 예제에 없어요", err)

    def test_trades_closed_candles_once_and_sends_coin_fields(self):
        with tempfile.TemporaryDirectory() as d:
            state = str(Path(d) / "coin.json")
            fake = FakeDevlog()
            try:
                with mock.patch.dict("os.environ", {"DEVLOG_TOKEN": "secret"}):
                    ex = self.binance(CLOSES, len(CLOSES) - 0.5)
                    code, out, _ = self.run_coin(ex, "--state", state, "--server", fake.url, "--window", "3")
                    self.assertEqual(code, 0, out)
                    sent = [c["params"]["arguments"] for c in fake.calls]
                    self.assertEqual([a["side"] for a in sent], ["BUY", "SELL"])
                    buy, sell = sent
                    self.assertEqual((buy["exchange"], buy["currency"], buy["symbol"], buy["symbol_name"]),
                                     ("BINANCE", "USDT", "BTCUSDT", "비트코인"))
                    # 100 USDT로 수수료까지 살 수 있는 만큼, 소수 8자리에서 버림
                    self.assertEqual(buy["quantity"], "0.98910990")
                    self.assertEqual(buy["executed_at"], "2026-10-13T16:00:00+09:00")  # 6시 봉이 7시(UTC)에 닫힘
                    self.assertIn("3개 1h봉 이동평균선 위로", buy["reason"])
                    self.assertIn("realized_pnl", sell)
                    self.assertNotIn("300", json.dumps(sent))  # 아직 닫히지 않은 봉으로는 거래하지 않는다

                    # 같은 봉으로 다시 돌리면 새 체결이 없다
                    code, out, _ = self.run_coin(ex, "--state", state, "--server", fake.url, "--window", "3")
                    self.assertEqual(code, 0)
                    self.assertEqual(len(fake.calls), 2)
                    self.assertIn("보유 없음", out)
                    saved = json.loads(Path(state).read_text(encoding="utf-8"))
                    self.assertEqual(saved["last_seen"], {"BTCUSDT": "2026-10-13T10:00:00Z"})
                    self.assertEqual((saved["exchange"], saved["currency"]), ("BINANCE", "USDT"))
            finally:
                fake.close()

    def test_state_of_another_account_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "s.json"
            PaperBroker(cash=Decimal(1_000_000), currency="KRW", exchange="UPBIT").save(state)
            code, _, err = self.run_coin(self.binance(CLOSES, 11.5), "--state", str(state), "--dry-run")
        self.assertEqual(code, 2)
        self.assertIn("UPBIT spot(KRW) 계좌", err)

    def test_upbit_dry_run_prints_won(self):
        now = START + timedelta(hours=len(CLOSES) - 0.5)
        closes = [c * 1_000_000 for c in CLOSES]
        ex = Upbit(fetch=lambda url: upbit_rows(closes, START))
        real = ex.fetch_candles
        ex.fetch_candles = lambda symbol, interval, limit: real(symbol, interval, limit, now=now)
        with tempfile.TemporaryDirectory() as d, mock.patch.dict("os.environ", {}, clear=True):
            code, out, _ = self.run_coin(ex, "--exchange", "upbit", "--window", "3", "--state", str(Path(d) / "u.json"), "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("[dry-run]", out)
        self.assertIn("원", out)
        self.assertIn("UPBIT spot 1h봉 · KRW-BTC 11개", out)

    def test_futures_long_short_round_trip(self):
        closes = [100, 99, 98, 97, 96, 95, 101, 104, 106, 99, 97, 300]
        now = START + timedelta(hours=len(closes) - 0.5)
        ex = BinanceFutures(fetch=lambda url: binance_rows(closes, START))
        real = ex.fetch_candles
        ex.fetch_candles = lambda symbol, interval, limit: real(symbol, interval, limit, now=now)
        with tempfile.TemporaryDirectory() as d, mock.patch.dict("os.environ", {}, clear=True):
            state = Path(d) / "f.json"
            code, out, _ = self.run_coin(ex, "--market", "futures", "--leverage", "3", "--window", "3", "--state", str(state),
                                         "--dry-run")
            saved = json.loads(state.read_text(encoding="utf-8"))
        self.assertEqual(code, 0, out)
        self.assertIn("[선물 롱 진입 3x]", out)
        self.assertIn("[선물 롱 정리]", out)
        self.assertIn("[선물 숏 진입 3x]", out)
        self.assertEqual(saved["market"], "futures")
        self.assertEqual(saved["positions"]["BTCUSDT"]["side"], "SHORT")  # 마지막에 꺾여 숏을 들고 있다

    def test_futures_rules(self):
        code, _, err = self.run_coin(BinanceFutures(fetch=lambda url: []), "--market", "futures", "--leverage", "5", "--dry-run")
        self.assertEqual(code, 2)
        self.assertIn("1~3배", err)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["coin", "--exchange", "upbit", "--market", "futures", "--dry-run"])
        self.assertEqual(code, 2)
        self.assertIn("선물은 바이낸스만", err.getvalue())

    def test_default_trades_five_coins_in_one_account(self):
        urls = []
        now = START + timedelta(hours=len(CLOSES) - 0.5)
        ex = Upbit(fetch=lambda url: urls.append(url) or upbit_rows([c * 1000 for c in CLOSES], START))
        real = ex.fetch_candles
        ex.fetch_candles = lambda symbol, interval, limit: real(symbol, interval, limit, now=now)
        out, err = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as d, mock.patch.dict("os.environ", {}, clear=True), redirect_stdout(out), \
                mock.patch.dict(cli.EXCHANGES, {("upbit", "spot"): lambda: ex}):
            code = cli.main(["coin", "--exchange", "upbit", "--window", "3", "--state", str(Path(d) / "u.json"), "--dry-run"])
            saved = json.loads((Path(d) / "u.json").read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        self.assertEqual(COINS, ["BTC", "ETH", "XRP", "SOL", "DOGE"])
        self.assertEqual([u.split("market=")[1].split("&")[0] for u in urls], ["KRW-BTC", "KRW-ETH", "KRW-XRP", "KRW-SOL", "KRW-DOGE"])
        self.assertEqual(sorted(saved["last_seen"]), ["KRW-BTC", "KRW-DOGE", "KRW-ETH", "KRW-SOL", "KRW-XRP"])
        self.assertIn("도지코인", out.getvalue())

    def test_network_failure_keeps_state_and_exits_1(self):
        def down(url):
            raise OSError("연결 안 됨")
        with tempfile.TemporaryDirectory() as d:
            code, _, err = self.run_coin(Binance(fetch=down), "--state", str(Path(d) / "s.json"), "--dry-run")
            self.assertTrue((Path(d) / "s.json").exists())
        self.assertEqual(code, 1)
        self.assertIn("시세를 받지 못했어요", err)


if __name__ == "__main__":
    unittest.main()
