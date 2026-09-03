# -*- coding: utf-8 -*-
"""Regime 择时状态机测试（批次A）。"""
from __future__ import annotations

from collections import Counter

import numpy as np
import pandas as pd

from strategylab.engine.regime import RegimeDetector


def _three_phase_df() -> pd.DataFrame:
    """震荡(0-250) → 强势上升(250-550) → 下跌(550-800)"""
    rng = np.random.default_rng(7)
    rets = np.concatenate([
        rng.normal(0.0001, 0.012, 250),
        rng.normal(0.0012, 0.010, 300),
        rng.normal(-0.0010, 0.014, 250),
    ])
    close = 3000 * np.cumprod(1 + rets)
    dates = pd.date_range("2023-01-01", periods=800, freq="B")
    return pd.DataFrame({"close": close, "high": close * 1.008, "low": close * 0.992},
                        index=dates)


def _run(df, **cfg) -> list[str]:
    rd = RegimeDetector({"confirm_days": 3, **cfg})
    states = []
    for i in range(50, len(df)):  # 前 50 天 warmup
        states.append(rd.update(df.iloc[:i + 1]))
    return states


class TestRegimeDetector:
    def test_state_transitions(self):
        df = _three_phase_df()
        states = _run(df)
        cnt = Counter(states)
        # 三段行情应识别出多种状态（至少 2 种）
        assert len(cnt) >= 2
        # 下跌段应最终落到 downtrend（或至少出现）
        assert "downtrend" in cnt

    def test_can_trade_only_in_uptrends(self):
        df = _three_phase_df()
        states = _run(df)
        rd = RegimeDetector({"confirm_days": 3})
        for i in range(50, len(df)):
            rd.update(df.iloc[:i + 1])
        # choppy/panic/downtrend 不可开新仓
        if rd._current in ("strong_uptrend", "uptrend_volatile"):
            assert rd.can_trade()
        else:
            assert not rd.can_trade()

    def test_cash_ratio_mapping(self):
        rd = RegimeDetector()
        rd._current = "panic"
        rd.state_days = 10
        assert rd.cash_ratio() == 1.0
        rd._current = "strong_uptrend"
        rd.state_days = 10
        assert rd.cash_ratio() == 0.0

    def test_uncertain_transition_cash_floor(self):
        rd = RegimeDetector({"cash_map": {"strong_uptrend": 0.0}})
        rd._current = "strong_uptrend"
        rd.state_days = 1  # 刚切换
        # 不确定态：现金比例不低于 50%
        assert rd.cash_ratio() >= 0.5

    def test_health_report_shape(self):
        df = _three_phase_df()
        rd = RegimeDetector({"confirm_days": 3})
        for i in range(50, len(df)):
            rd.update(df.iloc[:i + 1])
        h = rd.health_report()
        assert set(h) == {"state", "cash_ratio", "can_trade", "state_days",
                          "switch_count", "healthy", "note"}

    def test_cooldown_freezes_switch(self):
        df = _three_phase_df()
        rd = RegimeDetector({"confirm_days": 3, "cooldown_days": 5})
        for i in range(50, 200):
            rd.update(df.iloc[:i + 1])
        state_before = rd._current
        # 强制进入冷却期，喂上升行情，验证状态冻结
        rd._cooldown_left = 5
        s = rd.update(df.iloc[:400])
        assert s == state_before
        assert rd._current == state_before  # 冷却期内不切换
        assert rd._cooldown_left == 4       # 冷却倒计时推进

    def test_adx_static(self):
        df = _three_phase_df()
        adx = RegimeDetector.adx(df["high"], df["low"], df["close"])
        assert adx.isna().sum() < len(adx)  # 非全 NaN
