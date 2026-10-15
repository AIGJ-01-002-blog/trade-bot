# SPDX-License-Identifier: GPL-3.0-or-later
"""Freqtrade 백테스트를 거래소 API 없이 돌린다(GitHub 러너처럼 바이낸스 API가 451로 막힌 곳에서도).

python research/freqtrade/run_offline.py --data research/data --out research/results/freqtrade

- 가격은 research/fetch_data.py가 data.binance.vision에서 받은 1시간봉·펀딩비를 Freqtrade 자료 형식으로 바꿔 쓴다.
  4시간·1일봉은 1시간봉을 묶어 만들고, 선물 마크 가격은 마지막 체결가로 갈음한다.
- 시장 정보(최소 주문·정밀도)는 거래소에서 받지 못해 넉넉한 가짜 값을 넣는다. 레버리지 구간(청산 계산)은 Freqtrade에 들어 있는
  바이낸스 자료를 그대로 쓴다. 한국 맥에서 Docker로 `freqtrade backtesting`을 돌리면 진짜 시장 정보로 확인할 수 있다(README).
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
COINS = ["BTC", "ETH", "XRP", "SOL", "DOGE"]
SPANS = {"is": "20210101-20240101", "oos": "20240101-20261001"}


def fake_market(coin: str, futures: bool) -> dict:
    return {"id": f"{coin}USDT", "symbol": f"{coin}/USDT" + (":USDT" if futures else ""), "base": coin, "quote": "USDT",
            "settle": "USDT" if futures else None, "baseId": coin, "quoteId": "USDT", "active": True, "spot": not futures,
            "swap": futures, "future": False, "option": False, "margin": False, "linear": True if futures else None,
            "inverse": False if futures else None, "contract": futures, "contractSize": 1.0 if futures else None,
            "type": "swap" if futures else "spot", "precision": {"amount": 1e-8, "price": 1e-8},
            "limits": {"amount": {"min": 1e-8, "max": None}, "cost": {"min": 5, "max": None},
                       "leverage": {"min": 1, "max": 20}, "price": {}}, "info": {}}


def prepare(data: Path, datadir: Path, futures: bool) -> dict:
    from freqtrade.data.history import get_datahandler
    from freqtrade.enums import CandleType

    folder = "binance-futures" if futures else "binance-spot"
    dh = get_datahandler(datadir, "feather")
    markets = {}
    for coin in COINS:
        src = data / folder / f"{coin}-1h.csv.gz"
        if not src.exists():
            continue
        df = pd.read_csv(src)
        df["date"] = pd.to_datetime(df.pop("ts"), unit="ms", utc=True)
        df = df[["date", "open", "high", "low", "close", "volume"]].astype({c: float for c in ("open", "high", "low", "close", "volume")})
        m = fake_market(coin, futures)
        pair = m["symbol"]
        markets[pair] = m
        ct = CandleType.FUTURES if futures else CandleType.SPOT
        dh.ohlcv_store(pair, "1h", df, ct)
        for tf, rule in (("4h", "4h"), ("1d", "1D")):
            agg = df.set_index("date").resample(rule).agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
            dh.ohlcv_store(pair, tf, agg.dropna().reset_index(), ct)
        if futures:
            dh.ohlcv_store(pair, "1h", df, CandleType.MARK)
            fr_src = data / folder / f"{coin}-funding.csv.gz"
            fr = pd.DataFrame({"date": df["date"], "funding_rate": 0.0})
            if fr_src.exists():
                f = pd.read_csv(fr_src)
                f["date"] = pd.to_datetime(f.pop("ts"), unit="ms", utc=True).dt.floor("1h")
                fr = fr.set_index("date")
                fr.loc[fr.index.intersection(f["date"]), "funding_rate"] = f.groupby("date")["rate"].sum().reindex(
                    fr.index.intersection(f["date"])).to_numpy()
                fr = fr.reset_index()
            dh.ohlcv_store(pair, "1h", fr, CandleType.FUNDING_RATE)
    return markets


def run(mode: str, strategy: str, span: str, data: Path, work: Path) -> dict:
    from freqtrade.configuration import Configuration
    from freqtrade.enums import RunMode
    from freqtrade.optimize.backtesting import Backtesting

    futures = mode == "futures"
    ud = work / f"ud-{mode}"
    datadir = ud / "data" / "binance"
    if not ud.exists():
        datadir.mkdir(parents=True)
        shutil.copytree(HERE / "user_data" / "strategies", ud / "strategies")
        (ud / "markets.json").write_text(json.dumps(prepare(data, datadir, futures)))
    markets = json.loads((ud / "markets.json").read_text())
    cfg = json.loads((HERE / f"config-{mode}.json").read_text())
    cfg["exchange"]["pair_whitelist"] = list(markets)
    if strategy == "BaselineStrategy":
        cfg["stake_amount"] = 100  # 봇 기본: 1,000 USDT 중 100 USDT씩
    cfg_path = work / f"cfg-{mode}-{strategy}.json"
    cfg_path.write_text(json.dumps(cfg))
    args = {"config": [str(cfg_path)], "user_data_dir": str(ud), "datadir": str(datadir), "strategy": strategy,
            "timerange": SPANS[span], "export": "none", "strategy_path": str(ud / "strategies")}
    with mock.patch("freqtrade.exchange.exchange.Exchange._load_async_markets"), \
         mock.patch("freqtrade.exchange.exchange.Exchange.validate_config"), \
         mock.patch("freqtrade.exchange.exchange.Exchange.markets", new_callable=mock.PropertyMock, return_value=markets), \
         mock.patch("freqtrade.exchange.binance.Binance.additional_exchange_init"), \
         mock.patch("freqtrade.exchange.exchange.Exchange.reload_markets"):
        config = Configuration(args, RunMode.BACKTEST).get_config()
        bt = Backtesting(config)
        bt.start()
        st = bt.results["strategy"][strategy]
    start = st.get("starting_balance") or cfg.get("dry_run_wallet", 1000)
    daily = [d.get("abs_profit", 0.0) if isinstance(d, dict) else d[1] for d in st.get("daily_profit", [])]
    # 하루 손익 ÷ 그날 아침 잔고(닫힌 거래 기준)
    bal = start + np.cumsum([0.0] + daily[:-1]) if daily else np.array([start])
    worst_day = float(np.min(np.array(daily) / bal) * 100) if daily else 0.0
    exits = {r["key"]: r["trades"] for r in st.get("exit_reason_summary", []) if r.get("key") not in (None, "TOTAL")}
    return {"mode": mode, "strategy": strategy, "span": span, "timerange": SPANS[span],
            "return_pct": float(st["profit_total"]) * 100, "max_dd_pct": -float(st.get("max_drawdown_account", 0)) * 100,
            "worst_day_pct": worst_day, "trades": int(st["total_trades"]), "liquidations": int(exits.get("liquidation", 0)),
            "win_rate_pct": float(st.get("winrate", 0)) * 100, "exits": exits,
            "final_balance": float(st.get("final_balance", 0))}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", default="research/data")
    ap.add_argument("--out", default="research/results/freqtrade")
    ap.add_argument("--mode", choices=["spot", "futures"], action="append")
    ap.add_argument("--strategy", choices=["LadderStrategy", "BaselineStrategy"], action="append")
    ap.add_argument("--span", choices=list(SPANS), action="append")
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        for mode in a.mode or ["spot", "futures"]:
            for strategy in a.strategy or ["LadderStrategy", "BaselineStrategy"]:
                for span in a.span or ["is", "oos"]:
                    try:
                        rows.append(run(mode, strategy, span, Path(a.data), Path(tmp)))
                    except Exception as e:  # noqa: BLE001 (한 조합이 실패해도 나머지는 돈다)
                        rows.append({"mode": mode, "strategy": strategy, "span": span, "error": f"{type(e).__name__}: {e}"[:400]})
                    print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
    (out / "summary.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2))
    lines = ["# Freqtrade 대조 (바이낸스)", "", "| 시장 | 전략 | 구간 | 수익 | 최대 낙폭 | 최악의 하루 | 거래 | 청산 | 승률 |", "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        if "error" in r:
            lines.append(f"| {r['mode']} | {r['strategy']} | {r['span']} | 실패: {r['error'][:120]} | | | | | |")
        else:
            lines.append(f"| {r['mode']} | {r['strategy']} | {r['span']} | {r['return_pct']:.1f}% | {r['max_dd_pct']:.1f}% | "
                         f"{r['worst_day_pct']:.1f}% | {r['trades']} | {r['liquidations']} | {r['win_rate_pct']:.0f}% |")
    lines += ["", "최악의 하루는 닫힌 거래의 하루 손익 기준이라 엔진(시간마다 평가) 값보다 덜 나쁘게 나올 수 있어요."]
    (out / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
