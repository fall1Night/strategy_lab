# -*- coding: utf-8 -*-
"""日线 KDJ 监控 + 日线 MACD 金叉买入（Strategy Lab 4.0 新策略）。

策略逻辑（参数全部来自 .toml，可按需改）：
  - 监控：日线 KDJ 金叉（K 上穿 D）进入"监控态"(armed=True)；未 armed 前不买入。
  - 买入：监控态下，日线 MACD 金叉（DIF 上穿 DEA，任意位置）当日收盘买入
         `buy_amount` 元，按 A股 100 股整手取整，T+1；买入后 armed=False。
  - 清仓（满足任一）：
        1) 现价较"买入成交价(entry_price)"回落 ≥ `drawdown_threshold`（纯价格口径）；或
        2) 日线 MACD 死叉（DIF 下穿 DEA，任意位置）。
        清仓后 armed=False。
  - 再循环：清仓后需再次 KDJ 金叉 armed、再次 MACD 金叉买入；同一时间仅持一笔底仓。
  - 执行价：当日信号 + 当日收盘（含 look-ahead 偏差，由配置显式声明）。

偏差声明：本策略以当日信号（KDJ/MACD 金叉/死叉）触发、并以当日收盘价成交，
存在 look-ahead 偏差；仅用于策略原型回测，不构成实盘建议。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .base import BaseStrategy
from ..indicators import compute_kdj, compute_macd


class KdjMacdCross(BaseStrategy):
    type = "kdj_macd_cross"

    # ------------------------------------------------------------------ describe
    @staticmethod
    def describe(params: dict) -> str:
        """日线KDJ监控 + MACD金叉买入 策略说明（键访问全部兜底）。"""
        buy_amount_wan = (params.get("buy_amount") or 0) / 10000
        drawdown_threshold = params.get("drawdown_threshold") or 0.05
        exit_cfg = params.get("exit") or {}
        drawdown_threshold = exit_cfg.get("drawdown_threshold", drawdown_threshold)
        macd_death = exit_cfg.get("macd_death_cross", True)
        comm = (params.get("commission") or 0) * 10000
        tax = (params.get("stamp_tax") or 0) * 10000
        lot_size = params.get("lot_size") or 100

        return (
            f"- 监控：日线 KDJ 金叉（K 上穿 D）进入监控态（armed），未 armed 前不买入。\n"
            f"- 买入：监控态下日线 MACD 金叉（DIF 上穿 DEA，任意位置）当日收盘买入 "
            f"{buy_amount_wan:.0f} 万（A股整手）。\n"
            f"- 清仓：持仓回撤≥{drawdown_threshold * 100:.0f}%（相对买入成交价）"
            f"或 日线 MACD 任意死叉（DIF 下穿 DEA）当日收盘清仓。\n"
            f"- 再循环：清仓后解除监控，后续 KDJ 金叉可再武装、MACD 金叉可再买入；"
            f"同一时间仅持一笔底仓。\n"
            f"- 执行价：当日信号+当日收盘（含 look-ahead 偏差）；A股 T+1 / {lot_size}股整手 / "
            f"佣金{comm:.0f}bp双边 / 印花税{tax:.0f}bp卖方 / 前复权qfq。\n"
            f"- 偏差声明：本策略以当日信号触发并以当日收盘价成交，存在 look-ahead 偏差，"
            f"仅用于策略原型回测（MACD 死叉清仓：{'开' if macd_death else '关'}）。"
        )

    # ------------------------------------------------------------------ helpers
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
        exit_cfg = p.get("exit", {}) or {}
        drawdown_threshold = float(
            p.get("drawdown_threshold", exit_cfg.get("drawdown_threshold", 0.05))
        )
        macd_death_cross = bool(exit_cfg.get("macd_death_cross", True))
        macd_p = p["macd"]
        kdj_p = p["kdj"]

        # ---- 日线指标 ----
        d_dif, d_dea, d_hist = compute_macd(
            daily["close"], macd_p["fast"], macd_p["slow"], macd_p["signal"]
        )
        daily["dif"], daily["dea"], daily["hist"] = d_dif, d_dea, d_hist
        kdj_df = compute_kdj(daily, kdj_p["n"], kdj_p["m1"], kdj_p["m2"], return_full=True)
        daily["K"] = kdj_df["K"]
        daily["D"] = kdj_df["D"]
        daily["J"] = kdj_df["J"]

        # KDJ 金叉：K 上穿 D（当日收盘判定）；前日 <= 0（含极小正值抗噪）且当日 > 0
        diff_kd = kdj_df["K"] - kdj_df["D"]
        prev_kd = diff_kd.shift(1)
        daily["kdj_gold"] = (
            ((prev_kd <= 0) | (prev_kd.abs() < 1e-9)) & (diff_kd > 0)
        )
        # MACD 金叉：DIF 上穿 DEA（任意位置）
        diff_md = d_dif - d_dea
        daily["macd_gold"] = (diff_md.shift(1) <= 0) & (diff_md > 0)
        # MACD 死叉：DIF 下穿 DEA（任意位置）
        daily["macd_death"] = (diff_md.shift(1) >= 0) & (diff_md < 0)

        # ---- 状态 ----
        cash = initial_cash
        base_shares = 0
        pid_counter = 0
        base_trade = None
        armed = False
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

            kdj_gold = bool(row["kdj_gold"]) if pd.notna(row["kdj_gold"]) else False
            macd_gold = bool(row["macd_gold"]) if pd.notna(row["macd_gold"]) else False
            macd_death = bool(row["macd_death"]) if pd.notna(row["macd_death"]) else False

            # 1. 持仓中：清仓（最高优先级，回撤≥阈值 或 MACD 死叉）
            if base_shares > 0 and base_trade is not None:
                entry_price = base_trade["entry_price"]
                drawdown = (entry_price - close) / entry_price if entry_price else 0.0
                drawdown_exit = drawdown >= drawdown_threshold
                if drawdown_exit or (macd_death and macd_death_cross):
                    exit_price = close
                    cash += self._close_base(
                        base_trade, exit_price, date_str, i, trade_history, label="底仓(清仓)"
                    )
                    base_shares = 0
                    base_trade = None
                    armed = False
                equity_curve.append({"date": date_str, "value": round(cash + base_shares * close, 2)})
                continue

            # 2. 空仓：监控(armed) + 买入
            if kdj_gold:
                armed = True
            if armed and macd_gold and cash >= buy_amount:
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
                    armed = False  # 买入后解除监控，等清仓再重新 armed
            equity_curve.append({"date": date_str, "value": round(cash + base_shares * close, 2)})

        # 期末强制平仓（最后一根 bar 仍以当日收盘清仓）
        last_row = daily.iloc[-1]
        last_date = last_row["date"].strftime("%Y-%m-%d")
        last_close = float(last_row["close"])
        last_bar = len(daily) - 1
        if base_shares > 0 and base_trade is not None:
            cash += self._close_base(
                base_trade, last_close, last_date, last_bar, trade_history, label="底仓(期末平仓)"
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
            # T持有到清仓（不做J>80卖出）的PnL
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
