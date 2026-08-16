# -*- coding: utf-8 -*-
"""trading_cost 模块单测：覆盖默认口径向后兼容 + 各费用项。"""
from __future__ import annotations

import math

import pytest

from strategylab.engine.trading_cost import (
    TradeCostConfig,
    buy_cash_out,
    sell_cash_in,
    max_buy_volume,
    is_sh_stock,
)


def _cfg(**over):
    return TradeCostConfig.from_params({"commission": 0.0003, "stamp_tax": 0.0005, **over})


def test_default_cfg_backward_compatible_with_old_formula():
    """默认参数下，买/卖现金流必须与旧策略 _fee_cost/_fee_proceeds 完全一致。"""
    cfg = _cfg()
    size, price = 1000, 10.0
    # 旧：size*price*(1+commission)
    assert buy_cash_out(price, size, "600000.SH", cfg) == pytest.approx(size * price * 1.0003)
    # 旧：size*price*(1-commission-stamp_tax)
    assert sell_cash_in(price, size, "600000.SH", cfg) == pytest.approx(size * price * (1 - 0.0003 - 0.0005))


def test_min_commission():
    cfg = _cfg(min_commission=5.0)
    # 小笔交易佣金不足 5 元 → 取 5 元
    fees = buy_cash_out(1.0, 100, "600000.SH", cfg) - 1.0 * 100
    assert fees == pytest.approx(5.0)


def test_transfer_fee_sh_only():
    cfg = _cfg(transfer_fee_rate=0.00001)
    sh = buy_cash_out(10.0, 1000, "600000.SH", cfg)
    sz = buy_cash_out(10.0, 1000, "000001.SZ", cfg)
    # 沪市多收过户费 10*1000*0.00001 = 0.1 元
    assert sh - sz == pytest.approx(0.1)


def test_flow_fee_per_trade():
    cfg = _cfg(flow_fee=0.1)
    fees = buy_cash_out(10.0, 1000, "600000.SH", cfg) - 10.0 * 1000 * 1.0003
    assert fees == pytest.approx(0.1)


def test_slippage_ratio():
    cfg = _cfg(slippage={"type": "ratio", "ratio": 0.002})
    # 买上浮 ratio/2 = 0.001 → 10*(1.001)=10.01（2 位小数可精确表示）
    actual, _ = __import__("strategylab.engine.trading_cost", fromlist=["trade_cost"]).trade_cost(
        10.0, 100, "buy", "600000.SH", cfg
    )
    assert actual == pytest.approx(10.01)


def test_slippage_tick():
    cfg = _cfg(slippage={"type": "tick", "tick_size": 0.01, "tick_count": 2})
    # 买上浮 0.02
    actual, _ = __import__("strategylab.engine.trading_cost", fromlist=["trade_cost"]).trade_cost(
        10.0, 100, "buy", "600000.SH", cfg
    )
    assert actual == pytest.approx(10.02)
    # 卖下调 0.02
    actual_s, _ = __import__("strategylab.engine.trading_cost", fromlist=["trade_cost"]).trade_cost(
        10.0, 100, "sell", "600000.SH", cfg
    )
    assert actual_s == pytest.approx(9.98)


def test_max_buy_volume_default():
    cfg = _cfg()
    # 200000 元，价格 10，佣金 0.0003 → 约可买 19994 股 → 整手 19900
    vol = max_buy_volume(10.0, 200000, "600000.SH", cfg, cash_ratio=1.0)
    assert vol % 100 == 0
    assert vol > 0
    # 校验不超过资金
    assert buy_cash_out(10.0, vol, "600000.SH", cfg) <= 200000


def test_no_slippage_keeps_raw_price_precision():
    """回归保护：滑点为 0（默认）时不得把非 2 位小数收盘价舍入，否则会改变
    既有策略回测数值（旧 _fee_cost/_fee_proceeds 直接用原始收盘价计费）。"""
    cfg = _cfg()  # ratio=0 → 不施加滑点，也不应舍入
    raw = 10.090909090909092  # 非 2 位小数，模拟真实收盘价
    size = 9900
    # 期望：用原始价计费，等价于 size*raw*(1+commission)
    assert buy_cash_out(raw, size, "600000.SH", cfg) == pytest.approx(size * raw * 1.0003)
    # 若实现错误地 round(raw, 2)=10.09，则结果会偏 99920.97 而非 99929.97
    assert abs(buy_cash_out(raw, size, "600000.SH", cfg) - 99920.0) > 5


def test_is_sh_stock():
    assert is_sh_stock("600000.SH")
    assert is_sh_stock("sh600000")
    assert not is_sh_stock("000001.SZ")
    assert not is_sh_stock("")
