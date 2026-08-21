# -*- coding: utf-8 -*-
"""周线MACD + 日线RSI/KDJ 综合策略（综合 RSI 超买超卖 与 周线MACD+日线KDJ 双入口）。

综合来源：
  - ``rsi.py``            : RSI(12) 上穿 30 建仓信号 + 下穿 70 清仓 + 收益峰值回撤清仓；
  - ``kdj_macd_dual_entry.py`` : 周线 MACD 柱(hist)<0 监控区 + hist 较上周回升(动能筑底转强)
                               + 日线 KDJ 的 J 线尾盘买底仓。

本策略取其交集并去除「做T」，逻辑：
  监控区：周线 MACD 柱(hist) < 0 才进入监控；
  开始判定：RSI(12) 上穿 30「且」周线某一周 hist 较上周上涨 —— 两条件同时成立才 armed；
  买入底仓：armed 状态下，日线 KDJ 的 J 线 < j_buy(默认50) 当日收盘买入底仓；
  清仓：满足任一即清 ——
        (a) 日线 RSI(12) 下穿 overbought(默认70) 且有持仓；
        (b) 持仓期间净值从收益峰值回落 ≥ peak_drawdown_pct(默认5%，移动止盈/回撤止损)。
  解除监控：hist 回到 0 轴上方 或 已买入底仓 后自动 disarm。
  单底仓：同一时间仅持一笔底仓，清仓后下一轮可再买入。
  执行价：当日信号 + 当日收盘（含 look-ahead 偏差，由 describe 显式声明）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .base import BaseStrategy
from ..indicators import compute_kdj, compute_macd
from ..vendor.myt import RSI
from ..trading_cost import max_buy_volume


class RsiMacdKdjCombo(BaseStrategy):
    type = "rsi_macd_kdj_combo"

    # ------------------------------------------------------------------ describe
    @staticmethod
    def describe(params: dict) -> str:
        p = params or {}
        macd_p = p.get("macd") or {}
        kdj_p = p.get("kdj") or {}
        entry = p.get("entry") or {}
        rsi_p = p.get("rsi") or {}
        j_buy = float(entry.get("j_buy", 50))
        rsi_period = int(rsi_p.get("period", 12))
        rsi_oversold = float(rsi_p.get("oversold", 30))
        rsi_overbought = float(rsi_p.get("overbought", 70))
        peak_dd_pct = float((p.get("trailing_stop") or {}).get("peak_drawdown_pct", 0.05)) * 100
        comm = (p.get("commission") or 0) * 10000
        tax = (p.get("stamp_tax") or 0) * 10000
        lot_size = p.get("lot_size") or 100
        base_wan = (p.get("base_buy_amount") or 0) / 10000

        return (
            f"- 监控区：周线MACD柱(hist<0)才进入监控（hist回到0轴上方自动解除监控）。\n"
            f"- 开始判定：RSI({rsi_period}) 上穿 {rsi_oversold:.0f}「且」周线某一周 hist 较上周上涨（动能筑底转强）—— "
            f"两条件同时成立才 armed。\n"
            f"- 买入底仓：armed 状态下，日线 KDJ(9,3,3) 的 J 线 < {j_buy:.0f} 当日收盘买入约 {base_wan:.0f} 万底仓（整手）。\n"
            f"- 清仓：满足任一即清 —— (a) 日线 RSI({rsi_period}) 下穿 {rsi_overbought:.0f}(超买) 且有持仓；"
            f"(b) 持仓期间净值从收益峰值回落 ≥ {peak_dd_pct:.0f}%（移动止盈/回撤止损）。均当日收盘清仓全部。\n"
            f"- 单底仓：同一时间仅持一笔底仓，清仓后下一轮可再买入。\n"
            f"- 执行价：当日信号+当日收盘（含 look-ahead 偏差）；A股 T+1 / {lot_size}股整手 / "
            f"佣金{comm:.0f}bp双边 / 印花税{tax:.0f}bp卖方 / 前复权qfq。\n"
            f"- 周线MACD({macd_p.get('fast',12)},{macd_p.get('slow',26)},{macd_p.get('signal',9)}) 信号对齐采用 "
            f"backward merge（最新 weekly_date ≤ daily_date），周内不使用未来数据。\n"
            f"- 偏差声明：买入与清仓以当日信号触发并以当日收盘价成交，存在 look-ahead 偏差。"
        )

    # ------------------------------------------------------------------ helpers
    def _close_base(self, base_trade, exit_price, exit_date, exit_bar, trade_history, label="底仓清仓"):
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
        self.commission = float(p["commission"])
        self.stamp_tax = float(p["stamp_tax"])
        self.lot_size = int(p["lot_size"])
        initial_cash = float(p["initial_cash"])
        base_buy = float(p["base_buy_amount"])
        macd_p = p["macd"]
        kdj_p = p["kdj"]
        entry = p["entry"]
        rsi_p = p.get("rsi") or {}
        rsi_period = int(rsi_p.get("period", 12))
        rsi_oversold = float(rsi_p.get("oversold", 30))
        rsi_overbought = float(rsi_p.get("overbought", 70))
        trailing_p = p.get("trailing_stop") or {}
        peak_dd = float(trailing_p.get("peak_drawdown_pct", 0.05))
        single_base = bool(p.get("single_base_position", {}).get("enabled", True))

        # ---- 日线指标 ----
        j = compute_kdj(daily, kdj_p["n"], kdj_p["m1"], kdj_p["m2"])
        daily["J"] = j
        rsi_arr = pd.Series(RSI(daily["close"].values, rsi_period))
        rsi_prev = rsi_arr.shift(1)
        daily["rsi_cross_up"] = (rsi_prev < rsi_oversold) & (rsi_arr >= rsi_oversold)   # 上穿超卖
        daily["rsi_cross_down"] = (rsi_prev > rsi_overbought) & (rsi_arr <= rsi_overbought)  # 下穿超买

        # ---- 周线指标 ----
        _w_dif, _w_dea, w_hist = compute_macd(
            weekly["close"], macd_p["fast"], macd_p["slow"], macd_p["signal"]
        )
        weekly["wk_hist"] = w_hist
        weekly["wk_hist_prev"] = w_hist.shift(1)

        # ---- 周线 → 日线对齐（backward，无未来数据）----
        wk = (weekly[["date", "wk_hist", "wk_hist_prev"]]
              .rename(columns={"date": "wk_date"}).sort_values("wk_date"))
        daily = daily.sort_values("date")
        daily = pd.merge_asof(daily, wk, left_on="date", right_on="wk_date", direction="backward")

        # ---- 状态 ----
        cash = initial_cash
        base_shares = 0
        pid_counter = 0
        base_trade = None
        monitoring_armed = False
        trade_history: list = []
        equity_curve: list = []
        peak_total_value: float = 0.0  # 持仓期间净值峰值（用于收益峰值回撤清仓）

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

            J = float(row["J"])
            rsi_up = bool(row["rsi_cross_up"]) if pd.notna(row["rsi_cross_up"]) else False
            rsi_down = bool(row["rsi_cross_down"]) if pd.notna(row["rsi_cross_down"]) else False
            wk_hist = row.get("wk_hist")
            wk_hist_prev = row.get("wk_hist_prev")

            holding = base_shares > 0

            # 持仓期间净值峰值 & 回撤检测（收益峰值回撤清仓用）
            current_value = cash + base_shares * close
            if holding and current_value > peak_total_value:
                peak_total_value = current_value
            peak_drop = holding and peak_total_value > 0 and (
                peak_total_value - current_value
            ) >= peak_total_value * peak_dd

            # 1. 清仓（最高优先级）：RSI 下穿 overbought 或 收益峰值回撤 ≥ peak_dd%
            if holding and (rsi_down or peak_drop):
                trigger = "峰值回撤清仓" if peak_drop else "RSI下穿清仓"
                cash += self._close_base(
                    base_trade, close, date_str, i, trade_history, label=f"底仓({trigger})"
                )
                base_shares = 0
                base_trade = None
                monitoring_armed = False
                peak_total_value = 0.0
                equity_curve.append({"date": date_str, "value": round(cash, 2)})
                continue

            # 2. 持仓中：等待清仓信号（本策略不做T）
            if holding:
                equity_curve.append({"date": date_str, "value": round(cash + base_shares * close, 2)})
                continue

            # 3. 空仓：监控区 + 开始判定 + 买入底仓
            in_zone = pd.notna(wk_hist) and wk_hist < 0
            rising = (pd.notna(wk_hist) and pd.notna(wk_hist_prev) and wk_hist > wk_hist_prev)
            if entry.get("enabled"):
                # hist 回到 0 轴上方自动解除监控
                if entry.get("disarm_hist_above_zero") and not in_zone:
                    monitoring_armed = False
                # 开始判定：RSI(12) 上穿超卖「且」周线 hist 回升 —— 同时成立才 armed
                if in_zone and rising and rsi_up:
                    monitoring_armed = True
                # 买入底仓：armed 且 日线 J < j_buy
                if (monitoring_armed and J < float(entry.get("j_buy", 50)) and cash >= base_buy):
                    size = int(base_buy / close)
                    size = (size // self.lot_size) * self.lot_size
                    if size > 0:
                        cost_ = self._fee_cost(size, close)
                        # 资金不足则按最大可买量（整手）买入
                        if cost_ > cash:
                            size = max_buy_volume(
                                close, cash, self.symbol, self.cost_cfg, cash_ratio=1.0
                            )
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
                        peak_total_value = cash + size * close  # 建仓时设初始净值峰值
                        monitoring_armed = False
            equity_curve.append({"date": date_str, "value": round(cash + base_shares * close, 2)})

        # 期末强制平仓
        last_row = daily.iloc[-1]
        last_date = last_row["date"].strftime("%Y-%m-%d")
        last_close = float(last_row["close"])
        last_bar = len(daily) - 1
        if base_shares > 0 and base_trade is not None:
            cash += self._close_base(base_trade, last_close, last_date, last_bar, trade_history, label="底仓(期末平仓)")
            base_shares = 0
            base_trade = None
        if equity_curve:
            equity_curve[-1]["value"] = round(cash, 2)

        # 排序（仅展示顺序）
        trade_history.sort(key=lambda t: (t["entry_date"], t["exit_date"]))

        # 防御性自检：禁止「未清仓又开仓」
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
        """合并成交明细：按 position_id 归组，每组一笔底仓。"""
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
            positions.append({
                "position_id": pid, "entry_date": b["entry_date"], "exit_date": b["exit_date"],
                "side": b.get("side", "long"), "base_size": int(b["size"]),
                "entry_price": round(float(b["entry_price"]), 4), "exit_price": round(float(b["exit_price"]), 4),
                "holding_bars": b.get("holding_bars", 0), "base_pnl": round(float(b["pnl"]), 2),
                "base_pnl_pct": round(float(b["pnl_pct"]), 2), "t_count": len(ts),
                "total_pnl": round(total_pnl, 2), "total_pnl_pct": round(total_pct, 2),
                "hold_no_t_pct": round(float(b["pnl_pct"]), 2),
                "t_hold_to_exit_pct": round(total_pct, 2),
                "trades": [],
            })
        positions.sort(key=lambda p: p["entry_date"])
        return positions
