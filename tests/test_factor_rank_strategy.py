# -*- coding: utf-8 -*-
"""factor_rank 策略契约测试（批次D）。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from strategylab.engine.strategies import get_strategy_class, list_strategies
from strategylab.engine.strategies.factor_rank import FactorRankStrategy


def _daily(n: int = 400, seed: int = 3, start: str = "2023-01-01") -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.date_range(start, periods=n, freq="B")
    close = 20 * np.cumprod(1 + rng.normal(0.0003, 0.02, n))
    return pd.DataFrame({
        "date": dates,
        "open": close * 0.995,
        "high": close * 1.02,
        "low": close * 0.98,
        "close": close,
        "vol": rng.integers(100000, 1000000, n),
    })


class TestFactorRankStrategy:
    def test_registered(self):
        assert "factor_rank" in list_strategies()
        assert get_strategy_class("factor_rank") is FactorRankStrategy

    def test_run_contract(self):
        daily = _daily()
        cfg = {
            "type": "factor_rank", "name": "测试",
            "params": {
                "factor": "lowvol_60", "direction": -1,
                "lookback": 30, "entry_pctile": 0.3, "exit_pctile": 0.7,
                "initial_cash": 100000, "buy_ratio": 0.95,
                "stop": {"enabled": True, "stop_loss_pct": 0.07},
            },
        }
        res = FactorRankStrategy(cfg).run(daily, daily, "2023-01-01", "2024-02-01",
                                          symbol="600519.SH", symbol_name="茅台")
        # 契约：三个输出
        assert {"equity_curve", "trade_history", "positions"} == set(res)
        assert len(res["equity_curve"]) > 0
        # equity_curve 结构
        first = res["equity_curve"][0]
        assert {"date", "value"} == set(first)
        # trade_history 结构（若有交易）
        for t in res["trade_history"]:
            assert {"entry_date", "exit_date", "side", "size", "entry_price",
                    "exit_price", "pnl", "pnl_pct", "holding_bars"} <= set(t)
        # positions 结构
        for ps in res["positions"]:
            assert "base_size" in ps and "entry_date" in ps and "exit_date" in ps

    def test_factor_rank_produces_trades(self):
        # 构造强波动行情，确保因子分位触发买卖
        daily = _daily(n=600, seed=11)
        cfg = {
            "type": "factor_rank",
            "params": {
                "factor": "mom_20", "direction": -1,  # 超跌反弹
                "lookback": 40, "entry_pctile": 0.25, "exit_pctile": 0.75,
                "initial_cash": 100000, "buy_ratio": 0.95,
                "stop": {"enabled": False},
            },
        }
        res = FactorRankStrategy(cfg).run(daily, daily, "2023-01-01", "2024-07-01",
                                          symbol="600519.SH")
        assert len(res["equity_curve"]) > 100
        # 期末无持仓（强制平仓）
        assert res["trade_history"]
        for t in res["trade_history"]:
            assert t["role"] == "底仓"
            assert t["symbol"] == "600519.SH"

    def test_stop_rules_engaged(self):
        # 持仓后价格持续下跌 → 硬止损触发（-7%）
        n = 300
        dates = pd.date_range("2023-01-01", periods=n, freq="B")
        close = np.concatenate([
            np.linspace(20, 25, 100),    # 上涨
            np.linspace(25, 15, 200),    # 持续下跌
        ])
        daily = pd.DataFrame({
            "date": dates, "open": close, "high": close * 1.01,
            "low": close * 0.99, "close": close,
            "vol": np.full(n, 500000),
        })
        cfg = {
            "type": "factor_rank",
            "params": {
                "factor": "lowvol_60", "direction": -1,
                "lookback": 30, "entry_pctile": 0.5, "exit_pctile": 0.9,
                "initial_cash": 100000, "buy_ratio": 0.95,
                "stop": {"enabled": True, "stop_loss_pct": 0.07},
            },
        }
        res = FactorRankStrategy(cfg).run(daily, daily, "2023-01-01", "2024-03-01",
                                          symbol="600519.SH")
        labels = [t["label"] for t in res["trade_history"]]
        assert any("止损" in lb for lb in labels), f"应触发止损，实际 labels={labels}"

    def test_unknown_factor_raises(self):
        daily = _daily()
        cfg = {"type": "factor_rank",
               "params": {"factor": "not_a_factor"}}
        with pytest.raises(ValueError):
            FactorRankStrategy(cfg).run(daily, daily, "2023-01-01", "2024-01-01")

    def test_describe(self):
        desc = FactorRankStrategy.describe({
            "factor": "rps_120", "direction": -1, "lookback": 60,
            "entry_pctile": 0.3, "exit_pctile": 0.7,
            "stop": {"enabled": True, "stop_loss_pct": 0.07},
        })
        assert "rps_120" in desc and "反转" in desc and "止损" in desc
