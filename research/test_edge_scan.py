"""edge_scan: 앞을 보지 않는지, 비용을 한쪽마다 한 번씩 빼는지 확인한다 (python -m unittest research/test_edge_scan.py)"""
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import edge_scan as E  # noqa: E402


def walk(n: int, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2021-01-01", periods=n, freq="1h", tz="UTC")
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    op = np.append(100.0, close[:-1])
    return pd.DataFrame({"open": op, "high": np.maximum(op, close) * 1.002, "low": np.minimum(op, close) * 0.998,
                         "close": close, "volume": 1.0}, index=idx)


class EdgeScanTest(unittest.TestCase):
    def test_rules_do_not_look_ahead(self):
        h1 = walk(24 * 300, 3)
        cut = 24 * 200
        later = h1.copy()
        later.iloc[cut + 1:, :4] *= 1.5  # 뒤쪽 값을 바꿔도 앞쪽 포지션은 그대로여야 한다
        for cand in E.CANDS:
            for kw in [dict(zip(cand.grid, c)) for c in __import__("itertools").product(*cand.grid.values())]:
                for fut in (False, True):
                    a, b = cand.make(**kw)(h1, fut), cand.make(**kw)(later, fut)
                    np.testing.assert_array_equal(a[:cut], b[:cut], err_msg=f"{cand.name} {kw} fut={fut}")

    def test_cost_once_per_side(self):
        h1 = walk(10, 1)
        pos = np.array([0, 1, 1, 1, 0, 0, 0, 0, 0, 0], dtype=float)
        b = E.bars(h1, pos, 0.001, None)
        t = E.trades(b, 0.001)
        self.assertEqual(len(t), 1)
        o = h1["open"].to_numpy()
        gross = o[5] / o[2] - 1  # 봉 1에서 정하고 봉 2 시가에 사서, 봉 4에서 정하고 봉 5 시가에 판다
        self.assertAlmostEqual(t["gross"].iloc[0], gross, places=12)
        self.assertAlmostEqual(t["net"].iloc[0], (1 + gross) * 0.999 ** 2 - 1, places=12)
        self.assertAlmostEqual(b["gross"].sum() - b["net"].sum(), 0.002, places=12)

    def test_short_pays_negative_funding(self):
        h1 = walk(6, 2)
        h1["open"] = 100.0
        pos = np.array([-1, -1, 0, 0, 0, 0], dtype=float)
        fund = pd.Series([0.0, 0.001, 0.0, 0, 0, 0], index=h1.index)
        b = E.bars(h1, pos, 0.0, fund)
        self.assertAlmostEqual(b["gross"].sum(), 0.001)  # 펀딩비가 플러스면 숏이 받는다


if __name__ == "__main__":
    unittest.main()
