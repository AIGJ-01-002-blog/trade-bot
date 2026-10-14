"""devlog 매매 일지로 체결 기록 보내기 (#243). MCP 도구 record_trade를 JSON-RPC로 부른다.
코인 체결(#255)은 거래소(exchange)와 통화(currency)를 함께 보낸다. devlog #254가 받는 모양이다.
토큰은 devlog › AI 연결에서 만든 쓰기 토큰을 환경 변수 DEVLOG_TOKEN으로만 넘긴다. 파일이나 저장소에 적지 않는다.
"""
from __future__ import annotations

import json
import urllib.request
from decimal import Decimal

from .broker import Fill


class JournalError(RuntimeError):
    pass


def _num(v: Decimal) -> str:
    """0.00012345를 1.2345E-4로 쓰지 않도록 늘 고정 소수점으로 보낸다"""
    return format(v, "f")


def payload(fill: Fill, mode: str = "PAPER", strategy: str = "") -> dict:
    args: dict = {
        "external_id": fill.external_id,
        "mode": mode,
        "symbol": fill.symbol,
        "symbol_name": fill.symbol_name,
        "side": fill.side,
        "quantity": _num(fill.quantity),
        "price": _num(fill.price),
        "currency": fill.currency or "KRW",
        "fee": _num(fill.fee),
        "strategy": strategy,
        "reason": fill.reason,
        "executed_at": fill.executed_at,
    }
    if fill.exchange:
        args["exchange"] = fill.exchange
    if fill.realized_pnl is not None:
        args["realized_pnl"] = _num(fill.realized_pnl)
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "record_trade", "arguments": args}}


class Journal:
    def __init__(self, server: str, token: str, timeout: float = 15):
        if not token:
            raise JournalError("DEVLOG_TOKEN 환경 변수에 devlog 쓰기 토큰을 넣어 주세요.")
        self.url = server.rstrip("/") + "/api/mcp"
        self.token = token
        self.timeout = timeout

    def record(self, fill: Fill, mode: str = "PAPER", strategy: str = "") -> str:
        body = json.dumps(payload(fill, mode, strategy), default=lambda v: str(v) if isinstance(v, Decimal) else v).encode("utf-8")
        req = urllib.request.Request(self.url, data=body, method="POST", headers={
            "Content-Type": "application/json", "Accept": "application/json", "Authorization": "Bearer " + self.token})
        with urllib.request.urlopen(req, timeout=self.timeout) as res:  # noqa: S310 (devlog 주소만 부른다)
            try:
                data = json.loads(res.read().decode("utf-8"))
            except ValueError as e:  # 프록시 오류 화면처럼 JSON이 아닌 응답
                raise JournalError(f"devlog 응답을 읽지 못했어요: {e}") from e
        result = data.get("result") or {}
        text = "".join(c.get("text", "") for c in result.get("content", []))
        if data.get("error") or result.get("isError"):
            raise JournalError(text or str(data.get("error")))
        return text
