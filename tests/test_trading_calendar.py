# -*- coding: utf-8 -*-
"""trading_calendar 单测（零依赖模式下仅验证周末排除与区间计数）。"""
from __future__ import annotations

from strategylab.engine import trading_calendar as cal


def test_weekend_is_not_trade_day():
    # 2024-01-06 是周六，2024-01-07 是周日
    assert cal.is_trade_day("2024-01-06") is False
    assert cal.is_trade_day("2024-01-07") is False


def test_regular_weekday_is_trade_day():
    # 2024-01-08 是周一（非节假日时，无论是否装 holidays 库都应为交易日）
    assert cal.is_trade_day("2024-01-08") is True


def test_trade_days_count_excludes_weekends():
    # 2024-01-01(周一,元旦) ~ 2024-01-07(周日)：自然日 7 天
    # 至少排除 2 个周末日，且不超过 7
    n = cal.get_trade_days_count("2024-01-01", "2024-01-07")
    assert 0 < n <= 7
    # 该区间含 2 个周末日(6,7)，所以交易日 <= 5
    assert n <= 5


def test_is_trade_time_returns_bool():
    assert isinstance(cal.is_trade_time(), bool)
