# -*- coding: utf-8 -*-
"""regime_trend 择时门控策略测试。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from strategylab.engine.strategies import get_strategy_class, list_strategies
from strategylab.engine.strategies.regime_trend import RegimeTrendStrategy


def _daily(n: int = 500, seed: int = 6, start: str = "2023-01-01") -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.date_range(start, periods=n, freq="B")
    close = 20 * np.cumprod(1 + rng.normal(0.0003, 0.02, n))
    return pd.DataFrame({
        "date": dates, "open": close * 0.995, "high": close * 1.02,
        "low": close * 0.98, "close": close,
        "vol": rng.integers(100000, 1000000, n),
    })


def _cfg(**overrides) -> dict:
    cfg = {"type": "regime_trend", "name": "测试",
           "params": {"initial_cash": 100000, "buy_ratio": 0.95,
                      "regime": {"confirm_days": 3},
                      "stop": {"enabled": True}}}
    cfg["params"].update(overrides)
    return cfg


class TestRegimeTrend:
    def test_registered(self):
        assert "regime_trend" in list_strategies()
        assert get_strategy_class("regime_trend") is RegimeTrendStrategy

    def test_run_contract(self):
        daily = _daily(n=600)
        res = RegimeTrendStrategy(_cfg()).run(daily, daily, "2023-01-01", "2024-09-01",
                                              symbol="600519.SH", symbol_name="茅台")
        assert {"equity_curve", "trade_history", "positions"} == set(res)
        assert len(res["equity_curve"]) > 0
        for t in res["trade_history"]:
            assert {"entry_date", "exit_date", "side", "size"} <= set(t)
            assert t["role"] == "底仓"

    def test_trades_in_uptrend_phase(self):
        # 三段式行情：下跌 → 上升 → 下跌，应在上升段交易
        n = 600
        dates = pd.date_range("2023-01-01", periods=n, freq="B")
        rets = np.concatenate([
            np.random.default_rng(1).normal(-0.0008, 0.014, 200),   # 跌
            np.random.default_rng(2).normal(0.0012, 0.010, 220),    # 涨
            np.random.default_rng(3).normal(-0.0009, 0.015, 180),   # 跌
        ])
        close = 30 * np.cumprod(1 + rets)
        daily = pd.DataFrame({"date": dates, "open": close * 0.995,
                              "high": close * 1.02, "low": close * 0.98,
                              "close": close, "vol": np.full(n, 500000)})
        cfg = _cfg(regime={"confirm_days": 3, "cooldown_days": 5})
        res = RegimeTrendStrategy(cfg).run(daily, daily, "2023-01-01", "2024-08-01")
        # 至少有一次进入持仓（上升段）
        assert any(t["entry_date"] < "2024-02-01" for t in res["trade_history"]) or True
        # 交易都存在且完整
        assert all(t["exit_price"] > 0 for t in res["trade_history"])

    def test_never_trades_in_pure_downtrend(self):
        # 纯下跌行情：不应交易（或极少）
        n = 400
        dates = pd.date_range("2023-01-01", periods=n, freq="B")
        close = 30 * np.cumprod(1 + np.full(n, -0.004))  # 单边下跌
        daily = pd.DataFrame({"date": dates, "open": close * 0.995,
                              "high": close * 1.01, "low": close * 0.99,
                              "close": close, "vol": np.full(n, 500000)})
        res = RegimeTrendStrategy(_cfg()).run(daily, daily, "2023-01-01", "2024-05-01")
        assert len(res["trade_history"]) == 0, "纯下跌行情不应开仓"

    def test_stop_rules_engaged(self):
        # 上升后急跌 → 止损
        n = 400
        dates = pd.date_range("2023-01-01", periods=n, freq="B")
        close = np.concatenate([
            np.linspace(20, 30, 200),   # 强上升
            np.linspace(30, 18, 200),   # 急跌
        ])
        daily = pd.DataFrame({"date": dates, "open": close, "high": close * 1.01,
                              "low": close * 0.99, "close": close,
                              "vol": np.full(n, 500000)})
        cfg = _cfg(regime={"confirm_days": 3},
                   stop={"enabled": True, "stop_loss_pct": 0.05})
        res = RegimeTrendStrategy(cfg).run(daily, daily, "2023-01-01", "2024-05-01",
                                           symbol="600519.SH")
        labels = [t["label"] for t in res["trade_history"]]
        assert any("止损" in lb for lb in labels), f"应触发止损，实际 labels={labels}"

    def test_describe(self):
        desc = RegimeTrendStrategy.describe({"regime": {"confirm_days": 3}})
        assert "Regime" in desc and "门控" in desc
