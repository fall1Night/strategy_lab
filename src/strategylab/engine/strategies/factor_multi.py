# -*- coding: utf-8 -*-
"""多因子综合择时策略（基于移植的 factor_engine 因子池 + StopRules 卖出纪律）。

设计：多个因子方向化后做滚动 z-score 归一，等权合成一个综合分；
      综合分的滚动分位作为进出场信号（超卖低分位买、超买高分位卖）。

  - 因子配置 [{name, sign}]：name 取 FACTOR_FUNCS（lowvol_60/mom_20/mom_120/rps_120/near_high_250），
    sign 与 DEFAULT_DIRECTION 实证方向一致（+1 正用 / -1 反转）
  - 默认组合：lowvol_60 反转 + rps_120 反转 + near_high_250 正用（技术三因子）
  - 归一化窗口 z_win、进出场分位 entry/exit_pctile、信号窗口 lookback 均可配

执行价：当日信号 + 当日收盘（含 look-ahead 偏差，由 describe 显式声明）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .base import BaseStrategy
from ..factors.factor_engine import FACTOR_FUNCS
from ..risk.stop_rules import StopRules


class FactorMultiStrategy(BaseStrategy):
    type = "factor_multi"

    @staticmethod
    def describe(params: dict) -> str:
        p = params or {}
        factors = p.get("factors") or [{"name": "lowvol_60", "sign": -1},
                                       {"name": "rps_120", "sign": -1},
                                       {"name": "near_high_250", "sign": 1}]
        names = "+".join(f"{f['name']}({'正' if float(f.get('sign', 1)) > 0 else '反'})" for f in factors)
        lb = int(p.get("lookback", 60))
        entry = float(p.get("entry_pctile", 0.3))
        exit_ = float(p.get("exit_pctile", 0.7))
        use_stop = bool((p.get("stop") or {}).get("enabled", True))
        return (
            f"- 因子组合：{names}，方向化后滚动 z-score 等权合成。\n"
            f"- 信号：综合分 {lb} 日分位 ≤ {entry:.0%} → 买入，≥ {exit_:.0%} → 卖出。\n"
            f"- 卖出纪律：{'开启（硬止损/移动止损/止盈）' if use_stop else '关闭'}。\n"
            f"- 单笔底仓；执行价当日收盘（含 look-ahead 偏差）。A股 T+1 / 整手 / 前复权qfq。"
        )

    # ------------------------------------------------------------------ main
    def run(self, daily: pd.DataFrame, weekly: pd.DataFrame, start, end,
            symbol: str = "", symbol_name: str = "") -> dict:
        p = self.params
        self.symbol = symbol
        self.symbol_name = symbol_name
        initial_cash = float(p.get("initial_cash", 200000))
        buy_ratio = float(p.get("buy_ratio", 0.95))
        lot_size = int(p.get("lot_size", 100))
        factors = p.get("factors") or [{"name": "lowvol_60", "sign": -1},
                                       {"name": "rps_120", "sign": -1},
                                       {"name": "near_high_250", "sign": 1}]
        z_win = int(p.get("z_win", 120))
        lookback = int(p.get("lookback", 60))
        entry_pctile = float(p.get("entry_pctile", 0.30))
        exit_pctile = float(p.get("exit_pctile", 0.70))
        stop_cfg = dict(p.get("stop") or {})
        use_stop = bool(stop_cfg.pop("enabled", True))
        stop_rules = StopRules(stop_cfg) if use_stop else None
        ma_win = int(stop_cfg.get("trailing_stop_ma", 50))

        close = daily["close"].astype(float)

        # 多因子合成：方向化 → 滚动 z-score → 等权平均
        composite = pd.Series(0.0, index=close.index, dtype=float)
        n_factors = 0
        for f in factors:
            name = f.get("name")
            if name not in FACTOR_FUNCS:
                continue
            sign = float(f.get("sign", 1))
            raw = FACTOR_FUNCS[name](close)
            val = (raw * sign)
            mu = val.rolling(z_win, min_periods=max(z_win // 4, 10)).mean()
            sd = val.rolling(z_win, min_periods=max(z_win // 4, 10)).std()
            z = (val - mu) / sd.replace(0, np.nan)
            composite = composite.add(z.clip(-3, 3).fillna(0.0))
            n_factors += 1
        if n_factors == 0:
            raise ValueError(f"无可用因子，可选: {sorted(FACTOR_FUNCS)}")
        composite = composite / n_factors

        # 滚动分位信号
        pctile = composite.rolling(lookback, min_periods=max(lookback // 2, 5)).apply(
            lambda x: (x <= x[-1]).mean(), raw=True)
        ma50 = close.rolling(ma_win).mean()

        cash = initial_cash
        shares = 0
        entry_price = 0.0
        entry_date = ""
        entry_bar = 0
        peak_price = 0.0
        pid = 0
        trade_history: list = []
        equity_curve: list = []

        eval_start = pd.Timestamp(start)
        eval_end = pd.Timestamp(end)
        n = len(daily)

        for i in range(n):
            row = daily.iloc[i]
            date = row["date"]
            if date < eval_start or date > eval_end:
                continue
            date_str = date.strftime("%Y-%m-%d")
            close_px = float(row["close"])
            sig = float(pctile.iloc[i]) if pd.notna(pctile.iloc[i]) else 0.5

            if shares > 0:
                peak_price = max(peak_price, close_px)
                do_sell = sig >= exit_pctile
                sell_reason = f"综合分{exit_pctile:.0%}分位止盈"
                if stop_rules is not None:
                    hold_weeks = (date - pd.Timestamp(entry_date)).days / 7
                    d = stop_rules.check(
                        entry_price=entry_price, current_price=close_px,
                        ma_50=float(ma50.iloc[i]) if pd.notna(ma50.iloc[i]) else None,
                        peak_price=peak_price, hold_weeks=hold_weeks,
                    )
                    if d.should_sell:
                        do_sell = True
                        sell_reason = f"止损({d.action})"
                if do_sell:
                    cash += self._close(shares, entry_price, close_px, date_str, i,
                                        entry_date, entry_bar, pid, trade_history,
                                        reason=sell_reason)
                    shares = 0
                    peak_price = 0.0
                equity_curve.append({"date": date_str, "value": round(cash + shares * close_px, 2)})
                continue

            if sig <= entry_pctile and cash > 0:
                budget = cash * buy_ratio
                size = int(budget / close_px)
                size = (size // lot_size) * lot_size
                if size > 0:
                    cost_ = self._fee_cost(size, close_px)
                    if cost_ > cash:
                        from ..trading_cost import max_buy_volume
                        size = max_buy_volume(close_px, cash, self.symbol, self.cost_cfg, cash_ratio=1.0)
                        if size <= 0:
                            equity_curve.append({"date": date_str, "value": round(cash, 2)})
                            continue
                        cost_ = self._fee_cost(size, close_px)
                    cash -= cost_
                    shares = size
                    entry_price = close_px
                    entry_date = date_str
                    entry_bar = i
                    peak_price = close_px
                    pid += 1
            equity_curve.append({"date": date_str, "value": round(cash + shares * close_px, 2)})

        # 期末强制平仓
        last = daily.iloc[-1]
        if shares > 0:
            cash += self._close(shares, entry_price, float(last["close"]),
                                last["date"].strftime("%Y-%m-%d"), n - 1,
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


if __name__ == "__main__":
    rng = __import__("numpy").random.default_rng(3)
    import pandas as pd
    dates = pd.date_range("2023-01-01", periods=500, freq="B")
    close = 30 * __import__("numpy").cumprod(1 + rng.normal(0.0003, 0.02, 500))
    daily = pd.DataFrame({"date": dates, "open": close * 0.995,
                          "high": close * 1.02, "low": close * 0.98,
                          "close": close, "vol": rng.integers(100000, 1000000, 500)})
    cfg = {"type": "factor_multi", "params": {"initial_cash": 100000,
                                              "stop": {"enabled": True}}}
    res = FactorMultiStrategy(cfg).run(daily, daily, "2023-01-01", "2024-09-01")
    print(f"交易 {len(res['trade_history'])} 笔 | 净值点 {len(res['equity_curve'])} | 期末 {res['equity_curve'][-1]}")
