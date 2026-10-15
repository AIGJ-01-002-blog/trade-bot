"""백테스트용 1시간봉·펀딩비 받기. 표준 라이브러리만 쓴다(봇과 같은 원칙).

python research/fetch_data.py --out research/data --start 2021-01 --end 2026-09

- 바이낸스 현물·USDT-M 선물: data.binance.vision 월별 압축 파일(공개 자료, 키 없음). API(api.binance.com·fapi)는
  미국 등에서 451로 막혀 GitHub 러너에서도 받을 수 있는 이쪽을 쓴다.
- 업비트: 공개 캔들 API(/v1/candles/minutes/60)를 200개씩 거꾸로 받는다. 초당 10번 제한 안으로 쉰다.
- 4시간·1일봉은 1시간봉을 UTC 기준으로 묶어 만든다(두 거래소 모두 UTC 0시에 하루가 바뀐다).
"""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

COINS = ["BTC", "ETH", "XRP", "SOL", "DOGE"]
VISION = "https://data.binance.vision/data"
UA = {"User-Agent": "devlog-trade-bot-research/1"}


def months(start: str, end: str) -> list[str]:
    y, m = map(int, start.split("-"))
    ey, em = map(int, end.split("-"))
    out = []
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def get(url: str, tries: int = 4) -> bytes | None:
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:  # noqa: S310
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None  # 그달 자료가 없음(상장 전 등)
            if e.code == 429 or e.code >= 500:
                time.sleep(2 ** i)
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            time.sleep(2 ** i)
    raise RuntimeError(f"받지 못했어요: {url}")


def _ms(v: str) -> int:
    t = int(float(v))
    return t // 1000 if t > 10**14 else t  # 2025년부터 현물 자료는 마이크로초


def vision_rows(path: str, symbol: str, kind: str, span: list[str]) -> list[list[str]]:
    rows: list[list[str]] = []
    for ym in span:
        name = f"{symbol}-1h-{ym}.zip" if kind == "klines" else f"{symbol}-fundingRate-{ym}.zip"
        sub = f"{path}/{kind}/{symbol}/1h/{name}" if kind == "klines" else f"{path}/{kind}/{symbol}/{name}"
        raw = get(f"{VISION}/{sub}")
        if raw is None:
            continue
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            text = z.read(z.namelist()[0]).decode("utf-8")
        for r in csv.reader(io.StringIO(text)):
            if r and r[0][:1].isdigit():  # 머리줄 건너뛰기(선물 자료에 있음)
                rows.append(r)
    return rows


def write(path: Path, header: list[str], rows: list[list]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    print(f"{path} {len(rows)}줄", flush=True)


def binance(out: Path, span: list[str]) -> None:
    for coin in COINS:
        sym = f"{coin}USDT"
        for market, path in (("spot", "spot/monthly"), ("futures", "futures/um/monthly")):
            rows = vision_rows(path, sym, "klines", span)
            data = {_ms(r[0]): [_ms(r[0]), r[1], r[2], r[3], r[4], r[5]] for r in rows}
            write(out / f"binance-{market}" / f"{coin}-1h.csv.gz", ["ts", "open", "high", "low", "close", "volume"],
                  [data[k] for k in sorted(data)])
        fr = vision_rows("futures/um/monthly", sym, "fundingRate", span)
        data = {_ms(r[0]): [_ms(r[0]), r[2]] for r in fr}
        write(out / "binance-futures" / f"{coin}-funding.csv.gz", ["ts", "rate"], [data[k] for k in sorted(data)])


def upbit(out: Path, start: str, end: str) -> None:
    first = datetime.strptime(start + "-01", "%Y-%m-%d").replace(tzinfo=timezone.utc)
    ey, em = map(int, end.split("-"))
    last = datetime(ey + (em == 12), em % 12 + 1, 1, tzinfo=timezone.utc)
    for coin in COINS:
        market, to, data = f"KRW-{coin}", last, {}
        while to > first:
            url = (f"https://api.upbit.com/v1/candles/minutes/60?market={market}&count=200"
                   f"&to={to.strftime('%Y-%m-%dT%H:%M:%SZ')}")
            raw = get(url)
            batch = json.loads(raw or b"[]")
            time.sleep(0.15)
            if not batch:
                break
            for c in batch:
                t = datetime.strptime(c["candle_date_time_utc"], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
                if first <= t < last:
                    ms = int(t.timestamp() * 1000)
                    data[ms] = [ms, c["opening_price"], c["high_price"], c["low_price"], c["trade_price"], c["candle_acc_trade_volume"]]
            oldest = min(datetime.strptime(c["candle_date_time_utc"], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc) for c in batch)
            if oldest >= to:
                break
            to = oldest
        write(out / "upbit-spot" / f"{coin}-1h.csv.gz", ["ts", "open", "high", "low", "close", "volume"],
              [data[k] for k in sorted(data)])


def fear_greed(out: Path) -> None:
    """공포·탐욕 지수(alternative.me, 2018년부터 하루 1개). 못 받으면 건너뛴다(백테스트는 지수 없이 돈다)"""
    try:
        raw = get("https://api.alternative.me/fng/?limit=0&format=json")
        rows = sorted([int(d["timestamp"]) * 1000, int(d["value"])] for d in json.loads(raw or b"{}").get("data", []))
        write(out / "fear-greed.csv.gz", ["ts", "value"], rows)
    except (RuntimeError, ValueError, urllib.error.URLError) as e:
        print(f"공포·탐욕 지수를 받지 못했어요: {e}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--out", default="research/data")
    p.add_argument("--start", default="2021-01")
    p.add_argument("--end", default="2026-09")
    p.add_argument("--only", choices=["binance", "upbit", "fng"], action="append")
    a = p.parse_args(argv)
    out, only = Path(a.out), set(a.only or ["binance", "upbit", "fng"])
    if "binance" in only:
        binance(out, months(a.start, a.end))
    if "upbit" in only:
        upbit(out, a.start, a.end)
    if "fng" in only:
        fear_greed(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
