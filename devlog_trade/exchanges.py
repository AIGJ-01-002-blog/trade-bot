"""코인 거래소 공개 시세 (#255). API 키 없이 캔들(봉)만 받는다. 주문·잔고 API는 부르지 않는다.

거래소마다 Exchange를 하나씩 둔다. 바이비트도 같은 틀(fetch_candles)로 붙인다.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable

# 봉 간격 → 길이. 바이낸스·업비트가 함께 주는 간격만 둔다
INTERVALS: dict[str, timedelta] = {
    "1m": timedelta(minutes=1), "5m": timedelta(minutes=5), "15m": timedelta(minutes=15), "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1), "4h": timedelta(hours=4), "1d": timedelta(days=1),
}

COIN_NAMES = {"BTC": "비트코인", "ETH": "이더리움", "XRP": "리플", "SOL": "솔라나", "DOGE": "도지코인"}
COINS = list(COIN_NAMES)  # 기본으로 거래하는 코인


class ExchangeError(RuntimeError):
    pass


@dataclass(frozen=True)
class Candle:
    open_time: datetime  # UTC
    close_time: datetime  # UTC. 이 시각이 지나야 다 만들어진 봉이다
    close: Decimal


Fetch = Callable[[str], object]


def http_get_json(url: str, timeout: float = 15) -> object:
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "devlog-trade-bot"})
    try:
        res = urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 (거래소 공개 시세 주소만 부른다)
    except urllib.error.HTTPError as e:
        if e.code in (403, 451):  # 바이낸스는 서비스하지 않는 지역(미국 등)에서 451을 준다
            raise ExchangeError(f"이 지역에서는 이 시세 주소가 막혀 있어요 (HTTP {e.code}). 한국 등 서비스 지역에서 돌려 주세요.") from e
        raise
    with res:
        try:
            return json.loads(res.read().decode("utf-8"))
        except ValueError as e:
            raise ExchangeError(f"거래소 응답을 읽지 못했어요: {e}") from e


class Exchange:
    code = ""  # record_trade의 exchange
    currency = ""  # 가격·수수료·손익의 통화
    fee_rate = Decimal(0)  # 일반 회원 기본 수수료율. 쓰는 등급에 맞게 --fee-rate로 바꾼다
    market = "spot"  # spot | futures
    default_cash = Decimal(0)
    default_budget = Decimal(0)

    def __init__(self, fetch: Fetch = http_get_json):
        self.fetch = fetch

    def base_asset(self, symbol: str) -> str:
        raise NotImplementedError

    def symbol_for(self, coin: str) -> str:
        """코인 이름(BTC)을 이 거래소의 마켓 코드로"""
        raise NotImplementedError

    def coin_name(self, symbol: str) -> str:
        base = self.base_asset(symbol)
        name = COIN_NAMES.get(base, base)
        return name + " 선물" if self.market == "futures" else name

    def fetch_candles(self, symbol: str, interval: str, limit: int, now: datetime | None = None) -> list[Candle]:
        """다 만들어진 봉만 오래된 순으로 돌려준다. 아직 진행 중인 마지막 봉은 뺀다"""
        if interval not in INTERVALS:
            raise ExchangeError(f"봉 간격은 {', '.join(INTERVALS)} 중 하나예요.")
        now = now or datetime.now(timezone.utc)
        candles = sorted(self._candles(symbol, interval, limit), key=lambda c: c.open_time)
        return [c for c in candles if c.close_time <= now]

    def _candles(self, symbol: str, interval: str, limit: int) -> list[Candle]:
        raise NotImplementedError


class Binance(Exchange):
    """현물 시세. data-api.binance.vision은 바이낸스가 시세 조회용으로 따로 연 주소라 키도 지역 제한도 없다"""

    code = "BINANCE"
    currency = "USDT"
    fee_rate = Decimal("0.001")
    default_cash = Decimal(1000)
    default_budget = Decimal(100)
    base_url = "https://data-api.binance.vision"
    path = "/api/v3/klines"

    def base_asset(self, symbol: str) -> str:
        return symbol[: -len(self.currency)] if symbol.endswith(self.currency) else symbol

    def symbol_for(self, coin: str) -> str:
        return coin.upper() + self.currency

    def _candles(self, symbol: str, interval: str, limit: int) -> list[Candle]:
        if not symbol.endswith(self.currency):
            raise ExchangeError(f"바이낸스는 {self.currency} 마켓만 받아요. 예: BTCUSDT")
        query = urllib.parse.urlencode({"symbol": symbol, "interval": interval, "limit": min(limit, 1000)})
        rows = self.fetch(f"{self.base_url}{self.path}?{query}")
        if not isinstance(rows, list):
            raise ExchangeError(f"바이낸스 시세를 받지 못했어요: {rows}")
        step = INTERVALS[interval]
        out = []
        for r in rows:  # [여는 시각(ms), 시가, 고가, 저가, 종가, 거래량, 닫는 시각(ms), …]
            opened = datetime.fromtimestamp(int(r[0]) / 1000, timezone.utc)
            out.append(Candle(opened, opened + step, Decimal(str(r[4]))))
        return out


class Upbit(Exchange):
    code = "UPBIT"
    currency = "KRW"
    fee_rate = Decimal("0.0005")
    default_cash = Decimal(1_000_000)
    default_budget = Decimal(100_000)
    base_url = "https://api.upbit.com"
    PATHS = {"1m": "minutes/1", "5m": "minutes/5", "15m": "minutes/15", "30m": "minutes/30", "1h": "minutes/60",
             "4h": "minutes/240", "1d": "days"}

    def base_asset(self, symbol: str) -> str:
        return symbol.split("-", 1)[1] if "-" in symbol else symbol

    def symbol_for(self, coin: str) -> str:
        return f"{self.currency}-{coin.upper()}"

    def _candles(self, symbol: str, interval: str, limit: int) -> list[Candle]:
        if not symbol.startswith(self.currency + "-"):
            raise ExchangeError(f"업비트는 {self.currency} 마켓만 받아요. 예: KRW-BTC")
        query = urllib.parse.urlencode({"market": symbol, "count": min(limit, 200)})
        rows = self.fetch(f"{self.base_url}/v1/candles/{self.PATHS[interval]}?{query}")
        if not isinstance(rows, list):
            raise ExchangeError(f"업비트 시세를 받지 못했어요: {rows}")
        step = INTERVALS[interval]
        out = []
        for r in rows:  # 최신 봉이 먼저 온다. candle_date_time_utc가 봉 시작 시각
            opened = datetime.fromisoformat(r["candle_date_time_utc"]).replace(tzinfo=timezone.utc)
            out.append(Candle(opened, opened + step, Decimal(str(r["trade_price"]))))
        return out


class BinanceFutures(Binance):
    """USDT-M 무기한 선물 시세. 선물 공개 시세는 fapi 주소에만 있다(지역에 따라 막힐 수 있다).
    매매 일지의 거래소는 현물과 같은 BINANCE로 남기고, 이름·이유에 "선물"을 붙여 구분한다"""

    market = "futures"
    fee_rate = Decimal("0.0005")  # 일반 회원 테이커
    base_url = "https://fapi.binance.com"
    path = "/fapi/v1/klines"


# (거래소, 시장) → 어댑터. 업비트는 선물이 없다. 바이비트는 여기에 더한다
EXCHANGES: dict[tuple[str, str], type[Exchange]] = {
    ("binance", "spot"): Binance, ("binance", "futures"): BinanceFutures, ("upbit", "spot"): Upbit}
