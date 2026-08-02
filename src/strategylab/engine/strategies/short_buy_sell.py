# -*- coding: utf-8 -*-
"""短买卖逻辑（基于 25 日 RSV 的双 EMA 快慢线交叉）。

原始指标规格（来自用户）：
  - 典型价 TypicalPrice = (最高 + 最低 + 收盘) / 3  （已按标准修正原稿的 (收+高+高)/3 笔误）
  - 趋势线_基础 = EMA(TypicalPrice, 10)；趋势线_昨日 = 趋势线_基础 前移一根
  - MA5/10/20/60 = SMA(收盘, 5/10/20/60)  —— 用户注明「定义但未使用」
  - 25 日最高 / 25 日最低；RSV = (收盘 - 25日最低) / (25日最高 - 25日最低) × 100
  - 慢线 VAR3 = EMA(RSV, 20)；快线 VAR4 = EMA(RSV, 5)
  - 买入信号 = VAR4 上穿 VAR3（金叉）；卖出信号 = VAR3 上穿 VAR4（死叉）
  - 卖出时若「距上次买入 ≤ 3 根」标记为「止损」，否则标记为「短卖」

实际驱动交易的只有 VAR3/VAR4 的快慢线交叉；典型价 EMA 趋势线(10) 与
MA5/10/20/60 在原规格中未参与任何信号判定，按「装饰项、未使用」处理，不计算。
如需启用趋势线过滤，可后续在 run() 中基于 trend_base 增加门控。

执行价：当日信号 + 当日收盘（含 look-ahead 偏差，由 describe() 显式声明）。
「止损」仅为死叉早于预期的文字标签，并非价格止损。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .base import BaseStrategy


class ShortBuySell(BaseStrategy):
    type = "short_buy_sell"

    # ------------------------------------------------------------------ describe
    @staticmethod
    def describe(params: dict) -> str:
        """短买卖逻辑 策略说明（键访问全部兜底）。"""
        p = params or {}
        buy_amount_wan = (p.get("buy_amount") or 0) / 10000
        rsv = p.get("rsv", {}) or {}
        rsv_period = rsv.get("rsv_period", 25)
        fast = rsv.get("fast", 5)
        slow = rsv.get("slow", 20)
        comm = (p.get("commission") or 0) * 10000
        tax = (p.get("stamp_tax") or 0) * 10000
        lot_size = p.get("lot_size", 100)

        return (
            f"- 信号：{rsv_period}日 RSV = (C-{rsv_period}日低)/({rsv_period}日高-{rsv_period}日低)×100；"
            f"快线 VAR4=EMA(RSV,{fast})，慢线 VAR3=EMA(RSV,{slow})。\n"
            f"- 买入（短买）：VAR4 上穿 VAR3（金叉）当日收盘买入 {buy_amount_wan:.0f} 万（A股整手），单笔底仓。\n"
            f"- 卖出：VAR3 上穿 VAR4（死叉）当日收盘卖出；"
            f"距上次买入 ≤3 根标记为「止损」，否则标记为「短卖」。\n"
            f"- 再循环：清仓后下一金叉可再次买入；同一时间仅持一笔底仓。\n"
            f"- 说明：原规格中的典型价 EMA 趋势线(10) 与 MA5/10/20/60 不参与信号，"
            f"按「装饰项、未使用」处理，本策略不计算。\n"
            f"- 执行价：当日信号+当日收盘（含 look-ahead 偏差）；A股 T+1 / {lot_size}股整手 / "
            f"佣金{comm:.0f}bp双边 / 印花税{tax:.0f}bp卖方 / 前复权qfq。\n"
            f"- 偏差声明：本策略以当日信号触发并以当日收盘价成交，存在 look-ahead 偏差，"
            f"仅用于策略原型回测；「止损」仅为死叉早现的标签，并非价格止损。"
        )

    # ------------------------------------------------------------------ helpers
    def _fee_cost(self, size, price):
        return size * price * (1 + self.commission)

    def _fee_proceeds(self, size, price):
        return size * price * (1 - self.commission - self.stamp_tax)

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

    # ------------------------------------------------------------------ main
    def run(self, daily: pd.DataFrame, weekly: pd.DataFrame, start, end,
            symbol: str = "", symbol_name: str = "") -> dict:
        p = self.params
        self.symbol = symbol
        self.symbol_name = symbol_name
        self.commission = float(p.get("commission", 0.0003))
        self.stamp_tax = float(p.get("stamp_tax", 0.0005))
        self.lot_size = int(p.get("lot_size", 100))
        initial_cash = float(p.get("initial_cash", 200000))
        buy_amount = float(p.get("buy_amount", 100000))
        rsv_p = p.get("rsv", {}) or {}
        rsv_period = int(rsv_p.get("rsv_period", 25))
        fast = int(rsv_p.get("fast", 5))
        slow = int(rsv_p.get("slow", 20))

        # ---- 指标（基于完整日线，含 eval 窗口前的预热，EMA/RSV 自然收敛）----
        high_n = daily["high"].rolling(rsv_period).max()
        low_n = daily["low"].rolling(rsv_period).min()
        denom = high_n - low_n
        # 25 日最高=最低（横盘）时分母为 0 → 置 RSV=50（中性），避免除零与假交叉
        rsv = (daily["close"] - low_n) / denom.replace(0, np.nan) * 100.0
        rsv = rsv.fillna(50.0)
        var4 = rsv.ewm(span=fast, adjust=False).mean()   # 快线
        var3 = rsv.ewm(span=slow, adjust=False).mean()   # 慢线
        diff = var4 - var3
        prev = diff.shift(1)
        # 金叉（VAR4 上穿 VAR3）：前日 <= 0（含极小正值抗噪）且当日 > 0
        golden = ((prev <= 0) | (prev.abs() < 1e-9)) & (diff > 0)
        # 死叉（VAR3 上穿 VAR4）：前日 >= 0 且当日 < 0
        death = (prev >= 0) & (diff < 0)

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

            # 1. 持仓中：死叉卖出（带标签：早现=止损 / 否则=短卖）
            if base_shares > 0 and base_trade is not None:
                if is_death:
                    holding_bars = i - base_trade["entry_bar"]
                    label = "止损" if holding_bars <= 3 else "短卖"
                    cash += self._close_base(
                        base_trade, close, date_str, i, trade_history, label=label
                    )
                    base_shares = 0
                    base_trade = None
                equity_curve.append({"date": date_str, "value": round(cash + base_shares * close, 2)})
                continue

            # 2. 空仓：金叉买入（单笔底仓，已持仓则忽略后续金叉）
            if is_golden and cash >= buy_amount:
                size = int(buy_amount / close)
                size = (size // self.lot_size) * self.lot_size
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

        # 期末强制平仓（最后一根日线 bar 以当日收盘清仓）
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

        # 排序（仅展示顺序）
        trade_history.sort(key=lambda t: (t["entry_date"], t["exit_date"]))

        # 防御性自检：禁止「未清仓又开仓」（底仓区间不得相交）
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

    # ------------------------------------------------------------------ merge
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
