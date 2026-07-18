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


class BaseStrategy:
    type: str = "base"

    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.params = cfg.get("params", {})

    def run(self, daily, weekly, start, end) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError
