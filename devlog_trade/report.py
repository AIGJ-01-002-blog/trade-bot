"""devlog 자동매매 화면(devlog.life/trade, devlog spec 101)에 봇 상태 알리기. MCP 도구 report_trade_bot을 JSON-RPC로 부른다.

- jobs가 한 바퀴를 다 돈 뒤 한 번 부른다. 모의 계좌의 현금·포지션·평가 손익과 적용 중인 규칙, 새 모의 체결,
  AI 진입 판단(허락·막음), 지난 날의 하루 요약을 보낸다.
- 매매 일지(record_trade)와 다른 전용 표에 남아 공개 글·일지에 섞이지 않는다. 그래서 --dry-run 계좌도 보낸다.
- 실계좌(trade-live-…)는 보내지 않는다. devlog가 실계좌가 섞인 요청을 통째로 거절한다.
- 알리지 못해도(토큰 없음, 도구가 아직 없는 서버, 네트워크) 매매는 멈추지 않는다. 사건은 보관함 파일에 두었다가 다음 바퀴에 다시 보낸다.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable, Iterator

from . import __version__
from .broker import Fill, state_lock
from .journal import JournalError, call_tool

KST = timezone(timedelta(hours=9))
OUTBOX = "trade-bot-report.json"  # 상태 파일과 같은 폴더. 계좌 파일 이름(trade-state-…)과 겹치지 않는다
EVENTS_PER_CALL = 200  # devlog가 한 번에 받는 사건 수
KEEP_EVENTS = 1000  # 오래 못 보내도 보관함이 끝없이 크지 않게 오래된 사건부터 버린다
TOOL = "report_trade_bot"


def is_live(account: str) -> bool:
    return account.startswith("trade-live-")


def _num(v: Decimal) -> str:
    return format(v, "f")


@contextmanager
def _outbox(folder: Path) -> Iterator[dict]:
    """봇(jobs)과 MCP 서버가 같은 보관함을 번갈아 쓴다. 상태 파일처럼 잠그고 읽고 쓴다"""
    path = folder / OUTBOX
    with state_lock(path):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        yield data
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)


def _day(at: str) -> str:
    try:
        return datetime.fromisoformat(at).astimezone(KST).strftime("%Y-%m-%d")
    except ValueError:
        return datetime.now(KST).strftime("%Y-%m-%d")


def add_event(folder: Path, event: dict) -> None:
    """사건 하나를 보관함에 넣고 하루 요약용 수를 센다. 같은 id는 한 번만 넣는다"""
    if is_live(event.get("account", "")):
        return
    try:
        with _outbox(folder) as data:
            events = data.setdefault("events", [])
            if any(e.get("id") == event["id"] for e in events):
                return
            events.append(event)
            del events[:-KEEP_EVENTS]
            day = data.setdefault("days", {}).setdefault(_day(event.get("at", "")), {"fills": 0, "allow": 0, "block": 0, "pnl": {}})
            if event["kind"] == "FILL":
                day["fills"] += 1
                if event.get("realized_pnl") is not None:
                    cur = event.get("currency", "")
                    day["pnl"][cur] = _num(Decimal(day["pnl"].get(cur, "0")) + Decimal(event["realized_pnl"]))
            elif event["kind"] == "AI":
                day["allow" if event["verdict"] == "ALLOW" else "block"] += 1
    except (OSError, TimeoutError) as e:  # 화면 알림 때문에 매매를 멈추지 않는다
        print(f"자동매매 화면용 사건을 남기지 못했어요: {e}")


def fill_event(account: str, market: str, fill: Fill) -> dict:
    side = fill.side
    if market == "futures":  # 선물 체결은 BUY·SELL로 남지만 화면에는 진입 방향과 정리를 나눠 보인다
        side = "CLOSE" if fill.realized_pnl is not None else ("LONG" if fill.side == "BUY" else "SHORT")
    event = {"id": f"fill:{fill.external_id}"[:80], "kind": "FILL", "at": fill.executed_at, "account": account,
             "symbol": fill.symbol, "side": side, "quantity": _num(fill.quantity), "price": _num(fill.price),
             "currency": fill.currency or "KRW", "text": fill.reason[:1000]}
    if fill.realized_pnl is not None:
        event["realized_pnl"] = _num(fill.realized_pnl)
    return event


def ai_event(account: str, symbol: str, bar_date: str, verdict: str, side: str, reason: str, at: str) -> dict:
    """AI 진입 판단. bar_date(봉 시작 시각)로 id를 만들어 같은 봉의 판단은 한 번만 남는다"""
    key = "".join(ch for ch in bar_date if ch.isalnum())
    event = {"id": f"ai:{account[:40]}:{symbol}:{key}"[:80], "kind": "AI", "at": at, "account": account, "symbol": symbol,
             "verdict": verdict, "text": reason[:1000]}
    if side:
        event["side"] = side
    return event


def set_rules(folder: Path, account: str, rules: dict[str, str], ai_model: str = "") -> None:
    """계좌마다 지금 적용 중인 규칙과 AI 판단 모델(꺼져 있으면 빈 값). 다음 알림 때 계좌와 함께 보낸다"""
    if is_live(account):
        return
    try:
        with _outbox(folder) as data:
            data.setdefault("rules", {})[account] = {k[:40]: str(v)[:200] for k, v in rules.items()}
            data.setdefault("ai", {})[account] = ai_model
    except (OSError, TimeoutError) as e:
        print(f"자동매매 화면용 규칙을 남기지 못했어요: {e}")


def _summary(day: str, d: dict) -> dict:
    pnl = ", ".join(f"{v} {k}" for k, v in sorted(d.get("pnl", {}).items())) or "없음"
    text = f"{day} 모의 체결 {d.get('fills', 0)}건 · 실현 손익 {pnl} · AI 허락 {d.get('allow', 0)}건, 막음 {d.get('block', 0)}건"
    return {"id": f"sum:{day}", "kind": "SUMMARY", "at": f"{day}T23:59:59+09:00", "text": text}


def build(folder: Path, accounts: dict, now: datetime) -> tuple[dict, dict]:
    """보낼 내용과, 보낸 뒤 보관함에서 지울 표시를 만든다. accounts는 MCP trade_bot_accounts의 결과 모양"""
    with _outbox(folder) as data:
        snapshot = json.loads(json.dumps(data))
    rules, models = snapshot.get("rules", {}), snapshot.get("ai", {})
    out = []
    for acc in accounts.get("accounts", []):
        name = acc.get("account", "")
        if "error" in acc or is_live(name) or acc.get("mode", "PAPER") != "PAPER":
            continue
        row = {k: v for k, v in acc.items() if k not in ("mode", "unsent_fills", "available", "leverage")}
        row["rules"] = rules.get(name, {})
        out.append(row)
    today = now.astimezone(KST).strftime("%Y-%m-%d")
    done_days = sorted(d for d in snapshot.get("days", {}) if d < today)  # 끝난 날만 요약한다. 같은 id는 한 번만 남기 때문이다
    summaries = [_summary(d, snapshot["days"][d]) for d in done_days]
    events = snapshot.get("events", [])[:EVENTS_PER_CALL - len(summaries)] + summaries
    active = sorted({m for m in models.values() if m})
    payload = {"bot_version": __version__,
               "ai": {"enabled": bool(active), "model": ", ".join(active)[:60], "calls_today": accounts.get("ai", {}).get("calls_today", 0)},
               "accounts": out, "events": events}
    return payload, {"event_ids": [e["id"] for e in events if e["kind"] != "SUMMARY"], "days": done_days}


def _forget(folder: Path, sent: dict) -> None:
    ids = set(sent["event_ids"])
    with _outbox(folder) as data:
        data["events"] = [e for e in data.get("events", []) if e.get("id") not in ids]
        for d in sent["days"]:
            data.get("days", {}).pop(d, None)


def report(folder: Path, accounts: Callable[[], dict], server: str, token: str, now: datetime | None = None,
           send: Callable[[str, str, str, dict], str] | None = None) -> bool:
    """한 바퀴 뒤 한 번 알린다. 실패해도 예외를 올리지 않고 False만 돌려준다(매매는 이어 간다)"""
    if not token:
        print("자동매매 화면에 알리지 않아요 (DEVLOG_TOKEN 없음).")
        return False
    send = send or (lambda url, tok, name, args: call_tool(url, tok, name, args))
    try:
        payload, sent = build(folder, accounts(), now or datetime.now(KST))
        send(server.rstrip("/") + "/api/mcp", token, TOOL, payload)
        _forget(folder, sent)
    except (JournalError, OSError, TimeoutError, ValueError) as e:
        print(f"자동매매 화면에 알리지 못했어요 (매매는 그대로, 다음 바퀴에 다시 보내요): {e}")
        return False
    print(f"자동매매 화면에 알렸어요: 계좌 {len(payload['accounts'])}개, 사건 {len(payload['events'])}건")
    return True


def enabled() -> bool:
    """TRADE_REPORT=off면 알리지 않는다. 기본은 켜짐(DEVLOG_TOKEN이 있을 때만 실제로 보낸다)"""
    return os.environ.get("TRADE_REPORT", "on").strip().lower() not in ("off", "0", "false", "no")
