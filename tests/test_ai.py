"""AI 매매 판단(#263): 켜고 끄기, 15분봉 기준, 하루 호출·월 예산 상한, 이상한 답은 관망, 이유가 매매 일지로 가는지 본다."""

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
from devlog_trade import ai
from devlog_trade.exchanges import INTERVALS, Binance
from devlog_trade.strategy import Bar, MovingAverageCross

from .test_bot import CLOSES, START, FakeDevlog, binance_rows

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=ai.KST)


class FakeClient:
    label = "fake:model"

    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts = []

    def ask(self, prompt, actions):
        self.prompts.append(prompt)
        self.actions = actions
        a = self.answers.pop(0) if self.answers else ("HOLD", "관망")
        if isinstance(a, Exception):
            raise a
        return ai.Reply(a[0], a[1], 1000, 100)

    def cost_krw(self, reply):
        return Decimal(10)


def bars(closes):
    return [Bar(f"2026-10-13T{i:02d}:00:00Z", "BTCUSDT", "비트코인", Decimal(c)) for i, c in enumerate(closes)]


class ParseTest(unittest.TestCase):
    def test_accepts_only_known_actions_with_a_reason(self):
        self.assertEqual(ai.parse_reply('{"action": "buy", "reason": "  거래량  증가 "}'), ("BUY", "거래량 증가"))
        for bad in ("hello", "[]", '{"action": "SHORT", "reason": "x"}', '{"action": "BUY", "reason": ""}'):
            with self.assertRaises(ai.AiError):
                ai.parse_reply(bad)

    def test_long_reason_fits_the_journal(self):
        _, reason = ai.parse_reply(json.dumps({"action": "HOLD", "reason": "가" * 500}))
        self.assertEqual(len("AI: " + reason), 284)
        with self.assertRaises(ai.AiError):
            ai.parse_reply('{"action": "BUY", "reason": "x"}', ai.ACTIONS["futures"])


class GeminiTest(unittest.TestCase):
    def test_reads_json_answer_and_tokens_and_costs_in_won(self):
        sent = {}

        def post(url, body, headers, timeout):
            sent.update(url=url, body=body, headers=headers)
            return {"candidates": [{"content": {"parts": [{"text": '{"action":"SELL","reason":"고점 이탈"}'}]}}],
                    "usageMetadata": {"promptTokenCount": 1_000_000, "candidatesTokenCount": 0}}

        g = ai.Gemini("key", post=post)
        r = g.ask("p")
        self.assertEqual((r.action, r.reason), ("SELL", "고점 이탈"))
        self.assertIn("gemini-2.5-flash-lite:generateContent", sent["url"])
        self.assertEqual(sent["headers"], {"x-goog-api-key": "key"})  # 키는 주소가 아니라 헤더로
        self.assertEqual(sent["body"]["generationConfig"]["responseMimeType"], "application/json")
        self.assertEqual(g.cost_krw(r), Decimal(140))  # 100만 토큰 × 0.10달러 × 1400원

    def test_needs_a_key(self):
        with self.assertRaises(ai.AiError):
            ai.Gemini("")

    def test_empty_candidates_is_an_ai_error(self):
        with self.assertRaises(ai.AiError):
            ai.Gemini("k", post=lambda *a: {"candidates": []}).ask("p")


class BudgetTest(unittest.TestCase):
    def test_daily_calls_and_monthly_cost(self):
        with tempfile.TemporaryDirectory() as d:
            now = [NOW]
            b = ai.Budget(Path(d) / "u.json", daily_calls=2, monthly_krw=Decimal(25), now=lambda: now[0])
            b.record(Decimal(10))
            self.assertEqual(b.blocked(), "")
            b.record(Decimal(10))
            self.assertIn("호출 상한 2회", b.blocked())
            now[0] = NOW + timedelta(days=1)  # 다음 날은 호출 수가 다시 0, 이번 달 비용은 이어짐
            self.assertEqual(b.blocked(), "")
            b.record(Decimal(10))
            self.assertIn("이번 달 AI 예산 25원", b.blocked())
            now[0] = datetime(2026, 11, 1, 0, 30, tzinfo=ai.KST)  # 새 달
            self.assertEqual(b.blocked(), "")


class StrategyTest(unittest.TestCase):
    def make(self, client, since="", daily=100, d=None, market="spot"):
        budget = ai.Budget(Path(d) / "u.json", daily, Decimal(30000), now=lambda: NOW)
        account = ai.Account(lambda: Decimal(1000), "USDT", Decimal("0.001"), market, Decimal(2))
        logs = []
        s = ai.AiStrategy(MovingAverageCross(window=3), client, budget, "1h", {"BTCUSDT": since}, account, log=logs.append)
        return s, budget, logs

    def test_ai_decides_and_reason_is_prefixed(self):
        with tempfile.TemporaryDirectory() as d:
            client = FakeClient(("BUY", "반등 시작"), ("SELL", "보유 중이 아니라 무시돼야 함"))
            s, budget, _ = self.make(client, d=d)
            self.assertEqual(s.on_bar(bars([100])[0], False, Decimal(0)).reason, "AI: 반등 시작")
            self.assertIsNone(s.on_bar(bars([100, 101])[1], False, Decimal(0)))  # 안 가진 걸 팔라는 답
            self.assertIn("포지션: 보유 없음", client.prompts[0])
            self.assertIn("현금: 1000 USDT", client.prompts[0])
            self.assertEqual(budget.used(), (2, Decimal(20)))

    def test_warm_up_bars_never_call_ai(self):
        with tempfile.TemporaryDirectory() as d:
            client = FakeClient()
            s, _, _ = self.make(client, since="2026-10-13T05:00:00Z", d=d)
            for b in bars([100, 99, 98, 97, 96, 95]):
                s.on_bar(b, False, Decimal(0))
            self.assertEqual(client.prompts, [])
            s.on_bar(bars([100] * 7)[6], False, Decimal(0))
            self.assertEqual(len(client.prompts), 1)
            self.assertEqual(client.prompts[0].count("2026-10-13T"), 7)  # 데운 봉도 AI에게 보여 준다

    def test_bad_or_slow_answer_means_hold_not_rule(self):
        with tempfile.TemporaryDirectory() as d:
            client = FakeClient(ai.AiError("형식"), TimeoutError("늦음"))
            s, budget, logs = self.make(client, d=d)
            for b in bars([100, 99, 98])[:2]:
                self.assertIsNone(s.on_bar(b, False, Decimal(0)))
            self.assertEqual(len(logs), 2)
            self.assertIn("관망", logs[0])
            self.assertEqual(budget.used()[0], 2)  # 실패한 호출도 상한에 센다

    def test_stop_loss_stays_with_the_rule(self):
        with tempfile.TemporaryDirectory() as d:
            client = FakeClient()
            s, _, _ = self.make(client, d=d)
            sig = s.on_bar(bars([90])[0], True, Decimal(100))
            self.assertEqual(sig.side, "SELL")
            self.assertIn("손절", sig.reason)
            self.assertEqual(client.prompts, [])

    def test_over_cap_falls_back_to_rule(self):
        with tempfile.TemporaryDirectory() as d:
            client = FakeClient(("HOLD", "관망"))
            s, _, logs = self.make(client, daily=1, d=d)
            signals = [s.on_bar(b, False, Decimal(0)) for b in bars([100, 99, 98, 97, 101])]
            self.assertEqual(len(client.prompts), 1)
            self.assertEqual(signals[-1].side, "BUY")  # 규칙의 이동평균 돌파
            self.assertNotIn("AI:", signals[-1].reason)
            self.assertEqual(len(logs), 1)  # 내려간다는 안내는 한 번만


    def test_futures_actions_flip_and_close(self):
        with tempfile.TemporaryDirectory() as d:
            client = FakeClient(("SHORT", "고점에서 꺾임"), ("LONG", "다시 반등"), ("CLOSE", "변동성 큼"), ("BUY", "선물에 없는 행동"))
            s, _, _ = self.make(client, d=d, market="futures")
            b = bars([100, 101, 102, 103])
            self.assertEqual([x.side for x in s.on_bar_futures(b[0], None, Decimal(0))], ["SHORT"])
            self.assertEqual(client.actions, ("LONG", "SHORT", "CLOSE", "HOLD"))
            self.assertIn("USDT-M 선물 2배", client.prompts[0])
            flip = s.on_bar_futures(b[1], "SHORT", Decimal(101))
            self.assertEqual([x.side for x in flip], ["CLOSE", "LONG"])
            self.assertEqual(flip[1].reason, "AI: 다시 반등")
            self.assertEqual([x.side for x in s.on_bar_futures(b[2], "LONG", Decimal(101))], ["CLOSE"])
            self.assertEqual(s.on_bar_futures(b[3], None, Decimal(0)), [])  # FakeClient는 형식 검사를 안 하니 모르는 행동은 무시

    def test_futures_stop_loss_stays_with_the_rule(self):
        with tempfile.TemporaryDirectory() as d:
            client = FakeClient()
            s, _, _ = self.make(client, d=d, market="futures")
            sig = s.on_bar_futures(bars([104])[0], "SHORT", Decimal(100))
            self.assertEqual([(x.side, x.reason) for x in sig], [("CLOSE", "손절선 -3% 도달")])
            self.assertEqual(client.prompts, [])


class SwitchTest(unittest.TestCase):
    def args(self, *argv):
        base = dict(ai=None, ai_provider=None, ai_model=None, ai_daily_calls=10, ai_monthly_krw="100", ai_usage="u.json",
                    strategy="ma-cross")
        return cli.argparse.Namespace(**{**base, **dict(argv)})

    def test_off_by_default_and_env_or_flag_turns_it_on(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertFalse(ai.enabled(self.args()))
        with mock.patch.dict("os.environ", {"AI_DECISION": "on"}):
            self.assertTrue(ai.enabled(self.args()))
            self.assertFalse(ai.enabled(self.args(("ai", "off"))))  # 플래그가 환경 변수보다 먼저

    def test_only_15m_and_longer(self):
        self.assertEqual([i for i in INTERVALS if ai.eligible(i, INTERVALS)], ["15m", "30m", "1h", "4h", "1d"])

    def test_ollama_provider(self):
        with mock.patch.dict("os.environ", {"AI_PROVIDER": "ollama", "AI_MODEL": "qwen2.5:14b"}):
            c = ai.make_client(self.args())
        self.assertEqual(c.label, "ollama:qwen2.5:14b")
        self.assertEqual(c.cost_krw(ai.Reply("HOLD", "x", 10**6, 10**6)), 0)


class CoinAiCliTest(unittest.TestCase):
    def run_coin(self, *argv, answers=()):
        ex = Binance(fetch=lambda url: binance_rows(CLOSES, START))
        real = ex.fetch_candles
        ex.fetch_candles = lambda symbol, interval, limit: real(symbol, interval, limit, now=START + timedelta(hours=len(CLOSES) - 0.5))
        client = FakeClient(*answers)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), mock.patch.dict(cli.EXCHANGES, {("binance", "spot"): lambda: ex}), \
                mock.patch.object(ai, "make_client", lambda args: client):
            code = cli.main(["coin", "--symbol", "BTCUSDT", *argv])
        return code, out.getvalue(), err.getvalue(), client

    def test_ai_reason_and_strategy_go_to_the_journal(self):
        with tempfile.TemporaryDirectory() as d:
            fake = FakeDevlog()
            try:
                with mock.patch.dict("os.environ", {"DEVLOG_TOKEN": "secret", "AI_DECISION": "on"}):
                    code, out, err, client = self.run_coin("--state", str(Path(d) / "s.json"), "--server", fake.url,
                                                           "--window", "3", "--ai-usage", str(Path(d) / "u.json"),
                                                           answers=[("BUY", "하락 멈춤")])
                self.assertEqual(code, 0, out + err)
                sent = [c["params"]["arguments"] for c in fake.calls]
                self.assertEqual(sent[0]["reason"], "AI: 하락 멈춤")
                self.assertEqual(sent[0]["strategy"], "ma-cross+ai:fake:model")
                self.assertEqual(sent[1]["reason"], "손절선 -3% 도달")  # 97원에서 손절은 AI에게 묻지 않는다
                self.assertEqual(len(client.prompts), len(CLOSES) - 2)  # 닫힌 봉 11개 가운데 손절 봉만 빼고
            finally:
                fake.close()

    def test_off_means_no_ai_calls(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict("os.environ", {"AI_DECISION": "off"}):
            code, out, _, client = self.run_coin("--state", str(Path(d) / "s.json"), "--dry-run", "--window", "3")
        self.assertEqual(code, 0)
        self.assertEqual(client.prompts, [])
        self.assertIn("이동평균선 위로", out)

    def test_short_candles_stay_rule_based(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict("os.environ", {"AI_DECISION": "on"}):
            code, _, err, client = self.run_coin("--state", str(Path(d) / "s.json"), "--dry-run", "--interval", "5m")
        self.assertEqual(code, 0)
        self.assertEqual(client.prompts, [])
        self.assertIn("5m봉은 AI 판단 없이", err)

    def test_missing_key_stops_instead_of_silently_trading(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict("os.environ", {"AI_DECISION": "on"}, clear=True):
            ex = Binance(fetch=lambda url: binance_rows(CLOSES, START))
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err), mock.patch.dict(cli.EXCHANGES, {("binance", "spot"): lambda: ex}):
                code = cli.main(["coin", "--symbol", "BTCUSDT", "--state", str(Path(d) / "s.json"), "--dry-run"])
        self.assertEqual(code, 2)
        self.assertIn("GEMINI_API_KEY", err.getvalue())


if __name__ == "__main__":
    unittest.main()
