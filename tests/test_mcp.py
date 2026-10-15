"""매매 봇 MCP 서버: stdio JSON-RPC, 시세·계좌 조회, 모의 주문, 실계좌 막기, AI 켜고 끄기"""

import argparse
import io
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

from devlog_trade import ai, mcp
from devlog_trade.broker import PaperBroker
from devlog_trade.exchanges import Binance, BinanceFutures
from devlog_trade.futures import FuturesBroker

from .test_bot import CLOSES, START, binance_rows

PRICES = CLOSES[:-1]  # 마지막 봉까지 다 닫힌 시세. 마지막 종가 97


def fake_exchange(exchange, market):
    cls = BinanceFutures if market == "futures" else Binance
    return cls(fetch=lambda url: binance_rows(PRICES, START))


class McpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        env = mock.patch.dict("os.environ", {"TRADE_CONTROL": str(self.root / "trade-control.json")})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self.tmp.cleanup)
        self.tools = mcp.TradeBotTools(self.root, exchange_for=fake_exchange)

    def test_accounts_label_testnet_and_skip_ladder_sidecars(self):
        self.spot()
        (self.root / "trade-state-binance-spot-1h.ladder.json").write_text("{}", encoding="utf-8")
        for name, testnet in (("trade-live-binance-spot-1m.json", True), ("trade-live-upbit-spot-1m.json", None)):
            path = self.root / self.spot(name)
            data = json.loads(path.read_text(encoding="utf-8"))
            data.update(mode="LIVE", **({} if testnet is None else {"testnet": testnet}))
            path.write_text(json.dumps(data), encoding="utf-8")
        got = {a["account"]: a.get("mode", a.get("error")) for a in self.tools.accounts(with_prices=False)["accounts"]}
        self.assertEqual(got, {"trade-state-binance-spot-1h.json": "PAPER", "trade-live-binance-spot-1m.json": "TESTNET",
                               "trade-live-upbit-spot-1m.json": "LIVE"})

    def spot(self, name="trade-state-binance-spot-1h.json"):
        PaperBroker(cash=Decimal(1000), fee_rate=Decimal("0.001"), currency="USDT", exchange="BINANCE").save(self.root / name)
        return name

    def rpc(self, *msgs):
        out = io.StringIO()
        mcp.Server(self.tools).serve(io.StringIO("".join(json.dumps(m) + "\n" for m in msgs)), out)
        return [json.loads(line) for line in out.getvalue().splitlines()]

    def test_stdio_handshake_lists_trade_bot_tools_and_skips_notifications(self):
        replies = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                           {"jsonrpc": "2.0", "method": "notifications/initialized"},
                           {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                           {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "record_trade", "arguments": {}}})
        self.assertEqual([r["id"] for r in replies], [1, 2, 3])
        self.assertEqual(replies[0]["result"]["protocolVersion"], "2025-06-18")
        names = [t["name"] for t in replies[1]["result"]["tools"]]
        self.assertEqual(names, ["trade_bot_prices", "trade_bot_accounts", "trade_bot_order", "trade_bot_ai"])
        self.assertTrue(replies[2]["result"]["isError"])  # devlog의 record_trade는 이 서버에 없다

    def test_prices_give_closes_and_moving_averages(self):
        r = self.tools.prices("btc", count=5)
        self.assertEqual((r["symbol"], r["last_close"], r["sma5"]), ("BTCUSDT", "97", "101.4"))
        self.assertEqual(len(r["closes"]), 5)

    def test_prices_without_closed_candles_is_a_tool_error(self):
        empty = mock.Mock(code="BINANCE", market="spot", currency="USDT")
        empty.fetch_candles.return_value = []
        empty.symbol_for.return_value = "NEWUSDT"
        tools = mcp.TradeBotTools(self.root, exchange_for=lambda e, m: empty)
        with self.assertRaisesRegex(mcp.ToolError, "봉이 아직 없어요"):
            tools.prices("new")

    def test_paper_buy_then_sell_saves_state_and_labels_the_journal(self):
        name = self.spot()
        r = self.tools.order(name, "BTC", "BUY", "지지선 반등", amount="100")
        self.assertTrue(r["filled"])
        self.assertEqual(r["mode"], "PAPER")
        self.assertIn("[대화 주문] 지지선 반등", r["fill"]["reason"])
        self.assertIn("남기지 않았어요", r["recorded"])
        held = PaperBroker.load(self.root / name).holding("BTCUSDT")
        self.assertGreater(held, 0)
        r = self.tools.order(name, "BTC", "SELL", "정리", fraction="1")
        self.assertEqual(r["fill"]["side"], "SELL")
        self.assertEqual(PaperBroker.load(self.root / name).holding("BTCUSDT"), 0)
        accounts = self.tools.accounts()
        self.assertEqual(accounts["accounts"][0]["positions"], [])

    def test_futures_long_shows_unrealized_pnl_then_closes(self):
        name = "trade-state-binance-futures-1h.json"
        FuturesBroker(cash=Decimal(1000), leverage=Decimal(3)).save(self.root / name)
        self.assertTrue(self.tools.order(name, "ETH", "LONG", "돌파", amount="50")["filled"])
        acc = self.tools.accounts()["accounts"][0]
        self.assertEqual((acc["positions"][0]["side"], acc["unrealized_pnl"]), ("LONG", "0"))
        with self.assertRaises(mcp.ToolError):
            self.tools.order(name, "ETH", "BUY", "현물 행동")
        self.assertIn("정리", self.tools.order(name, "ETH", "CLOSE", "손절")["fill"]["reason"])

    def test_bad_account_names_and_live_without_server_flag_are_refused(self):
        self.spot()
        for bad in ("../trade-state-x.json", "jobs.conf", "trade-state-none.json"):
            with self.assertRaises(mcp.ToolError):
                self.tools.order(bad, "BTC", "BUY", "x")
        with self.assertRaises(mcp.ToolError):
            self.tools.order("trade-state-binance-spot-1h.json", "BTC", "BUY", "x", live=True)
        self.spot("trade-live-binance-spot-1h.json")
        with self.assertRaises(mcp.ToolError):
            self.tools.order("trade-live-binance-spot-1h.json", "BTC", "BUY", "x")  # live=true 없이
        with self.assertRaisesRegex(mcp.ToolError, "--live"):
            self.tools.order("trade-live-binance-spot-1h.json", "BTC", "BUY", "x", live=True)
        live_args = argparse.Namespace(live=True, testnet=False, server="https://devlog.life", max_order="10",
                                       max_order_loss="5", max_daily_loss="10")
        tools = mcp.TradeBotTools(self.root, record=False, live_args=live_args, exchange_for=fake_exchange)
        with self.assertRaisesRegex(mcp.ToolError, "--record"):
            tools.order("trade-live-binance-spot-1h.json", "BTC", "BUY", "x", live=True)

    def test_ai_switch_wins_over_flag_and_clear_goes_back(self):
        args = argparse.Namespace(ai="on")
        self.assertTrue(ai.enabled(args))
        self.assertEqual(self.tools.ai_switch("off")["ai"], "off")
        self.assertFalse(ai.enabled(args))
        self.assertEqual(self.tools.ai_switch("on")["ai"], "on")
        self.assertTrue(ai.enabled(argparse.Namespace(ai=None)))
        self.tools.ai_switch("clear")
        self.assertIsNone(ai.switch())
        self.assertEqual(self.tools.accounts()["ai"]["switch"], "jobs.conf 설정을 따름")


if __name__ == "__main__":
    unittest.main()
