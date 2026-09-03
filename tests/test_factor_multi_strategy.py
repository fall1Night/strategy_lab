# -*- coding: utf-8 -*-
"""factor_multi 多因子综合策略测试。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from strategylab.engine.strategies import get_strategy_class, list_strategies
from strategylab.engine.strategies.factor_multi import FactorMultiStrategy

DEFAULT_FACTORS = [{"name": "lowvol_60", "sign": -1},
                   {"name": "rps_120", "sign": -1},
                   {"name": "near_high_250", "sign": 1}]


def _daily(n: int = 600, seed: int = 8, start: str = "2023-01-01") -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.date_range(start, periods=n, freq="B")
    close = 20 * np.cumprod(1 + rng.normal(0.0003, 0.02, n))
    return pd.DataFrame({
        "date": dates, "open": close * 0.995, "high": close * 1.02,
        "low": close * 0.98, "close": close,
        "vol": rng.integers(100000, 1000000, n),
    })


def _cfg(**overrides) -> dict:
    cfg = {"type": "factor_multi", "name": "测试",
           "params": {"initial_cash": 100000, "buy_ratio": 0.95,
                      "factors": DEFAULT_FACTORS,
                      "stop": {"enabled": True}}}
    cfg["params"].update(overrides)
    return cfg


class TestFactorMulti:
    def test_registered(self):
        assert "factor_multi" in list_strategies()
        assert get_strategy_class("factor_multi") is FactorMultiStrategy

    def test_run_contract(self):
        daily = _daily()
        res = FactorMultiStrategy(_cfg()).run(daily, daily, "2023-01-01", "2024-09-01",
                                              symbol="600519.SH", symbol_name="茅台")
        assert {"equity_curve", "trade_history", "positions"} == set(res)
        assert len(res["equity_curve"]) > 0
        for t in res["trade_history"]:
            assert {"entry_date", "exit_date", "side", "size"} <= set(t)
            assert t["role"] == "底仓"

    def test_produces_trades(self):
        daily = _daily(n=700, seed=13)
        res = FactorMultiStrategy(_cfg()).run(daily, daily, "2023-01-01", "2024-09-01")
        assert res["trade_history"], "多因子组合应产生交易"

    def test_single_factor_works(self):
        # 单因子配置也支持
        daily = _daily(seed=17)
        cfg = _cfg(factors=[{"name": "lowvol_60", "sign": -1}])
        res = FactorMultiStrategy(cfg).run(daily, daily, "2023-01-01", "2024-09-01")
        assert len(res["equity_curve"]) > 100

    def test_unknown_factor_raises(self):
        daily = _daily()
        cfg = _cfg(factors=[{"name": "not_a_factor", "sign": 1}])
        with pytest.raises(ValueError):
            FactorMultiStrategy(cfg).run(daily, daily, "2023-01-01", "2024-01-01")

    def test_stop_rules_engaged(self):
        n = 300
        dates = pd.date_range("2023-01-01", periods=n, freq="B")
        close = np.concatenate([
            np.linspace(20, 26, 120),
            np.linspace(26, 15, 180),
        ])
        daily = pd.DataFrame({"date": dates, "open": close, "high": close * 1.01,
                              "low": close * 0.99, "close": close,
                              "vol": np.full(n, 500000)})
        cfg = _cfg(factors=[{"name": "near_high_250", "sign": 1}],
                   entry_pctile=0.6, exit_pctile=0.9,
                   stop={"enabled": True, "stop_loss_pct": 0.05})
        res = FactorMultiStrategy(cfg).run(daily, daily, "2023-01-01", "2024-01-01",
                                           symbol="600519.SH")
        labels = [t["label"] for t in res["trade_history"]]
        assert any("止损" in lb for lb in labels), f"应触发止损，实际 labels={labels}"

    def test_describe(self):
        desc = FactorMultiStrategy.describe({})
        assert "lowvol_60" in desc and "rps_120" in desc and "near_high_250" in desc
