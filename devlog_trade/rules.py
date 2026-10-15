"""대화하는 AI가 MCP(trade_bot_rules)로 정한 매매 기준. 봇은 jobs.conf의 줄을 돌릴 때 계좌마다 이 값을 덮어 쓴다.

- 판단은 사람의 AI가, 주문은 봇 코드가 상한 안에서 낸다(규칙형). 이 파일은 그 경계다.
- 모의 계좌(trade-state-…)만 받는다. 실계좌 설정은 대화로 바꾸지 않는다.
- 파일은 봇 폴더의 trade-rules.json(TRADE_RULES로 바꿈). git에 올리지 않는다.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .broker import state_lock

FIELDS = ("coins", "window", "stop_loss", "take_profit", "daily_profit_stop", "budget", "paused")
LADDER_FIELDS = ("coins", "paused")  # 거름망 기법은 자기 규칙이 있어 코인과 멈춤만 바꾼다
MAX_COINS = 10
WINDOW = (2, 60)
STOP_LOSS = (Decimal("0.005"), Decimal("0.2"))
TAKE_PROFIT = (Decimal("0.005"), Decimal("0.5"))  # 0은 끔(평균선이 꺾일 때 정리)
DAILY_PROFIT_STOP = (Decimal("0.005"), Decimal("0.2"))  # 0은 끔
RISK_PER_TRADE = Decimal("0.01")  # 세 겹 거름망 복리법(2026-10-15 민서님): 한 번 손실은 처음 현금의 1%까지
ACCOUNT = re.compile(r"^trade-state-[A-Za-z0-9_.-]+\.json$")
COIN = re.compile(r"^[A-Z0-9]{2,15}$")


class RuleError(ValueError):
    """기준 값이 상한을 넘거나 형식이 틀렸을 때. MCP가 이유를 그대로 돌려준다"""


def path() -> Path:
    return Path(os.environ.get("TRADE_RULES", "trade-rules.json"))


def load() -> dict:
    """{"accounts": {계좌: {필드: 값, "reason", "changed_at"}}}. 없거나 깨졌으면 빈 기준"""
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"accounts": {}}
    return data if isinstance(data.get("accounts"), dict) else {"accounts": {}}


def for_account(account: str) -> dict:
    got = load()["accounts"].get(account)
    return got if isinstance(got, dict) else {}


def clean(account: str, values: dict, ladder: bool, max_budget: Decimal) -> dict:
    """들어온 값을 검사해 저장할 모양으로 바꾼다. 상한은 코드가 정하고 AI가 넘지 못한다"""
    if not ACCOUNT.match(account or ""):
        raise RuleError("기준은 모의 계좌(trade-state-…json)만 바꿀 수 있어요. 실계좌는 대화로 바꾸지 않아요.")
    allowed = LADDER_FIELDS if ladder else FIELDS
    out: dict = {}
    for key, value in values.items():
        if value is None:
            continue
        if key not in allowed:
            raise RuleError(f"이 계좌에서는 {', '.join(allowed)}만 바꿀 수 있어요. 세 겹 거름망은 코인과 멈춤만 돼요.")
        if key == "coins":
            coins = [c.strip().upper() for c in str(value).split(",") if c.strip()]
            if not coins or len(coins) > MAX_COINS or not all(COIN.match(c) for c in coins):
                raise RuleError(f"coins는 BTC,ETH처럼 쉼표로 1~{MAX_COINS}개예요.")
            out[key] = ",".join(dict.fromkeys(coins))
        elif key == "window":
            try:
                n = int(value)
            except (TypeError, ValueError):
                raise RuleError("window는 정수예요.") from None
            if not WINDOW[0] <= n <= WINDOW[1]:
                raise RuleError(f"window는 {WINDOW[0]}~{WINDOW[1]}봉 사이예요.")
            out[key] = n
        elif key in ("take_profit", "daily_profit_stop"):
            try:
                d = Decimal(str(value))
            except InvalidOperation:
                raise RuleError(f"{key}는 숫자예요.") from None
            low, high = TAKE_PROFIT if key == "take_profit" else DAILY_PROFIT_STOP
            if d != 0 and not low <= d <= high:
                raise RuleError(f"{key}는 0(끔) 또는 {low}~{high}({low * 100:g}%~{high * 100:g}%) 사이예요.")
            out[key] = f"{d.normalize():f}"
        elif key in ("stop_loss", "budget"):
            try:
                d = Decimal(str(value))
            except InvalidOperation:
                raise RuleError(f"{key}는 숫자예요.") from None
            if key == "stop_loss" and not STOP_LOSS[0] <= d <= STOP_LOSS[1]:
                raise RuleError(f"stop_loss는 {STOP_LOSS[0]}~{STOP_LOSS[1]}(0.5%~20%) 사이예요.")
            if key == "budget" and not Decimal(0) < d <= max_budget:
                raise RuleError(f"budget은 0보다 크고 봇 기본값 {max_budget:f} 이하예요. AI는 비중을 줄이기만 할 수 있어요.")
            out[key] = f"{d.normalize():f}"
        elif key == "paused":
            out[key] = bool(value)
    return out


def check_risk(budget: Decimal, stop_loss: Decimal, leverage: Decimal, cash: Decimal) -> None:
    """손절에 걸렸을 때 잃는 돈(금액 × 레버리지 × 손절)이 처음 현금의 1%를 넘으면 거절한다"""
    loss = budget * leverage * stop_loss
    if loss > cash * RISK_PER_TRADE:
        raise RuleError(f"손절에 걸리면 한 번에 {loss.normalize():f}를 잃어 상한(처음 현금 {cash.normalize():f}의 1%)을 넘어요. "
                        "금액이나 손절 폭을 줄여 주세요.")


def save(account: str, values: dict, reason: str, now: datetime) -> dict:
    """계좌 기준을 합쳐 쓴다. 빈 values면 그 계좌 기준을 지워 jobs.conf대로 돌린다"""
    p = path()
    with state_lock(p):
        data = load()
        if values:
            row = {**data["accounts"].get(account, {}), **values,
                   "reason": " ".join(str(reason).split())[:200], "changed_at": now.isoformat(timespec="seconds")}
            data["accounts"][account] = row
        else:
            data["accounts"].pop(account, None)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)
    return data["accounts"].get(account, {})


def apply(args, account: str) -> dict:
    """coin 실행 직전에 계좌 기준을 args에 덮어 쓴다. 쓴 기준을 돌려준다(없으면 빈 dict)"""
    row = for_account(account)
    if row.get("coins"):
        args.coins, args.symbol = row["coins"], None
    if args.rule != "ladder":
        if row.get("window"):
            args.window = int(row["window"])
        if row.get("stop_loss"):
            args.stop_loss = row["stop_loss"]
        if row.get("budget"):
            args.budget = row["budget"]
        for key in ("take_profit", "daily_profit_stop"):  # "0"도 jobs.conf 값을 덮어 끈다
            if row.get(key) is not None:
                setattr(args, key, row[key])
    return row
