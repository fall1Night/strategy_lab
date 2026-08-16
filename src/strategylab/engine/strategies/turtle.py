# -*- coding: utf-8 -*-
"""海龟交易法则（Turtle Trading Rules）— 唐奇安通道突破 + ATR 止损 + 海龟单位仓位。

策略逻辑（参数全部来自 .toml，可按需改）：
  - 入场：当日收盘价 > 过去 ``entry_lookback``(默认 20) 个交易日的最高收盘价
          （唐奇安通道上轨，shift(1) 避免未来函数）→ 买入。
  - 离场：持仓后，当日收盘价 < 过去 ``exit_lookback``(默认 10) 个交易日的最低收盘价
          （唐奇安通道下轨）→ 卖出（通道离场）。
  - ATR 止损（默认开启）：建仓后，价格自建仓价回落超过
          ``atr_stop_multiple``(默认 2) × ATR 时止损离场。
  - 仓位管理（海龟单位）：每笔风险固定为账户权益的 ``risk_per_trade``(默认 1%)，
          按 ATR 折算股数，并受单笔买入额上限 ``base_buy_amount`` 约束，避免小 ATR 时过度杠杆。

执行价：当日信号 + 当日收盘（与现有策略一致的 ``same_day_close`` 声明，含 look-ahead 偏差）。
海龟是「一笔清仓后才开下一笔」的日线系统，因此不做「做T」，role 恒为「底仓」。
"""
from __future__ import annotations

import math

import pandas as pd

from .base import BaseStrategy


class TurtleStrategy(BaseStrategy):
    type = "turtle"

    # ------------------------------------------------------------------ describe
    @staticmethod
    def describe(params: dict) -> str:
        """海龟交易法则（唐奇安通道突破 + ATR 止损）策略说明。

        文案严格对齐 engine/strategies/turtle.py 的实现，不改动任何策略逻辑。
        所有键访问均兜底，缺键时使用与 turtle.toml 一致的默认值。
        """
        turtle = params.get("turtle") or {}
        entry_lookback = turtle.get("entry_lookback", 20)
        exit_lookback = turtle.get("exit_lookback", 10)
        atr_period = turtle.get("atr_period", 14)
        atr_stop_multiple = turtle.get("atr_stop_multiple", 2.0)
        use_atr_stop = turtle.get("use_atr_stop", True)
        risk_per_trade = turtle.get("risk_per_trade", 0.01)
        trailing_stop = turtle.get("trailing_stop", False)
        base_buy_amount_wan = (params.get("base_buy_amount") or 0) / 10000
        comm = (params.get("commission") or 0) * 10000
        tax = (params.get("stamp_tax") or 0) * 10000
        lot_size = params.get("lot_size") or 100

        return (
            f"- 入场：日线收盘价突破过去 {entry_lookback} 日最高价买入；"
            f"唐奇安通道使用 shift(1) 对齐（取前一日通道上轨对比当日收盘），无未来函数。\n"
            f"- 离场（优先级：先 ATR 止损，后通道离场）：\n"
            f"  ① ATR 止损——持仓回撤超过 {atr_stop_multiple}× ATR（ATR 周期 {atr_period}）即离场"
            f"（use_atr_stop={str(use_atr_stop).lower()}）；\n"
            f"  ② 通道离场——收盘价跌破过去 {exit_lookback} 日最低价离场。\n"
            f"- 仓位（海龟单位 N）：N = 单笔风险({risk_per_trade:.0%}) × 账户权益 / "
            f"({atr_stop_multiple} × ATR)；按 N 估算可买股数；"
            f"base_buy_amount={base_buy_amount_wan:.0f}万 上限约束，避免小 ATR 时过度杠杆；"
            f"trailing_stop={str(trailing_stop).lower()}（止损基线不随新高上移）。\n"
            f"- 执行价：当日信号 + 当日收盘（含 look-ahead 偏差）；"
            f"A股 T+1 / {lot_size}股整手 / 佣金{comm:.0f}bp双边 / 印花税{tax:.0f}bp卖方 / 前复权 qfq。"
        )

    # ------------------------------------------------------------------ helpers
    # 以下 _close_base / positions 合并结构与 KdjMacdDualEntry 完全一致，改策略时请保持原样。
    # 费税计算已上移至 BaseStrategy（统一走 trading_cost 模块）。
    def _close_base(self, base_trade, exit_price, exit_date, exit_bar, trade_history, label="底仓清仓"):
        """写一笔底仓平仓到 trade_history，返回回款 proceeds。"""
        size = base_trade["size"]
        entry_price = base_trade["entry_price"]
        cost = self._fee_cost(size, entry_price)
        proceeds = self._fee_proceeds(size, exit_price)
        pnl = proceeds - cost
        pnl_pct = pnl / cost * 100.0 if cost else 0.0
        trade_history.append({
            "entry_date": base_trade["entry_date"], "exit_date": exit_date,
            "side": "long", "size": size,
            "entry_price": round(entry_price, 4), "exit_price": round(exit_price, 4),
            "pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 2),
            "holding_bars": exit_bar - base_trade["entry_bar"],
            "symbol": self.symbol, "symbol_name": self.symbol_name,
            "label": label, "role": "底仓",
            "position_id": base_trade.get("position_id", ""),
        })
        return proceeds

    def _build_positions(self, trade_history):
        """合并成交明细：一个持仓组 = 一笔底仓（海龟无做T，trades 为空）。"""
        groups = {}
        for t in trade_history:
            pid = t.get("position_id", "")
            groups.setdefault(pid, {"base": None, "ts": []})
            if t.get("role") == "底仓":
                groups[pid]["base"] = t
            else:
                groups[pid]["ts"].append(t)

        positions = []
        for pid, g in groups.items():
            b = g["base"]
            if not b:
                continue
            ts = g["ts"]
            total_pnl = float(b["pnl"]) + sum(float(t["pnl"]) for t in ts)
            invested = float(b["size"]) * float(b["entry_price"])
            total_pct = (total_pnl / invested * 100.0) if invested else 0.0
            exit_px = float(b["exit_price"])
            t_hold_pnl = sum(
                float(t["size"]) * (exit_px - float(t["entry_price"])) for t in ts
            )
            total_hold_pnl = float(b["pnl"]) + t_hold_pnl
            total_hold_pct = (total_hold_pnl / invested * 100.0) if invested else 0.0
            sub = []
            for t in sorted(ts, key=lambda x: (x["entry_date"], x["exit_date"])):
                sub.append({
                    "entry_date": t["entry_date"], "exit_date": t["exit_date"],
                    "size": int(t["size"]), "entry_price": round(float(t["entry_price"]), 4),
                    "exit_price": round(float(t["exit_price"]), 4),
                    "holding_bars": t.get("holding_bars", 0),
                    "pnl": round(float(t["pnl"]), 2), "pnl_pct": round(float(t["pnl_pct"]), 2),
                })
            positions.append({
                "position_id": pid, "entry_date": b["entry_date"], "exit_date": b["exit_date"],
                "side": b.get("side", "long"), "base_size": int(b["size"]),
                "entry_price": round(float(b["entry_price"]), 4), "exit_price": round(float(b["exit_price"]), 4),
                "holding_bars": b.get("holding_bars", 0), "base_pnl": round(float(b["pnl"]), 2),
                "base_pnl_pct": round(float(b["pnl_pct"]), 2), "t_count": len(sub),
                "total_pnl": round(total_pnl, 2), "total_pnl_pct": round(total_pct, 2),
                "hold_no_t_pct": round(float(b["pnl_pct"]), 2),
                "t_hold_to_exit_pct": round(total_hold_pct, 2),
                "trades": sub,
            })
        positions.sort(key=lambda p: p["entry_date"])
        return positions

    # ------------------------------------------------------------------ indicators
    @staticmethod
    def _donchian(close: pd.Series, lookback: int) -> pd.Series:
        """唐奇安通道：过去 ``lookback`` 日最高/最低收盘价，shift(1) 避免未来函数。"""
        return close.rolling(lookback).max().shift(1)

    @staticmethod
    def _donchian_low(close: pd.Series, lookback: int) -> pd.Series:
        return close.rolling(lookback).min().shift(1)

    @staticmethod
    def _atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
        """平均真实波幅（SMA），shift(1) 确保只用历史数据。"""
        prev_close = close.shift(1)
        tr = pd.concat(
            [
                (high - low),
                (high - prev_close).abs(),
                (low - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        return tr.rolling(period).mean().shift(1)

    # ------------------------------------------------------------------ main
    def run(self, daily: pd.DataFrame, weekly: pd.DataFrame, start, end,
            symbol: str = "", symbol_name: str = "") -> dict:
        p = self.params
        self.symbol = symbol
        self.symbol_name = symbol_name
        self.commission = float(p["commission"])
        self.stamp_tax = float(p["stamp_tax"])
        self.lot_size = int(p["lot_size"])
        initial_cash = float(p["initial_cash"])
        base_buy = float(p["base_buy_amount"])

        tp = p.get("turtle", {})
        entry_lookback = int(tp.get("entry_lookback", 20))
        exit_lookback = int(tp.get("exit_lookback", 10))
        atr_period = int(tp.get("atr_period", 14))
        atr_stop_multiple = float(tp.get("atr_stop_multiple", 2.0))
        use_atr_stop = bool(tp.get("use_atr_stop", True))
        risk_per_trade = float(tp.get("risk_per_trade", 0.01))
        trailing_stop = bool(tp.get("trailing_stop", False))

        # ---- 指标（仅用历史数据，无未来函数）----
        daily = daily.sort_values("date").reset_index(drop=True)
        close = daily["close"].astype(float)
        high = daily["high"].astype(float)
        low = daily["low"].astype(float)

        donchian_high = self._donchian(close, entry_lookback)
        donchian_low = self._donchian_low(close, exit_lookback)
        atr = self._atr(high, low, close, atr_period)

        # ---- 状态 ----
        cash = initial_cash
        holding = False
        pid_counter = 0
        position_id = None
        entry_price = 0.0
        entry_atr = 0.0
        entry_bar = 0
        entry_date = ""
        size = 0
        atr_stop_price = float("inf")
        highest_close = 0.0
        trade_history: list = []
        equity_curve: list = []

        eval_start_ts = pd.Timestamp(start)
        eval_end_ts = pd.Timestamp(end)
        n = len(daily)

        last_eval_date = None
        last_eval_close = None
        last_eval_bar = None

        for i in range(n):
            row = daily.iloc[i]
            date = row["date"]
            date_str = date.strftime("%Y-%m-%d")
            close_i = float(row["close"])

            if date < eval_start_ts or date > eval_end_ts:
                continue

            # 记录最后一个评估日（用于期末强制平仓，避免用到窗口外未来价格）
            last_eval_date = date_str
            last_eval_close = close_i
            last_eval_bar = i

            dh = float(donchian_high.iloc[i]) if pd.notna(donchian_high.iloc[i]) else float("nan")
            dl = float(donchian_low.iloc[i]) if pd.notna(donchian_low.iloc[i]) else float("nan")
            atr_i = float(atr.iloc[i]) if pd.notna(atr.iloc[i]) else float("nan")

            # ============ 空仓：唐奇安上轨突破买入 ============
            if not holding:
                if pd.notna(dh) and close_i > dh:
                    # 海龟单位：让一次「atr_stop_multiple × ATR」的止损恰好亏掉
                    # risk_per_trade 的账户权益。即 shares × (atr_stop_multiple × atr)
                    # = cash × risk_per_trade，故 shares = unit_risk / (atr_stop_multiple × atr)。
                    # 注意：分母不含 close（close 会让股数变成 1/¥ 量级、高价股被每手取整归零，
                    # 导致有突破信号却零成交）。小 ATR 时股数偏大，由下方 base_buy 上限约束兜底。
                    if atr_i > 0:
                        unit_risk = cash * risk_per_trade
                        shares = int(math.floor(unit_risk / (atr_stop_multiple * atr_i)))
                    else:
                        # ATR 无效时回退用固定单笔买入额折算股数
                        shares = int(math.floor(base_buy / close_i))
                    # 单笔买入额上限约束（避免小 ATR 时过度杠杆）
                    if shares * close_i > base_buy:
                        shares = int(math.floor(base_buy / close_i))
                    # 取整到每手
                    shares = (shares // self.lot_size) * self.lot_size
                    cost_needed = self._fee_cost(shares, close_i) if shares > 0 else 0.0
                    if shares > 0 and cash >= cost_needed:
                        cash -= cost_needed
                        pid_counter += 1
                        position_id = pid_counter
                        entry_price = close_i
                        entry_atr = atr_i
                        entry_bar = i
                        entry_date = date_str
                        size = shares
                        holding = True
                        highest_close = close_i
                        if entry_atr > 0:
                            atr_stop_price = entry_price - atr_stop_multiple * entry_atr
                        else:
                            atr_stop_price = float("inf")
                equity_curve.append({
                    "date": date_str,
                    "value": round(cash + (size if holding else 0) * close_i, 2),
                })
                continue

            # ============ 持仓：先 ATR 止损，再通道离场 ============
            exit_now = False
            label = ""
            if use_atr_stop and entry_atr > 0 and close_i < atr_stop_price:
                exit_now = True
                label = "ATR止损"
            elif pd.notna(dl) and close_i < dl:
                exit_now = True
                label = "通道离场"

            # 可选：ATR 止损基线随新高上移（trailing），默认关闭
            if trailing_stop and entry_atr > 0:
                highest_close = max(highest_close, close_i)
                candidate = highest_close - atr_stop_multiple * entry_atr
                if candidate > atr_stop_price:
                    atr_stop_price = candidate

            if exit_now:
                base_trade = {
                    "entry_date": entry_date, "entry_price": entry_price,
                    "size": size, "entry_bar": entry_bar, "position_id": position_id,
                }
                cash += self._close_base(base_trade, close_i, date_str, i, trade_history, label=label)
                holding = False
                position_id = None
                size = 0
                atr_stop_price = float("inf")

            equity_curve.append({"date": date_str, "value": round(cash + size * close_i, 2)})

        # ---- 期末强制平仓（按最后一根评估日收盘）----
        if holding and size > 0 and last_eval_date is not None:
            base_trade = {
                "entry_date": entry_date, "entry_price": entry_price,
                "size": size, "entry_bar": entry_bar, "position_id": position_id,
            }
            cash += self._close_base(
                base_trade, last_eval_close, last_eval_date, last_eval_bar,
                trade_history, label="期末平仓",
            )
            holding = False
            size = 0
        if equity_curve:
            equity_curve[-1]["value"] = round(cash, 2)

        # 排序（仅展示顺序）
        trade_history.sort(key=lambda t: (t["entry_date"], t["exit_date"]))

        # 防御性自检：禁止「未清仓又开仓」（海龟天然不应重叠，保留兜底）
        base_intervals = sorted(
            [(t["entry_date"], t["exit_date"]) for t in trade_history if t.get("role") == "底仓"],
            key=lambda x: x[0],
        )
        for k in range(1, len(base_intervals)):
            if base_intervals[k][0] < base_intervals[k - 1][1]:
                raise RuntimeError(
                    f"{symbol_name} 检测到底仓重叠(未清仓又开仓): "
                    f"{base_intervals[k - 1]} 与 {base_intervals[k]} 时间区间相交。"
                )

        positions = self._build_positions(trade_history)
        return {"equity_curve": equity_curve, "trade_history": trade_history, "positions": positions}
