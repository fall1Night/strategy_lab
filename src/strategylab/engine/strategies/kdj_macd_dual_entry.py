# -*- coding: utf-8 -*-
"""周线 MACD + 日线 KDJ 双入口建仓 + 日线 J 线做 T + 日线 MACD 水上死叉清仓。

策略逻辑（参数全部来自 .toml，可按需改）：
  建仓（单入口路径A，同一时间仅持有一笔底仓）：
    路径A: 周线 MACD 柱(hist)<0 进入监控区；当某一周 hist 较上周上涨（动能筑底转强）→
          判定日线 J < j_buy 买入底仓；hist 回到 0 轴上方自动解除监控。
  做T（完全基于日线 KDJ 的 J 线）：
    - 加仓：J 较近 5 日高点回落 ≥ drop_from_high 且仍在下降（J<前日J）→ 买 t_buy_amount。
    - 停止加仓：J 反弹（J≥前日J）→ 不买。
    - 卖出加仓：J > j_sell_threshold → 卖光全部加仓部分，底仓不动。
  清仓：日线 MACD 水上死叉（DIF>0, DEA>0, 且 DIF 下穿 DEA）→ 当日收盘清仓全部。
  执行价：当日信号 + 当日收盘（含 look-ahead 偏差，由配置显式声明）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .base import BaseStrategy
from ..indicators import compute_kdj, compute_macd


class KdjMacdDualEntry(BaseStrategy):
    type = "kdj_macd_dual_entry"

    # ------------------------------------------------------------------ describe
    @staticmethod
    def describe(params: dict) -> str:
        """周线MACD+日线KDJ 双入口做T 策略说明（功能不变，键访问全部兜底）。"""
        entry = params.get("entry") or {}
        pa = entry.get("path_a") or {}
        tt = params.get("t_trade") or {}
        comm = (params.get("commission") or 0) * 10000
        tax = (params.get("stamp_tax") or 0) * 10000
        lot_size = params.get("lot_size") or 100
        t_amt_wan = (params.get("t_buy_amount") or 0) / 10000

        return (
            f"- 建仓（两条平行入口，同一时间仅持有一笔底仓）：\n"
            f"  路径A：周线MACD柱(hist)<0进入监控区；当某一周hist较上周上涨（动能筑底转强）开始判定；"
            f"日线KDJ的J线<{pa.get('j_buy', 50)}当日收盘买入底仓；hist回到0轴上方自动解除监控。\n"
            f"- 做T（仅看日线J线）：J较近5日高点回落≥{tt.get('drop_from_high', 30)}且仍在下降（J<前日J）当日收盘买{t_amt_wan}万；"
            f"J反弹（J≥前日J）停止加仓；J>{tt.get('j_sell_threshold', 80)}当日收盘卖光全部加仓部分，底仓不动。\n"
            f"- 清仓：日线MACD水上死叉（DIF>0且DEA>0且DIF下穿DEA）当日收盘清仓全部。\n"
            f"- 执行价：当日信号+当日收盘（含 look-ahead 偏差）；A股 T+1 / {lot_size}股整手 / "
            f"佣金{comm:.0f}bp双边 / 印花税{tax:.0f}bp卖方 / 前复权qfq。\n"
            f"- 周线信号对齐采用 backward merge（最新 weekly_date ≤ daily_date），周内不使用未来数据。"
        )

    # ------------------------------------------------------------------ helpers
    def _fee_cost(self, size, price):
        return size * price * (1 + self.commission)

    def _fee_proceeds(self, size, price):
        return size * price * (1 - self.commission - self.stamp_tax)

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

    def _close_t(self, lots, exit_price, exit_date, exit_bar, trade_history, label="T加仓"):
        total_size = sum(l["size"] for l in lots)
        if total_size <= 0:
            return 0.0
        total_cost = sum(self._fee_cost(l["size"], l["price"]) for l in lots)
        avg_entry = sum(l["size"] * l["price"] for l in lots) / total_size
        proceeds = self._fee_proceeds(total_size, exit_price)
        pnl = proceeds - total_cost
        pnl_pct = pnl / total_cost * 100.0 if total_cost else 0.0
        trade_history.append({
            "entry_date": lots[0]["date"], "exit_date": exit_date,
            "side": "long", "size": total_size,
            "entry_price": round(avg_entry, 4), "exit_price": round(exit_price, 4),
            "pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 2),
            "holding_bars": exit_bar - lots[0]["bar"],
            "symbol": self.symbol, "symbol_name": self.symbol_name,
            "label": label, "role": "做T",
            "position_id": lots[0].get("position_id", ""),
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
        t_buy = float(p["t_buy_amount"])
        macd_p = p["macd"]
        kdj_p = p["kdj"]
        entry = p["entry"]
        pa = entry["path_a"]
        tt = p["t_trade"]
        exit_cfg = p["exit"]
        single_base = bool(p.get("single_base_position", {}).get("enabled", True))

        # ---- 日线指标 ----
        d_dif, d_dea, d_hist = compute_macd(daily["close"], macd_p["fast"], macd_p["slow"], macd_p["signal"])
        daily["dif"], daily["dea"], daily["hist"] = d_dif, d_dea, d_hist
        j = compute_kdj(daily, kdj_p["n"], kdj_p["m1"], kdj_p["m2"])
        daily["J"] = j
        daily["J_prev"] = j.shift(1)
        daily["J_5d_high"] = j.rolling(5).max()
        daily["above_water"] = (daily["dif"] > 0) & (daily["dea"] > 0)
        daily["death_cross"] = (daily["dif"].shift(1) >= daily["dea"].shift(1)) & (daily["dif"] < daily["dea"])
        daily["clear_signal"] = daily["above_water"] & daily["death_cross"]

        # ---- 周线指标 ----
        w_dif, w_dea, w_hist = compute_macd(weekly["close"], macd_p["fast"], macd_p["slow"], macd_p["signal"])
        weekly["wk_hist"] = w_hist
        weekly["wk_hist_prev"] = w_hist.shift(1)
        weekly["wk_trigger"] = (
            (weekly["wk_hist"] < 0) & (weekly["wk_hist"] > weekly["wk_hist_prev"])
        ) if (pa.get("armed_hist_below_zero") and pa.get("armed_hist_rising")) else pd.Series(False, index=weekly.index)
        weekly["wk_J"] = compute_kdj(weekly, kdj_p["n"], kdj_p["m1"], kdj_p["m2"])
        weekly["wk_J_prev"] = weekly["wk_J"].shift(1)

        # ---- 周线 → 日线对齐（backward，无未来数据）----
        wk = (weekly[["date", "wk_hist", "wk_hist_prev", "wk_trigger", "wk_J", "wk_J_prev"]]
              .rename(columns={"date": "wk_date"}).sort_values("wk_date"))
        daily = daily.sort_values("date")
        daily = pd.merge_asof(daily, wk, left_on="date", right_on="wk_date", direction="backward")

        # ---- 状态 ----
        cash = initial_cash
        base_shares = 0
        pid_counter = 0
        base_trade = None
        t_lots: list = []
        t_shares = 0
        monitoring_armed = False
        last_trigger_week_date = None
        trade_history: list = []
        equity_curve: list = []
        peak_total_value: float = 0.0  # 持仓期间总资产峰值（用于 6% 回撤清仓）

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
            J_prev = row["J_prev"]
            J_5d_high = row["J_5d_high"]
            clear_sig = bool(row["clear_signal"]) if pd.notna(row["clear_signal"]) else False
            wk_trig = bool(row["wk_trigger"]) if pd.notna(row["wk_trigger"]) else False
            wk_date = row["wk_date"]

            holding = (base_shares > 0) or (t_shares > 0)

            # 峰值回落检测（持仓期间总资产从最高点回落 >= 6% → 清仓）
            current_value = cash + (base_shares + t_shares) * close
            if current_value > peak_total_value:
                peak_total_value = current_value
            peak_drop = peak_total_value > 0 and (peak_total_value - current_value) >= peak_total_value * 0.06

            # 1. 清仓（最高优先级）：MACD 水上死叉 或 峰值回落 6%，满足其一即清
            exit_cond = (
                (clear_sig and exit_cfg.get("daily_macd_water_death_cross", True))
                or peak_drop
            )
            if holding and exit_cond:
                exit_price = close
                if t_shares > 0 and t_lots:
                    cash += self._close_t(t_lots, exit_price, date_str, i, trade_history, label="T加仓(清仓)")
                    t_shares = 0
                    t_lots = []
                if base_shares > 0 and base_trade is not None:
                    cash += self._close_base(base_trade, exit_price, date_str, i, trade_history, label="底仓(清仓)")
                    base_shares = 0
                    base_trade = None
                monitoring_armed = False
                peak_total_value = 0.0
                equity_curve.append({"date": date_str, "value": round(cash, 2)})
                continue

            # 2. 持仓中：做T
            if holding:
                if J > tt.get("j_sell_threshold", 80) and t_shares > 0:
                    eligible = [lot for lot in t_lots if lot["bar"] < i]
                    if eligible:
                        cash += self._close_t(eligible, close, date_str, i, trade_history, label="T加仓(J>80卖出)")
                        eligible_ids = {id(l) for l in eligible}
                        t_lots = [lot for lot in t_lots if id(lot) not in eligible_ids]
                        t_shares = sum(l["size"] for l in t_lots)
                if (pd.notna(J_5d_high) and pd.notna(J_prev)
                        and (J_5d_high - J) >= tt.get("drop_from_high", 30) and (J < J_prev)):
                    if cash >= t_buy:
                        size = int(t_buy / close)
                        size = (size // self.lot_size) * self.lot_size
                        if size > 0:
                            cash -= self._fee_cost(size, close)
                            t_lots.append({"date": date_str, "price": close, "size": size, "bar": i,
                                           "position_id": base_trade["position_id"]})
                            t_shares += size
                equity_curve.append({"date": date_str, "value": round(cash + (base_shares + t_shares) * close, 2)})
                continue

            # 3. 空仓：监控 + 建仓（单底仓约束）
            if (not single_base) or base_shares == 0:
                bought_a = False
                # 路径A
                if pa.get("enabled") and wk_trig and pd.notna(wk_date) and wk_date != last_trigger_week_date:
                    monitoring_armed = True
                    last_trigger_week_date = wk_date
                if pa.get("disarm_hist_above_zero") and pd.notna(row.get("wk_hist")) and row["wk_hist"] >= 0:
                    monitoring_armed = False
                if (pa.get("enabled") and monitoring_armed and J < pa.get("j_buy", 50)
                        and cash >= base_buy):
                    size = int(base_buy / close)
                    size = (size // self.lot_size) * self.lot_size
                    if size > 0:
                        cost_ = self._fee_cost(size, close)
                        cash -= cost_
                        base_shares = size
                        pid_counter += 1
                        base_trade = {"entry_date": date_str, "entry_price": close, "size": size,
                                      "entry_bar": i, "position_id": pid_counter}
                        peak_total_value = cash + size * close  # 建仓时设初始峰值
                        monitoring_armed = False
                        bought_a = True
        equity_curve.append({"date": date_str, "value": round(cash + (base_shares + t_shares) * close, 2)})

        # 期末强制平仓
        last_row = daily.iloc[-1]
        last_date = last_row["date"].strftime("%Y-%m-%d")
        last_close = float(last_row["close"])
        last_bar = len(daily) - 1
        if t_shares > 0 and t_lots:
            cash += self._close_t(t_lots, last_close, last_date, last_bar, trade_history, label="T加仓(期末平仓)")
            t_shares = 0
            t_lots = []
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
        """合并成交明细：一个持仓组 = 一笔底仓 + 其若干做T，下方子行列出每笔做T收益。"""
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
