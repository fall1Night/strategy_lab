# -*- coding: utf-8 -*-
"""策略冒烟测试：用合成日线数据跑全部已注册策略，验证结构与无异常。

不依赖行情网络；重点验证：
  - 重构后的 _fee_cost/_fee_proceeds（走 trading_cost）不报错；
  - 新策略 dual_ma / rsi 可正常生成 equity_curve / trade_history / positions；
  - 回测返回字典结构正确、权益曲线与评估窗口对齐。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from strategylab.engine.config import load_strategy_by_arg
from strategylab.engine.strategies import list_strategies, get_strategy_class


def _make_daily(n: int = 400) -> pd.DataFrame:
    idx = pd.date_range("2022-01-01", periods=n, freq="D")
    # 正弦波 + 缓慢上行，制造多次均线/RSI 交叉
    wave = np.sin(np.linspace(0, 24 * np.pi, n)) * 6.0
    close = 20.0 + wave + np.linspace(0, 8, n)
    return pd.DataFrame({
        "date": idx,
        "open": close,
        "high": close + 1.0,
        "low": close - 1.0,
        "close": close,
        "vol": 1000.0,
    })


def _make_weekly(n: int = 80) -> pd.DataFrame:
    idx = pd.date_range("2022-01-01", periods=n, freq="W")
    close = 20.0 + np.linspace(0, 8, n)
    return pd.DataFrame({
        "date": idx,
        "open": close,
        "high": close + 1.0,
        "low": close - 1.0,
        "close": close,
        "vol": 5000.0,
    })


def test_all_strategies_registered_and_runnable():
    daily = _make_daily()
    weekly = _make_weekly()
    start, end = "2022-06-01", "2023-12-31"

    for alias in list_strategies():
        cfg = load_strategy_by_arg(alias)
        cls = get_strategy_class(cfg["type"])
        strat = cls(cfg)
        res = strat.run(daily, weekly, start, end, symbol="600000.SH", symbol_name="测试")

        assert isinstance(res, dict), f"{alias}: 返回非 dict"
        assert "equity_curve" in res and "trade_history" in res and "positions" in res
        assert len(res["equity_curve"]) > 0, f"{alias}: 权益曲线为空"
        # 权益曲线与评估窗口对齐（约 1.5 年自然日，取子集）
        assert res["equity_curve"][0]["date"] >= start
        # 每点含 date/value
        assert all(set(pt) >= {"date", "value"} for pt in res["equity_curve"])


def test_new_strategies_exist():
    assert "dual_ma" in list_strategies()
    assert "rsi" in list_strategies()


def test_dual_ma_and_rsi_produce_trades():
    daily = _make_daily()
    weekly = _make_weekly()
    start, end = "2022-06-01", "2023-12-31"
    for alias in ("dual_ma", "rsi"):
        cfg = load_strategy_by_arg(alias)
        strat = get_strategy_class(cfg["type"])(cfg)
        res = strat.run(daily, weekly, start, end, symbol="600000.SH", symbol_name="测试")
        # 振荡行情下应至少产生若干笔交易
        assert len(res["trade_history"]) > 0, f"{alias}: 未产生任何交易"
