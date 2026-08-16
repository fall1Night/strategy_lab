# -*- coding: utf-8 -*-
"""双均线策略（金叉买入 / 死叉卖出），移植自 OSkhQuant 的「双均线精简」示例。

与原示例的差异：
  - 原示例用 khMA 在线拉取 xtquant 行情；本实现直接基于回测已加载的日线 DataFrame
    计算 MA，无外部依赖、可离线回测；
  - 费税统一走 BaseStrategy 的 ``_fee_cost`` / ``_fee_proceeds``（即 trading_cost 模块），
    不再各自内联；
  - T+1 / 整手 / 前复权口径与原项目一致。

执行价：当日信号 + 当日收盘（含 look-ahead 偏差，由 describe 显式声明）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .base import BaseStrategy
from ..vendor.myt import MA


class DualMA(BaseStrategy):
    type = "dual_ma"

    # ------------------------------------------------------------------ describe
    @staticmethod
    def describe(params: dict) -> str:
        p = params or {}
        sp = int(p.get("short_period", 5))
        lp = int(p.get("long_period", 20))
        buy_amount_wan = (p.get("buy_amount") or 0) / 10000
        comm = (p.get("commission") or 0) * 10000
        tax = (p.get("stamp_tax") or 0) * 10000
        lot_size = p.get("lot_size", 100)
        return (
            f"- 信号：MA({sp}) 上穿 MA({lp}) 金叉→当日收盘买入 {buy_amount_wan:.0f} 万（整手）；"
            f"MA({sp}) 下穿 MA({lp}) 死叉→当日收盘清仓。\n"
            f"- 单笔底仓，同一时间仅持一笔；清仓后下一金叉可再买入。\n"
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
        sp = int(p.get("short_period", 5))
        lp = int(p.get("long_period", 20))
        initial_cash = float(p.get("initial_cash", 200000))
        buy_amount = float(p.get("buy_amount", 100000))
        lot_size = int(p.get("lot_size", 100))

        # ---- 指标（基于完整日线，含 eval 窗口前预热，MA 自然收敛）----
        ma_short = pd.Series(MA(daily["close"].values, sp))
        ma_long = pd.Series(MA(daily["close"].values, lp))
        diff = ma_short - ma_long
        prev = diff.shift(1)
        golden = ((prev <= 0) | prev.isna()) & (diff > 0)   # 金叉
        death = (prev >= 0) & (diff < 0)                     # 死叉

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

            is_golden = bool(golden.iloc[i]) if pd.notna(golden.iloc[i]) else False
            is_death = bool(death.iloc[i]) if pd.notna(death.iloc[i]) else False

            # 1. 持仓中：死叉卖出（清仓）
            if base_shares > 0 and base_trade is not None:
                if is_death:
                    cash += self._close_base(
                        base_trade, close, date_str, i, trade_history, label="死叉清仓"
                    )
                    base_shares = 0
                    base_trade = None
                equity_curve.append({"date": date_str, "value": round(cash + base_shares * close, 2)})
                continue

            # 2. 空仓：金叉买入（单笔底仓）
            if is_golden and cash >= buy_amount:
                size = int(buy_amount / close)
                size = (size // lot_size) * lot_size
                if size > 0:
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
        """合并成交明细：一个持仓组 = 一笔底仓（+ 其若干做T，本策略无做T，ts 恒空）。"""
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
