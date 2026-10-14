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
from devlog_trade.broker import PaperBroker
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


if __name__ == "__main__":
    unittest.main()
