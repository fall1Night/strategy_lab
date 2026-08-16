# -*- coding: utf-8 -*-
"""策略基类。

每个策略 = 一个子类，约定：
  - 类属性 `type` 字符串，与策略配置 .toml 的 [type] 对应；
  - `run(daily, weekly, start, end)` 返回
        {"equity_curve": [...], "trade_history": [...], "positions": [...]}
    equity_curve: [{"date","value"}]
    trade_history: 每笔成交(底仓/做T) dict，含 role/position_id 便于合并展示
    positions: 已合并的持仓组（一个底仓 + 其做T），用于成交明细表
新增策略只需在 strategies/ 下加一个子类并注册到 registry。
"""
from __future__ import annotations

from typing import Any

from ..trading_cost import (
    TradeCostConfig,
    buy_cash_out,
    sell_cash_in,
)


class BaseStrategy:
    type: str = "base"

    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.params = cfg.get("params", {})
        # 统一的交易成本配置（从 params 解析，默认与旧策略口径一致）
        self.cost_cfg: TradeCostConfig = TradeCostConfig.from_params(self.params)

    def run(self, daily, weekly, start, end) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError

    # ------------------------------------------------------------------
    # 共享交易成本钩子（取代各策略重复内联的 _fee_cost / _fee_proceeds）
    # ------------------------------------------------------------------
    def _fee_cost(self, size, price) -> float:
        """买入总现金流出（本金 + 费用），等价于旧 ``size*price*(1+commission)``。"""
        return buy_cash_out(price, size, getattr(self, "symbol", ""), self.cost_cfg)

    def _fee_proceeds(self, size, price) -> float:
        """卖出总现金流入（本金 - 费用），等价于旧 ``size*price*(1-commission-stamp_tax)``。"""
        return sell_cash_in(price, size, getattr(self, "symbol", ""), self.cost_cfg)
