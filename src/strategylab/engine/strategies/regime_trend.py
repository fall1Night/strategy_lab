# -*- coding: utf-8 -*-
"""Regime 择时门控策略（基于移植的 regime.RegimeDetector + StopRules）。

逻辑：用标的日线近似市场状态（强上升/上升波动才持仓），状态切换时买卖。
  - 持仓态：strong_uptrend / uptrend_volatile（RegimeDetector.can_trade()）
  - 空仓态：choppy / downtrend / panic（只减不加）
  - 持仓期间挂接 StopRules 卖出纪律兜底（可选开关）

★局限声明：单标的回测没有市场指数，本策略用标的自身日线近似 Regime。
  理想用法是组合级回测 + 真实指数（沪深300），本实现作为单标的近似。
"""
from __future__ import annotations

import pandas as pd

from .base import BaseStrategy
from ..regime import RegimeDetector
from ..risk.stop_rules import StopRules


class RegimeTrendStrategy(BaseStrategy):
    type = "regime_trend"

    @staticmethod
    def describe(params: dict) -> str:
        p = params or {}
        confirm = int((p.get("regime") or {}).get("confirm_days", 3))
        use_stop = bool((p.get("stop") or {}).get("enabled", True))
        return (
            f"- 门控：Regime 五档状态机（连续 {confirm} 天确认切换），仅 强上升/上升波动 持仓，"
            f"震荡/下跌/恐慌 空仓。\n"
            f"- 卖出：状态转空 或{' StopRules 纪律' if use_stop else ''}触发。\n"
            f"- 单笔底仓；执行价当日收盘（含 look-ahead 偏差）。A股 T+1 / 整手 / 前复权qfq。\n"
            f"- 局限：单标的回测用标的自身日线近似市场状态（无指数），组合级应接真实指数。"
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
        regime_cfg = dict(p.get("regime") or {})
        regime_cfg.setdefault("confirm_days", 3)
        stop_cfg = dict(p.get("stop") or {})
        use_stop = bool(stop_cfg.pop("enabled", True))
        stop_rules = StopRules(stop_cfg) if use_stop else None
        ma_win = int(stop_cfg.get("trailing_stop_ma", 50))

        # Regime 输入：标的自身 OHLC
        idx_df = pd.DataFrame({
            "close": daily["close"].astype(float),
            "high": daily["high"].astype(float),
            "low": daily["low"].astype(float),
        })
        rd = RegimeDetector(regime_cfg)
        rd.fit(idx_df)  # 一次性预计算滚动指标（O(n)），循环里改用 update_at(i)
        ma50 = daily["close"].astype(float).rolling(ma_win).mean()

        cash = initial_cash
        shares = 0
        entry_price = 0.0
        entry_date = ""
        entry_bar = 0
        peak_price = 0.0
        pid = 0
        trade_history: list = []
        equity_curve: list = []
        warmup = max(100, regime_cfg.get("warmup", 100))  # 指标预热

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

            if i < warmup:
                equity_curve.append({"date": date_str, "value": round(cash, 2)})
                continue
            state = rd.update_at(i)
            can_trade = rd.can_trade()

            if shares > 0:
                peak_price = max(peak_price, close_px)
                do_sell = not can_trade  # 状态转空 → 清仓
                sell_reason = f"Regime转空({state})"
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

            if can_trade and cash > 0:
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
    cfg = {"type": "regime_trend", "params": {"initial_cash": 100000,
                                              "regime": {"confirm_days": 3},
                                              "stop": {"enabled": True}}}
    res = RegimeTrendStrategy(cfg).run(daily, daily, "2023-01-01", "2024-09-01")
    print(f"交易 {len(res['trade_history'])} 笔 | 净值点 {len(res['equity_curve'])} | 期末 {res['equity_curve'][-1]}")
