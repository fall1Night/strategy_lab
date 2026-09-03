# -*- coding: utf-8 -*-
"""因子引擎与经典指标测试（批次A）。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from strategylab.engine.factors.factor_engine import (
    DEFAULT_DIRECTION,
    FACTOR_FUNCS,
    compute_factor_panel,
    direction_summary,
)
from strategylab.engine.factors.classic_indicators import (
    CLASSIC_FACTORS,
    compute_all,
)


def _panel(n_stocks: int = 5, n_days: int = 400) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    dates = pd.date_range("2024-01-01", periods=n_days, freq="B")
    return pd.DataFrame(
        {f"code{i}": 100 * np.cumprod(1 + rng.normal(0.0003, 0.015, n_days))
         for i in range(n_stocks)}, index=dates)


class TestFactorEngine:
    def test_panel_shape(self):
        closes = _panel()
        panel = compute_factor_panel(closes)
        # (日期, 因子) × 股票
        assert panel.shape[0] == len(closes)
        assert len(panel.columns.get_level_values(0).unique()) == len(
            [f for f, s in DEFAULT_DIRECTION.items() if s != 0])

    def test_direction_applied(self):
        closes = _panel()
        panel = compute_factor_panel(closes, factors=["lowvol_60"])
        lowvol = panel["lowvol_60"]
        raw = FACTOR_FUNCS["lowvol_60"](closes.astype(float))
        # 方向化：lowvol_60 sign=-1，所以 panel = -raw
        assert np.allclose(panel["lowvol_60"].dropna(), -raw.dropna())

    def test_dropped_factor_excluded(self):
        closes = _panel()
        panel = compute_factor_panel(closes, factors=["new_high_250", "mom_20"])
        # new_high_250 被剔除
        assert "new_high_250" not in panel.columns.get_level_values(0)
        assert "mom_20" in panel.columns.get_level_values(0)

    def test_all_dropped_raises(self):
        with pytest.raises(ValueError):
            compute_factor_panel(_panel(), direction={"lowvol_60": 0, "mom_20": 0})

    def test_direction_summary(self):
        df = direction_summary()
        assert set(df.columns) == {"因子", "方向", "sign"}
        assert len(df) == len(DEFAULT_DIRECTION)


class TestClassicIndicators:
    def test_all_indicators_computable(self):
        close = _panel()
        panels = compute_all(close)
        assert set(panels) == set(CLASSIC_FACTORS)
        for name, df in panels.items():
            assert df.shape == close.shape

    def test_selected_indicators(self):
        close = _panel()
        panels = compute_all(close, names=["macd_hist", "rsi14"])
        assert set(panels) == {"macd_hist", "rsi14"}

    def test_macd_hist_nonempty(self):
        close = _panel()
        df = compute_all(close)["macd_hist"]
        # warmup 后应有非空值
        assert df.notna().sum().sum() > 0
