# -*- coding: utf-8 -*-
"""Regime 市场状态识别（移植自 deepseek-harness-quant strategy/timing.py，MIT）。

依据：规则法（危机优先）+ 切换纪律（防"过敏型误判"烧掉切换成本）。
输入：沪深300 日线（或任意基准指数）
输出：五档状态 → 现金比例 → 可交易开关

状态定义（config 可覆盖）：
| 状态 | 判定 | 现金 |
|---|---|---|
| panic 恐慌崩跌 | 波动率>30% 且 相关性>0.7（危机优先） | 100% |
| downtrend 下跌 | 价<MA200 且 ADX>25 | 80% |
| choppy 震荡压缩 | ADX<20 或 价在 MA50/MA200 之间缠绕 | 50% |
| uptrend_volatile 上升波动加剧 | 价>MA200 但 ATR 扩张 | 20% |
| strong_uptrend 强势上升 | 价>MA50>MA200 且 ADX>25 | 0% |

★切换纪律（防误判烧切换成本）：
- 连续 N 天确认才切换（默认 5，可配 3-5）
- 切换后冷却期 N 个交易日禁止再切换（防 whipsaw）
- 不确定态：刚切换 N 天内降仓 50% 等待（宁可错过不可放大风险）
"""
from __future__ import annotations

import numpy as np
import pandas as pd


class RegimeDetector:
    """规则法 Regime 检测器（危机优先）"""

    def __init__(self, config: dict | None = None):
        cfg = config or {}
        self.ma_fast = cfg.get("ma_fast", 50)
        self.ma_slow = cfg.get("ma_slow", 200)
        self.adx_window = cfg.get("adx_window", 14)
        self.atr_window = cfg.get("atr_window", 20)
        self.vol_window = cfg.get("vol_window", 60)        # 波动率窗口（年化）
        self.crisis_vol = cfg.get("crisis_vol", 0.30)      # 危机波动率阈值（年化30%）
        self.adx_trend = cfg.get("adx_trend", 25)          # 趋势 ADX 阈值
        self.adx_range = cfg.get("adx_range", 20)          # 震荡 ADX 阈值
        self.cash_map = cfg.get("cash_map", {
            "strong_uptrend": 0.00, "uptrend_volatile": 0.20,
            "choppy": 0.50, "downtrend": 0.80, "panic": 1.00,
        })
        # 切换纪律
        self.confirm_days = cfg.get("confirm_days", 5)     # 连续 N 天确认（3快/5稳，默认5）
        self.cooldown_days = cfg.get("cooldown_days", 0)   # 切换后冷却期（交易日）
        self.uncertain_transition_days = cfg.get("uncertain_transition_days", 5)  # 刚切换后视为不确定的天数
        self._pending = None                               # 待确认状态
        self._pending_days = 0
        self._current = "choppy"                           # 初始保守：震荡
        self.switch_count = 0                              # 健康自检：切换次数
        self.state_days = 0                                # 状态持续天数
        self._cooldown_left = 0                            # 剩余冷却天数

    # ---------- 指标计算 ----------
    @staticmethod
    def adx(high, low, close, window=14):
        """ADX（平均趋向指数）：>25 趋势 / <20 震荡"""
        up = high.diff()
        down = -low.diff()
        plus_dm = np.where((up > down) & (up > 0), up, 0.0)
        minus_dm = np.where((down > up) & (down > 0), down, 0.0)
        tr = pd.concat([high - low, (high - close.shift()).abs(),
                        (low - close.shift()).abs()], axis=1).max(axis=1)
        atr = tr.rolling(window).mean()
        plus_di = 100 * pd.Series(plus_dm, index=high.index).rolling(window).mean() / atr
        minus_di = 100 * pd.Series(minus_dm, index=high.index).rolling(window).mean() / atr
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
        return dx.rolling(window).mean()

    @staticmethod
    def annualized_vol(close, window=60):
        """年化波动率"""
        return close.pct_change().rolling(window).std() * np.sqrt(252)

    # ---------- 状态判定（核心）----------
    def _classify(self, df: pd.DataFrame) -> str:
        """单日截面分类（危机优先）"""
        close = df["close"]
        high, low = df["high"], df["low"]
        vol = self.annualized_vol(close, self.vol_window).iloc[-1]
        adx_val = self.adx(high, low, close, self.adx_window).iloc[-1]
        ma_f, ma_s = close.rolling(self.ma_fast).mean().iloc[-1], close.rolling(self.ma_slow).mean().iloc[-1]
        price = close.iloc[-1]
        atr_now = (high - low).rolling(self.atr_window).mean().iloc[-1]
        atr_prev = (high - low).rolling(self.atr_window).mean().iloc[-self.atr_window * 2:-self.atr_window].mean() if len(df) > self.atr_window * 2 else atr_now

        # 危机优先（宁可误判少赚，不可漏判巨亏）
        if vol > self.crisis_vol:  # 相关性数据通常无，用波动率近似
            return "panic"
        # 强势上升
        if price > ma_f > ma_s and adx_val > self.adx_trend:
            return "strong_uptrend"
        # 下跌趋势
        if price < ma_s and adx_val > self.adx_trend:
            return "downtrend"
        # 震荡压缩
        if adx_val < self.adx_range:
            return "choppy"
        # 上升波动加剧（价格在均线上方但波动放大）
        if price > ma_s and atr_now > atr_prev * 1.2:
            return "uptrend_volatile"
        return "choppy"

    # ---------- 带切换纪律的状态机 ----------
    def update(self, df: pd.DataFrame) -> str:
        """每日更新：单日分类 → 连续确认 → 渐进切换 + 冷却期（低频化）"""
        new_state = self._classify(df)

        # 冷却期：切换后 N 个交易日内冻结切换（pending 不积累），防震荡市 whipsaw
        if self._cooldown_left > 0:
            self._cooldown_left -= 1
            return self._current

        # 切换确认机制：连续 N 天相同才确认（防过敏型误判）
        if new_state == self._pending:
            self._pending_days += 1
        else:
            self._pending, self._pending_days = new_state, 1

        if self._pending_days < self.confirm_days:
            return self._current  # 未确认，维持现状（不切换）

        # 确认切换
        if new_state != self._current:
            self._current = new_state
            self.switch_count += 1
            self.state_days = 0
            self._cooldown_left = self.cooldown_days   # 切换后进入冷却
            self._pending = None                       # 重置待确认，防止冷却结束后旧信号立即触发
            self._pending_days = 0
        self.state_days += 1
        return self._current

    # ---------- 向量化路径（O(n)，消除每根重算整段指标的 O(n^2)）----------
    def fit(self, df: pd.DataFrame) -> None:
        """一次性预计算全部滚动指标序列（O(n)），供 ``update_at`` 逐根读取标量。

        等价于 ``_classify`` 内部对整段窗口做的滚动计算，但只算一遍，而非在
        每根 K 线上重算。``run()`` 循环改为 ``fit(df)`` + ``update_at(i)``。
        """
        close = df["close"].astype(float)
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        self._close_s = close
        self._vol_s = self.annualized_vol(close, self.vol_window)
        self._adx_s = self.adx(high, low, close, self.adx_window)
        self._ma_f_s = close.rolling(self.ma_fast).mean()
        self._ma_s_s = close.rolling(self.ma_slow).mean()
        self._atr_s = (high - low).rolling(self.atr_window).mean()

    def _classify_at(self, i: int) -> str:
        """与 ``_classify`` 等价，但读取已预计算的滚动序列第 ``i`` 个标量（O(1)）。"""
        vol = self._vol_s.iloc[i]
        adx_val = self._adx_s.iloc[i]
        ma_f = self._ma_f_s.iloc[i]
        ma_s = self._ma_s_s.iloc[i]
        price = self._close_s.iloc[i]
        atr_now = self._atr_s.iloc[i]
        w = self.atr_window
        atr_prev = self._atr_s.iloc[max(0, i - w * 2): i - w].mean() if i > w * 2 else atr_now

        if pd.notna(vol) and vol > self.crisis_vol:
            return "panic"
        if price > ma_f > ma_s and pd.notna(adx_val) and adx_val > self.adx_trend:
            return "strong_uptrend"
        if price < ma_s and pd.notna(adx_val) and adx_val > self.adx_trend:
            return "downtrend"
        if pd.notna(adx_val) and adx_val < self.adx_range:
            return "choppy"
        if price > ma_s and atr_now > atr_prev * 1.2:
            return "uptrend_volatile"
        return "choppy"

    def update_at(self, i: int) -> str:
        """向量化逐根更新：分类 → 连续确认 → 渐进切换 + 冷却期（与 ``update`` 同逻辑）。"""
        new_state = self._classify_at(i)

        if self._cooldown_left > 0:
            self._cooldown_left -= 1
            return self._current

        if new_state == self._pending:
            self._pending_days += 1
        else:
            self._pending, self._pending_days = new_state, 1

        if self._pending_days < self.confirm_days:
            return self._current

        if new_state != self._current:
            self._current = new_state
            self.switch_count += 1
            self.state_days = 0
            self._cooldown_left = self.cooldown_days
            self._pending = None
            self._pending_days = 0
        self.state_days += 1
        return self._current

    # ---------- 输出 ----------
    def cash_ratio(self) -> float:
        """当前现金比例（由状态映射；刚切换后 = 不确定态降仓等待）"""
        if self.state_days < self.uncertain_transition_days:
            # 不确定态：刚切换 N 天内降仓 50% 等待，宁可错过不可放大风险
            return max(self.cash_map.get(self._current, 0.5), 0.5)
        return self.cash_map.get(self._current, 0.5)

    def can_trade(self) -> bool:
        """是否可以开新仓（panic/downtrend/choppy 只减不加）"""
        return self._current in ("strong_uptrend", "uptrend_volatile")

    def health_report(self) -> dict:
        """Regime 健康自检"""
        return {
            "state": self._current,
            "cash_ratio": self.cash_ratio(),
            "can_trade": self.can_trade(),
            "state_days": self.state_days,
            "switch_count": self.switch_count,
            "healthy": self.switch_count <= 3 and self.state_days >= 5,
            "note": "每周切换<3次、状态持续>5天 为健康",
        }


if __name__ == "__main__":
    print("=== Regime 规则法测试（模拟数据）===")
    rng = np.random.default_rng(7)
    n = 800
    dates = pd.date_range("2023-01-01", periods=n, freq="B")
    # 三段式模拟：震荡(0-250) → 强势上升(250-550) → 下跌(550-800)
    rets = np.concatenate([
        rng.normal(0.0001, 0.012, 250),
        rng.normal(0.0012, 0.010, 300),
        rng.normal(-0.0010, 0.014, 250),
    ])
    close = 3000 * np.cumprod(1 + rets)
    df = pd.DataFrame({"close": close,
                       "high": close * 1.008, "low": close * 0.992},
                      index=dates)

    rd = RegimeDetector({"confirm_days": 3})
    states = []
    for i in range(50, n):  # 前 50 天用于指标 warmup
        s = rd.update(df.iloc[:i + 1])
        states.append(s)
    from collections import Counter
    cnt = Counter(states)
    print("状态分布:", dict(cnt))
    print("最终状态:", rd._current, "| 现金比例:", rd.cash_ratio(), "| 可交易:", rd.can_trade())
    print("健康自检:", rd.health_report())
