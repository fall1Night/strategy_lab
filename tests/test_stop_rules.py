# -*- coding: utf-8 -*-
"""止损/止盈规则引擎测试（批次A，复用源项目 8 场景）。"""
from __future__ import annotations

from strategylab.engine.risk.stop_rules import StopRules, StopType

CFG = {
    "stop_loss_pct": 0.07, "stop_loss_below_pivot": 0.08,
    "time_stop_weeks": 3, "time_stop_min_gain": 0.03,
    "profit_take_low": 0.20, "profit_take_high": 0.25,
    "trailing_stop_ma": 50, "high_drawdown_exit": 0.08, "atr_stop_mult": 2.0,
}


def _sr():
    return StopRules(CFG)


class TestStopRules:
    def test_normal_hold(self):
        d = _sr().check(entry_price=10, current_price=10.5, hold_weeks=2)
        assert not d.should_sell
        assert d.action == "持有"

    def test_hard_loss(self):
        d = _sr().check(entry_price=10, current_price=9.2, hold_weeks=2)  # -8%
        assert d.should_sell
        assert d.action == "卖出"
        assert StopType.HARD_LOSS in [t for t, _ in d.reasons]

    def test_time_stop(self):
        d = _sr().check(entry_price=10, current_price=10.2, hold_weeks=4)  # 4周+2%<3%
        assert d.should_sell
        assert StopType.TIME_STOP in [t for t, _ in d.reasons]

    def test_atr_stop(self):
        # 止损价 10-2*0.35=9.3，9.2 已破
        d = _sr().check(entry_price=10, current_price=9.2, atr=0.35, hold_weeks=2)
        assert d.should_sell
        assert StopType.ATR_STOP in [t for t, _ in d.reasons]

    def test_trailing_ma(self):
        d = _sr().check(entry_price=10, current_price=9.8, ma_50=9.9, hold_weeks=2)
        assert d.should_sell
        assert StopType.TRAILING_MA in [t for t, _ in d.reasons]

    def test_trailing_high(self):
        # 10.2/11.2-1=-8.9% ≤ -8%
        d = _sr().check(entry_price=10, current_price=10.2, peak_price=11.2, hold_weeks=2)
        assert d.should_sell
        assert StopType.TRAILING_HIGH in [t for t, _ in d.reasons]

    def test_take_profit_partial(self):
        d = _sr().check(entry_price=10, current_price=12.2, hold_weeks=2)  # +22%
        assert d.action == "分批止盈(卖50%)"
        assert not d.should_sell  # 分批止盈不触发全额卖出

    def test_take_profit_full(self):
        d = _sr().check(entry_price=10, current_price=12.8, hold_weeks=2)  # +28%
        assert d.should_sell
        assert d.action == "全部止盈"
        assert StopType.TAKE_PROFIT in [t for t, _ in d.reasons]

    def test_invalid_price_no_crash(self):
        d = _sr().check(entry_price=10, current_price=0)
        assert not d.should_sell

    def test_explicit_take_profit_ratio(self):
        d = _sr().check(entry_price=10, current_price=10, hold_weeks=1,
                        take_profit_ratio=0.26)
        assert d.should_sell
        assert d.action == "全部止盈"
