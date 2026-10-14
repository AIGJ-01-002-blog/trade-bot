"""AI 매매 판단 (#263). AI_DECISION=on이면 15분봉 이상에서 봉이 닫힐 때 AI에게 매수·매도·관망과 이유 한 줄을 묻는다.

- 기본은 꺼짐. 꺼져 있거나, 1분·5분봉이거나, 하루 호출 상한·월 예산을 넘으면 규칙 전략(이동평균·손절)이 그대로 판단한다.
- 손절은 AI가 켜져 있어도 규칙이 먼저 판다. AI가 손실을 키우지 못하게 하는 안전장치다.
- AI 응답이 형식에 맞지 않거나 늦으면 관망한다(주문하지 않는다). 규칙으로 대신 사지 않는다.
- AI가 낸 이유는 체결의 reason으로 record_trade에 가서 매매 일지에 보인다.
- 기본 제공자는 Gemini(gemini-2.5-flash-lite, devlog 서버가 쓰는 모델과 같음). 맥북에서 Ollama를 띄웠으면 AI_PROVIDER=ollama.
  상위 모델은 AI_PROVIDER=anthropic(기본 claude-opus-5-5)·openai(기본 gpt-5)로 고른다. 모델별 성과는 모의 계좌를 나눠 비교한다.
  키는 환경 변수 GEMINI_API_KEY·ANTHROPIC_API_KEY·OPENAI_API_KEY로만 넘긴다. 파일이나 저장소에 적지 않는다.
- 표준 라이브러리만 쓰는 봇이라 제공자 SDK 대신 각 제공자의 HTTP API를 urllib로 부른다.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable, Protocol

from .strategy import Bar, Signal

KST = timezone(timedelta(hours=9))
MIN_AI_INTERVAL = timedelta(minutes=15)
REASON_MAX = 280  # devlog 매매 일지 이유는 300자까지. 앞에 붙는 "AI: "를 뺀 길이

# 100만 토큰당 달러. 쓰는 모델 가격이 바뀌면 --ai-price-in/--ai-price-out으로 바꾼다
GEMINI_PRICE = {"gemini-2.5-flash-lite": (Decimal("0.10"), Decimal("0.40")), "gemini-2.5-flash": (Decimal("0.30"), Decimal("2.50"))}
# Anthropic 공식 표(2026-10 기준). 생각(thinking) 토큰은 출력으로 과금된다
ANTHROPIC_PRICE = {"claude-opus-5-5": (Decimal("4"), Decimal("20")), "claude-sonnet-5-5": (Decimal("2"), Decimal("10")),
                   "claude-haiku-5-5": (Decimal("0.10"), Decimal("0.50"))}
# OpenAI는 여러 가격 비교 사이트가 같게 적은 GPT-5 계열 값(2026-10). 새 모델을 쓰면 --ai-price-in/--ai-price-out으로 넣는다
OPENAI_PRICE = {"gpt-5": (Decimal("1.25"), Decimal("10")), "gpt-5-mini": (Decimal("0.25"), Decimal("2")),
                "gpt-5-nano": (Decimal("0.05"), Decimal("0.40"))}
# 모르는 모델은 그 제공자의 가장 비싼 값으로 어림한다(예산을 적게 잡아 넘치지 않게)
PRICE_FALLBACK = {"gemini": GEMINI_PRICE["gemini-2.5-flash"], "anthropic": ANTHROPIC_PRICE["claude-opus-5-5"],
                  "openai": (Decimal("5"), Decimal("30"))}

SYSTEM_PROMPT = (
    "너는 코인 모의매매 봇의 판단 보조다. 방금 닫힌 봉까지의 종가, 포지션, 평균 단가, 쓸 수 있는 현금을 보고 "
    "주어진 행동 가운데 하나를 고른다. 수수료를 넘는 근거가 없으면 HOLD(관망)를 고른다. "
    "reason에는 판단 근거를 한국어 한 문장(120자 이내)으로 쓴다."
)
# 시장마다 고를 수 있는 행동. 현물은 사고팔기만, 선물은 롱·숏 진입과 정리
ACTIONS = {"spot": ("BUY", "SELL", "HOLD"), "futures": ("LONG", "SHORT", "CLOSE", "HOLD")}
ACTION_HELP = {
    "spot": "BUY(사기, 보유 없을 때만), SELL(다 팔기, 보유 중일 때만), HOLD(관망)",
    "futures": "LONG(롱 진입, 숏이면 정리 후 롱), SHORT(숏 진입, 롱이면 정리 후 숏), CLOSE(포지션 정리), HOLD(관망)",
}


def response_schema(actions: tuple[str, ...], strict: bool = False) -> dict:
    schema = {"type": "object", "properties": {"action": {"type": "string", "enum": list(actions)}, "reason": {"type": "string"}},
              "required": ["action", "reason"]}
    if strict:  # Anthropic·OpenAI 구조화 출력은 추가 속성을 막아야 받는다
        schema["additionalProperties"] = False
    return schema


class AiError(RuntimeError):
    pass


@dataclass
class Reply:
    action: str
    reason: str
    tokens_in: int = 0
    tokens_out: int = 0


Post = Callable[[str, dict, dict, float], dict]


def http_post_json(url: str, body: dict, headers: dict, timeout: float) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as res:  # noqa: S310 (AI 제공자 주소만 부른다)
        return json.loads(res.read().decode("utf-8"))


def parse_reply(text: str, actions: tuple[str, ...] = ACTIONS["spot"]) -> tuple[str, str]:
    """{"action": "BUY|SELL|HOLD", "reason": "..."}. 형식이 다르거나 고를 수 없는 행동이면 AiError"""
    try:
        data = json.loads(text)
    except ValueError as e:
        raise AiError(f"AI 답이 JSON이 아니에요: {text[:80]!r}") from e
    if not isinstance(data, dict):
        raise AiError("AI 답이 객체가 아니에요")
    action = str(data.get("action", "")).strip().upper()
    reason = " ".join(str(data.get("reason", "")).split())
    if action not in actions or not reason:
        raise AiError(f"AI 답 형식이 맞지 않아요: {text[:80]!r}")
    return action, reason[:REASON_MAX]


class Client(Protocol):
    label: str

    def ask(self, prompt: str, actions: tuple[str, ...]) -> Reply: ...

    def cost_krw(self, reply: Reply) -> Decimal: ...


class Gemini:
    def __init__(self, api_key: str, model: str = "gemini-2.5-flash-lite", timeout: float = 20, post: Post = http_post_json,
                 price: tuple[Decimal, Decimal] | None = None, usd_krw: Decimal = Decimal(1400)):
        if not api_key:
            raise AiError("AI 판단을 켜려면 GEMINI_API_KEY 환경 변수에 Gemini 키를 넣어 주세요.")
        self.api_key, self.model, self.timeout, self.post = api_key, model, timeout, post
        self.price = price or GEMINI_PRICE.get(model, GEMINI_PRICE["gemini-2.5-flash"])
        self.usd_krw = usd_krw
        self.label = f"gemini:{model}"

    def ask(self, prompt: str, actions: tuple[str, ...] = ACTIONS["spot"]) -> Reply:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_path(self.model)}:generateContent"
        body = {
            "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 256, "responseMimeType": "application/json",
                                 "responseSchema": response_schema(actions)},
        }
        data = self.post(url, body, {"x-goog-api-key": self.api_key}, self.timeout)
        usage = data.get("usageMetadata") or {}
        try:
            text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
        except (KeyError, IndexError, TypeError) as e:
            raise AiError(f"Gemini 답에 내용이 없어요: {str(data)[:120]}") from e
        action, reason = parse_reply(text, actions)
        return Reply(action, reason, int(usage.get("promptTokenCount", 0)), int(usage.get("candidatesTokenCount", 0)))

    def cost_krw(self, reply: Reply) -> Decimal:
        return usd_cost_krw(reply, self.price, self.usd_krw)


def usd_cost_krw(reply: "Reply", price: tuple[Decimal, Decimal], usd_krw: Decimal) -> Decimal:
    return (reply.tokens_in * price[0] + reply.tokens_out * price[1]) / Decimal(1_000_000) * usd_krw


class Anthropic:
    """Claude Messages API. 생각은 끌 수 없는 모델이라 effort=low로 짧게 생각하게 하고, 답은 JSON 스키마로 받는다.
    안전 분류기가 거절하면 같은 요청을 다른 모델이 이어 받도록 fallbacks="default"를 켠다(Opus 5.5·Sonnet 5.5)"""

    FALLBACK_MODELS = ("claude-opus-5-5", "claude-sonnet-5-5")

    def __init__(self, api_key: str, model: str = "claude-opus-5-5", timeout: float = 90, post: Post = http_post_json,
                 price: tuple[Decimal, Decimal] | None = None, usd_krw: Decimal = Decimal(1400), effort: str = "low"):
        if not api_key:
            raise AiError("Claude로 판단하려면 ANTHROPIC_API_KEY 환경 변수에 키를 넣어 주세요.")
        self.api_key, self.model, self.timeout, self.post, self.effort = api_key, model_path(model), timeout, post, effort
        self.price = price or ANTHROPIC_PRICE.get(model, PRICE_FALLBACK["anthropic"])
        self.usd_krw = usd_krw
        self.label = f"anthropic:{model}"

    def ask(self, prompt: str, actions: tuple[str, ...] = ACTIONS["spot"]) -> Reply:
        body = {"model": self.model, "max_tokens": 8000, "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": prompt}],
                "output_config": {"effort": self.effort,
                                  "format": {"type": "json_schema", "schema": response_schema(actions, strict=True)}}}
        headers = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"}
        if self.model in self.FALLBACK_MODELS:
            body["fallbacks"] = "default"
            headers["anthropic-beta"] = "server-side-fallback-2026-07-01"
        data = self.post("https://api.anthropic.com/v1/messages", body, headers, self.timeout)
        usage = data.get("usage") or {}
        if data.get("stop_reason") == "refusal":
            raise AiError(f"Claude가 답을 거절했어요: {(data.get('stop_details') or {}).get('category')}")
        text = "".join(b.get("text", "") for b in data.get("content") or [] if b.get("type") == "text")
        if not text:
            raise AiError(f"Claude 답에 내용이 없어요: {str(data)[:120]}")
        action, reason = parse_reply(text, actions)
        return Reply(action, reason, int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0)))

    def cost_krw(self, reply: Reply) -> Decimal:
        return usd_cost_krw(reply, self.price, self.usd_krw)


class OpenAI:
    """OpenAI Chat Completions. ChatGPT Plus 구독과는 별개로 API 키(종량제)가 있어야 한다.
    추론 모델(gpt-5 계열·o 계열)은 reasoning_effort=low로 짧게 생각하게 한다"""

    def __init__(self, api_key: str, model: str = "gpt-5", timeout: float = 90, post: Post = http_post_json,
                 price: tuple[Decimal, Decimal] | None = None, usd_krw: Decimal = Decimal(1400)):
        if not api_key:
            raise AiError("GPT로 판단하려면 OPENAI_API_KEY 환경 변수에 API 키를 넣어 주세요. ChatGPT Plus 구독으로는 API를 쓸 수 없어요.")
        self.api_key, self.model, self.timeout, self.post = api_key, model_path(model), timeout, post
        self.price = price or OPENAI_PRICE.get(model, PRICE_FALLBACK["openai"])
        self.usd_krw = usd_krw
        self.label = f"openai:{model}"

    def ask(self, prompt: str, actions: tuple[str, ...] = ACTIONS["spot"]) -> Reply:
        body = {"model": self.model, "max_completion_tokens": 4000,
                "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}],
                "response_format": {"type": "json_schema", "json_schema": {
                    "name": "trade_decision", "strict": True, "schema": response_schema(actions, strict=True)}}}
        if self.model.startswith(("gpt-5", "o")):
            body["reasoning_effort"] = "low"
        data = self.post("https://api.openai.com/v1/chat/completions", body, {"Authorization": f"Bearer {self.api_key}"},
                         self.timeout)
        usage = data.get("usage") or {}
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as e:
            raise AiError(f"GPT 답에 내용이 없어요: {str(data)[:120]}") from e
        if message.get("refusal"):
            raise AiError(f"GPT가 답을 거절했어요: {str(message['refusal'])[:80]}")
        action, reason = parse_reply(str(message.get("content") or ""), actions)
        return Reply(action, reason, int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0)))

    def cost_krw(self, reply: Reply) -> Decimal:
        return usd_cost_krw(reply, self.price, self.usd_krw)


class Ollama:
    """맥북·집 PC에서 띄운 Ollama. 돈이 들지 않아 월 예산에 0원으로 잡힌다"""

    def __init__(self, base_url: str = "http://localhost:11434", model: str = "qwen2.5:7b", timeout: float = 60,
                 post: Post = http_post_json):
        self.base_url, self.model, self.timeout, self.post = base_url.rstrip("/"), model, timeout, post
        self.label = f"ollama:{model}"

    def ask(self, prompt: str, actions: tuple[str, ...] = ACTIONS["spot"]) -> Reply:
        body = {"model": self.model, "stream": False, "format": response_schema(actions), "options": {"temperature": 0.2},
                "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}]}
        data = self.post(self.base_url + "/api/chat", body, {}, self.timeout)
        action, reason = parse_reply(str((data.get("message") or {}).get("content", "")), actions)
        return Reply(action, reason, int(data.get("prompt_eval_count", 0)), int(data.get("eval_count", 0)))

    def cost_krw(self, reply: Reply) -> Decimal:
        return Decimal(0)


def model_path(model: str) -> str:
    if not model.replace("-", "").replace(".", "").replace("_", "").isalnum():
        raise AiError(f"모델 이름이 이상해요: {model}")
    return model


class Budget:
    """하루 호출 수와 이번 달 추정 비용을 파일 하나에 모은다. 코인·거래소마다 따로 도는 봉들이 같은 파일을 쓴다"""

    def __init__(self, path: Path, daily_calls: int, monthly_krw: Decimal, now: Callable[[], datetime] = lambda: datetime.now(KST)):
        self.path, self.daily_calls, self.monthly_krw, self.now = path, daily_calls, monthly_krw, now

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _today(self) -> tuple[str, str]:
        d = self.now().astimezone(KST)
        return d.strftime("%Y-%m-%d"), d.strftime("%Y-%m")

    def used(self) -> tuple[int, Decimal]:
        """오늘 호출 수, 이번 달 추정 비용(원)"""
        days = self._read().get("days", {})
        today, month = self._today()
        calls = int(days.get(today, {}).get("calls", 0))
        cost = sum((Decimal(v.get("cost_krw", "0")) for k, v in days.items() if k.startswith(month)), Decimal(0))
        return calls, cost

    def blocked(self) -> str:
        """넘었으면 이유, 아니면 빈 문자열"""
        calls, cost = self.used()
        if calls >= self.daily_calls:
            return f"오늘 AI 호출 상한 {self.daily_calls}회를 다 썼어요"
        if cost >= self.monthly_krw:
            return f"이번 달 AI 예산 {self.monthly_krw:,.0f}원을 다 썼어요(추정 {cost:,.0f}원)"
        return ""

    def record(self, cost_krw: Decimal) -> None:
        data = self._read()
        days = data.setdefault("days", {})
        today, _ = self._today()
        day = days.setdefault(today, {"calls": 0, "cost_krw": "0"})
        day["calls"] = int(day.get("calls", 0)) + 1
        day["cost_krw"] = str(Decimal(day.get("cost_krw", "0")) + cost_krw)
        # 지난달보다 오래된 날은 지운다
        keep = (self.now().astimezone(KST) - timedelta(days=62)).strftime("%Y-%m-%d")
        data["days"] = {k: v for k, v in days.items() if k >= keep}
        # 여러 봇이 같은 파일을 쓰니 임시 파일 이름에 프로세스 번호를 붙인다. 동시에 쓰면 한두 번은 덜 셀 수 있다
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)


@dataclass
class Account:
    """AI에게 보여 줄 계좌 모습. 부를 때마다 지금 값을 읽는다"""
    cash: Callable[[], Decimal]  # 새로 들어갈 때 쓸 수 있는 돈 (선물은 증거금을 뺀 잔고)
    currency: str
    fee_rate: Decimal
    market: str = "spot"
    leverage: Decimal = Decimal(1)


class AiStrategy:
    """규칙 전략을 감싼다. 규칙은 늘 봉을 받아 이동평균을 데우고, AI를 못 쓸 때 그대로 판단한다.
    Session.trade_coin이 부르는 on_bar(현물)·on_bar_futures(선물) 모양을 그대로 따른다"""

    def __init__(self, rule, client: Client, budget: Budget, interval: str, since: dict[str, str], account: Account,
                 history: int = 60, log: Callable[[str], None] = lambda m: print(m, file=sys.stderr)):
        self.rule, self.client, self.budget, self.interval = rule, client, budget, interval
        self.since, self.account, self.history, self.log = dict(since), account, history, log
        self.bars: dict[str, list[Bar]] = {}
        self.warned = False

    def _seen(self, bar: Bar) -> bool:
        """봉을 쌓고, 상태 파일에 남은 봉(데우기 구간)이면 True"""
        seen = self.bars.setdefault(bar.symbol, [])
        seen.append(bar)
        del seen[:-self.history]
        return bar.date <= self.since.get(bar.symbol, "")

    def _ask(self, bar: Bar, position: str, hint: str) -> str | None:
        """AI의 행동을 돌려준다. 상한을 넘었으면 None(규칙으로), 답이 이상하면 "HOLD"(관망)"""
        blocked = self.budget.blocked()
        if blocked:
            if not self.warned:
                self.log(f"{blocked}. 규칙 매매로 내려가요.")
                self.warned = True
            return None
        actions = ACTIONS[self.account.market]
        try:
            reply = self.client.ask(self.prompt(bar, position, hint), actions)
        except (AiError, OSError, ValueError, KeyError, TypeError) as e:
            self.budget.record(Decimal(0))
            self.log(f"AI 판단을 받지 못해 관망해요 ({bar.symbol} {bar.date}): {e}")
            return "HOLD"
        self.budget.record(self.client.cost_krw(reply))
        self.reason = "AI: " + reply.reason
        return reply.action

    def on_bar(self, bar: Bar, holding: bool, avg_price: Decimal) -> Signal | None:
        rule_signal = self.rule.on_bar(bar, holding, avg_price)
        if self._seen(bar):
            return rule_signal
        stop = self.rule.stop_loss
        if holding and avg_price > 0 and bar.close <= avg_price * (1 - stop):
            return Signal("SELL", f"손절선 -{stop * 100:.0f}% 도달")  # 손절은 AI에게 묻지 않고 규칙대로
        position = f"보유 중, 평균 단가 {avg_price:f} {self.account.currency}" if holding else "보유 없음"
        action = self._ask(bar, position, f"{rule_signal.side} ({rule_signal.reason})" if rule_signal else "신호 없음")
        if action is None:
            return rule_signal
        if action == "BUY" and not holding or action == "SELL" and holding:
            return Signal(action, self.reason)
        return None

    def on_bar_futures(self, bar: Bar, side: str | None, entry: Decimal) -> list[Signal]:
        rule_signals = self.rule.on_bar_futures(bar, side, entry)
        if self._seen(bar):
            return rule_signals
        stop = self.rule.stop_loss
        if side == "LONG" and bar.close <= entry * (1 - stop) or side == "SHORT" and bar.close >= entry * (1 + stop):
            return [Signal("CLOSE", f"손절선 -{stop * 100:.0f}% 도달")]
        position = f"{side} 보유 중, 진입가 {entry:f} {self.account.currency}" if side else "포지션 없음"
        action = self._ask(bar, position, ", ".join(f"{s.side} ({s.reason})" for s in rule_signals) or "신호 없음")
        if action is None:
            return rule_signals
        if action == "CLOSE":
            return [Signal("CLOSE", self.reason)] if side else []
        if action in ("LONG", "SHORT") and side != action:
            return ([Signal("CLOSE", self.reason)] if side else []) + [Signal(action, self.reason)]
        return []

    def prompt(self, bar: Bar, position: str, hint: str) -> str:
        a = self.account
        closes = "\n".join(f"{b.date} {b.close:f}" for b in self.bars[bar.symbol])
        market = "현물" if a.market == "spot" else f"USDT-M 선물 {a.leverage:f}배"
        return (f"마켓: {bar.symbol} ({bar.name}), {market}, 봉: {self.interval}, 통화: {a.currency}\n"
                f"쓸 수 있는 현금: {a.cash():f} {a.currency}, 수수료율: {a.fee_rate:f}\n"
                f"포지션: {position}\n이동평균 규칙의 신호(참고): {hint}\n"
                f"고를 수 있는 행동: {ACTION_HELP[a.market]}\n"
                f"최근 {len(self.bars[bar.symbol])}개 봉 종가 (봉 시작 시각 UTC, 오래된 것부터):\n{closes}\n"
                'JSON으로 답해 주세요: {"action": "...", "reason": "..."}')


def add_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("AI 매매 판단 (#263)")
    g.add_argument("--ai", choices=("on", "off"), help="AI 매매 판단. 기본은 환경 변수 AI_DECISION, 없으면 off")
    g.add_argument("--ai-provider", choices=PROVIDERS, help="기본은 환경 변수 AI_PROVIDER, 없으면 gemini")
    g.add_argument("--ai-model", help="기본 gemini-2.5-flash-lite, anthropic은 claude-opus-5-5, openai는 gpt-5, "
                                      "Ollama는 qwen2.5:7b (환경 변수 AI_MODEL)")
    g.add_argument("--ai-price-in", help="모델 입력 가격(100만 토큰당 달러). 표에 없는 모델이면 넣어 주세요")
    g.add_argument("--ai-price-out", help="모델 출력 가격(100만 토큰당 달러, 생각 토큰 포함)")
    g.add_argument("--ai-daily-calls", type=int, default=int(os.environ.get("AI_DAILY_CALLS", "2000")),
                   help="하루 AI 호출 상한. 모든 코인·거래소 합계 (기본 2000)")
    g.add_argument("--ai-monthly-krw", default=os.environ.get("AI_MONTHLY_KRW", "30000"), help="월 AI 예산(원). 기본 30000")
    g.add_argument("--ai-usage", default=os.environ.get("AI_USAGE_FILE", "trade-ai-usage.json"),
                   help="AI 사용량 파일. 여러 봇이 같은 파일을 써야 상한이 합쳐져요")


def enabled(args: argparse.Namespace) -> bool:
    value = args.ai or os.environ.get("AI_DECISION", "off")
    return value.strip().lower() in ("on", "1", "true", "yes")


def eligible(interval: str, intervals: dict[str, timedelta]) -> bool:
    return interval in intervals and intervals[interval] >= MIN_AI_INTERVAL


PROVIDERS = ("gemini", "anthropic", "openai", "ollama")


def make_client(args: argparse.Namespace) -> Client:
    provider = (args.ai_provider or os.environ.get("AI_PROVIDER", "gemini")).lower()
    if provider not in PROVIDERS:
        raise AiError(f"AI 제공자는 {', '.join(PROVIDERS)} 중 하나예요.")
    model = args.ai_model or os.environ.get("AI_MODEL")
    price_in, price_out = getattr(args, "ai_price_in", None), getattr(args, "ai_price_out", None)
    price = (Decimal(price_in), Decimal(price_out)) if price_in and price_out else None
    if provider == "ollama":
        return Ollama(os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"), model or "qwen2.5:7b")
    if provider == "anthropic":
        return Anthropic(os.environ.get("ANTHROPIC_API_KEY", ""), model or "claude-opus-5-5", price=price)
    if provider == "openai":
        return OpenAI(os.environ.get("OPENAI_API_KEY", ""), model or "gpt-5", price=price)
    return Gemini(os.environ.get("GEMINI_API_KEY", ""), model or "gemini-2.5-flash-lite", price=price)


def wrap(args: argparse.Namespace, rule, interval: str, intervals: dict[str, timedelta], broker) -> tuple[object, str]:
    """AI가 켜져 있고 봉이 15분 이상이면 규칙 전략을 감싼 AiStrategy와 그 이름을, 아니면 규칙 전략을 그대로 돌려준다.
    키가 없으면 AiError를 낸다(켜 놓고 키를 빠뜨린 걸 조용히 넘기지 않는다)"""
    if not enabled(args):
        return rule, args.strategy
    if not eligible(interval, intervals):
        print(f"{interval}봉은 AI 판단 없이 규칙으로 매매해요 (AI는 15분봉 이상).", file=sys.stderr)
        return rule, args.strategy
    client = make_client(args)
    budget = Budget(Path(args.ai_usage), args.ai_daily_calls, Decimal(args.ai_monthly_krw))
    market = getattr(broker, "market", "spot")
    cash = broker.available if market == "futures" else (lambda: broker.cash)
    account = Account(cash, broker.currency, broker.fee_rate, market, getattr(broker, "leverage", Decimal(1)))
    return (AiStrategy(rule, client, budget, interval, broker.last_seen, account),
            f"{args.strategy}+ai:{client.label}"[:60])
