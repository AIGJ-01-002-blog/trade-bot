"""세 겹 거름망 복리법 백테스트를 돌리고 보고서(Markdown)를 쓴다.

python research/fetch_data.py --out research/data          # 자료 받기 (한 번)
python research/run_backtest.py --data research/data --out research/results

- 계좌 셋: 바이낸스 현물(USDT), 바이낸스 USDT-M 선물(USDT), 업비트 현물(원). 코인 5종을 한 계좌로 돈다.
- 앞 구간(2021-01 ~ 2023-12)에서 조합을 고르고, 뒤 구간(2024-01 ~ 2026-09)에서 확인한다. 고른 값의 이웃 조합도 뒤 구간에 돌린다.
- 비교 기준은 지금 봇 규칙(1시간봉 5봉 평균선, 종가 손절 3%, 처음 잔고 10%씩, 선물 3배).
- 현물 계좌는 엔진이 낸 주문을 vectorbt(from_orders)에 다시 넣어 장부가 맞는지 대조한다. 선물은 vectorbt에 레버리지·
  펀딩비·청산이 없어 엔진 장부만 쓴다. 바이낸스 선물의 정밀 확인은 freqtrade/ 폴더(Freqtrade, 한국 맥에서)로 한다.
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ladder as L  # noqa: E402

COINS = ["BTC", "ETH", "XRP", "SOL", "DOGE"]
ACCOUNTS = {  # 이름: (자료 폴더, 시장, 수수료(테이커), 처음 잔고, 통화)
    "binance-spot": ("binance-spot", "spot", 0.001, 1000.0, "USDT"),
    "binance-futures": ("binance-futures", "futures", 0.0005, 1000.0, "USDT"),
    "upbit-spot": ("upbit-spot", "spot", 0.0005, 1_000_000.0, "KRW"),
}
SLIP = 0.0005
IS = (pd.Timestamp("2021-01-01", tz="UTC"), pd.Timestamp("2024-01-01", tz="UTC"))
OOS = (pd.Timestamp("2024-01-01", tz="UTC"), pd.Timestamp("2026-10-01", tz="UTC"))
# 앞 구간에서 고를 조합. 가운데 값이 설계 문서 기본값이다
GRID = {"atr_mult": [1.2, 1.5, 2.0], "breakout_bars": [10, 20], "regime_sma": [50, 100]}
WARMUP_DAYS = 120  # 1일 SMA100까지 데우는 데 필요한 앞쪽 자료(앞 구간 첫 넉 달은 그래서 거래가 없다)


def load(data: Path, folder: str, coin: str, what: str = "1h") -> pd.DataFrame | None:
    path = data / folder / f"{coin}-{what}.csv.gz"
    if not path.exists():
        return None
    df = pd.read_csv(path)
    df.index = pd.to_datetime(df.pop("ts"), unit="ms", utc=True)
    return df.astype(float).sort_index()


def fear_greed(data: Path) -> pd.Series | None:
    path = data / "fear-greed.csv.gz"
    if path.exists():
        df = pd.read_csv(path)
        return pd.Series(df["value"].to_numpy(dtype=float), index=pd.to_datetime(df["ts"], unit="ms", utc=True)).sort_index()
    return None


def score(m: dict) -> float:
    """앞 구간에서 조합 고르는 점수: 수익 ÷ 최대 낙폭(낙폭이 0이면 1%로 본다). 거래 30건 미만은 고르지 않는다"""
    if m["trades"] < 30:
        return -1e9
    return m["return_pct"] / max(1.0, -m["max_dd_pct"])


def vbt_check(res: L.Result, h1: dict[str, pd.DataFrame], fee: float, init: float) -> dict:
    """엔진의 현물 주문을 vectorbt에 그대로 넣어 최종 잔고·최대 낙폭을 다시 계산한다"""
    import vectorbt as vbt

    idx = res.equity.index - L.HOUR
    close = pd.DataFrame({c: h1[c]["close"].reindex(idx).ffill() for c in h1})
    size = pd.DataFrame(np.nan, index=idx, columns=close.columns)
    price = close.copy()
    for ts, coin, qty, px in res.orders:
        cur = size.at[ts, coin]
        size.at[ts, coin] = qty if np.isnan(cur) else cur + qty
        price.at[ts, coin] = px
    # 코어로 옮긴 돈은 vectorbt에서는 현금으로 남겨 둔다. 엔진 곡선도 옮긴 돈을 더한 값이라 둘이 같은 기준이 된다
    pf = vbt.Portfolio.from_orders(close, size=size, price=price, fees=fee, init_cash=init, cash_sharing=True,
                                   group_by=True, size_type="amount", allow_partial=False, raise_reject=True, freq="1h")
    value = pf.value()
    engine_final = res.equity.iloc[-1]
    return {"vbt_final": float(value.iloc[-1]), "engine_final": float(engine_final),
            "diff_pct": float((value.iloc[-1] / engine_final - 1) * 100),
            "vbt_max_dd_pct": float((value / value.cummax() - 1).min() * 100), "vbt_orders": int(pf.orders.count())}


def run_account(name: str, data: Path, fng: pd.Series | None, out: Path, log) -> dict:
    folder, market, fee, init, cur = ACCOUNTS[name]
    h1 = {c: df for c in COINS if (df := load(data, folder, c)) is not None and len(df)}
    if not h1:
        log(f"{name}: 자료가 없어 건너뜀")
        return {"account": name, "skipped": "자료 없음"}
    funding = {}
    if market == "futures":
        for c in h1:
            fr = load(data, folder, c, "funding")
            if fr is not None and len(fr):
                funding[c] = fr["rate"]
    cover = {c: f"{df.index[0]:%Y-%m-%d} ~ {df.index[-1]:%Y-%m-%d}" for c, df in h1.items()}
    rows = []
    feats_cache: dict = {}

    def feats_for(p: L.Params):
        key = (p.atr_mult, p.breakout_bars, p.regime_sma)
        if key not in feats_cache:
            feats_cache[key] = {c: L.features(df, p, funding.get(c), fng) for c, df in h1.items()}
        return feats_cache[key]

    def run(p: L.Params, span) -> tuple[L.Result, dict]:
        r = L.Ladder(feats_for(p), market, fee, SLIP, init, p, funding).run(*span)
        return r, L.metrics(r, init)

    t0 = time.time()
    keys = list(GRID)
    for combo in itertools.product(*GRID.values()):
        p = L.with_params(L.Params(), **dict(zip(keys, combo)))
        _, m = run(p, IS)
        rows.append({**dict(zip(keys, combo)), **{f"is_{k}": v for k, v in m.items() if k != "name"}, "is_score": score(m)})
    grid = pd.DataFrame(rows)
    best = grid.sort_values("is_score", ascending=False).iloc[0]
    chosen = {k: (type(GRID[k][0]))(best[k]) for k in keys}
    log(f"{name}: 앞 구간 {len(grid)}개 조합 {time.time() - t0:.0f}초, 고른 값 {chosen}")

    p_best = L.with_params(L.Params(), **chosen)
    r_oos, m_oos = run(p_best, OOS)
    r_is, m_is = run(p_best, IS)
    # 이웃 조합(한 축만 옆 값으로)
    neigh = []
    for k in keys:
        vals = GRID[k]
        i = vals.index(chosen[k])
        for j in (i - 1, i + 1):
            if 0 <= j < len(vals):
                p = L.with_params(p_best, **{k: vals[j]})
                _, m = run(p, OOS)
                neigh.append({"change": f"{k}={vals[j]}", **{k2: m[k2] for k2 in ("return_pct", "max_dd_pct", "trades")}})
    base_is = L.metrics(L.Baseline(h1, market, fee, SLIP, init, funding).run(*IS), init)
    base_oos = L.metrics(L.Baseline(h1, market, fee, SLIP, init, funding).run(*OOS), init)
    r_arr = r_oos.trades["r"].to_numpy() if len(r_oos.trades) else np.array([])
    mc = L.monte_carlo_dd(r_arr, 0.01)
    check = None
    if market == "spot":
        try:
            check = vbt_check(r_oos, h1, fee, init)
        except Exception as e:  # noqa: BLE001 (대조 실패는 보고서에 적고 계속)
            check = {"error": str(e)[:300]}
    out.mkdir(parents=True, exist_ok=True)
    grid.to_csv(out / f"{name}-grid-is.csv", index=False)
    if len(r_oos.trades):
        r_oos.trades.to_csv(out / f"{name}-trades-oos.csv", index=False)
    r_oos.equity.resample("1D").last().to_csv(out / f"{name}-equity-oos.csv", header=["equity"])
    return {"account": name, "currency": cur, "init": init, "cover": cover, "chosen": chosen,
            "is": m_is, "oos": m_oos, "pass": L.passes(m_oos), "neighbors": neigh, "baseline_is": base_is,
            "baseline_oos": base_oos, "mc_dd95_at_1pct": mc, "vbt": check,
            "exits_oos": r_oos.trades["exit"].value_counts().to_dict() if len(r_oos.trades) else {},
            "by_coin_oos": r_oos.trades.groupby("coin")["pnl"].agg(["count", "sum"]).round(2).to_dict("index") if len(r_oos.trades) else {},
            "funding_cover": {c: f"{s.index[0]:%Y-%m-%d}" for c, s in funding.items()}}


def fmt(v, d=1):
    return f"{v:,.{d}f}" if isinstance(v, (int, float, np.floating)) else str(v)


def report(results: list[dict], fng_ok: bool) -> str:
    lines = ["# 세 겹 거름망 복리법 백테스트 결과", "",
             f"앞 구간 {IS[0]:%Y-%m} ~ 2023-12에서 조합을 고르고, 뒤 구간 {OOS[0]:%Y-%m} ~ 2026-09에서 확인했어요. "
             "수수료(바이낸스 현물 0.1%, 선물 0.05%, 업비트 0.05%)와 미끄러짐 0.05%, 선물 펀딩비를 뺐어요. "
             "뉴스·AI는 과거 자료가 없어 빼고(AI 확신도 1.0), 공포·탐욕 지수는 " + ("넣었어요." if fng_ok else "받지 못해 뺐어요."), "",
             "## 한눈에 (뒤 구간)", "",
             "| 계좌 | 기법 수익 | 최대 낙폭 | 최악의 하루 | 거래 | 청산 | 지금 규칙 수익 | 지금 규칙 낙폭 | 통과 |",
             "|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        if "skipped" in r:
            lines.append(f"| {r['account']} | {r['skipped']} | | | | | | | |")
            continue
        o, b = r["oos"], r["baseline_oos"]
        lines.append(f"| {r['account']} | {fmt(o['return_pct'])}% | {fmt(o['max_dd_pct'])}% | {fmt(o['worst_day_pct'])}% | "
                     f"{o['trades']} | {o['liquidations']} | {fmt(b['return_pct'])}% | {fmt(b['max_dd_pct'])}% | "
                     f"{'✅' if r['pass']['all'] else '❌'} |")
    lines += ["", "통과 기준(설계 문서 8장): 뒤 구간 수익 플러스, 최대 낙폭 15% 이하, 청산 0건, 최악의 하루 -3% 이내, 거래 30건 이상.", ""]
    for r in results:
        if "skipped" in r:
            continue
        o, i = r["oos"], r["is"]
        lines += [f"## {r['account']} ({r['currency']}, 처음 {fmt(r['init'], 0)})", "",
                  f"- 자료: " + ", ".join(f"{c} {v}" for c, v in r["cover"].items()),
                  f"- 앞 구간에서 고른 값: {r['chosen']}",
                  f"- 앞 구간: 수익 {fmt(i['return_pct'])}%, 낙폭 {fmt(i['max_dd_pct'])}%, 거래 {i['trades']}",
                  f"- 뒤 구간: 수익 {fmt(o['return_pct'])}%, 낙폭 {fmt(o['max_dd_pct'])}%, 최악의 하루 {fmt(o['worst_day_pct'])}%, "
                  f"거래 {o['trades']}, 승률 {fmt(o['win_rate_pct'])}%, 평균 {fmt(o['avg_r'], 2)}R, 수수료 {fmt(o['fees'], 2)}, "
                  f"펀딩비 {fmt(o['funding'], 2)}, 코어로 옮긴 돈 {fmt(o['to_core'], 2)}",
                  f"- 통과 항목: " + ", ".join(f"{k} {'✅' if v else '❌'}" for k, v in r["pass"].items() if k != "all"),
                  f"- 정리 이유: {r['exits_oos']}",
                  f"- 코인별(건수, 손익): {r['by_coin_oos']}",
                  f"- 몬테카를로(거래 순서 1,000번 섞기, 한 번 위험 1%로 가정) 최대 낙폭 95% 지점: {fmt(r['mc_dd95_at_1pct'])}%",
                  f"- 지금 규칙: 앞 구간 {fmt(r['baseline_is']['return_pct'])}% / 뒤 구간 {fmt(r['baseline_oos']['return_pct'])}% "
                  f"(낙폭 {fmt(r['baseline_oos']['max_dd_pct'])}%, 거래 {r['baseline_oos']['trades']}, 청산 {r['baseline_oos']['liquidations']})"]
        if r["funding_cover"]:
            lines.append(f"- 펀딩비 자료 시작: {r['funding_cover']}")
        if r["vbt"]:
            v = r["vbt"]
            lines.append(f"- vectorbt 대조: " + (f"실패 {v['error']}" if "error" in v else
                         f"최종 {fmt(v['vbt_final'], 2)} vs 엔진 {fmt(v['engine_final'], 2)} (차이 {fmt(v['diff_pct'], 3)}%), "
                         f"vectorbt 낙폭 {fmt(v['vbt_max_dd_pct'])}%, 주문 {v['vbt_orders']}건"))
        lines += ["", "이웃 조합(뒤 구간):", "", "| 바꾼 값 | 수익 | 낙폭 | 거래 |", "|---|---|---|---|"]
        lines += [f"| {n['change']} | {fmt(n['return_pct'])}% | {fmt(n['max_dd_pct'])}% | {n['trades']} |" for n in r["neighbors"]]
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", default="research/data")
    ap.add_argument("--out", default="research/results")
    ap.add_argument("--account", choices=list(ACCOUNTS), action="append")
    a = ap.parse_args(argv)
    data, out = Path(a.data), Path(a.out)
    fng = fear_greed(data)
    results = []
    for name in a.account or list(ACCOUNTS):
        results.append(run_account(name, data, fng, out, lambda m: print(m, flush=True)))
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    md = report(results, fng is not None)
    (out / "report.md").write_text(md, encoding="utf-8")
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
