# -*- coding: utf-8 -*-
"""指标共振策略（基于移植的 classic_indicators 指标池 + StopRules 卖出纪律）。

信号设计（多重确认，降低假信号）：
  - 买入：MACD 柱由负转正（金叉）+ RSI 从超卖区回升（<30 → ≥30）
           + 收盘价在 MA(ma_filter) 上方（趋势过滤，可配 0 关闭）
  - 卖出：MACD 柱转负（死叉）或 RSI 下穿超买线（>70 → ≤70）
          或 StopRules 纪律触发（硬止损/时间/移动/止盈）
  - 单笔底仓，同一时间仅持一笔。

执行价：当日信号 + 当日收盘（含 look-ahead 偏差，由 describe 显式声明）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .base import BaseStrategy
from ..factors.classic_indicators import macd_hist, ma200_trend, rsi
from ..risk.stop_rules import StopRules


class IndicatorComboStrategy(BaseStrategy):
    type = "indicator_combo"

    @staticmethod
    def describe(params: dict) -> str:
        p = params or {}
        fast = int(p.get("macd_fast", 12))
        slow = int(p.get("macd_slow", 26))
        sig = int(p.get("macd_signal", 9))
        rp = int(p.get("rsi_period", 14))
        os = float(p.get("rsi_oversold", 30))
        ob = float(p.get("rsi_overbought", 70))
        maf = int(p.get("ma_filter", 200))
        sw = int(p.get("signal_window", 3))
        use_stop = bool((p.get("stop") or {}).get("enabled", True))
        return (
            f"- 买入：MACD({fast},{slow},{sig}) 柱由负转正 + RSI({rp}) 从超卖区回升（<{os:.0f} → ≥{os:.0f}）"
            f"{f' + 收盘价在 MA{maf} 上方' if maf else ''}，近 {sw} 日窗口共振。\n"
            f"- 卖出：MACD 柱转负 或 RSI 下穿超买（>{ob:.0f} → ≤{ob:.0f}）"
            f"{' 或 StopRules 纪律' if use_stop else ''}。\n"
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
        fast = int(p.get("macd_fast", 12))
        slow = int(p.get("macd_slow", 26))
        sig = int(p.get("macd_signal", 9))
        rp = int(p.get("rsi_period", 14))
        oversold = float(p.get("rsi_oversold", 30))
        overbought = float(p.get("rsi_overbought", 70))
        maf = int(p.get("ma_filter", 200))
        signal_window = int(p.get("signal_window", 3))   # 信号共振窗口（近 N 日）
        stop_cfg = dict(p.get("stop") or {})
        use_stop = bool(stop_cfg.pop("enabled", True))
        stop_rules = StopRules(stop_cfg) if use_stop else None
        ma_win = int(stop_cfg.get("trailing_stop_ma", 50))

        close = daily["close"].astype(float)
        # 指标（输入为 1 列 DataFrame，输出同形状）
        close_df = close.to_frame("close")
        mh = macd_hist(close_df, fast, slow, sig)["close"]
        rsi_s = rsi(close_df, rp)["close"]
        ma_f = close.rolling(maf).mean() if maf > 0 else None
        ma50 = close.rolling(ma_win).mean()

        # 信号（shift 防未来函数）+ 近 N 日窗口聚合（MACD 金叉与 RSI 回升无需精确同日）
        macd_cross_up = ((mh.shift(1) <= 0) & (mh > 0)).rolling(signal_window).max()
        macd_cross_dn = ((mh.shift(1) >= 0) & (mh < 0)).rolling(signal_window).max()
        rsi_up_os = ((rsi_s.shift(1) < oversold) & (rsi_s >= oversold)).rolling(signal_window).max()
        rsi_dn_ob = ((rsi_s.shift(1) > overbought) & (rsi_s <= overbought)).rolling(signal_window).max()
        above_ma = pd.Series(True, index=close.index) if ma_f is None else (close > ma_f)

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
            buy_sig = bool(macd_cross_up.iloc[i]) and bool(rsi_up_os.iloc[i]) and bool(above_ma.iloc[i])
            sell_sig = bool(macd_cross_dn.iloc[i]) or bool(rsi_dn_ob.iloc[i])

            if shares > 0:
                peak_price = max(peak_price, close_px)
                do_sell = sell_sig
                sell_reason = "MACD死叉/RSI超买"
                # 止损纪律优先（风控铁律：任何止损规则触发即卖）
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

            if buy_sig and cash > 0:
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
    # 自测：合成数据
    rng = np.random.default_rng(3)
    dates = pd.date_range("2023-01-01", periods=500, freq="B")
    close = 30 * np.cumprod(1 + rng.normal(0.0003, 0.02, 500))
    daily = pd.DataFrame({"date": dates, "open": close * 0.995,
                          "high": close * 1.02, "low": close * 0.98,
                          "close": close, "vol": rng.integers(100000, 1000000, 500)})
    cfg = {"type": "indicator_combo", "params": {"initial_cash": 100000,
                                                 "stop": {"enabled": True}}}
    res = IndicatorComboStrategy(cfg).run(daily, daily, "2023-01-01", "2024-09-01")
    print(f"交易 {len(res['trade_history'])} 笔 | 净值点 {len(res['equity_curve'])} | 期末 {res['equity_curve'][-1]}")
