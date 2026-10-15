"""사고팔 때 디스코드 알림: 실거래·모의 채널 나누기, 목표가·손절가·손익·잔액 문구, 실패해도 매매는 그대로"""

import argparse
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from decimal import Decimal
from pathlib import Path
from unittest import mock

from devlog_trade import __main__ as cli
from devlog_trade import notify
from devlog_trade.broker import Fill, PaperBroker
from devlog_trade.strategy import Bar, MovingAverageCross

from .test_bot import CLOSES

BUY = Fill("x1", "KRW-BTC", "비트코인", "BUY", Decimal("0.00004"), Decimal(150000000), Decimal(3), None,
           "종가가 5개 1h봉 이동평균선 위로 올라섬", "2026-10-15T19:00:00+09:00", "UPBIT", "KRW")
SELL = Fill("x2", "KRW-BTC", "비트코인", "SELL", Decimal("0.00004"), Decimal(156000000), Decimal("3.12"), Decimal("233.88"),
            "익절선 +4% 도달", "2026-10-15T22:00:00+09:00", "UPBIT", "KRW")
ENV = {"DISCORD_WEBHOOK_LIVE": "https://discord.test/live", "DISCORD_WEBHOOK_PAPER": "https://discord.test/paper"}


class NotifyTest(unittest.TestCase):
    def test_buy_shows_target_and_stop(self):
        t = notify.text("LIVE", "trade-live-upbit-spot-1h.json", "spot", BUY, notify.Plan(Decimal("0.02"), Decimal("0.04")),
                        "현금 4,000원 · 보유 KRW-BTC", None)
        self.assertIn("[🔴 실거래] 매수 비트코인", t)
        self.assertIn("가격 150,000,000원", t)
        self.assertIn("금액 6,000원", t)
        # 6,000원어치: +4%면 +240원에서 수수료 3원 + 3.12원을 뺀 약 +234원, -2%면 -120원 - 3원 - 2.94원 = 약 -126원
        self.assertIn("**익절가 156,000,000원 (+4%) · 약 +234원**", t)
        self.assertIn("**손절가 147,000,000원 (-2%) · 약 -126원**", t)
        self.assertIn("잔액 현금 4,000원", t)

    def test_sell_shows_profit_and_day(self):
        t = notify.text("PAPER", "trade-state-upbit-spot-1h.json", "spot", SELL, notify.Plan(), "현금 10,230원", Decimal("233.88"))
        self.assertIn("[📝 모의] 매도", t)
        self.assertIn("이번 손익 +234원 (+3.90%)", t)
        self.assertIn("오늘 손익 +234원", t)
        self.assertNotIn("익절가", t)
        self.assertTrue(t.startswith("🎯 **익절**"))  # 익절·손절로 나간 매도는 맨 위에 굵게

    def test_short_entry_flips_target_and_stop(self):
        f = Fill("x3", "BTCUSDT", "비트코인", "SELL", Decimal("0.01"), Decimal(100000), Decimal("0.5"), None, "숏", "2026-10-15T19:00:00+09:00",
                 "BINANCE", "USDT")
        t = notify.text("PAPER", "a", "futures", f, notify.Plan(Decimal("0.02"), Decimal("0.04")), "지갑 1000 USDT", None)
        self.assertIn("숏 진입", t)
        self.assertIn("익절가 96000 USDT", t)
        self.assertIn("손절가 102000 USDT", t)
        # 숏은 값이 내려가면 번다: +40 - 수수료 0.5 - 0.48, 오르면 -20 - 0.5 - 0.51
        self.assertIn("약 +39.02 USDT", t)
        self.assertIn("약 -21.01 USDT", t)

    def test_routes_live_and_paper_to_their_own_channels(self):
        sent = []
        with mock.patch.dict("os.environ", ENV, clear=True):
            for mode in ("LIVE", "PAPER", "TESTNET"):
                self.assertTrue(notify.send(mode, mode, post=lambda url, body: sent.append((url, json.loads(body)["content"]))))
        self.assertEqual(sent, [("https://discord.test/live", "LIVE"), ("https://discord.test/paper", "PAPER"),
                                ("https://discord.test/paper", "TESTNET")])  # 테스트넷은 진짜 돈이 아니라 모의 채널

    def test_missing_channel_or_failure_never_stops_trading(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertFalse(notify.send("LIVE", "x", post=lambda url, body: self.fail("보내면 안 돼요")))

        def boom(url, body):
            raise OSError(f"connection refused {url}")
        err = io.StringIO()
        with mock.patch.dict("os.environ", ENV, clear=True), redirect_stderr(err):
            self.assertFalse(notify.send("LIVE", "x", post=boom))
        self.assertIn("OSError", err.getvalue())
        self.assertNotIn("discord.test", err.getvalue())  # 웹후크 주소는 로그에 남기지 않는다

    def test_bot_sends_every_fill_with_balance(self):
        sent = []
        with tempfile.TemporaryDirectory() as d, mock.patch.dict("os.environ", ENV, clear=True), \
                mock.patch.object(notify, "_post", lambda url, body: sent.append((url, json.loads(body)["embeds"][0]))):
            s = cli.Session(argparse.Namespace(state=str(Path(d) / "s.json"), dry_run=True),
                            lambda: PaperBroker(cash=Decimal(1000), currency="USDT", exchange="BINANCE"))
            s.plan = notify.Plan(Decimal("0.03"), Decimal(0))
            bars = [Bar(f"2026-10-13T{i:02d}:00:00Z", "BTCUSDT", "비트코인", Decimal(c)) for i, c in enumerate(CLOSES)]
            s.trade_coin(MovingAverageCross(window=3), "BTCUSDT", bars, Decimal(100), lambda bar: bar.date)
        self.assertEqual(len(sent), len(s.pending))
        self.assertTrue(all(url == "https://discord.test/paper" for url, _ in sent))
        self.assertIn("손절가", sent[0][1]["description"])
        self.assertIn("잔액 현금", sent[0][1]["description"])
        self.assertIn("이번 손익", sent[1][1]["description"])
        self.assertEqual(sent[0][1]["color"], notify.GREEN)  # 매수는 초록
        self.assertEqual(sent[1][1]["color"], notify.RED)  # 매도는 빨강

    def test_stop_loss_exit_is_bold_and_red(self):
        f = Fill("x4", "KRW-BTC", "비트코인", "SELL", Decimal("0.00004"), Decimal(147000000), Decimal(3), Decimal(-123),
                 "손절선 -2% 도달", "2026-10-15T22:00:00+09:00", "UPBIT", "KRW")
        t = notify.text("LIVE", "a", "spot", f, notify.Plan(), "현금", None)
        self.assertTrue(t.startswith("🛑 **손절**"))
        self.assertEqual(notify.color(f), notify.RED)


if __name__ == "__main__":
    unittest.main()
