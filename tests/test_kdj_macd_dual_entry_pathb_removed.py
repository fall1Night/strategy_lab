# -*- coding: utf-8 -*-
"""回归测试：验证 kdj_macd_dual_entry 已删除「路径B」，且无回归（路径A 正常建仓）。

覆盖三个层面：
  1. 源码不再包含任何路径B残留标识（与主干核验的 Grep 零命中一致）；
  2. toml 配置 [params.entry] 下只有 path_a、没有 path_b，且能正常加载/实例化；
  3. 行为回归：
     - 场景A（路径A特征）：应产生底仓；
     - 场景B（仅路径B特征、无路径A特征、日线J始终>=j_buy）：不应产生底仓。

测试使用受管 Python（pytest）。无 pytest 时可用 `python tests/xxx.py` 直接跑（见 __main__）。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

# 让 tests/ 能 import src/strategylab（与 conftest.py / qa_helpers.py 保持一致；
# 同时保证脱离 pytest 单独运行也能找到包）。
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np
import pandas as pd
from pandas.tseries.offsets import BDay

from strategylab.engine.indicators import compute_macd, compute_kdj
from strategylab.engine.strategies.kdj_macd_dual_entry import KdjMacdDualEntry
from strategylab.engine.config import load_strategy

ROOT = Path(__file__).resolve().parents[1]
STRATEGY_FILE = ROOT / "src" / "strategylab" / "engine" / "strategies" / "kdj_macd_dual_entry.py"
TOML_FILE = ROOT / "src" / "strategylab" / "resources" / "strategies" / "kdj_macd_dual_entry.toml"

# 主干核验用的路径B残留标识（Grep 应零命中）
PATH_B_TOKENS = (
    "path_b", "wk_entry_b", "last_entryb_week_date", "skip_daily_j", "路径B", "左侧抄底",
)


# --------------------------------------------------------------------------- #
# 数据构造
# --------------------------------------------------------------------------- #
def build_scenario_a():
    """场景A：周线下行筑底后回升（hist<0 且某周转涨 → 路径A触发），日线 J 在底部 < j_buy。

    返回 (daily, weekly)，字段含 date/open/high/low/close，预留足够 warmup。
    """
    n_weeks, dpw = 30, 5
    n = n_weeks * dpw
    decline_end = (n_weeks // 2) * dpw  # 底部所在日索引
    P = np.zeros(n)
    for d in range(n):
        if d <= decline_end:
            P[d] = 100.0 - 45.0 * (d / decline_end)          # 100 -> 55 线性下行
        else:
            P[d] = 55.0 + 37.0 * ((d - decline_end) / (n - 1 - decline_end))  # 55 -> 92 回升

    start = pd.Timestamp("2023-01-02")
    dates = [start + BDay(i) for i in range(n)]
    np.random.seed(0)
    close = P + (np.random.rand(n) - 0.5) * 0.6
    high = close + np.abs(np.random.rand(n)) * 0.8 + 0.2
    low = close - np.abs(np.random.rand(n)) * 0.8 - 0.2
    open_ = close + (np.random.rand(n) - 0.5) * 0.4
    daily = pd.DataFrame({
        "date": dates, "open": open_, "high": high, "low": low, "close": close,
    })
    daily["date"] = pd.to_datetime(daily["date"])

    # 周线 = 每 5 个交易日抽样（与策略 merge_asof backward 对齐口径一致）
    idxs = list(range(dpw - 1, n, dpw))
    weekly = pd.DataFrame({
        "date": [dates[i] for i in idxs],
        "open": [open_[i] for i in idxs],
        "high": [high[i] for i in idxs],
        "low": [low[i] for i in idxs],
        "close": [close[i] for i in idxs],
    })
    weekly["date"] = pd.to_datetime(weekly["date"])
    return daily, weekly


def build_scenario_b():
    """场景B：周线整体下行（hist<0 且从未转涨 → 无路径A触发），但在第9/11周出现

    「hist<0 & 周J<30 & 周J较上周上涨」的旧路径B特征。日线 J 始终 >= j_buy(50)。
    周线/日线 OHLC 独立构造（策略不会由日线重采样周线），故可让周线带路径B特征、
    日线 J 维持高位，互不牵制。

    注：纯线性下行的周线收盘在最后一格会出现 EMA 端点伪「hist 转涨」，故 eval 窗口
    显式排除末周（见测试内 end 取值），确保窗口内只有路径B特征、没有路径A特征。
    """
    wclose = [100 - 2 * i for i in range(16)]  # 纯线性下行 -> hist 单调（末格除外）
    nw = len(wclose)
    start = pd.Timestamp("2023-01-02")
    wk_dates = []
    d = 0
    for i in range(nw):
        d += 5
        wk_dates.append(start + BDay(d - 1))
    whigh, wlow = [], []
    for i, c in enumerate(wclose):
        # 底部周(i==10)收在近低位 -> 周J 极低；邻周(i==9,11)收在远离低位 -> 周J 回升(仍<30)
        m = 0.99 if i == 10 else (0.90 if i in (9, 11) else 0.97)
        whigh.append(c * 1.01)
        wlow.append(c * m)
    weekly = pd.DataFrame({
        "date": wk_dates, "open": wclose, "high": whigh, "low": wlow, "close": wclose,
    })
    weekly["date"] = pd.to_datetime(weekly["date"])

    nd = nw * 5
    ddates = [start + BDay(i) for i in range(nd)]
    # 日线：收在高位附近 -> 日线 J 维持高位（>=50），与周线路径B特征解耦
    np.random.seed(7)
    close = np.linspace(50, 70, nd) + np.sin(np.linspace(0, 6 * np.pi, nd)) * 1.5
    high = close * 1.003
    low = close * 0.90
    daily = pd.DataFrame({
        "date": ddates, "open": close, "high": high, "low": low, "close": close,
    })
    daily["date"] = pd.to_datetime(daily["date"])
    return daily, weekly


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _load_cfg() -> dict:
    return load_strategy(TOML_FILE)


def _count_base(trade_history: list) -> list:
    return [t for t in trade_history if t.get("role") == "底仓"]


def _align_weekly(daily: pd.DataFrame, weekly: pd.DataFrame) -> pd.DataFrame:
    """复刻策略内部的 周线指标计算 + 周线->日线 backward 对齐，便于断言建仓日落在触发周区间内。

    注意：独立计算周线指标（不依赖策略对入参的就地修改），保证与策略口径一致且自包含。
    """
    w_hist = compute_macd(weekly["close"], 12, 26, 9)[2]
    wk = weekly[["date"]].copy()
    wk["wk_hist"] = w_hist
    wk["wk_hist_prev"] = w_hist.shift(1)
    wk["wk_J"] = compute_kdj(weekly, 9, 3, 3)
    wk["wk_J_prev"] = wk["wk_J"].shift(1)
    # 触发判定（与 toml 默认一致：armed_hist_below_zero & armed_hist_rising 均为 True）
    wk["wk_trigger"] = (wk["wk_hist"] < 0) & (wk["wk_hist"] > wk["wk_hist_prev"])
    wk = wk.rename(columns={"date": "wk_date"}).sort_values("wk_date")
    d = daily.sort_values("date").copy()
    d = pd.merge_asof(d, wk, left_on="date", right_on="wk_date", direction="backward")
    return d


# --------------------------------------------------------------------------- #
# 测试用例
# --------------------------------------------------------------------------- #
def test_source_file_parses_and_no_path_b_residue():
    """源码可解析，且不再残留任何路径B标识。"""
    src = STRATEGY_FILE.read_text(encoding="utf-8")
    tree = ast.parse(src)
    assert tree is not None
    for token in PATH_B_TOKENS:
        assert token not in src, f"源码仍残留路径B标识: {token!r}"


def test_toml_no_path_b_and_loads():
    """toml 配置无 path_b 段、有 path_a 段，且能正常加载并实例化策略（不抛 KeyError）。"""
    cfg = _load_cfg()
    assert "params" in cfg
    entry = cfg["params"]["entry"]
    assert "path_a" in entry, "toml 缺少 [params.entry.path_a] 段"
    assert "path_b" not in entry, "toml 仍含 [params.entry.path_b] 段（删除不彻底）"
    # 用真实配置实例化并确认类型正确（间接验证 params 键齐全）
    strat = KdjMacdDualEntry(cfg)
    assert strat.type == "kdj_macd_dual_entry"


def test_scenario_a_builds_base_position():
    """场景A：路径A特征存在时应正常建底仓，且建仓日满足路径A条件（落在触发周、当日J<j_buy）。"""
    cfg = _load_cfg()
    strat = KdjMacdDualEntry(cfg)
    daily, weekly = build_scenario_a()

    start = str(daily["date"].min().date())
    end = str(daily["date"].max().date())
    res = strat.run(daily.copy(), weekly.copy(), start, end, "TEST", "回归A")

    bases = _count_base(res["trade_history"])
    assert len(bases) >= 1, "路径A应能建底仓，但 trade_history 中无 role=='底仓' 成交（疑似路径A被破坏）"

    # 入口确实来自路径A：建仓日应落在某个「周线触发周」对齐的日线区间，且当日 J < j_buy(50)
    aligned = _align_weekly(daily, weekly)
    trigger_dates = set(
        pd.to_datetime(aligned.loc[aligned["wk_trigger"] == True, "date"]).dt.strftime("%Y-%m-%d")
    )
    entry_date = bases[0]["entry_date"]
    assert entry_date in trigger_dates, (
        f"底仓建仓日 {entry_date} 不在任何周线触发周对齐区间内，疑似非路径A入口"
    )
    # 日线 J 单独计算（与策略内部 compute_kdj 口径一致），按建仓日取值
    daily_J = compute_kdj(daily, 9, 3, 3)
    j_at_entry = float(daily_J[daily["date"] == pd.Timestamp(entry_date)].iloc[0])
    assert j_at_entry < 50, f"路径A要求建仓日 J<50，实际 J={j_at_entry}"


def test_scenario_b_no_base_position_path_b_removed():
    """场景B：仅具旧路径B特征、无路径A特征、日线J>=j_buy → 不应产生底仓（路径B已删除）。"""
    cfg = _load_cfg()
    strat = KdjMacdDualEntry(cfg)
    daily, weekly = build_scenario_b()

    start = str(daily["date"].min().date())
    end = str(weekly["date"].iloc[14].date())  # 排除末周 EMA 端点伪触发，窗口内仅含路径B特征

    # --- 数据有效性自检：确保本场景确实只含「路径B特征」、不含「路径A特征」 ---
    w_hist = compute_macd(weekly["close"], 12, 26, 9)[2]
    wk_J = compute_kdj(weekly, 9, 3, 3)
    wk_J_prev = wk_J.shift(1)
    wk_hist_prev = w_hist.shift(1)
    in_window = range(0, 15)  # wk0..wk14（eval 窗口内）
    path_b_weeks = [
        i for i in in_window
        if w_hist.iloc[i] < 0 and wk_J.iloc[i] < 30 and wk_J.iloc[i] > wk_J_prev.iloc[i]
    ]
    path_a_weeks = [
        i for i in in_window
        if w_hist.iloc[i] < 0 and w_hist.iloc[i] > wk_hist_prev.iloc[i]
    ]
    daily_J = compute_kdj(daily, 9, 3, 3)
    assert path_b_weeks, "测试数据构造失败：未出现旧路径B触发特征(hist<0 & 周J<30 & 周J上涨)"
    assert not path_a_weeks, "测试数据构造失败：eval 窗口内出现了路径A触发特征(hist<0 & hist上涨)"
    assert (daily_J >= 50).all(), "测试数据构造失败：日线 J 应始终 >= j_buy(50)"

    # --- 核心回归断言：路径B已删除 → 不应产生底仓 ---
    res = strat.run(daily.copy(), weekly.copy(), start, end, "TEST", "回归B")
    bases = _count_base(res["trade_history"])
    assert len(bases) == 0, (
        f"路径B已删除但仍在仅具路径B特征的区间产生底仓 {len(bases)} 笔，疑似路径B残留：{bases}"
    )


if __name__ == "__main__":
    # 无 pytest 时的兜底运行器
    import traceback

    tests = [
        ("test_source_file_parses_and_no_path_b_residue", test_source_file_parses_and_no_path_b_residue),
        ("test_toml_no_path_b_and_loads", test_toml_no_path_b_and_loads),
        ("test_scenario_a_builds_base_position", test_scenario_a_builds_base_position),
        ("test_scenario_b_no_base_position_path_b_removed", test_scenario_b_no_base_position_path_b_removed),
    ]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except Exception as e:  # noqa: BLE001
            failed.append(name)
            print(f"FAIL  {name}: {e}")
            traceback.print_exc()
    if failed:
        raise SystemExit(f"失败用例: {failed}")
    print("ALL PASS")
