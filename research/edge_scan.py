"""단타 후보 규칙들이 수수료·미끄러짐(선물은 펀딩비까지)을 빼고도 남는지 훑어본다 (#36, 기준은 #35).

python research/edge_scan.py --data research/data --out research/results/edge

- 후보마다 작은 조합을 앞 구간(2021-01 ~ 2023-12)에서 고르고 뒤 구간(2024-01 ~ 2026-09)에서 확인한다.
- 신호는 봉이 닫힌 뒤에 정하고 다음 봉 시가에 사고판다(앞을 보지 않게). 포지션은 코인마다 0 또는 ±1(레버리지 1배).
- 계좌 하나에 코인 5종을 같은 돈으로 나눠 담는다(매 봉 같은 비중으로 맞춘다고 본다).
- 비용은 한쪽(사거나 팔 때)마다 수수료 + 미끄러짐. 선물은 메이커 체결(지정가가 다 체결된다는 낙관) 시나리오도 함께 본다.
- 비용 없는 성과(gross)도 같이 적어, 우위가 원래 없는지 비용에 먹히는지 나눠 본다.
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ladder as L  # noqa: E402
from run_backtest import COINS, IS, OOS, load  # noqa: E402

# 계좌: (자료 폴더, 선물 여부, {시나리오: 한쪽 비용})
ACCOUNTS = {
    "binance-spot": ("binance-spot", False, {"taker": 0.001 + 0.0005}),
    "upbit-spot": ("upbit-spot", False, {"taker": 0.0005 + 0.0005}),
    "binance-futures": ("binance-futures", True, {"taker": 0.0005 + 0.0005, "maker": 0.0002}),
}
STRESS = 1.5  # 비용을 이만큼 올려도 플러스여야 통과
MIN_TRADES = 100


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def rsi(s: pd.Series, n: int) -> pd.Series:
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def on_h1(feat: pd.Series, rule: str, h1_index: pd.DatetimeIndex) -> pd.Series:
    """큰 봉 값을 그 봉이 닫힌 뒤부터 1시간봉에 붙인다"""
    return L._on_close(feat.to_frame("v"), pd.Timedelta(rule), h1_index)["v"]


def hold(entry: np.ndarray, exit_: np.ndarray, side: int = 1) -> np.ndarray:
    """들어갈 조건과 나올 조건이 다른 규칙: 들어간 뒤에는 나올 조건이 설 때까지 들고 있는다"""
    out = np.zeros(len(entry))
    on = False
    for i in range(len(entry)):
        if on and exit_[i]:
            on = False
        elif not on and entry[i]:
            on = True
        out[i] = side if on else 0
    return out


# 규칙: (1시간봉, 선물 여부) -> 봉이 닫힌 뒤 정한 포지션(-1, 0, 1)
Rule = Callable[[pd.DataFrame, bool], np.ndarray]


def r_ma5(n: int) -> Rule:
    """지금 봇 규칙에 가까운 것: 1시간봉 n봉 평균선 위면 산다(선물은 아래면 판다). 손절 3%는 넣지 않았다"""
    def f(h1: pd.DataFrame, fut: bool) -> np.ndarray:
        m = sma(h1["close"], n)
        up = (h1["close"] > m).to_numpy()
        dn = (h1["close"] < m).to_numpy()
        return up * 1.0 - (dn * 1.0 if fut else 0)
    return f


def r_donchian(n: int, regime: int) -> Rule:
    """1시간봉 n봉 신고가 돌파 + 일봉 regime일 평균선 위에서만. n/2봉 신저가에 나온다"""
    def f(h1: pd.DataFrame, fut: bool) -> np.ndarray:
        c = h1["close"]
        hi = h1["high"].rolling(n).max().shift(1)
        lo = h1["low"].rolling(n // 2).min().shift(1)
        d1 = L.resample(h1, "1D")
        trend = on_h1(d1["close"] - sma(d1["close"], regime), "1D", h1.index)
        long_ = hold(((c > hi) & (trend > 0)).to_numpy(), (c < lo).to_numpy())
        if not fut:
            return long_
        hi2 = h1["high"].rolling(n // 2).max().shift(1)
        lo2 = h1["low"].rolling(n).min().shift(1)
        short = hold(((c < lo2) & (trend < 0)).to_numpy(), (c > hi2).to_numpy(), -1)
        return np.where(long_ != 0, long_, short)
    return f


def r_rsi2(tf: str, th: float) -> Rule:
    """tf봉 RSI(2)가 th 아래로 꺾이면 사고(200봉 평균선 위에서만), 종가가 5봉 평균선을 넘으면 판다"""
    def f(h1: pd.DataFrame, fut: bool) -> np.ndarray:
        b = h1 if tf == "1h" else L.resample(h1, tf)
        c = b["close"]
        r, m200, m5 = rsi(c, 2), sma(c, 200), sma(c, 5)
        ent_l, ex_l = (r < th) & (c > m200), c > m5
        ent_s, ex_s = (r > 100 - th) & (c < m200), c < m5
        if tf != "1h":  # 큰 봉이 닫힌 그 1시간봉에서만 정한다
            seq = on_h1(pd.Series(np.arange(len(b), dtype=float), index=b.index), tf, h1.index)
            new = (seq != seq.shift(1)).to_numpy() & seq.notna().to_numpy()
            pick = lambda s: (on_h1(s.astype(float), tf, h1.index).fillna(0).to_numpy() > 0) & new  # noqa: E731
            ent_l, ex_l, ent_s, ex_s = (pick(x) for x in (ent_l, ex_l, ent_s, ex_s))
        else:
            ent_l, ex_l, ent_s, ex_s = (x.to_numpy() for x in (ent_l, ex_l, ent_s, ex_s))
        long_ = hold(ent_l, ex_l)
        if not fut:
            return long_
        return np.where(long_ != 0, long_, hold(ent_s, ex_s, -1))
    return f


def r_tsmom(look: int) -> Rule:
    """4시간봉 시계열 모멘텀: look봉 전보다 오르면 산다(선물은 내리면 판다). 4시간마다 다시 본다"""
    def f(h1: pd.DataFrame, fut: bool) -> np.ndarray:
        c = L.resample(h1, "4h")["close"]
        mom = on_h1(c / c.shift(look) - 1, "4h", h1.index).to_numpy()
        return (mom > 0) * 1.0 - ((mom < 0) * 1.0 if fut else 0)
    return f


def r_volbreak(k: float) -> Rule:
    """일봉 변동성 돌파(래리 윌리엄스): 1시간봉 종가가 오늘 시가 + k × 어제 폭을 넘으면 사고, 그날 마지막 봉에 판다(UTC 하루)"""
    def f(h1: pd.DataFrame, fut: bool) -> np.ndarray:
        day = h1.index.floor("1D")
        d1 = L.resample(h1, "1D")
        rng = (d1["high"] - d1["low"]).shift(1)
        target = (d1["open"] + k * rng).reindex(day).to_numpy()
        above = pd.Series(h1["close"].to_numpy() > target, index=h1.index)
        on = above.groupby(day).cummax().to_numpy()
        last = h1.index.hour == 23
        return np.where(last, 0.0, on * 1.0)
    return f


@dataclass(frozen=True)
class Cand:
    name: str
    grid: dict[str, list]
    make: Callable[..., Rule]


CANDS = [
    Cand("ma5 (지금 규칙)", {"n": [5]}, r_ma5),
    Cand("돌파+일봉 추세", {"n": [20, 50, 100], "regime": [50, 100]}, r_donchian),
    Cand("RSI(2) 되돌림", {"tf": ["1h", "4h"], "th": [5.0, 10.0, 20.0]}, r_rsi2),
    Cand("4시간 모멘텀", {"look": [6, 12, 42]}, r_tsmom),
    Cand("변동성 돌파", {"k": [0.3, 0.5, 0.7]}, r_volbreak),
]


def bars(h1: pd.DataFrame, pos: np.ndarray, cost: float, funding: pd.Series | None) -> pd.DataFrame:
    """봉 t에서 정한 포지션은 t+1 시가부터 t+2 시가까지 들고 있다. 봉마다 비용 뺀 수익과 들고 있던 포지션을 낸다"""
    o = h1["open"].to_numpy()
    r = np.append(o[1:] / o[:-1] - 1, 0.0)
    held = np.append(0.0, pos[:-1])
    held = np.nan_to_num(held)
    turn = np.abs(np.diff(np.append(0.0, held)))
    fund = np.zeros(len(h1))
    if funding is not None and len(funding):
        fund = funding.reindex(h1.index).fillna(0).to_numpy()
    gross = held * r - held * fund
    return pd.DataFrame({"held": held, "gross": gross, "net": gross - turn * cost}, index=h1.index)


def trades(b: pd.DataFrame, cost: float) -> pd.DataFrame:
    """같은 방향으로 이어 든 구간 하나가 거래 하나. 들어갈 때·나올 때 비용을 한 번씩 뺀다"""
    h = b["held"].to_numpy()
    if not len(h):
        return pd.DataFrame(columns=["entry", "side", "gross", "net"])
    run = np.cumsum(np.append(1, h[1:] != h[:-1]))
    df = pd.DataFrame({"run": run, "h": h, "lg": np.log1p(b["gross"].to_numpy())}, index=b.index)
    df = df[df["h"] != 0]
    if not len(df):
        return pd.DataFrame(columns=["entry", "side", "gross", "net"])
    g = df.groupby("run")
    gross = np.expm1(g["lg"].sum().to_numpy())
    return pd.DataFrame({"entry": g.apply(lambda x: x.index[0]).to_numpy(), "side": g["h"].first().to_numpy(),
                         "gross": gross, "net": (1 + gross) * (1 - cost) ** 2 - 1})


def stats(port: pd.Series, tr: pd.DataFrame) -> dict:
    eq = (1 + port).cumprod()
    n = len(tr)
    net = tr["net"].to_numpy(dtype=float) if n else np.array([])
    gross = tr["gross"].to_numpy(dtype=float) if n else np.array([])
    wins, losses = net[net > 0].sum(), -net[net < 0].sum()
    sd = net.std(ddof=1) if n > 1 else np.nan
    return {"trades": n,
            "gross_bp": float(gross.mean() * 1e4) if n else np.nan,
            "net_bp": float(net.mean() * 1e4) if n else np.nan,
            "win_pct": float((net > 0).mean() * 100) if n else np.nan,
            "pf": float(wins / losses) if losses > 0 else np.inf if wins > 0 else np.nan,
            "t": float(net.mean() / sd * np.sqrt(n)) if n > 1 and sd > 0 else np.nan,
            "return_pct": float((eq.iloc[-1] - 1) * 100) if len(eq) else 0.0,
            "max_dd_pct": float((eq / eq.cummax() - 1).min() * 100) if len(eq) else 0.0,
            "per_year": float(n / max(1e-9, (port.index[-1] - port.index[0]).days / 365.25)) if len(port) else 0.0}


def evaluate(h1s: dict, funding: dict, fut: bool, rule: Rule, cost: float, span) -> dict:
    sleeves, trs = [], []
    for c, h1 in h1s.items():
        b = bars(h1, rule(h1, fut), cost, funding.get(c))
        b = b[(b.index >= span[0]) & (b.index < span[1])]
        sleeves.append(b["net"].rename(c))
        t = trades(b, cost)
        t["coin"] = c
        trs.append(t)
    port = pd.concat(sleeves, axis=1).fillna(0).mean(axis=1)
    tr = pd.concat(trs, ignore_index=True)
    return stats(port, tr)


def passes(m: dict, stress: dict) -> bool:
    return (m["trades"] >= MIN_TRADES and m["net_bp"] > 0 and m["t"] >= 2 and m["pf"] >= 1.2
            and m["max_dd_pct"] >= -15 and stress["return_pct"] > 0)


def run_account(name: str, data: Path, log) -> dict:
    folder, fut, scen = ACCOUNTS[name]
    h1s = {c: df for c in COINS if (df := load(data, folder, c)) is not None and len(df)}
    if not h1s:
        return {"account": name, "skipped": "자료 없음"}
    funding = {}
    if fut:
        for c in h1s:
            fr = load(data, folder, c, "funding")
            if fr is not None and len(fr):
                funding[c] = fr["rate"].groupby(fr.index.floor("1h")).sum()
    rows, out = [], []
    for sc, cost in scen.items():
        for cand in CANDS:
            keys = list(cand.grid)
            best, best_ret = None, -np.inf
            for combo in itertools.product(*cand.grid.values()):
                kw = dict(zip(keys, combo))
                rule = cand.make(**kw)
                m_is = evaluate(h1s, funding, fut, rule, cost, IS)
                m_oos = evaluate(h1s, funding, fut, rule, cost, OOS)
                rows.append({"account": name, "scenario": sc, "cand": cand.name, **kw,
                             **{f"is_{k}": v for k, v in m_is.items()}, **{f"oos_{k}": v for k, v in m_oos.items()}})
                score = m_is["return_pct"] / max(1.0, -m_is["max_dd_pct"]) if m_is["trades"] >= MIN_TRADES else -np.inf
                if best is None or score > best_ret:
                    best, best_ret = (kw, rule, m_is, m_oos), score
            kw, rule, m_is, m_oos = best
            stress = evaluate(h1s, funding, fut, rule, cost * STRESS, OOS)
            out.append({"scenario": sc, "cost_side_pct": cost * 100, "cand": cand.name, "chosen": kw,
                        "is": m_is, "oos": m_oos, "stress_oos": stress, "pass": passes(m_oos, stress)})
            log(f"{name} {sc} {cand.name} {kw}: 뒤 구간 {m_oos['return_pct']:.1f}% 거래 {m_oos['trades']} "
                f"건당 {m_oos['net_bp']:.1f}bp (비용 전 {m_oos['gross_bp']:.1f}bp)")
    return {"account": name, "coins": list(h1s), "results": out, "grid": rows}


def f(v, d=1):
    if isinstance(v, (int, float, np.floating)) and not np.isfinite(v):
        return "∞" if v == np.inf else "-"
    return f"{v:,.{d}f}" if isinstance(v, (float, np.floating)) else str(v)


def report(results: list[dict]) -> str:
    lines = ["# 단타 후보 우위 스캔 (수수료·미끄러짐 뺀 뒤)", "",
             f"앞 구간 {IS[0]:%Y-%m} ~ 2023-12에서 조합을 고르고 뒤 구간 {OOS[0]:%Y-%m} ~ 2026-09에서 확인했어요. "
             "코인 5종을 같은 돈으로 나눠 담고 레버리지는 1배예요. 비용은 한쪽마다 수수료 + 미끄러짐이고 선물은 펀딩비도 뺐어요.", "",
             f"통과 기준(#35): 거래 {MIN_TRADES}건 이상, 건당 순수익 > 0, t ≥ 2, PF ≥ 1.2, 최대 낙폭 ≥ -15%, 비용 {STRESS}배에서도 수익 플러스.",
             "'비용 전'은 같은 거래를 비용 없이 쳤을 때 건당 수익이에요. 이게 비용(왕복)보다 작으면 우위가 비용에 먹혀요.", ""]
    for r in results:
        if "skipped" in r:
            lines += [f"## {r['account']}: {r['skipped']}", ""]
            continue
        lines += [f"## {r['account']}", "",
                  "| 시나리오(한쪽 비용) | 후보 | 고른 값 | 거래 | 연간 | 비용 전 bp | 비용 후 bp | 승률 | PF | t | 수익 | 낙폭 | 비용×1.5 수익 | 통과 |",
                  "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for x in r["results"]:
            o, s = x["oos"], x["stress_oos"]
            lines.append(f"| {x['scenario']} ({x['cost_side_pct']:.2f}%) | {x['cand']} | {x['chosen']} | {o['trades']} | "
                         f"{f(o['per_year'], 0)} | {f(o['gross_bp'])} | {f(o['net_bp'])} | {f(o['win_pct'])}% | {f(o['pf'], 2)} | "
                         f"{f(o['t'], 2)} | {f(o['return_pct'])}% | {f(o['max_dd_pct'])}% | {f(s['return_pct'])}% | "
                         f"{'✅' if x['pass'] else '❌'} |")
        g = pd.DataFrame(r["grid"])
        top = g.sort_values("oos_return_pct", ascending=False).head(3)
        lines += ["", "뒤 구간을 미리 보고 골랐을 때(과적합, 참고만) 가장 좋았던 조합 셋:", ""]
        for _, t in top.iterrows():
            params = {k: t[k] for k in g.columns if k not in ("account", "scenario", "cand") and not k.startswith(("is_", "oos_"))
                      and pd.notna(t[k])}
            lines.append(f"- {t['scenario']} {t['cand']} {params}: {f(t['oos_return_pct'])}%, 낙폭 {f(t['oos_max_dd_pct'])}%, "
                         f"거래 {t['oos_trades']}, 건당 {f(t['oos_net_bp'])}bp")
        lines.append("")
    ok = [(r["account"], x["scenario"], x["cand"]) for r in results if "results" in r for x in r["results"] if x["pass"]]
    lines += ["## 결론", "", ("통과: " + ", ".join(f"{a} {s} {c}" for a, s, c in ok)) if ok else
              "통과한 후보가 없어요. 비용을 뺀 뒤 남는 우위는 확인되지 않았어요. 실계좌는 그대로 막아 둬요(#37).", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", default="research/data")
    ap.add_argument("--out", default="research/results/edge")
    ap.add_argument("--account", choices=list(ACCOUNTS), action="append")
    a = ap.parse_args(argv)
    data, out = Path(a.data), Path(a.out)
    results = [run_account(n, data, lambda m: print(m, flush=True)) for n in a.account or list(ACCOUNTS)]
    out.mkdir(parents=True, exist_ok=True)
    grid = [row for r in results for row in r.get("grid", [])]
    if grid:
        pd.DataFrame(grid).to_csv(out / "grid.csv", index=False)
    slim = [{k: v for k, v in r.items() if k != "grid"} for r in results]
    (out / "summary.json").write_text(json.dumps(slim, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    md = report(results)
    (out / "report.md").write_text(md, encoding="utf-8")
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
