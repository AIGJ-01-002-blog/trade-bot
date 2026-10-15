"""devlog 자동매매 화면(/trade) 알림: 모의 체결·AI 판단·규칙·하루 요약을 report_trade_bot으로 보내고, 실계좌는 빼고, 실패해도 매매는 그대로"""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

from devlog_trade import __main__ as cli
from devlog_trade import __version__, ai, report
from devlog_trade.broker import Fill
from devlog_trade.exchanges import Binance
from devlog_trade.strategy import MovingAverageCross

from .test_ai import FakeClient, bars
from .test_bot import CLOSES, START, FakeDevlog, binance_rows

NOW = datetime(2026, 10, 15, 13, 0, tzinfo=report.KST)


def fill(side="BUY", pnl=None, at="2026-10-15T12:00:00+09:00", id_="paper-1"):
    return Fill(id_, "BTCUSDT", "비트코인", side, Decimal("0.001"), Decimal("66000"), Decimal("0.066"),
                None if pnl is None else Decimal(pnl), "종가가 평균 위로", at, "BINANCE", "USDT")


class OutboxTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def outbox(self):
        return json.loads((self.dir / report.OUTBOX).read_text(encoding="utf-8"))

    def test_fill_events_dedupe_and_futures_sides(self):
        report.add_event(self.dir, report.fill_event("trade-state-binance-spot-1h.json", "spot", fill()))
        report.add_event(self.dir, report.fill_event("trade-state-binance-spot-1h.json", "spot", fill()))  # 같은 체결
        report.add_event(self.dir, report.fill_event("trade-state-binance-futures-1h.json", "futures", fill("SELL", id_="paper-2")))
        report.add_event(self.dir, report.fill_event("trade-state-binance-futures-1h.json", "futures", fill("BUY", "-1.5", id_="paper-3")))
        events = self.outbox()["events"]
        self.assertEqual([e["side"] for e in events], ["BUY", "SHORT", "CLOSE"])
        self.assertEqual(events[0]["id"], "fill:paper-1")
        self.assertEqual((events[2]["realized_pnl"], events[0]["text"]), ("-1.5", "종가가 평균 위로"))
        self.assertEqual(self.outbox()["days"]["2026-10-15"], {"fills": 3, "allow": 0, "block": 0, "pnl": {"USDT": "-1.5"}})

    def test_live_accounts_never_reach_the_outbox(self):
        report.add_event(self.dir, report.fill_event("trade-live-binance-spot-1h.json", "spot", fill()))
        report.set_rules(self.dir, "trade-live-binance-spot-1h.json", {"전략": "x"})
        self.assertFalse((self.dir / report.OUTBOX).exists())

    def accounts(self):
        return {"accounts": [
            {"account": "trade-state-binance-spot-1h.json", "mode": "PAPER", "exchange": "BINANCE", "market": "spot", "currency": "USDT",
             "cash": "900", "positions": [{"symbol": "BTCUSDT", "quantity": "0.001", "avg_price": "66000"}], "unsent_fills": 2},
            {"account": "trade-live-binance-spot-1h.json", "mode": "LIVE", "exchange": "BINANCE", "market": "spot", "currency": "USDT",
             "cash": "5", "positions": []},
            {"account": "trade-state-broken.json", "error": "읽지 못했어요"}],
            "ai": {"switch": "on", "calls_today": 7}}

    def test_payload_drops_live_and_broken_accounts_and_summarizes_only_finished_days(self):
        report.set_rules(self.dir, "trade-state-binance-spot-1h.json", {"전략": "5봉 이동평균 교차", "봉": "1h"}, "gemini:gemini-2.5-flash-lite")
        report.add_event(self.dir, report.fill_event("trade-state-binance-spot-1h.json", "spot", fill(at="2026-10-14T22:00:00+09:00")))
        report.add_event(self.dir, report.ai_event("trade-state-binance-spot-1h.json", "BTCUSDT", "2026-10-15T03:00:00Z", "BLOCK",
                                                   "BUY", "AI: 급등 직후", "2026-10-15T13:00:00+09:00"))
        sent = []
        with redirect_stdout(io.StringIO()):
            ok = report.report(self.dir, self.accounts, "https://devlog.example/", "token", now=NOW,
                               send=lambda url, tok, name, args: sent.append((url, tok, name, args)) or "ok")
        self.assertTrue(ok)
        url, tok, name, args = sent[0]
        self.assertEqual((url, tok, name), ("https://devlog.example/api/mcp", "token", "report_trade_bot"))
        self.assertEqual([a["account"] for a in args["accounts"]], ["trade-state-binance-spot-1h.json"])
        self.assertNotIn("unsent_fills", args["accounts"][0])
        self.assertEqual(args["accounts"][0]["rules"], {"전략": "5봉 이동평균 교차", "봉": "1h"})
        self.assertEqual(args["ai"], {"enabled": True, "model": "gemini:gemini-2.5-flash-lite", "calls_today": 7})
        self.assertEqual(args["bot_version"], __version__)
        kinds = [(e["kind"], e["id"]) for e in args["events"]]
        self.assertEqual(kinds[-1], ("SUMMARY", "sum:2026-10-14"))  # 어제만. 오늘은 끝나야 요약한다
        self.assertIn("모의 체결 1건", args["events"][-1]["text"])
        self.assertEqual(len(kinds), 3)
        left = self.outbox()
        self.assertEqual(left["events"], [])
        self.assertEqual(list(left["days"]), ["2026-10-15"])

    def test_failed_report_keeps_events_and_does_not_raise(self):
        report.add_event(self.dir, report.fill_event("trade-state-binance-spot-1h.json", "spot", fill()))

        def down(url, tok, name, args):
            raise report.JournalError("없는 도구예요: report_trade_bot")

        out = io.StringIO()
        with redirect_stdout(out):
            self.assertFalse(report.report(self.dir, self.accounts, "https://devlog.example", "token", now=NOW, send=down))
            self.assertFalse(report.report(self.dir, self.accounts, "https://devlog.example", "", now=NOW, send=down))
        self.assertIn("다음 바퀴에 다시 보내요", out.getvalue())
        self.assertIn("DEVLOG_TOKEN 없음", out.getvalue())
        self.assertEqual(len(self.outbox()["events"]), 1)

    def test_one_call_sends_at_most_200_events_and_keeps_the_rest(self):
        for i in range(250):
            report.add_event(self.dir, report.fill_event("trade-state-a.json", "spot", fill(id_=f"paper-{i}")))
        sent = []
        with redirect_stdout(io.StringIO()):
            report.report(self.dir, lambda: {"accounts": []}, "https://d", "t", now=NOW, send=lambda *a: sent.append(a[3]))
        self.assertEqual(len(sent[0]["events"]), 200)
        self.assertEqual(len(self.outbox()["events"]), 50)


class AiVerdictTest(unittest.TestCase):
    def test_ai_follow_is_allow_and_skipped_rule_signal_is_block(self):
        with tempfile.TemporaryDirectory() as d:
            seen = []
            budget = ai.Budget(Path(d) / "u.json", 100, Decimal(30000), now=lambda: NOW)
            account = ai.Account(lambda: Decimal(1000), "USDT", Decimal("0.001"))
            client = FakeClient(("HOLD", "쉬어요"), ("HOLD", "쉬어요"), ("HOLD", "쉬어요"), ("HOLD", "쉬어요"), ("HOLD", "급등 직후"),
                                ("BUY", "반등"))
            s = ai.AiStrategy(MovingAverageCross(window=3), client, budget, "1h", {}, account,
                              on_verdict=lambda bar, v, side, reason: seen.append((bar.date[11:13], v, side, reason)))
            b = bars([100, 99, 98, 97, 101, 102])
            for x in b[:5]:
                s.on_bar(x, False, Decimal(0))
            self.assertEqual(seen, [("04", "BLOCK", "BUY", "AI: 급등 직후")])  # 관망끼리는 알리지 않고, 규칙 매수를 쉰 것만
            s.on_bar(b[5], False, Decimal(0))
            self.assertEqual(seen[-1], ("05", "ALLOW", "BUY", "AI: 반등"))


class JobsReportTest(unittest.TestCase):
    def run_jobs(self, d, fake, *extra):
        ex = Binance(fetch=lambda url: binance_rows(CLOSES, START))
        real = ex.fetch_candles
        ex.fetch_candles = lambda symbol, interval, limit: real(symbol, interval, limit, now=START + timedelta(hours=len(CLOSES) - 0.5))
        (Path(d) / "jobs.conf").write_text("--symbol BTCUSDT --window 3 --dry-run\n", encoding="utf-8")
        cwd = os.getcwd()
        out = io.StringIO()
        try:
            os.chdir(d)
            with redirect_stdout(out), redirect_stderr(io.StringIO()), mock.patch.dict(cli.EXCHANGES, {("binance", "spot"): lambda: ex}), \
                    mock.patch.dict("os.environ", {"DEVLOG_TOKEN": "secret", "DEVLOG_SERVER": fake.url, "AI_DECISION": "off",
                                                   "TRADE_CONTROL": str(Path(d) / "trade-control.json")}):
                code = cli.main(["jobs", "jobs.conf", *extra])
        finally:
            os.chdir(cwd)
        return code, out.getvalue()

    def test_dry_run_jobs_report_once_per_round_and_a_down_devlog_does_not_change_the_exit_code(self):
        with tempfile.TemporaryDirectory() as d:
            down = FakeDevlog(fail=True)
            try:
                code, out = self.run_jobs(d, down)
            finally:
                down.close()
            self.assertEqual(code, 0, out)
            self.assertIn("자동매매 화면에 알리지 못했어요", out)
            self.assertEqual([c["params"]["name"] for c in down.calls], ["report_trade_bot"])  # --dry-run이라 매매 일지는 안 부른다
            fake = FakeDevlog()
            try:
                code, out = self.run_jobs(d, fake)
            finally:
                fake.close()
            self.assertEqual(code, 0, out)
            args = fake.calls[0]["params"]["arguments"]
            self.assertEqual(fake.auth[0], "Bearer secret")
            acc = args["accounts"][0]
            self.assertEqual(acc["account"], "trade-state-binance-spot-1h.json")
            self.assertEqual(acc["rules"]["모드"], "모의(--dry-run)")
            self.assertEqual(acc["rules"]["AI 판단"], "꺼짐")
            fills = [e for e in args["events"] if e["kind"] == "FILL"]
            self.assertEqual([e["side"] for e in fills], ["BUY", "SELL"])  # 실패한 첫 바퀴의 체결을 이번에 보낸다
            self.assertTrue(all(e["at"].endswith("+09:00") for e in fills))
            self.assertEqual(args["ai"]["enabled"], False)

    def test_no_report_flag_skips_devlog(self):
        with tempfile.TemporaryDirectory() as d:
            fake = FakeDevlog()
            try:
                code, _ = self.run_jobs(d, fake, "--no-report")
            finally:
                fake.close()
            self.assertEqual((code, fake.calls), (0, []))


if __name__ == "__main__":
    unittest.main()
