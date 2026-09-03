# -*- coding: utf-8 -*-
"""因子排名择时策略（接入移植的因子引擎 + StopRules 卖出纪律）。

设计背景：strategy_lab 的回测为**单标的**，无法做截面排名；
因此本策略将因子用于**时序择时**——因子方向化值的滚动分位作为持仓信号：

  - 因子分位 ≤ entry_pctile（超卖/低估值区）→ 买入
  - 因子分位 ≥ exit_pctile（超买/高估值区）→ 卖出
  - 持仓期间挂接 StopRules 卖出纪律（硬止损/时间/移动/止盈，可选开关）

可用因子见 ``factors.factor_engine.FACTOR_FUNCS``（lowvol_60 / near_high_250 /
mom_20 / mom_120 / rps_120 / new_high_250），方向遵循 DEFAULT_DIRECTION 实证表。

执行价：当日信号 + 当日收盘（含 look-ahead 偏差，由 describe 显式声明）。
"""
from __future__ import annotations

import pandas as pd

from .base import BaseStrategy
from ..factors.factor_engine import FACTOR_FUNCS
from ..risk.stop_rules import StopRules


class FactorRankStrategy(BaseStrategy):
    type = "factor_rank"

    @staticmethod
    def describe(params: dict) -> str:
        p = params or {}
        factor = p.get("factor", "lowvol_60")
        direction = float(p.get("direction", -1))
        lookback = int(p.get("lookback", 60))
        entry = float(p.get("entry_pctile", 0.30))
        exit_ = float(p.get("exit_pctile", 0.70))
        use_stop = bool((p.get("stop") or {}).get("enabled", True))
        stop_loss = float((p.get("stop") or {}).get("stop_loss_pct", 0.07)) * 100
        return (
            f"- 信号：因子 `{factor}`（方向 {'正用' if direction > 0 else '反转'}）× 滚动 {lookback} 日分位；"
            f"分位 ≤ {entry:.0%} → 买入，分位 ≥ {exit_:.0%} → 卖出。\n"
            f"- 卖出纪律：{'开启（硬止损 ' + f'{stop_loss:.0f}%' + ' / 移动止损 / 止盈）' if use_stop else '关闭'}。\n"
            f"- 单笔底仓，同一时间仅持一笔；清仓后分位回落可再买入。\n"
            f"- 执行价：买入/卖出以当日收盘成交（含 look-ahead 偏差）。A股 T+1 / 整手 / 前复权qfq。\n"
            f"- 偏差声明：信号触发当日即以收盘价成交，存在 look-ahead 偏差。"
        )

    # ------------------------------------------------------------------ main
    def run(self, daily: pd.DataFrame, weekly: pd.DataFrame, start, end,
            symbol: str = "", symbol_name: str = "") -> dict:
        p = self.params
        self.symbol = symbol
        self.symbol_name = symbol_name
        factor = p.get("factor", "lowvol_60")
        if factor not in FACTOR_FUNCS:
            raise ValueError(f"未知因子: {factor!r}，可选: {sorted(FACTOR_FUNCS)}")
        direction = float(p.get("direction", -1))
        lookback = int(p.get("lookback", 60))
        entry_pctile = float(p.get("entry_pctile", 0.30))
        exit_pctile = float(p.get("exit_pctile", 0.70))
        initial_cash = float(p.get("initial_cash", 200000))
        buy_ratio = float(p.get("buy_ratio", 0.95))
        lot_size = int(p.get("lot_size", 100))
        stop_cfg = dict(p.get("stop") or {})
        use_stop = bool(stop_cfg.pop("enabled", True))
        stop_rules = StopRules(stop_cfg) if use_stop else None
        ma_win = int(stop_cfg.get("trailing_stop_ma", 50))

        # ---- 因子时序信号 ----
        raw = FACTOR_FUNCS[factor](daily["close"].astype(float))
        value = raw * direction  # 方向化：越大越好
        pctile = value.rolling(lookback, min_periods=max(lookback // 2, 5)).apply(
            lambda x: (x <= x[-1]).mean(), raw=True)

        # ---- 状态 ----
        cash = initial_cash
        shares = 0
        entry_price = 0.0
        entry_date = ""
        entry_bar = 0
        peak_price = 0.0
        pid = 0
        trade_history: list = []
        equity_curve: list = []
        ma50 = daily["close"].rolling(ma_win).mean()

        eval_start = pd.Timestamp(start)
        eval_end = pd.Timestamp(end)
        n = len(daily)

        for i in range(n):
            row = daily.iloc[i]
            date = row["date"]
            if date < eval_start or date > eval_end:
                continue
            date_str = date.strftime("%Y-%m-%d")
            close = float(row["close"])
            sig = float(pctile.iloc[i]) if pd.notna(pctile.iloc[i]) else 0.5

            # 1. 持仓中：卖出信号（分位过高）或 StopRules 触发 → 卖出
            if shares > 0:
                peak_price = max(peak_price, close)
                do_sell = sig >= exit_pctile
                sell_reason = f"因子{exit_pctile:.0%}分位止盈"
                if stop_rules is not None:
                    hold_weeks = (date - pd.Timestamp(entry_date)).days / 7
                    d = stop_rules.check(
                        entry_price=entry_price, current_price=close,
                        ma_50=float(ma50.iloc[i]) if pd.notna(ma50.iloc[i]) else None,
                        peak_price=peak_price, hold_weeks=hold_weeks,
                    )
                    if d.should_sell:
                        do_sell = True
                        sell_reason = f"止损({d.action})"
                if do_sell:
                    cash += self._close(shares, entry_price, close, date_str, i,
                                        entry_date, entry_bar, pid, trade_history,
                                        reason=sell_reason)
                    shares = 0
                    peak_price = 0.0
                equity_curve.append({"date": date_str, "value": round(cash + shares * close, 2)})
                continue

            # 2. 空仓：因子分位 ≤ entry → 买入
            if sig <= entry_pctile and cash > 0:
                budget = cash * buy_ratio
                size = int(budget / close)
                size = (size // lot_size) * lot_size
                if size > 0:
                    cost_ = self._fee_cost(size, close)
                    if cost_ > cash:
                        from ..trading_cost import max_buy_volume
                        size = max_buy_volume(close, cash, self.symbol, self.cost_cfg, cash_ratio=1.0)
                        if size <= 0:
                            equity_curve.append({"date": date_str, "value": round(cash, 2)})
                            continue
                        cost_ = self._fee_cost(size, close)
                    cash -= cost_
                    shares = size
                    entry_price = close
                    entry_date = date_str
                    entry_bar = i
                    peak_price = close
                    pid += 1
            equity_curve.append({"date": date_str, "value": round(cash + shares * close, 2)})

        # 期末强制平仓
        last = daily.iloc[-1]
        last_date = last["date"].strftime("%Y-%m-%d")
        last_close = float(last["close"])
        if shares > 0:
            cash += self._close(shares, entry_price, last_close, last_date, n - 1,
                                entry_date, entry_bar, pid, trade_history, reason="期末平仓")
            shares = 0
        if equity_curve:
            equity_curve[-1]["value"] = round(cash, 2)

        trade_history.sort(key=lambda t: (t["entry_date"], t["exit_date"]))
        positions = self._build_positions(trade_history)
        return {"equity_curve": equity_curve, "trade_history": trade_history,
                "positions": positions}

    # ------------------------------------------------------------------ helper
    def _close(self, shares, entry_price, exit_price, exit_date, exit_bar,
               entry_date, entry_bar, pid, trade_history, reason="卖出"):
        """卖出底仓，返回现金流入"""
        cost = self._fee_cost(shares, entry_price)
        proceeds = self._fee_proceeds(shares, exit_price)
        pnl = proceeds - cost
        pnl_pct = pnl / cost * 100.0 if cost else 0.0
        trade_history.append({
            "entry_date": entry_date, "exit_date": exit_date,
            "side": "long", "size": shares,
            "entry_price": round(entry_price, 4), "exit_price": round(exit_price, 4),
            "pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 2),
            "holding_bars": exit_bar - entry_bar,
            "symbol": self.symbol, "symbol_name": self.symbol_name,
            "label": reason, "role": "底仓", "position_id": str(pid),
        })
        return proceeds

    @staticmethod
    def _build_positions(trade_history: list) -> list:
        positions = []
        for t in trade_history:
            if t.get("role") != "底仓":
                continue
            invested = float(t["size"]) * float(t["entry_price"])
            total_pct = (float(t["pnl"]) / invested * 100.0) if invested else 0.0
            positions.append({
                "position_id": t.get("position_id", ""),
                "entry_date": t["entry_date"], "exit_date": t["exit_date"],
                "side": "long", "base_size": int(t["size"]),
                "entry_price": round(float(t["entry_price"]), 4),
                "exit_price": round(float(t["exit_price"]), 4),
                "holding_bars": t.get("holding_bars", 0),
                "base_pnl": round(float(t["pnl"]), 2),
                "base_pnl_pct": round(float(t["pnl_pct"]), 2),
                "t_count": 0, "total_pnl": round(float(t["pnl"]), 2),
                "total_pnl_pct": round(total_pct, 2),
                "hold_no_t_pct": round(float(t["pnl_pct"]), 2),
                "trades": [],
            })
        positions.sort(key=lambda x: x["entry_date"])
        return positions
