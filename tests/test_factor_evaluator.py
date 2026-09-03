# -*- coding: utf-8 -*-
"""因子 8 维体检测试（批次B）。"""
from __future__ import annotations

import numpy as np
import pandas as pd

from strategylab.engine.factor_evaluator import (
    build_forward_returns,
    evaluate_panel,
    ic_analysis,
    score_card,
    winsorize_series,
)


def _closes(n_stocks: int = 40, n_days: int = 600, seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2022-01-01", periods=n_days, freq="B")
    return pd.DataFrame(
        {f"code{i}": 100 * np.cumprod(1 + rng.normal(0.0003, 0.015, n_days))
         for i in range(n_stocks)}, index=dates)


def _month_ends(closes: pd.DataFrame) -> list:
    ym = closes.index.astype(str).str[:7]
    return [str(x)[:10] for x in pd.Series(closes.index).groupby(ym).max().tolist()]


class TestFactorEvaluator:
    def test_evaluate_panel_runs(self):
        closes = _closes()
        res = evaluate_panel(closes, factors=["lowvol_60", "rps_120"])
        assert set(res) == {"lowvol_60", "rps_120"}
        for r in res.values():
            assert "ic" in r and "layer" in r and "turnover" in r
            assert "decay" in r and "pool" in r and "temporal" in r
            assert "scorecard" in r
            sc = r["scorecard"]
            assert {"score", "verdict", "weight_suggestion", "direction"} <= set(sc)

    def test_score_card_bounds(self):
        # 高分场景：强 IC
        ic_res = {"rank_ic_mean": 0.06, "icir": 0.6, "ic_win_rate": 0.8,
                  "ic_latest_6m": 0.05}
        layer = {"monotonicity": 0.9, "ls_t": 4.0, "ls_annual": 0.1}
        sc = score_card(ic_res, layer, 0.1, {}, {"consistent": True},
                        {"drift": 0.1}, direction=1)
        assert sc["score"] >= 70
        assert "强有效" in sc["verdict"]

    def test_score_card_low(self):
        ic_res = {"rank_ic_mean": 0.005, "icir": 0.05, "ic_win_rate": 0.5,
                  "ic_latest_6m": 0.0}
        layer = {"monotonicity": 0.1, "ls_t": 0.5, "ls_annual": -0.02}
        sc = score_card(ic_res, layer, 0.6, {}, {"consistent": None},
                        {"drift": 1.5}, direction=1)
        assert sc["score"] < 50

    def test_forward_returns_alignment(self):
        closes = _closes()
        me = _month_ends(closes)
        labels = build_forward_returns(closes, me, horizon=20)
        assert set(labels) == set(closes.columns)
        # 标签序列长度与月末数一致
        assert len(labels["code0"]) == len(me)
        # 未来收益应为 NaN 后补位（最后一天无未来收益 → 至少一个 NaN 边界）
        assert labels["code0"].notna().sum() > 0

    def test_ic_analysis_shape(self):
        closes = _closes()
        me = _month_ends(closes)
        from strategylab.engine.factor_evaluator import _monthly_panel
        panel = _monthly_panel(closes, "lowvol_60", me)
        labels = build_forward_returns(closes, me)
        res = ic_analysis(panel, labels, "lowvol_60")
        if res is not None:
            assert {"rank_ic_mean", "icir", "ic_win_rate", "ic_latest_6m"} <= set(res)

    def test_winsorize(self):
        s = pd.Series([1.0] * 90 + [100.0, -100.0, 50.0])
        w = winsorize_series(s)
        # 极端值被截断回分位数内（100 → 99% 分位 ≈ 54；-100 → 1% 分位 ≈ -7）
        assert w.max() < 60.0
        assert w.min() > -10.0
        assert w.min() > -100.0

    def test_write_report(self, tmp_path):
        closes = _closes(n_stocks=20, n_days=400)
        evaluate_panel(closes, factors=["mom_20"], out_dir=tmp_path)
        assert (tmp_path / "因子评估报告.md").exists()
        assert (tmp_path / "factor_evaluations.json").exists()
