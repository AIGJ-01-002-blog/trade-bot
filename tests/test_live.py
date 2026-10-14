"""실계좌 주문(#264)을 가짜 거래소 HTTP로 확인한다. 진짜 거래소에는 한 번도 보내지 않는다.

켜는 조건·출금 권한 거절·상한·중복 주문 막기·서명 모양을 본다.
진짜 테스트넷으로 확인하려면 BINANCE_TESTNET_API_KEY·SECRET을 넣고 DEVLOG_TRADE_TESTNET_IT=1로 돌린다(맨 아래).
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import tempfile
import unittest
import urllib.parse
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

from devlog_trade import live
from devlog_trade.live import (BinanceFuturesTrader, BinanceSpotTrader, LiveBroker, LiveRefused, Limits, UpbitTrader,
                               client_order_id, open_live, require_live)

AT = "2026-10-14T10:00:00+09:00"
LIMITS = Limits(Decimal(100), Decimal(20), Decimal(50))


class FakeExchange:
    """(메서드, 경로) → 처리 함수. 보낸 요청을 calls에 모은다. 주문은 orders에 쌓여 조회에 잡힌다"""

    def __init__(self):
        self.calls = []
        self.orders = {}
        self.routes = {}

    def __call__(self, method, url, headers, body):
        u = urllib.parse.urlsplit(url)
        q = dict(urllib.parse.parse_qsl(u.query))
        if body:
            q.update(json.loads(body))
        self.calls.append((method, u.netloc, u.path, q, headers, url))
        handler = self.routes.get((method, u.path))
        if handler is None:
            raise AssertionError(f"모르는 요청: {method} {url}")
        return handler(q)


def binance_spot(fx: FakeExchange, withdrawals=False, commission_asset="USDT"):
    fx.routes[("GET", "/sapi/v1/account/apiRestrictions")] = lambda q: (200, {"enableWithdrawals": withdrawals})
    fx.routes[("GET", "/api/v3/account")] = lambda q: (200, {"balances": [{"asset": "USDT", "free": "500"},
                                                                          {"asset": "BTC", "free": "0.01"}]})
    fx.routes[("GET", "/api/v3/exchangeInfo")] = lambda q: (200, {"symbols": [
        {"symbol": "BTCUSDT", "filters": [{"filterType": "LOT_SIZE", "stepSize": "0.00001000"}]}]})

    def order(q):
        n = len(fx.orders) + 1
        if q["side"] == "BUY":
            qty, quote = Decimal(q["quoteOrderQty"]) / Decimal(50000), Decimal(q["quoteOrderQty"])
        else:
            qty, quote = Decimal(q["quantity"]), Decimal(q["quantity"]) * Decimal(40000)
        fx.orders[q["newClientOrderId"]] = {"orderId": n, "status": "FILLED", "executedQty": str(qty),
                                            "cummulativeQuoteQty": str(quote)}
        return 200, {"orderId": n}

    fx.routes[("POST", "/api/v3/order")] = order
    fx.routes[("GET", "/api/v3/order")] = lambda q: (200, fx.orders[q["origClientOrderId"]]) \
        if q["origClientOrderId"] in fx.orders else (400, {"code": -2013, "msg": "Order does not exist."})
    fx.routes[("GET", "/api/v3/myTrades")] = lambda q: (200, [{"commission": "0.1", "commissionAsset": commission_asset}])


def spot_trader(fx, testnet=False):
    return BinanceSpotTrader("k", "s", testnet=testnet, send=fx)


class SwitchTest(unittest.TestCase):
    def args(self, **kw):
        base = dict(live=False, testnet=False, max_order="100", max_order_loss="20", max_daily_loss="50", leverage=1,
                    symbol="BTCUSDT")
        return argparse.Namespace(**{**base, **kw})

    def test_paper_is_the_default(self):
        with self.assertRaisesRegex(LiveRefused, "--live"):
            require_live(self.args())

    def test_mainnet_needs_setting_and_flag(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(LiveRefused, "DEVLOG_TRADE_LIVE=1"):
                require_live(self.args(live=True))
        with mock.patch.dict(os.environ, {"DEVLOG_TRADE_LIVE": "1"}):
            require_live(self.args(live=True))
            with self.assertRaises(LiveRefused):
                require_live(self.args())  # 설정만 있고 확인 플래그가 없으면 여전히 모의

    def test_testnet_needs_only_the_flag(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            require_live(self.args(live=True, testnet=True))

    def test_limits_are_required_and_leverage_is_capped(self):
        with self.assertRaisesRegex(LiveRefused, "--max-daily-loss"):
            Limits(Decimal(100), Decimal(20), Decimal(0))
        with self.assertRaisesRegex(LiveRefused, "1~7배"):
            Limits(Decimal(100), Decimal(20), Decimal(50), leverage=8)
        Limits(Decimal(100), Decimal(20), Decimal(50), leverage=7)

    def test_keys_come_only_from_env(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(LiveRefused, "BINANCE_TESTNET_API_KEY"):
                BinanceSpotTrader.from_env(testnet=True)
        with mock.patch.dict(os.environ, {"UPBIT_ACCESS_KEY": "a", "UPBIT_SECRET_KEY": "b"}, clear=True):
            self.assertEqual(UpbitTrader.from_env(testnet=False).key, "a")

    def test_open_live_wires_everything_on_testnet(self):
        fx = FakeExchange()
        binance_spot(fx)
        env = {"BINANCE_TESTNET_API_KEY": "k", "BINANCE_TESTNET_API_SECRET": "s"}
        with mock.patch.dict(os.environ, env, clear=True), tempfile.TemporaryDirectory() as d:
            b = open_live(self.args(live=True, testnet=True), "binance", "spot", Path(d) / "live.json", send=fx)
        self.assertEqual((b.cash, b.mode, b.exchange), (Decimal(500), "LIVE", "BINANCE"))
        self.assertTrue(all(c[1] == "testnet.binance.vision" for c in fx.calls))


class KeyCheckTest(unittest.TestCase):
    def test_binance_key_with_withdrawals_is_refused(self):
        fx = FakeExchange()
        binance_spot(fx, withdrawals=True)
        with self.assertRaisesRegex(LiveRefused, "출금 권한"):
            spot_trader(fx).check_key()
        binance_spot(fx, withdrawals=False)
        spot_trader(fx).check_key()

    def test_upbit_key_that_can_read_withdrawals_is_refused(self):
        fx = FakeExchange()
        fx.routes[("GET", "/v1/withdraws/chance")] = lambda q: (200, {"currency": {"code": "BTC"}})
        with self.assertRaisesRegex(LiveRefused, "출금 권한"):
            UpbitTrader("a", "b", send=fx).check_key()
        fx.routes[("GET", "/v1/withdraws/chance")] = lambda q: (401, {"error": {"name": "out_of_scope", "message": "권한 없음"}})
        UpbitTrader("a", "b", send=fx).check_key()

    def test_state_file_of_paper_account_is_refused(self):
        fx = FakeExchange()
        binance_spot(fx)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s.json"
            path.write_text(json.dumps({"cash": "1000", "fee_rate": "0", "sell_tax_rate": "0", "positions": {},
                                        "exchange": "BINANCE", "currency": "USDT"}))
            with self.assertRaisesRegex(LiveRefused, "실계좌"):
                LiveBroker.open_state(path, spot_trader(fx), LIMITS)


class SigningTest(unittest.TestCase):
    def test_binance_hmac_signature_covers_the_query(self):
        fx = FakeExchange()
        binance_spot(fx)
        spot_trader(fx).free_cash()
        url = fx.calls[0][5]
        query, sig = url.split("?", 1)[1].rsplit("&signature=", 1)
        self.assertEqual(sig, hmac.new(b"s", query.encode(), hashlib.sha256).hexdigest())
        self.assertEqual(fx.calls[0][4]["X-MBX-APIKEY"], "k")

    def test_upbit_jwt_carries_query_hash(self):
        fx = FakeExchange()
        fx.routes[("POST", "/v1/orders")] = lambda q: (201, {"uuid": "u1"})
        UpbitTrader("access", "secret", send=fx).place("KRW-BTC", "BUY", Decimal("0.001"), Decimal(95_000_000), "cid", False)
        _, _, _, q, headers, _ = fx.calls[0]
        self.assertEqual((q["side"], q["ord_type"], q["price"]), ("bid", "price", "95000"))
        token = headers["Authorization"].removeprefix("Bearer ")
        head, body, sig = token.split(".")
        pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
        claims = json.loads(base64.urlsafe_b64decode(pad(body)))
        sent = {k: v for k, v in q.items()}
        expect = hashlib.sha512(urllib.parse.unquote(urllib.parse.urlencode(sent)).encode()).hexdigest()
        self.assertEqual((claims["access_key"], claims["query_hash"], claims["query_hash_alg"]), ("access", expect, "SHA512"))
        good = hmac.new(b"secret", f"{head}.{body}".encode(), hashlib.sha256).digest()
        self.assertEqual(base64.urlsafe_b64decode(pad(sig)), good)


def live_broker(fx, limits=LIMITS):
    b = LiveBroker(cash=Decimal(500), trader=spot_trader(fx), limits=limits, currency="USDT", exchange="BINANCE",
                   log=lambda m: None)
    return b


class LiveBrokerTest(unittest.TestCase):
    def test_buy_is_capped_and_uses_real_fill(self):
        fx = FakeExchange()
        binance_spot(fx)
        b = live_broker(fx)
        fill = b.buy("BTCUSDT", "비트코인", Decimal("0.01"), Decimal(50000), "돌파", AT)  # 500 USDT 요청 → 상한 100
        order = next(c for c in fx.calls if c[0] == "POST")
        self.assertEqual(order[3]["quoteOrderQty"], "100")
        self.assertEqual(order[3]["newClientOrderId"], client_order_id("BTCUSDT", "BUY", AT))
        self.assertEqual((fill.quantity, fill.price, fill.fee), (Decimal("0.002"), Decimal(50000), Decimal("0.1")))
        self.assertTrue(fill.external_id.startswith("live-BINANCE-"))
        self.assertEqual(b.holding("BTCUSDT"), Decimal("0.002"))
        self.assertEqual(b.cash, Decimal(500))  # 체결 뒤 거래소 잔고로 다시 맞춘다

    def test_same_bar_twice_does_not_order_twice(self):
        fx = FakeExchange()
        binance_spot(fx)
        b = live_broker(fx)
        first = b.buy("BTCUSDT", "비트코인", Decimal("0.001"), Decimal(50000), "", AT)
        again = live_broker(fx).buy("BTCUSDT", "비트코인", Decimal("0.001"), Decimal(50000), "", AT)  # 저장 전에 죽고 다시 돈 경우
        self.assertEqual(sum(1 for c in fx.calls if c[0] == "POST"), 1)
        self.assertEqual(first.external_id, again.external_id)

    def test_fee_taken_in_coin_reduces_quantity(self):
        fx = FakeExchange()
        binance_spot(fx, commission_asset="BTC")
        fx.routes[("GET", "/api/v3/myTrades")] = lambda q: (200, [{"commission": "0.000002", "commissionAsset": "BTC"}])
        fill = live_broker(fx).buy("BTCUSDT", "비트코인", Decimal("0.002"), Decimal(50000), "", AT)
        self.assertEqual(fill.quantity, Decimal("0.001998"))
        self.assertEqual(fill.fee, Decimal("0.1"))

    def test_big_loss_on_one_sale_halts_new_buys_but_not_sales(self):
        fx = FakeExchange()
        binance_spot(fx)
        b = live_broker(fx)
        b.buy("BTCUSDT", "비트코인", Decimal("0.002"), Decimal(50000), "", AT)
        sell = b.sell("BTCUSDT", "비트코인", Decimal("0.002"), Decimal(40000), "손절", "2026-10-14T11:00:00+09:00")
        self.assertEqual(sell.quantity, Decimal("0.002"))  # 스텝 0.00001에 맞춘 수량
        self.assertEqual(sell.realized_pnl, Decimal("-20.10000000"))  # 80 - 100 - 수수료 0.1
        self.assertIn("한 번 청산 손실", b.halted)
        self.assertIsNone(b.buy("BTCUSDT", "비트코인", Decimal("0.001"), Decimal(40000), "", "2026-10-14T12:00:00+09:00"))

    def test_daily_loss_cap_resets_next_kst_day(self):
        fx = FakeExchange()
        binance_spot(fx)
        b = live_broker(fx, Limits(Decimal(100), Decimal(1000), Decimal(15)))
        b.buy("BTCUSDT", "비트코인", Decimal("0.002"), Decimal(50000), "", AT)
        b.sell("BTCUSDT", "비트코인", Decimal("0.002"), Decimal(40000), "", "2026-10-14T11:00:00+09:00")
        self.assertIn("하루 손실", b.halted)
        self.assertIsNone(b.buy("BTCUSDT", "비트코인", Decimal("0.001"), Decimal(40000), "", "2026-10-14T23:00:00+09:00"))
        # 한국 시간 자정(UTC 15:00)이 지나면 다시 산다
        self.assertIsNotNone(b.buy("BTCUSDT", "비트코인", Decimal("0.001"), Decimal(40000), "", "2026-10-14T15:30:00Z"))
        self.assertEqual(b.halted, "")

    def test_rejected_order_returns_none(self):
        fx = FakeExchange()
        binance_spot(fx)
        fx.routes[("POST", "/api/v3/order")] = lambda q: (400, {"code": -1013, "msg": "Filter failure: NOTIONAL"})
        logs = []
        b = live_broker(fx)
        b.log = logs.append
        self.assertIsNone(b.buy("BTCUSDT", "비트코인", Decimal("0.00001"), Decimal(50000), "", AT))
        self.assertIn("NOTIONAL", logs[0])

    def test_timeout_after_order_finds_the_order_instead_of_resending(self):
        fx = FakeExchange()
        binance_spot(fx)
        real = fx.routes[("POST", "/api/v3/order")]

        def lost(q):
            real(q)
            raise TimeoutError("timed out")

        fx.routes[("POST", "/api/v3/order")] = lost
        fill = live_broker(fx).buy("BTCUSDT", "비트코인", Decimal("0.001"), Decimal(50000), "", AT)
        self.assertIsNotNone(fill)
        self.assertEqual(sum(1 for c in fx.calls if c[0] == "POST"), 1)

    def test_state_keeps_live_fields(self):
        fx = FakeExchange()
        binance_spot(fx)
        b = live_broker(fx)
        b.buy("BTCUSDT", "비트코인", Decimal("0.002"), Decimal(50000), "", AT)
        b.halted = "테스트"
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "live.json"
            b.save(path)
            again = LiveBroker.open_state(path, spot_trader(fx, testnet=True), LIMITS)
        self.assertEqual((again.halted, again.holding("BTCUSDT"), again.mode), ("테스트", Decimal("0.002"), "LIVE"))


class FuturesTest(unittest.TestCase):
    def fx(self):
        fx = FakeExchange()
        fx.routes[("POST", "/fapi/v1/leverage")] = lambda q: (200, {"leverage": int(q["leverage"])})
        fx.routes[("POST", "/fapi/v1/marginType")] = lambda q: (400, {"code": -4046, "msg": "No need to change margin type."})
        fx.routes[("GET", "/fapi/v1/exchangeInfo")] = lambda q: (200, {"symbols": [
            {"symbol": "BTCUSDT", "filters": [{"filterType": "MARKET_LOT_SIZE", "stepSize": "0.001"}]}]})

        def order(q):
            fx.orders[q["newClientOrderId"]] = {"orderId": 7, "status": "FILLED", "executedQty": q["quantity"],
                                                "avgPrice": "60000", "reduceOnly": q.get("reduceOnly"),
                                                "updateTime": 1791972000000}
            return 200, {"orderId": 7}

        fx.routes[("POST", "/fapi/v1/order")] = order
        fx.routes[("GET", "/fapi/v1/order")] = lambda q: (200, fx.orders[q["origClientOrderId"]]) \
            if q["origClientOrderId"] in fx.orders else (400, {"code": -2013, "msg": "Order does not exist."})
        fx.routes[("GET", "/fapi/v1/userTrades")] = lambda q: (200, [{"commission": "0.024", "commissionAsset": "USDT"}])
        return fx

    def test_leverage_over_seven_is_refused_before_any_request(self):
        fx = self.fx()
        with self.assertRaises(LiveRefused):
            BinanceFuturesTrader("k", "s", True, fx).set_leverage("BTCUSDT", 8)
        self.assertEqual(fx.calls, [])

    def test_close_is_reduce_only_and_quantity_follows_step(self):
        fx = self.fx()
        t = BinanceFuturesTrader("k", "s", True, fx)
        r = t.market_order("BTCUSDT", "SELL", Decimal("0.0019"), Decimal(60000), "c1", reduce_only=True)
        sent = next(c for c in fx.calls if c[0] == "POST")[3]
        self.assertEqual((sent["quantity"], sent["reduceOnly"]), ("0.001", "true"))
        self.assertEqual((r.price, r.fee), (Decimal(60000), Decimal("0.024")))
        self.assertTrue(all(c[1] == "testnet.binancefuture.com" for c in fx.calls))


    def broker(self, fx, amt="-0.001"):
        fx.routes[("GET", "/fapi/v2/balance")] = lambda q: (200, [{"asset": "USDT", "availableBalance": "300"}])
        fx.routes[("GET", "/fapi/v2/positionRisk")] = lambda q: (200, [{"symbol": "BTCUSDT", "positionAmt": amt}])
        return live.LiveFuturesBroker(cash=Decimal(300), trader=BinanceFuturesTrader("k", "s", True, fx),
                                      limits=Limits(Decimal(60), Decimal(20), Decimal(50), leverage=3), leverage=Decimal(3),
                                      log=lambda m: None)

    def test_short_entry_is_sell_and_close_is_reduce_only_buy(self):
        fx = self.fx()
        b = self.broker(fx)
        qty = b.quantity_for(Decimal(100), Decimal(60000))  # 모의 선물과 같은 수량 계산
        entry = b.open("BTCUSDT", "비트코인 선물", "SHORT", qty, Decimal(60000), "하락 돌파", AT)
        self.assertEqual((entry.side, entry.exchange, entry.symbol_name), ("SELL", "BINANCE", "비트코인 선물"))
        self.assertEqual(entry.reason, "[선물 숏 진입 3x] 하락 돌파")
        self.assertEqual(entry.quantity, Decimal("0.001"))  # 상한 60 USDT / 60000 → 스텝 0.001
        lev = [c for c in fx.calls if c[2] == "/fapi/v1/leverage"]
        self.assertEqual((lev[0][3]["symbol"], lev[0][3]["leverage"]), ("BTCUSDT", "3"))
        margin = next(c for c in fx.calls if c[2] == "/fapi/v1/marginType")
        self.assertEqual(margin[3]["marginType"], "ISOLATED")  # 레버리지보다 먼저 격리 마진
        self.assertEqual(b.positions["BTCUSDT"].side, "SHORT")
        close = b.close("BTCUSDT", Decimal(60000), "", "2026-10-14T11:00:00+09:00")
        sent = [c for c in fx.calls if c[0] == "POST" and c[2] == "/fapi/v1/order"]
        self.assertEqual((sent[0][3]["side"], sent[1][3]["side"], sent[1][3].get("reduceOnly")), ("SELL", "BUY", "true"))
        self.assertEqual((close.side, close.reason), ("BUY", "[선물 숏 정리]"))
        self.assertEqual(close.realized_pnl, Decimal("-0.024"))  # 같은 값에 사고팔아 수수료만큼 손해
        self.assertNotIn("BTCUSDT", b.positions)
        self.assertEqual(close.executed_at, "2026-10-14T19:00:00+09:00")  # 봉 시각이 아니라 거래소 체결 시각

    def test_leverage_is_set_once_per_symbol(self):
        fx = self.fx()
        b = self.broker(fx, amt="0.001")
        b.open("BTCUSDT", "비트코인 선물", "LONG", Decimal("0.001"), Decimal(60000), "", AT)
        b.close("BTCUSDT", Decimal(60000), "", "2026-10-14T11:00:00+09:00")
        b.open("BTCUSDT", "비트코인 선물", "LONG", Decimal("0.001"), Decimal(60000), "", "2026-10-14T12:00:00+09:00")
        self.assertEqual(sum(1 for c in fx.calls if c[2] == "/fapi/v1/leverage"), 1)

    def test_position_gone_on_exchange_is_dropped_without_order(self):
        fx = self.fx()
        b = self.broker(fx, amt="0")
        b.positions["BTCUSDT"] = live.FuturesPosition("LONG", Decimal("0.001"), Decimal(60000), Decimal(20), "비트코인 선물")
        self.assertIsNone(b.close("BTCUSDT", Decimal(50000), "", AT))
        self.assertNotIn("BTCUSDT", b.positions)
        self.assertFalse(any(c[0] == "POST" for c in fx.calls))

    def test_futures_state_survives_and_keeps_last_seen(self):
        fx = self.fx()
        b = self.broker(fx)
        b.open("BTCUSDT", "비트코인 선물", "SHORT", Decimal("0.001"), Decimal(60000), "", AT)
        b.last_seen["BTCUSDT"] = "2026-10-14T00:00:00Z"
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "f.json"
            b.save(path)
            again = live.LiveFuturesBroker.open_state(path, BinanceFuturesTrader("k", "s", True, fx), b.limits)
            with self.assertRaisesRegex(LiveRefused, "상태 파일이 아니에요"):  # 선물 파일을 현물 계좌로 열지 않는다
                LiveBroker.open_state(path, spot_trader(fx, testnet=True), LIMITS)
        self.assertEqual((again.positions["BTCUSDT"].side, again.last_seen["BTCUSDT"]), ("SHORT", "2026-10-14T00:00:00Z"))
        self.assertEqual(again.leverage, Decimal(3))


    def test_isolated_margin_failure_stops_the_entry(self):
        fx = self.fx()
        fx.routes[("POST", "/fapi/v1/marginType")] = lambda q: (400, {"code": -4047, "msg": "Margin type cannot be changed if there exists open orders."})
        b = self.broker(fx)
        self.assertIsNone(b.open("BTCUSDT", "비트코인 선물", "LONG", Decimal("0.001"), Decimal(60000), "", AT))
        self.assertFalse(any(c[2] == "/fapi/v1/order" for c in fx.calls))


class UpbitTest(unittest.TestCase):
    def test_market_buy_cancel_state_with_volume_counts_as_filled(self):
        fx = FakeExchange()
        orders = {}

        def place(q):
            orders[q["identifier"]] = {"uuid": "u1", "state": "cancel", "executed_volume": "0.001", "paid_fee": "47.5",
                                       "trades": [{"funds": "95000"}]}
            return 201, {"uuid": "u1"}

        fx.routes[("POST", "/v1/orders")] = place
        fx.routes[("GET", "/v1/order")] = lambda q: (200, orders[q["identifier"]]) if q["identifier"] in orders \
            else (404, {"error": {"name": "order_not_found"}})
        r = UpbitTrader("a", "b", send=fx).market_order("KRW-BTC", "BUY", Decimal("0.001"), Decimal(95_000_000), "cid")
        self.assertEqual((r.quantity, r.price, r.fee), (Decimal("0.001"), Decimal(95_000_000), Decimal(48)))

    def test_no_testnet(self):
        with self.assertRaisesRegex(LiveRefused, "테스트넷"):
            UpbitTrader("a", "b", testnet=True)


@unittest.skipUnless(os.environ.get("DEVLOG_TRADE_TESTNET_IT") == "1", "진짜 바이낸스 테스트넷 확인은 DEVLOG_TRADE_TESTNET_IT=1일 때만")
class BinanceTestnetIT(unittest.TestCase):
    """테스트넷(가짜 돈)에만 주문한다. 메인넷 주소로는 절대 가지 않게 testnet=True로 고정한다"""

    def test_round_trip_on_spot_testnet(self):
        t = BinanceSpotTrader.from_env(testnet=True)
        self.assertEqual(t.url, "https://testnet.binance.vision")
        b = LiveBroker(cash=t.free_cash(), trader=t, limits=Limits(Decimal(20), Decimal(5), Decimal(10)),
                       currency="USDT", exchange="BINANCE")
        now = datetime.now(live.KST).replace(microsecond=0)  # 실행마다 새 주문 번호가 되도록 지금 시각을 봉 시각으로 쓴다
        price = Decimal(1_000_000)  # 시장가라 값은 수량 환산에만 쓰인다: 20 USDT 상한으로 산다
        buy = b.buy("BTCUSDT", "비트코인", Decimal(1), price, "testnet", now.isoformat())
        self.assertIsNotNone(buy)
        sell = b.sell("BTCUSDT", "비트코인", buy.quantity, buy.price, "testnet", (now + timedelta(seconds=1)).isoformat())
        self.assertIsNotNone(sell)


if __name__ == "__main__":
    unittest.main()
