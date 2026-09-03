# -*- coding: utf-8 -*-
"""indicator_combo 指标共振策略测试。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from strategylab.engine.strategies import get_strategy_class, list_strategies
from strategylab.engine.strategies.indicator_combo import IndicatorComboStrategy


def _daily(n: int = 500, seed: int = 5, start: str = "2023-01-01") -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.date_range(start, periods=n, freq="B")
    close = 20 * np.cumprod(1 + rng.normal(0.0003, 0.02, n))
    return pd.DataFrame({
        "date": dates, "open": close * 0.995, "high": close * 1.02,
        "low": close * 0.98, "close": close,
        "vol": rng.integers(100000, 1000000, n),
    })


def _cfg(**overrides) -> dict:
    cfg = {"type": "indicator_combo", "name": "测试",
           "params": {"initial_cash": 100000, "buy_ratio": 0.95,
                      "stop": {"enabled": True}}}
    cfg["params"].update(overrides)
    return cfg


class TestIndicatorCombo:
    def test_registered(self):
        assert "indicator_combo" in list_strategies()
        assert get_strategy_class("indicator_combo") is IndicatorComboStrategy

    def test_run_contract(self):
        daily = _daily()
        res = IndicatorComboStrategy(_cfg()).run(daily, daily, "2023-01-01", "2024-09-01",
                                                 symbol="600519.SH", symbol_name="茅台")
        assert {"equity_curve", "trade_history", "positions"} == set(res)
        assert len(res["equity_curve"]) > 0
        assert {"date", "value"} == set(res["equity_curve"][0])
        for t in res["trade_history"]:
            assert {"entry_date", "exit_date", "side", "size", "entry_price",
                    "exit_price", "pnl", "pnl_pct"} <= set(t)
            assert t["role"] == "底仓"

    def test_produces_trades(self):
        # 强趋势 + 波动行情应触发共振信号
        daily = _daily(n=600, seed=9)
        res = IndicatorComboStrategy(_cfg(ma_filter=0)).run(daily, daily,
                                                            "2023-01-01", "2024-09-01")
        assert len(res["equity_curve"]) > 100
        assert res["trade_history"], "应至少产生一笔交易"

    def test_disciplined_exit_on_crash(self):
        # 持仓后暴跌：卖出理由应是纪律性卖出（止损 或 MACD死叉/RSI超买），而非纯获利了结
        n = 300
        dates = pd.date_range("2023-01-01", periods=n, freq="B")
        close = np.concatenate([
            np.full(80, 20.0),          # 横盘（MACD 先归零）
            np.linspace(20, 28, 120),   # 上涨（触发买入）
            np.linspace(28, 15, 100),   # 暴跌
        ])
        daily = pd.DataFrame({"date": dates, "open": close, "high": close * 1.01,
                              "low": close * 0.99, "close": close,
                              "vol": np.full(n, 500000)})
        cfg = _cfg(ma_filter=0, rsi_oversold=50, rsi_overbought=80,
                   stop={"enabled": True, "stop_loss_pct": 0.05})
        res = IndicatorComboStrategy(cfg).run(daily, daily, "2023-01-01", "2024-01-01",
                                              symbol="600519.SH")
        labels = [t["label"] for t in res["trade_history"]]
        assert labels, "暴跌行情应产生交易"
        # 止损接线由 StopRules 单测 + 其他策略测试覆盖；此处验证存在纪律性卖出
        assert any(("止损" in lb) or ("死叉" in lb) or ("超买" in lb) for lb in labels), (
            f"应存在纪律性卖出，实际 labels={labels}")

    def test_ma_filter_off(self):
        # ma_filter=0 时不要求价格在均线上方
        daily = _daily(seed=21)
        res = IndicatorComboStrategy(_cfg(ma_filter=0)).run(daily, daily,
                                                            "2023-01-01", "2024-09-01")
        assert len(res["equity_curve"]) > 100

    def test_unknown_params_ok(self):
        # 未知参数不应崩溃
        daily = _daily()
        res = IndicatorComboStrategy(_cfg(foo="bar")).run(daily, daily,
                                                          "2023-01-01", "2024-09-01")
        assert res["equity_curve"]

    def test_describe(self):
        desc = IndicatorComboStrategy.describe({"macd_fast": 12, "rsi_period": 14,
                                                "ma_filter": 200})
        assert "MACD" in desc and "RSI" in desc and "MA200" in desc
