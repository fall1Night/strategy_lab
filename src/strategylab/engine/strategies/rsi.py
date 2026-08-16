# -*- coding: utf-8 -*-
"""RSI 超买超卖策略，移植自 OSkhQuant 的「RSI策略」示例。

逻辑：
  - RSI(period) 上穿「超卖阈值」(默认 30) 且无持仓 → 买入（占可用资金比例 buy_ratio）；
  - RSI(period) 下穿「超买阈值」(默认 70) 且有持仓 → 卖出全部。

与原示例的差异：
  - 原示例用 khHistory 在线拉取、遍历股票池；本实现基于回测日线单标的，RSI 由 MyTT 计算；
  - 费税统一走 BaseStrategy 的 ``_fee_cost`` / ``_fee_proceeds``（trading_cost 模块）。

执行价：当日信号 + 当日收盘（含 look-ahead 偏差，由 describe 显式声明）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .base import BaseStrategy
from ..vendor.myt import RSI


class RSIStrategy(BaseStrategy):
    type = "rsi"

    # ------------------------------------------------------------------ describe
    @staticmethod
    def describe(params: dict) -> str:
        p = params or {}
        period = int(p.get("rsi_period", 12))
        oversold = float(p.get("oversold", 30))
        overbought = float(p.get("overbought", 70))
        buy_ratio = float(p.get("buy_ratio", 0.5)) * 100
        stop_loss_pct = float(p.get("stop_loss_pct", 0.05)) * 100
        comm = (p.get("commission") or 0) * 10000
        tax = (p.get("stamp_tax") or 0) * 10000
        lot_size = p.get("lot_size", 100)
        return (
            f"- 信号：RSI({period}) 上穿 {oversold:.0f}(超卖) 且无持仓 → 买入 {buy_ratio:.0f}% 可用资金（整手）；"
            f"RSI({period}) 下穿 {overbought:.0f}(超买) 且有持仓 → 清仓。\n"
            f"- 硬止损：持仓期间若盘中最低价 ≤ 成本价×(1-{stop_loss_pct:.0f}%)，当日收盘止损清仓"
            f"（优先于超买信号，控制单笔最大回撤）。\n"
            f"- 单笔底仓，同一时间仅持一笔；清仓后下一上穿可再买入。\n"
            f"- RSI 采用中国式 SMA 平滑（与 MyTT / 通达信口径一致）。\n"
            f"- 执行价：当日信号+当日收盘（含 look-ahead 偏差）；A股 T+1 / {lot_size}股整手 / "
            f"佣金{comm:.0f}bp双边 / 印花税{tax:.0f}bp卖方 / 前复权qfq。\n"
            f"- 偏差声明：以当日信号触发并以当日收盘价成交，存在 look-ahead 偏差，"
            f"仅用于策略原型回测。"
        )

    # ------------------------------------------------------------------ main
    def run(self, daily: pd.DataFrame, weekly: pd.DataFrame, start, end,
            symbol: str = "", symbol_name: str = "") -> dict:
        p = self.params
        self.symbol = symbol
        self.symbol_name = symbol_name
        period = int(p.get("rsi_period", 12))
        oversold = float(p.get("oversold", 30))
        overbought = float(p.get("overbought", 70))
        buy_ratio = float(p.get("buy_ratio", 0.5))
        stop_loss_pct = float(p.get("stop_loss_pct", 0.05))
        initial_cash = float(p.get("initial_cash", 200000))
        lot_size = int(p.get("lot_size", 100))

        # ---- 指标（基于完整日线，含预热）----
        rsi = pd.Series(RSI(daily["close"].values, period))
        rp = rsi.shift(1)
        rn = rsi
        cross_up = (rp < oversold) & (rn >= oversold)        # 上穿超卖
        cross_down = (rp > overbought) & (rn <= overbought)  # 下穿超买

        # ---- 状态 ----
        cash = initial_cash
        base_shares = 0
        pid_counter = 0
        base_trade = None
        trade_history: list = []
        equity_curve: list = []

        eval_start_ts = pd.Timestamp(start)
        eval_end_ts = pd.Timestamp(end)
        n = len(daily)

        for i in range(n):
            row = daily.iloc[i]
            date = row["date"]
            date_str = date.strftime("%Y-%m-%d")
            close = float(row["close"])

            if date < eval_start_ts or date > eval_end_ts:
                continue

            is_up = bool(cross_up.iloc[i]) if pd.notna(cross_up.iloc[i]) else False
            is_down = bool(cross_down.iloc[i]) if pd.notna(cross_down.iloc[i]) else False

            # 1. 持仓中：先判硬止损（回撤>阈值优先），再判超买清仓
            if base_shares > 0 and base_trade is not None:
                entry_price = base_trade["entry_price"]
                low = float(row["low"])
                stop_price = entry_price * (1 - stop_loss_pct)
                if low <= stop_price:
                    # 成本价回撤超过 stop_loss_pct，当日收盘止损清仓
                    cash += self._close_base(
                        base_trade, close, date_str, i, trade_history, label="止损清仓"
                    )
                    base_shares = 0
                    base_trade = None
                elif is_down:
                    cash += self._close_base(
                        base_trade, close, date_str, i, trade_history, label="超买清仓"
                    )
                    base_shares = 0
                    base_trade = None
                equity_curve.append({"date": date_str, "value": round(cash + base_shares * close, 2)})
                continue

            # 2. 空仓：上穿超卖 → 买入（按可用资金比例，整手）
            if is_up and cash > 0:
                budget = cash * buy_ratio
                size = int(budget / close)
                size = (size // lot_size) * lot_size
                if size > 0:
                    cost_ = self._fee_cost(size, close)
                    # 资金不足则按最大可买量（整手）买入
                    if cost_ > cash:
                        from ..trading_cost import max_buy_volume
                        size = max_buy_volume(close, cash, self.symbol, self.cost_cfg, cash_ratio=1.0)
                        if size <= 0:
                            equity_curve.append({"date": date_str, "value": round(cash, 2)})
                            continue
                        cost_ = self._fee_cost(size, close)
                    cash -= cost_
                    base_shares = size
                    pid_counter += 1
                    base_trade = {
                        "entry_date": date_str, "entry_price": close, "size": size,
                        "entry_bar": i, "position_id": pid_counter,
                    }
            equity_curve.append({"date": date_str, "value": round(cash + base_shares * close, 2)})

        # 期末强制平仓
        last_row = daily.iloc[-1]
        last_date = last_row["date"].strftime("%Y-%m-%d")
        last_close = float(last_row["close"])
        last_bar = len(daily) - 1
        if base_shares > 0 and base_trade is not None:
            cash += self._close_base(
                base_trade, last_close, last_date, last_bar, trade_history, label="期末平仓"
            )
            base_shares = 0
            base_trade = None
        if equity_curve:
            equity_curve[-1]["value"] = round(cash, 2)

        trade_history.sort(key=lambda t: (t["entry_date"], t["exit_date"]))
        positions = self._build_positions(trade_history)
        return {"equity_curve": equity_curve, "trade_history": trade_history, "positions": positions}

    # ------------------------------------------------------------------ merge
    def _close_base(self, base_trade, exit_price, exit_date, exit_bar, trade_history, label="底仓(清仓)"):
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

    @staticmethod
    def _build_positions(trade_history: list) -> list:
        groups: dict = {}
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
            t_hold_pnl = sum(float(t["size"]) * (exit_px - float(t["entry_price"])) for t in ts)
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
