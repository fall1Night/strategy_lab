# -*- coding: utf-8 -*-
"""大周期定势 · 缩量回踩低吸策略（trend_pullback）。

把「大周期定方向 → 20 日线定趋势 → 缩量回踩找买点 → 放量上涨拿波段 →
破线放量立刻走」落成可执行规则。

结构
----
1. 大周期门控（周线）：周线快/慢均线近 N 周斜率 ≥ -flat_tol（走平或回升），
   且周收盘站在快均线上方 → 允许参与；否则一律空仓，杜绝下降趋势反弹。
2. 标的分型（日线滚动）：
   - 强庄老龙：近 lookback 日区间振幅 ≥ dragon_min_gain 且 涨停/大涨次数 ≥ dragon_limit_days
   - 低位回流：距区间高点回撤 ≥ low_pos_drawdown 且 近 range_window 日带宽 ≤ range_band
     且 近 20 日均量 / 近 120 日均量 ≤ vol_dry_ratio（缩量洗盘充分）
   - 未分类：仅允许「趋势启动」通道（最保守）
3. 入场三通道（优先级 A > B > C，同一日只取其一）：
   - A 趋势启动：MA5 首次上穿 MA20 + MA20 连续拐头向上 + 当日放量收阳（底部放量确认）
   - B 强庄回踩：强庄股回踩 MA20 不破 + 不创新低 + 缩量企稳（主升中继低吸）
   - C 小票回流：低位股回踩 MA10/MA20 不破 + 缩量极致（量能处近 60 日低位分位）
4. 量价否决（全局）：放量阴线 / 放量下跌（视为主力出货）当日禁止买入；
   持仓中遇出货信号立即清仓——此时所有均线支撑视为失效。
5. 出场优先级：出货 → 爆量滞涨分批止盈 → 破线放量止损 → MA5 死叉 MA20 → 大周期转坏。

执行价：当日信号 + 当日收盘（沿用项目既有口径，**含 look-ahead 偏差**，由 describe 声明）。
A 股 T+1（当日买入当日不可卖，天然满足）/ 整手 / 前复权 qfq。

★局限声明（见 docs 规则文档）
- 「无持续减持利空」需公告/股东数据，行情数据无法判定，本策略不实现该过滤。
- 「盘中剧烈震荡洗盘 / 假破位」需分钟级数据，日线近似为「回踩不破 + 缩量」，不识别盘中破位。
- 「控盘度 / 筹码集中」用「区间振幅 + 涨停次数」近似，非真实筹码分布。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .base import BaseStrategy


class TrendPullbackStrategy(BaseStrategy):
    type = "trend_pullback"

    # ------------------------------------------------------------------ 描述
    @staticmethod
    def describe(params: dict) -> str:
        p = params or {}
        mj = p.get("major") or {}
        pf = p.get("profile") or {}
        en = p.get("entry") or {}
        ex = p.get("exit") or {}
        maf = int(mj.get("ma_fast", 20))
        flat = float(mj.get("flat_tol", 0.005))
        ma20 = int(en.get("ma20", 20))
        shrink = float(en.get("pullback_vol", 0.8))
        stop_vol = float(ex.get("stop_vol", 1.0))
        tp = bool(ex.get("take_profit", True))
        return (
            f"- 大周期门控：周线 MA{maf}/MA{int(mj.get('ma_slow', 30))} 近 {int(mj.get('slope_weeks', 4))} 周"
            f"斜率 ≥ -{flat}（走平/回升）才允许入场，下降趋势一律空仓。\n"
            f"- 标的分型：强庄老龙（区间振幅 ≥ {float(pf.get('dragon_min_gain', 0.4)):.0%} 且大涨日 ≥ "
            f"{int(pf.get('dragon_limit_days', 2))} 天）/ 低位回流小票（回撤 ≥ {float(pf.get('low_pos_drawdown', 0.3)):.0%}"
            f"+ 横盘带宽 ≤ {float(pf.get('range_band', 0.25)):.0%} + 量能枯竭）。\n"
            f"- 入场：A 死叉后首金叉 MA5/MA{ma20} + MA{ma20} 拐头向上 + 放量收涨；"
            f"B 强庄回踩 MA{ma20} 不破 + 缩量 ≤ {shrink:.2f}× 量均；"
            f"C 小票回踩 MA10/MA{ma20} + 缩量极致。\n"
            f"- 量价否决：放量阴线 / 放量下跌（出货）禁买；持仓遇出货即清。\n"
            f"- 出场：爆量滞涨分批止盈{'(默认 50%)' if tp else '关闭'} → 破线放量止损（趋势票破 MA{ma20}+放量 "
            f"{stop_vol:.1f}×，小票破 MA10）→ 死叉 → 大周期转坏。\n"
            f"- 单笔底仓、可分批止盈；执行价当日收盘（含 look-ahead 偏差）。A股 T+1 / 整手 / 前复权 qfq。"
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

        mj = dict(p.get("major") or {})
        pf = dict(p.get("profile") or {})
        en = dict(p.get("entry") or {})
        ex = dict(p.get("exit") or {})

        # ---------- 参数 ----------
        w_fast = int(mj.get("ma_fast", 20))
        w_slow = int(mj.get("ma_slow", 30))
        slope_w = int(mj.get("slope_weeks", 4))
        flat_tol = float(mj.get("flat_tol", 0.005))
        req_above = bool(mj.get("require_above_fast", True))

        lb = int(pf.get("lookback", 250))
        dragon_gain = float(pf.get("dragon_min_gain", 0.40))
        dragon_lim = int(pf.get("dragon_limit_days", 2))
        lim_pct = float(pf.get("limit_pct", 0.098))
        low_dd = float(pf.get("low_pos_drawdown", 0.30))
        rw = int(pf.get("range_window", 60))
        range_band = float(pf.get("range_band", 0.25))
        vol_dry = float(pf.get("vol_dry_ratio", 0.85))

        n5 = int(en.get("ma5", 5))
        n10 = int(en.get("ma10", 10))
        n20 = int(en.get("ma20", 20))
        vma_n = int(en.get("vol_ma", 5))
        bkv = float(en.get("breakout_vol", 1.3))
        turn_days = int(en.get("turn_up_days", 1))
        touch = float(en.get("pullback_touch", 0.015))
        pb_vol = float(en.get("pullback_vol", 0.80))
        sc_vol = float(en.get("smallcap_vol", 0.70))
        sc_pctile = float(en.get("smallcap_vol_pctile", 0.30))
        nolow_w = int(en.get("no_newlow_window", 20))
        up_vol_min = float(en.get("up_vol_min", 1.0))
        down_vol_max = float(en.get("down_vol_max", 1.2))

        use_ma20_stop = bool(ex.get("use_ma20_stop", True))
        stop_vol = float(ex.get("stop_vol", 1.0))
        sc_stop_ma = int(ex.get("smallcap_stop_ma", 10))
        dc_exit = bool(ex.get("death_cross_exit", True))
        tp_on = bool(ex.get("take_profit", True))
        tp_gain = float(ex.get("tp_min_gain", 0.08))
        tp_blow = float(ex.get("tp_vol_blowoff", 2.0))
        tp_shadow = float(ex.get("tp_upper_shadow", 0.45))
        tp_ratio = float(ex.get("tp_ratio", 0.5))

        # ---------- 周线门控（对齐到最近一根已收盘周线，merge_asof 后向） ----------
        w = weekly.copy().sort_values("date")
        w["date"] = pd.to_datetime(w["date"])
        wc = w["close"].astype(float)
        wma_f = wc.rolling(w_fast, min_periods=max(5, w_fast // 2)).mean()
        wma_s = wc.rolling(w_slow, min_periods=max(8, w_slow // 2)).mean()
        wok = pd.Series(True, index=w.index)
        if req_above:
            wok &= (wc > wma_f)
        wok &= ((wma_f / wma_f.shift(slope_w) - 1.0) >= -flat_tol)
        wok &= ((wma_s / wma_s.shift(slope_w) - 1.0) >= -flat_tol)
        wok = wok.fillna(False)
        w_state = pd.DataFrame({"date": w["date"], "major_ok": wok})

        d = daily.copy()
        d["date"] = pd.to_datetime(d["date"])
        merged = pd.merge_asof(
            d[["date"]].sort_values("date"), w_state.sort_values("date"),
            on="date", direction="backward",
        )
        major_ok = merged["major_ok"].fillna(False).to_numpy(dtype=bool)

        # ---------- 日线指标（全部历史窗口，无未来函数） ----------
        close = d["close"].astype(float).to_numpy()
        open_px = d["open"].astype(float).to_numpy()
        high = d["high"].astype(float).to_numpy()
        low = d["low"].astype(float).to_numpy()
        has_vol = "vol" in d.columns and d["vol"].notna().any()
        vol = d["vol"].astype(float).to_numpy() if has_vol else None

        s_close = pd.Series(close)
        ma5 = s_close.rolling(n5).mean()
        ma10 = s_close.rolling(n10).mean()
        ma20 = s_close.rolling(n20).mean()
        vma = (pd.Series(vol).rolling(vma_n).mean() if has_vol
               else pd.Series(np.inf, index=s_close.index))
        prev_close = s_close.shift(1)

        # 金叉 / 死叉（点事件）
        gold = ((ma5.shift(1) <= ma20.shift(1)) & (ma5 > ma20)).to_numpy(dtype=bool)
        death = ((ma5.shift(1) >= ma20.shift(1)) & (ma5 < ma20)).to_numpy(dtype=bool)
        # MA20 连续拐头向上
        turn_up = pd.Series(True, index=s_close.index)
        for k in range(1, turn_days + 1):
            turn_up &= (ma20 > ma20.shift(k))
        turn_up = turn_up.to_numpy(dtype=bool)
        # 量能关系（无成交量的缓存 → 放行全部量条件）
        if has_vol:
            vol_ratio = vol / np.where(np.isnan(vma) | (vma == 0), np.nan, vma)
            up_vol_ok = vol_ratio >= up_vol_min
            bkv_ok = vol_ratio >= bkv
            pb_shrink = vol_ratio <= pb_vol
            sc_shrink = vol_ratio <= sc_vol
            stop_vol_ok = vol_ratio >= stop_vol
            blow_ok = vol_ratio >= tp_blow
            # 放量阴线 / 放量下跌（出货）
            down_vol_bad = (close < open_px) & (vol_ratio >= down_vol_max)
            distribute = (close < prev_close.to_numpy()) & (vol_ratio >= down_vol_max)
            # 近 5 日均量在近 60 日的分位（抛压耗尽）
            vol_pct = pd.Series(vol).rolling(vma_n).mean().rolling(60).rank(pct=True)
            vol_low_pos = vol_pct <= sc_pctile
        else:
            up_vol_ok = np.ones(len(d), dtype=bool)
            bkv_ok = np.ones(len(d), dtype=bool)
            pb_shrink = np.ones(len(d), dtype=bool)
            sc_shrink = np.ones(len(d), dtype=bool)
            stop_vol_ok = np.ones(len(d), dtype=bool)
            blow_ok = np.zeros(len(d), dtype=bool)
            down_vol_bad = np.zeros(len(d), dtype=bool)
            distribute = np.zeros(len(d), dtype=bool)
            vol_low_pos = np.ones(len(d), dtype=bool)

        # ---------- 标的分型（滚动窗口，min_periods 容忍数据前缘） ----------
        s_high = pd.Series(high)
        s_low = pd.Series(low)
        minp = max(60, int(lb * 0.6))
        hh = s_high.rolling(lb, min_periods=minp).max()
        ll = s_low.rolling(lb, min_periods=minp).min()
        rng = (hh / ll - 1.0)
        chg = (s_close / prev_close - 1.0).fillna(0.0)
        lim_cnt = (chg >= lim_pct).rolling(lb, min_periods=minp).sum()
        dd_high = s_close / s_close.rolling(lb, min_periods=minp).max() - 1.0
        band = ((s_high.rolling(rw, min_periods=rw // 2).max()
                 - s_low.rolling(rw, min_periods=rw // 2).min())
                / s_low.rolling(rw, min_periods=rw // 2).min())
        if has_vol:
            dry = (pd.Series(vol).rolling(20).mean()
                   / pd.Series(vol).rolling(120).mean())
        else:
            dry = pd.Series(1.0, index=s_close.index)

        is_dragon = ((rng >= dragon_gain) & (lim_cnt >= dragon_lim)).to_numpy(dtype=bool)
        is_low = ((dd_high <= -low_dd) & (band <= range_band)
                  & (dry <= vol_dry)).to_numpy(dtype=bool)
        # 分型数据不足（NaN）→ 都 False → 只放行通道 A
        is_dragon = np.nan_to_num(is_dragon, nan=False).astype(bool)
        is_low = np.nan_to_num(is_low, nan=False).astype(bool)

        # ---------- 首次金叉状态机（纯历史状态，eval 前也推进） ----------
        n = len(d)
        last_cross = np.zeros(n, dtype=np.int8)  # 0 无 / 1 金叉 / -1 死叉
        cur = 0
        for i in range(n):
            if gold[i]:
                cur = 1
            elif death[i]:
                cur = -1
            last_cross[i] = cur
        prev_cross = np.concatenate(([0], last_cross[:-1]))
        first_gold = gold & (prev_cross == -1)  # 死叉后的第一次金叉

        # 回踩不创新低（不含当日）
        low_ll = pd.Series(low).rolling(nolow_w).min().shift(1)
        no_new_low = low >= low_ll.to_numpy()
        no_new_low = np.nan_to_num(no_new_low, nan=True).astype(bool)

        # 回踩触线（用 low 触线近似盘中回踩 + 收盘不破）
        ma20_np = ma20.to_numpy()
        ma10_np = ma10.to_numpy()
        touch20 = (low <= ma20_np * (1 + touch)) & (close >= ma20_np)
        touch10 = (low <= ma10_np * (1 + touch)) & (close >= ma10_np)
        touch20 = np.nan_to_num(touch20, nan=False).astype(bool)
        touch10 = np.nan_to_num(touch10, nan=False).astype(bool)
        up_vol_ok = np.nan_to_num(up_vol_ok, nan=False).astype(bool)
        bkv_ok = np.nan_to_num(bkv_ok, nan=False).astype(bool)
        pb_shrink = np.nan_to_num(pb_shrink, nan=False).astype(bool)
        sc_shrink = np.nan_to_num(sc_shrink, nan=False).astype(bool)
        stop_vol_ok = np.nan_to_num(stop_vol_ok, nan=False).astype(bool)
        blow_ok = np.nan_to_num(blow_ok, nan=False).astype(bool)
        down_vol_bad = np.nan_to_num(down_vol_bad, nan=False).astype(bool)
        distribute = np.nan_to_num(distribute, nan=False).astype(bool)
        vol_low_pos = np.nan_to_num(vol_low_pos, nan=True).astype(bool)

        # 通道 A：死叉后首次金叉 + MA20 拐头向上 + 放量收涨（底部放量确认）
        ch_a = (first_gold & turn_up & bkv_ok
                & (close > prev_close.to_numpy()) & np.isfinite(ma20_np))
        # 通道 B：强庄回踩 MA20 缩量企稳
        ch_b = (is_dragon & touch20 & no_new_low & pb_shrink
                & np.isfinite(ma20_np))
        # 通道 C：低位小票回踩 MA10/MA20 + 缩量极致 + 量能低位分位
        ch_c = (is_low & (touch10 | touch20) & sc_shrink & vol_low_pos
                & np.isfinite(ma10_np))
        # 大周期不达标 / 出货日 / 放量阴线 → 一律不买
        buy_block = (~major_ok) | down_vol_bad | distribute
        buy_a = ch_a & ~buy_block
        buy_b = ch_b & ~buy_block
        buy_c = ch_c & ~buy_block

        # ---------- 回测主循环（当日信号 + 当日收盘成交，沿用项目口径） ----------
        cash = initial_cash
        shares = 0
        entry_price = 0.0
        entry_date = ""
        entry_bar = 0
        pid = 0
        base_recorded = False      # 该 position_id 是否已产生过「底仓」成交记录
        last_valid_close = close[0] if n else np.nan
        trade_history: list = []
        equity_curve: list = []
        dates = d["date"].dt.strftime("%Y-%m-%d").to_numpy()

        eval_start = pd.Timestamp(start)
        eval_end = pd.Timestamp(end)

        for i in range(n):
            ts = d["date"].iloc[i]
            if ts < eval_start or ts > eval_end:
                continue
            date_str = dates[i]
            cpx = float(close[i])
            if np.isnan(cpx):  # 停牌日：不交易，按最近有效收盘估值
                value = cash + shares * float(last_valid_close)
                equity_curve.append({"date": date_str, "value": round(value, 2)})
                continue
            last_valid_close = cpx

            if shares > 0:
                # 1) 放量下跌 = 主力出货 → 无条件清仓
                if distribute[i]:
                    cash += self._sell(shares, cpx, date_str, i, entry_price,
                                       entry_date, entry_bar, pid, trade_history,
                                       role="底仓" if not base_recorded else "减仓",
                                       reason="放量下跌出货")
                    base_recorded = True
                    shares = 0
                    pid += 1
                    base_recorded = False
                    equity_curve.append({"date": date_str, "value": round(cash, 2)})
                    continue
                # 2) 爆量滞涨 → 分批止盈（浮盈达标才止盈）
                gain_pct = cpx / entry_price - 1.0
                upper_shadow = np.where(high[i] > low[i],
                                        (high[i] - cpx) / (high[i] - low[i]), 0.0)
                stagnant = (upper_shadow >= tp_shadow) | (cpx <= float(close[i - 1]) if i > 0 else True)
                if (tp_on and blow_ok[i] and stagnant and gain_pct >= tp_gain
                        and shares >= lot_size * 2):
                    reduce_size = int(shares * tp_ratio / lot_size) * lot_size
                    if reduce_size >= shares:  # 减到不足以留一手 → 全清
                        reduce_size = shares
                    if reduce_size > 0:
                        cash += self._sell(reduce_size, cpx, date_str, i, entry_price,
                                           entry_date, entry_bar, pid, trade_history,
                                           role="底仓" if not base_recorded else "减仓",
                                           reason=f"爆量滞涨止盈{int(reduce_size/shares*100)}%"
                                           if reduce_size < shares else "爆量滞涨止盈(清仓)")
                        base_recorded = True
                        shares -= reduce_size
                        if shares <= 0:
                            pid += 1
                            base_recorded = False
                            equity_curve.append({"date": date_str, "value": round(cash, 2)})
                            continue
                # 3) 止损：趋势票破 MA20 + 放量；小票破 MA10 收不回
                if shares > 0:
                    trend_like = not bool(is_low[i])  # 非低位分型按趋势票管理
                    if trend_like and use_ma20_stop and not np.isnan(ma20_np[i]) \
                            and cpx < ma20_np[i] and stop_vol_ok[i]:
                        cash += self._sell(shares, cpx, date_str, i, entry_price,
                                           entry_date, entry_bar, pid, trade_history,
                                           role="底仓" if not base_recorded else "减仓",
                                           reason="破MA20放量止损")
                        base_recorded = True
                        shares = 0
                        pid += 1
                        base_recorded = False
                        equity_curve.append({"date": date_str, "value": round(cash, 2)})
                        continue
                    if (not trend_like) and not np.isnan(ma10_np[i]) and cpx < ma10_np[i]:
                        cash += self._sell(shares, cpx, date_str, i, entry_price,
                                           entry_date, entry_bar, pid, trade_history,
                                           role="底仓" if not base_recorded else "减仓",
                                           reason="破MA10止损(小票)")
                        base_recorded = True
                        shares = 0
                        pid += 1
                        base_recorded = False
                        equity_curve.append({"date": date_str, "value": round(cash, 2)})
                        continue
                # 4) MA5 死叉 MA20 → 趋势走坏清仓
                if shares > 0 and dc_exit and death[i]:
                    cash += self._sell(shares, cpx, date_str, i, entry_price,
                                       entry_date, entry_bar, pid, trade_history,
                                       role="底仓" if not base_recorded else "减仓",
                                       reason="MA5死叉MA20")
                    base_recorded = True
                    shares = 0
                    pid += 1
                    base_recorded = False
                    equity_curve.append({"date": date_str, "value": round(cash, 2)})
                    continue
                # 5) 大周期转坏 → 空仓
                if shares > 0 and not major_ok[i]:
                    cash += self._sell(shares, cpx, date_str, i, entry_price,
                                       entry_date, entry_bar, pid, trade_history,
                                       role="底仓" if not base_recorded else "减仓",
                                       reason="大周期转坏")
                    base_recorded = True
                    shares = 0
                    pid += 1
                    base_recorded = False
                equity_curve.append({"date": date_str, "value": round(cash + shares * cpx, 2)})
                continue

            # ---------- 空仓：入场 ----------
            buy_flag = None
            if buy_a[i]:
                buy_flag = "A趋势启动金叉"
            elif buy_b[i]:
                buy_flag = "B强庄回踩低吸"
            elif buy_c[i]:
                buy_flag = "C小票回流低吸"
            if buy_flag is not None and cash > 0:
                budget = cash * buy_ratio
                size = int(budget / cpx)
                size = (size // lot_size) * lot_size
                if size > 0:
                    cost_ = self._fee_cost(size, cpx)
                    if cost_ > cash:
                        from ..trading_cost import max_buy_volume
                        size = max_buy_volume(cpx, cash, self.symbol, self.cost_cfg, cash_ratio=1.0)
                        if size <= 0:
                            equity_curve.append({"date": date_str, "value": round(cash, 2)})
                            continue
                        cost_ = self._fee_cost(size, cpx)
                    cash -= cost_
                    shares = size
                    entry_price = cpx
                    entry_date = date_str
                    entry_bar = i
                    pid += 1
                    base_recorded = False
            equity_curve.append({"date": date_str, "value": round(cash + shares * cpx, 2)})

        # ---------- 期末强制平仓（在评估窗口内最后一根有效 K 上执行） ----------
        last_valid_i = None
        for j in range(n - 1, -1, -1):
            tsj = d["date"].iloc[j]
            if eval_start <= tsj <= eval_end and not pd.isna(close[j]):
                last_valid_i = j
                break
        if shares > 0 and last_valid_i is not None:
            cash += self._sell(shares, float(close[last_valid_i]), dates[last_valid_i],
                               last_valid_i, entry_price, entry_date, entry_bar, pid,
                               trade_history,
                               role="底仓" if not base_recorded else "减仓",
                               reason="期末平仓")
            shares = 0
        if equity_curve:
            equity_curve[-1]["value"] = round(cash, 2)

        trade_history.sort(key=lambda t: (t["entry_date"], t["exit_date"]))
        positions = self._build_positions(trade_history)
        return {"equity_curve": equity_curve, "trade_history": trade_history,
                "positions": positions}

    # ------------------------------------------------------------------ helper
    def _sell(self, shares, price, exit_date, exit_bar, entry_price, entry_date,
              entry_bar, pid, trade_history, role="底仓", reason="卖出"):
        """卖出并记录一笔成交，返回现金流入（净额）。"""
        cost = self._fee_cost(shares, entry_price)
        proceeds = self._fee_proceeds(shares, price)
        pnl = proceeds - cost
        pnl_pct = pnl / cost * 100.0 if cost else 0.0
        trade_history.append({
            "entry_date": entry_date, "exit_date": exit_date,
            "side": "long", "size": shares,
            "entry_price": round(entry_price, 4), "exit_price": round(price, 4),
            "pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 2),
            "holding_bars": exit_bar - entry_bar,
            "symbol": self.symbol, "symbol_name": self.symbol_name,
            "label": reason, "role": role, "position_id": str(pid),
        })
        return proceeds

    @staticmethod
    def _build_positions(trade_history: list) -> list:
        """合并成交明细：一个持仓组 = 一笔底仓 + 其若干分批止盈/清仓子交易。"""
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
                "entry_price": round(float(b["entry_price"]), 4),
                "exit_price": round(float(b["exit_price"]), 4),
                "holding_bars": b.get("holding_bars", 0),
                "base_pnl": round(float(b["pnl"]), 2),
                "base_pnl_pct": round(float(b["pnl_pct"]), 2),
                "t_count": len(sub),
                "total_pnl": round(total_pnl, 2), "total_pnl_pct": round(total_pct, 2),
                "hold_no_t_pct": round(float(b["pnl_pct"]), 2),
                "trades": sub,
            })
        positions.sort(key=lambda p: p["entry_date"])
        return positions


if __name__ == "__main__":
    # 冒烟自测：合成数据（上升趋势 + 回踩 + 放量）
    rng = np.random.default_rng(7)
    n_days = 900
    dates = pd.date_range("2021-01-04", periods=n_days, freq="B")
    trend = np.linspace(0, 1.2, n_days)
    noise = rng.normal(0, 0.018, n_days).cumsum() * 0.4
    close = 20 * np.exp(trend + noise)
    close = np.maximum(close, 3.0)
    open_px = close * (1 + rng.normal(0, 0.004, n_days))
    high = np.maximum(open_px, close) * (1 + np.abs(rng.normal(0, 0.008, n_days)))
    low = np.minimum(open_px, close) * (1 - np.abs(rng.normal(0, 0.008, n_days)))
    vol = rng.integers(3e5, 3e6, n_days).astype(float)
    # 让回踩段缩量、拉升段放量
    pull = rng.random(n_days) < 0.3
    vol[pull] = vol[pull] * 0.5
    daily = pd.DataFrame({"date": dates, "open": open_px, "high": high,
                          "low": low, "close": close, "vol": vol})
    weekly = daily.resample("W-FRI", on="date").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "vol": "sum"}
    ).reset_index()
    cfg = {"type": "trend_pullback", "params": {"initial_cash": 200000,
                                                "buy_ratio": 0.95}}
    res = TrendPullbackStrategy(cfg).run(daily, weekly, "2021-01-04", "2023-06-30")
    eq = res["equity_curve"]
    trades = res["trade_history"]
    print(f"交易 {len(trades)} 笔 | 净值点 {len(eq)} | "
          f"期末 {eq[-1]['value'] if eq else 'N/A'} | "
          f"首笔 {trades[0]['label'] if trades else '-'}")
