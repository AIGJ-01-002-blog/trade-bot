"""대화에서 정한 매매 기준(trade_bot_rules): 상한 검사, 실계좌 거절, 봇이 다음 바퀴에 덮어 쓰기·멈추기"""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

from devlog_trade import __main__ as cli
from devlog_trade import mcp, report, rules
from devlog_trade.broker import PaperBroker
from devlog_trade.exchanges import Binance

from .test_bot import CLOSES, START, binance_rows
from .test_mcp import fake_exchange


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = mock.patch.dict("os.environ", {"TRADE_RULES": str(self.root / "trade-rules.json"),
                                             "TRADE_CONTROL": str(self.root / "trade-control.json"), "AI_DECISION": "off"})
        env.start()
        self.addCleanup(env.stop)
        self.tools = mcp.TradeBotTools(self.root, exchange_for=fake_exchange)
        PaperBroker(cash=Decimal(1000), fee_rate=Decimal("0.001"), currency="USDT", exchange="BINANCE").save(
            self.root / "trade-state-binance-spot-1h.json")

    def test_clean_keeps_ai_inside_the_limits(self):
        ok = rules.clean("trade-state-binance-spot-1h.json", {"coins": "btc, eth,btc", "window": 7, "stop_loss": "0.030",
                                                              "budget": "50", "paused": False}, False, Decimal(500))
        self.assertEqual(ok, {"coins": "BTC,ETH", "window": 7, "stop_loss": "0.03", "budget": "50", "paused": False})
        bad = [("trade-live-binance-spot-1m.json", {"window": 7}, False), ("trade-state-x.json", {"window": 1}, False),
               ("trade-state-x.json", {"stop_loss": "0.5"}, False), ("trade-state-x.json", {"budget": "501"}, False),
               ("trade-state-x.json", {"budget": "0"}, False), ("trade-state-x.json", {"coins": "BTC;rm"}, False),
               ("trade-state-ladder-x.json", {"window": 7}, True)]
        for account, values, ladder in bad:
            with self.subTest(account=account, values=values), self.assertRaises(rules.RuleError):
                rules.clean(account, values, ladder, Decimal(500))

    def test_one_trade_loss_is_capped_at_one_percent_of_starting_cash(self):
        rules.check_risk(Decimal(100), Decimal("0.03"), Decimal(3), Decimal(1000))  # 9 USDT
        with self.assertRaises(rules.RuleError):
            rules.check_risk(Decimal(100), Decimal("0.03"), Decimal(5), Decimal(1000))  # 15 USDT

    def test_mcp_set_get_clear(self):
        account = "trade-state-binance-spot-1h.json"
        with self.assertRaisesRegex(mcp.ToolError, "reason"):
            self.tools.rules("set", account, window=7)
        with self.assertRaisesRegex(mcp.ToolError, "실계좌"):
            self.tools.rules("set", "trade-live-binance-spot-1m.json", "x", window=7)
        with self.assertRaisesRegex(mcp.ToolError, "줄이기만"):
            self.tools.rules("set", account, "크게", budget="150")  # 봇 기본 100 USDT보다 크게는 못 한다
        with self.assertRaisesRegex(mcp.ToolError, "1%"):
            self.tools.rules("set", account, "넓게", stop_loss="0.2")  # 100 × 20% = 20 > 1000의 1%
        got = self.tools.rules("set", account, "추세가 느려서 길게", window=7, coins="BTC,ETH")
        self.assertEqual((got["chosen"]["window"], got["chosen"]["coins"]), (7, "BTC,ETH"))
        self.tools.rules("set", account, "변동성이 커서", stop_loss="0.05")  # 앞 값은 남고 합쳐진다
        chosen = self.tools.rules()["chosen"][account]
        self.assertEqual((chosen["window"], chosen["stop_loss"], chosen["reason"]), (7, "0.05", "변동성이 커서"))
        self.tools.rules("clear", account)
        self.assertEqual(self.tools.rules()["chosen"], {})
        self.assertIn("trade_bot_rules", [t["name"] for t in self.tools.specs()])

    def run_coin(self, *extra):
        ex = Binance(fetch=lambda url: binance_rows(CLOSES, START))
        real = ex.fetch_candles
        ex.fetch_candles = lambda symbol, interval, limit: real(symbol, interval, limit, now=START + timedelta(hours=len(CLOSES) - 0.5))
        out, cwd = io.StringIO(), os.getcwd()
        try:
            os.chdir(self.root)
            with redirect_stdout(out), redirect_stderr(io.StringIO()), mock.patch.dict(cli.EXCHANGES, {("binance", "spot"): lambda: ex}):
                code = cli.main(["coin", "--symbol", "BTCUSDT", "--dry-run", *extra])
        finally:
            os.chdir(cwd)
        return code, out.getvalue()

    def test_bot_uses_chosen_rules_next_round_and_skips_paused_accounts(self):
        account = "trade-state-binance-spot-1h.json"
        self.tools.rules("set", account, "대화에서 정함", window=4, stop_loss="0.05")
        (self.root / account).unlink()  # 봇이 새 계좌로 시작
        code, out = self.run_coin("--window", "3")
        self.assertEqual(code, 0, out)
        shown = json.loads((self.root / report.OUTBOX).read_text(encoding="utf-8"))["rules"][account]
        self.assertEqual((shown["전략"], shown["손절"], shown["기준을 정한 곳"]), ("4봉 이동평균 교차", "5%", "대화(AI) · 대화에서 정함"))

        self.tools.rules("set", account, "잠깐 쉬기", paused=True)
        before = (self.root / account).read_text(encoding="utf-8")
        code, out = self.run_coin()
        self.assertEqual(code, 0)
        self.assertIn("건너뛰어요", out)
        self.assertEqual((self.root / account).read_text(encoding="utf-8"), before)


if __name__ == "__main__":
    unittest.main()
