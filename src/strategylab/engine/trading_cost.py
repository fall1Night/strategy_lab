# -*- coding: utf-8 -*-
"""统一的 A 股交易成本模型。

从开源项目 *OSkhQuant(khTrade.py)* 抽离而来，**去除 xtquant 依赖**，改为纯函数 +
``TradeCostConfig`` 数据类，供 strategy_lab 所有策略共享。

覆盖：佣金(含最低 5 元)、印花税(仅卖方)、过户费(仅沪市)、流量费(每笔固定)、
滑点(tick / ratio 双模式)、T+0 / T+1、整手取整、最大可买量反推。

向后兼容性
----------
默认参数（``min_commission=0``、``transfer_fee_rate=0``、``flow_fee=0``、
``slippage.ratio=0``）下，买入/卖出现金流与 strategy_lab 旧策略各自的
``_fee_cost`` / ``_fee_proceeds`` **完全一致**，因此把现有策略改挂本模块不会改变
任何回测数值；要启用更真实的成本，只需在策略 ``.toml`` 的 ``[params.trade_cost]``
打开对应开关即可。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def is_sh_stock(stock_code: str) -> bool:
    """判断是否为沪市股票（过户费仅沪市收取）。

    兼容 ``sh600216`` / ``600216.SH`` / ``1.600216`` 等多种写法。
    """
    if not stock_code:
        return False
    s = stock_code.lower()
    return s.startswith("sh") or ".sh" in s


@dataclass
class TradeCostConfig:
    """交易成本参数集合。"""

    min_commission: float = 0.0       # 最低佣金(元)，默认 0（保持旧口径）
    commission_rate: float = 0.0003   # 佣金比例(双边)
    stamp_tax_rate: float = 0.0005    # 印花税比例(仅卖方)
    transfer_fee_rate: float = 0.0    # 过户费率(仅沪市)，默认 0（可设 0.00001）
    flow_fee: float = 0.0             # 流量费(元/笔)，默认 0
    slippage: dict = field(default_factory=lambda: {
        "type": "ratio", "tick_size": 0.01, "tick_count": 2, "ratio": 0.0,
    })
    price_decimals: int = 2           # 价格小数位（股票 2 / ETF 3）
    t0_mode: bool = False             # True=当天买入可当天卖出
    lot_size: int = 100               # 每手股数

    @classmethod
    def from_params(cls, params: dict[str, Any] | None) -> "TradeCostConfig":
        """从策略 ``params`` 构建配置。

        优先读取 ``[params.trade_cost]`` 子表；未配置时回退到顶层
        ``commission`` / ``stamp_tax`` / ``lot_size``，保证旧 toml 仍可工作。
        """
        params = params or {}
        tc = params.get("trade_cost") or {}

        def val(key: str, default):
            """优先读 ``[params.trade_cost]`` 子表，其次顶层 ``params``，最后默认值。"""
            if key in tc:
                return tc[key]
            if key in params:
                return params[key]
            return default

        cfg = cls()
        cfg.commission_rate = float(val("commission_rate", params.get("commission", 0.0003)))
        cfg.stamp_tax_rate = float(val("stamp_tax_rate", params.get("stamp_tax", 0.0005)))
        cfg.lot_size = int(val("lot_size", params.get("lot_size", 100)))
        cfg.min_commission = float(val("min_commission", 0.0))
        cfg.transfer_fee_rate = float(val("transfer_fee_rate", 0.0))
        cfg.flow_fee = float(val("flow_fee", 0.0))
        cfg.price_decimals = int(val("price_decimals", 2))
        cfg.t0_mode = bool(val("t0_mode", False))
        slip = tc.get("slippage") or params.get("slippage")
        if isinstance(slip, dict):
            merged = dict(cfg.slippage)
            merged.update(slip)
            cfg.slippage = merged
        return cfg

    # ------------------------------------------------------------------ 滑点
    def apply_slippage(self, price: float, direction: str) -> float:
        """返回考虑滑点后的价格。``direction`` 为 ``"buy"`` / ``"sell"``。

        关键兼容性约定：当滑点实际不产生调整（ratio=0 或 tick_count=0）时，
        直接返回**原始价格**，不做 2 位小数舍入。否则会把 10.0909 这类真实收盘价
        舍入成 10.09，导致既有的 4 个策略回测数值（旧 ``_fee_cost/_fee_proceeds``
        直接用原始收盘价计费的口径）发生偏移。
        """
        slip = self.slippage or {}
        d = self.price_decimals
        stype = slip.get("type", "ratio")
        if stype == "tick":
            tick_size = float(slip.get("tick_size", 0.01))
            tick_count = float(slip.get("tick_count", 2))
            delta = tick_size * tick_count
            if delta == 0:
                return price
            if direction == "buy":
                return round(price + delta, d)
            return round(price - delta, d)
        # ratio 模式：买入上浮 ratio/2，卖出下调 ratio/2（与 khTrade 一致）
        ratio = float(slip.get("ratio", 0.0)) / 2.0
        if ratio == 0:
            return price
        if direction == "buy":
            return round(price * (1 + ratio), d)
        return round(price * (1 - ratio), d)

    # ------------------------------------------------------------------ 费用
    def commission(self, price: float, volume: int) -> float:
        if volume <= 0:
            return 0.0
        c = price * volume * self.commission_rate
        return c if c >= self.min_commission else self.min_commission

    def stamp_tax(self, price: float, volume: int, direction: str) -> float:
        if volume <= 0 or direction != "sell":
            return 0.0
        return price * volume * self.stamp_tax_rate

    def transfer_fee(self, stock_code: str, price: float, volume: int) -> float:
        if volume <= 0 or not is_sh_stock(stock_code):
            return 0.0
        return price * volume * self.transfer_fee_rate

    def flow(self) -> float:
        return self.flow_fee


def trade_cost(price: float, volume: int, direction: str, stock_code: str,
               cfg: TradeCostConfig) -> tuple[float, float]:
    """计算单笔交易的费用分解。

    Returns:
        ``(actual_price, fees)`` —— ``actual_price`` 为考虑滑点后的成交价；
        ``fees`` 为纯费用合计（佣金 + 印花税 + 过户费 + 流量费），**不含本金**。
    """
    if volume <= 0:
        return price, 0.0
    actual = cfg.apply_slippage(price, direction)
    fees = (
        cfg.commission(actual, volume)
        + cfg.stamp_tax(actual, volume, direction)
        + cfg.transfer_fee(stock_code, actual, volume)
        + cfg.flow()
    )
    return actual, fees


def buy_cash_out(price: float, volume: int, stock_code: str,
                 cfg: TradeCostConfig) -> float:
    """买入总现金流出（本金 + 费用）。等价于旧 ``_fee_cost``。"""
    actual, fees = trade_cost(price, volume, "buy", stock_code, cfg)
    return actual * volume + fees


def sell_cash_in(price: float, volume: int, stock_code: str,
                 cfg: TradeCostConfig) -> float:
    """卖出总现金流入（本金 - 费用）。等价于旧 ``_fee_proceeds``。"""
    actual, fees = trade_cost(price, volume, "sell", stock_code, cfg)
    return actual * volume - fees


def max_buy_volume(price: float, cash: float, stock_code: str,
                   cfg: TradeCostConfig, cash_ratio: float = 1.0) -> int:
    """计算可用资金下的最大整手买入量（扣成本后反推）。

    先按粗略口径初估再向下逐步用真实费用校验，保证不会超可用资金。
    """
    if price <= 0 or cash <= 0:
        return 0
    usable = cash * cash_ratio
    est = int(usable / (price * (1 + cfg.commission_rate + cfg.transfer_fee_rate))
              // cfg.lot_size) * cfg.lot_size
    if est < cfg.lot_size:
        return 0
    vol = est
    while vol >= cfg.lot_size:
        if buy_cash_out(price, vol, stock_code, cfg) <= usable:
            return vol
        vol -= cfg.lot_size
    return 0
