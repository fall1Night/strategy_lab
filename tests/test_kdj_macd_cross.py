# -*- coding: utf-8 -*-
"""回归测试：Strategy Lab 4.0 新策略 ``kdj_macd_cross``（日线 KDJ 监控 + MACD 金叉买入）。

覆盖（按主理人最终口径断言）：
  1. 自动注册：``list_strategies`` 含 ``kdj_macd_cross``；``list_available_strategies`` 含别名。
  2. toml 加载：type / buy_amount(100000) / exit.drawdown_threshold(0.05) 正确。
  3. armed 前置：无 KDJ 金叉 → 永不 armed → 永不买入；有 KDJ 金叉武装但无 MACD 金叉 → 也不买入。
  4. MACD 金叉买入（A股整手）：生成买入回合，size % 100 == 0，size*entry_price ≈ buy_amount。
  5. MACD 死叉清仓（任意位置）：多头延续高位死叉 → 清仓且为盈利（死叉先于回撤，验证"任意位置死叉"）。
  6. 回撤 ≥5% 清仓：用隔离手段（关闭 macd_death_cross）显式验证回撤阈值公式，并验证 <5% 不触发。
  7. 再循环：买入→清仓→再 KDJ 金叉→再买入，≥2 个独立不重叠回合（自检不抛 RuntimeError）。
  8. 期末强平：持仓到序列末未触发清仓 → 末仓 label == "底仓(期末平仓)"。
  9. 字段结构：trade_history / positions / equity_curve 字段齐全。
  10. T+1 / 费用：买入当日不平仓（holding_bars >= 1）；pnl 含费用（公式复核一致）。

说明：KDJ(快) 必然早于 MACD(慢) 同向金叉，故"只有 MACD 金叉但无 KDJ 金叉"在真实价格中不可构造；
      第 3 点改为双向验证 armed 前置（无 KDJ 金叉 / 有 KDJ 无 MACD 金叉 均不买入），等价覆盖该门控。
      回撤分支因 OR 竞合（急跌通常先死叉）难在自然序列单独触发，第 6 点改用隔离配置直接验证公式。

运行（受管 venv）：
  C:\\Users\\Administrator\\.workbuddy\\binaries\\python\\envs\\default\\Scripts\\python.exe -m pytest tests/test_kdj_macd_cross.py -v
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

# 让 tests/ 能 import src/strategylab（与 conftest.py / 既有测试保持一致）
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np
import pandas as pd

from strategylab.engine.strategies import list_strategies, get_strategy_class
from strategylab.engine.config import load_strategy_by_arg, list_available_strategies
from strategylab.engine.indicators import compute_kdj, compute_macd
from strategylab.engine.strategies.kdj_macd_cross import KdjMacdCross

# 主理人已验证的默认配置
CFG = load_strategy_by_arg("kdj_macd_cross")
BUY_AMOUNT = float(CFG["params"]["buy_amount"])          # 100000
LOT_SIZE = int(CFG["params"]["lot_size"])                # 100
DRAWDOWN_THRESHOLD = float(CFG["params"]["exit"]["drawdown_threshold"])  # 0.05
COMMISSION = float(CFG["params"]["commission"])          # 0.0003
STAMP_TAX = float(CFG["params"]["stamp_tax"])            # 0.0005


# --------------------------------------------------------------------------- #
# 数据构造工具
# --------------------------------------------------------------------------- #
def make_daily(prices, start: str = "2023-01-02") -> pd.DataFrame:
    """由收盘价序列构造日线 DataFrame（date/open/high/low/close/volume）。"""
    n = len(prices)
    dates = pd.bdate_range(start, periods=n)
    return pd.DataFrame({
        "date": dates,
        "open": [float(p) for p in prices],
        "high": [p * 1.02 for p in prices],
        "low": [p * 0.97 for p in prices],
        "close": [float(p) for p in prices],
        "volume": [1000] * n,
    })


def make_weekly(daily: pd.DataFrame) -> pd.DataFrame:
    """周线（新策略未使用，但 run 签名需要；按约定重采样）。"""
    return daily.resample("W", on="date").last().reset_index(drop=True)


def run_strategy(cfg: dict, prices, symbol: str = "TEST", symbol_name: str = "测试") -> tuple:
    """构造日线 + 周线并运行策略，返回 (daily_df, result_dict)。"""
    df = make_daily(prices)
    wk = make_weekly(df)
    inst = KdjMacdCross(cfg)
    start = str(df["date"].min().date())
    end = str(df["date"].max().date())
    res = inst.run(df.copy(), wk.copy(), start, end, symbol=symbol, symbol_name=symbol_name)
    return df, res


def count_kdj_gold(df: pd.DataFrame) -> int:
    kdj = compute_kdj(df, 9, 3, 3, return_full=True)
    kd = kdj["K"] - kdj["D"]
    mask = (((kd.shift(1) <= 0) | (kd.shift(1).abs() < 1e-9)) & (kd > 0)).fillna(False)
    return int(mask.sum())


def count_macd_gold(df: pd.DataFrame) -> int:
    dif, dea, _ = compute_macd(df["close"], 12, 26, 9)
    md = dif - dea
    mask = ((md.shift(1) <= 0) & (md > 0)).fillna(False)
    return int(mask.sum())


def bases(res: dict) -> list:
    return [t for t in res["trade_history"] if t.get("role") == "底仓"]


def clears(res: dict) -> list:
    return [t for t in bases(res) if t.get("exit_date")]


# --------------------------------------------------------------------------- #
# 1. 自动注册
# --------------------------------------------------------------------------- #
def test_auto_registration():
    """策略已自动注册：list_strategies 含 type；list_available_strategies 含别名。"""
    assert "kdj_macd_cross" in list_strategies(), "kdj_macd_cross 未自动注册到注册表"
    aliases = [alias for _name, alias in list_available_strategies()]
    assert "kdj_macd_cross" in aliases, "list_available_strategies 未发现 kdj_macd_cross 别名"
    cls = get_strategy_class("kdj_macd_cross")
    assert cls.type == "kdj_macd_cross"
    assert cls is KdjMacdCross


# --------------------------------------------------------------------------- #
# 2. toml 加载
# --------------------------------------------------------------------------- #
def test_toml_loads_correctly():
    """load_strategy_by_arg('kdj_macd_cross') 的 type/buy_amount/drawdown 正确。"""
    cfg = load_strategy_by_arg("kdj_macd_cross")
    assert cfg["type"] == "kdj_macd_cross"
    assert cfg["params"]["buy_amount"] == 100000
    assert cfg["params"]["exit"]["drawdown_threshold"] == 0.05
    # 实例化验证字段齐全（间接确认 params 结构未破坏）
    inst = KdjMacdCross(cfg)
    assert inst.type == "kdj_macd_cross"


# --------------------------------------------------------------------------- #
# 3. armed 前置（双向验证）
# --------------------------------------------------------------------------- #
def test_no_kdj_gold_never_buys():
    """无 KDJ 金叉 → 永不 armed → 永不买入（验证 armed 前置）。"""
    # 单调下行：既无 KDJ 金叉也无 MACD 金叉，确定性不买入
    prices = list(np.linspace(20.0, 9.0, 50))
    df, res = run_strategy(CFG, prices)
    assert count_kdj_gold(df) == 0, "构造应为零 KDJ 金叉（armed 永不置位）"
    assert len(bases(res)) == 0, "无 KDJ 金叉武装时不应产生任何底仓"


def test_kdj_armed_but_no_macd_gold_never_buys():
    """有 KDJ 金叉武装、但全程无 MACD 金叉 → 仍不买入（验证 MACD 金叉为买入必要条件）。"""
    # 下行中的小反弹：KDJ 会金叉武装，但整体下行使 MACD 始终无金叉
    prices = [11.0] * 20 + list(np.linspace(11.0, 8.0, 40))
    df, res = run_strategy(CFG, prices)
    assert count_kdj_gold(df) >= 1, "构造应至少出现一次 KDJ 金叉（已 armed）"
    assert count_macd_gold(df) == 0, "构造应全程无 MACD 金叉"
    assert len(bases(res)) == 0, "已 armed 但无 MACD 金叉时不应买入"


# --------------------------------------------------------------------------- #
# 4. MACD 金叉买入（整手）
# --------------------------------------------------------------------------- #
def test_macd_gold_buys_whole_lot():
    """KDJ 金叉武装 + MACD 金叉 → 买入；size 为 100 股整手，金额≈buy_amount。"""
    prices = [10.0] * 15 + list(np.linspace(10.0, 11.0, 25))
    df, res = run_strategy(CFG, prices)
    bs = bases(res)
    assert len(bs) >= 1, "KDJ 金叉 + MACD 金叉 应产生买入回合"
    first = bs[0]
    assert first.get("entry_date"), "底仓应记录 entry_date（买入动作）"
    size = int(first["size"])
    # A股整手：size 必为 lot_size 的整数倍
    assert size % LOT_SIZE == 0, f"买入 size={size} 非 {LOT_SIZE} 股整手"
    assert size > 0
    # 金额约束：整手取整后向下，不超过 buy_amount，且误差不超过一手市值
    assert size * first["entry_price"] <= BUY_AMOUNT + 1e-6, "买入金额不应超过 buy_amount"
    slack = BUY_AMOUNT - size * first["entry_price"]
    assert slack <= LOT_SIZE * first["entry_price"] + 1.0, "整手取整误差应不超过一手市值"


# --------------------------------------------------------------------------- #
# 5. MACD 死叉清仓（任意位置，盈利位）
# --------------------------------------------------------------------------- #
def test_macd_death_cross_exits_at_profit():
    """持仓后多头延续、高位 MACD 死叉 → 清仓，且回撤幅度为盈利（死叉先于回撤，验证任意位置死叉）。"""
    prices = [10.0] * 15 + list(np.linspace(10.0, 14.0, 45)) + list(np.linspace(14.0, 13.5, 12))
    df, res = run_strategy(CFG, prices)
    cl = clears(res)
    assert len(cl) >= 1, "高位 MACD 死叉应触发清仓"
    rec = cl[0]
    assert rec.get("label") == "底仓(清仓)", "应标注为清仓（非期末平仓）"
    # 死叉在高位的盈利区触发：exit_price 高于 entry_price
    assert rec["exit_price"] > rec["entry_price"], "高位死叉清仓应在盈利位（exit>entry）"


# --------------------------------------------------------------------------- #
# 6. 回撤 ≥5% 清仓（隔离验证回撤阈值公式）
# --------------------------------------------------------------------------- #
def _cfg_no_death() -> dict:
    """隔离配置：关闭 MACD 死叉清仓，使只有回撤分支能清仓。"""
    c = copy.deepcopy(CFG)
    c["params"]["exit"]["macd_death_cross"] = False
    return c


def test_drawdown_threshold_triggers_clear():
    """隔离（关闭死叉）：持仓期间收盘价较买入价跌 ≥5% → 回撤清仓，且回撤幅度≥5%。"""
    prices = [10.0] * 20 + list(np.linspace(10.0, 11.0, 14)) + list(np.linspace(11.0, 9.4, 8))
    df, res = run_strategy(_cfg_no_death(), prices)
    cl = clears(res)
    assert len(cl) == 1, "回撤≥5% 应触发一次回撤清仓"
    rec = cl[0]
    assert rec.get("label") == "底仓(清仓)", "回撤触发应为清仓（非期末平仓）"
    realized_dd = (rec["entry_price"] - rec["exit_price"]) / rec["entry_price"]
    assert realized_dd >= DRAWDOWN_THRESHOLD - 1e-9, (
        f"回撤清仓幅度 {realized_dd:.4f} 应 ≥ 阈值 {DRAWDOWN_THRESHOLD}"
    )


def test_drawdown_below_threshold_does_not_clear():
    """隔离（关闭死叉）：回撤 <5% 后回升 → 不触发回撤清仓，持仓到期末平仓。"""
    prices = [10.0] * 20 + list(np.linspace(10.0, 11.0, 14)) + \
        list(np.linspace(11.0, 9.8, 6)) + list(np.linspace(9.8, 11.5, 8))
    df, res = run_strategy(_cfg_no_death(), prices)
    # 不应有"回撤清仓"记录（只有可能的期末平仓）
    dd_clears = [t for t in clears(res) if t.get("label") == "底仓(清仓)"]
    assert len(dd_clears) == 0, "回撤<5% 不应触发回撤清仓"
    bs = bases(res)
    assert len(bs) >= 1, "应已建仓"
    assert bs[-1].get("label") == "底仓(期末平仓)", "回撤不足时应持有到期末平仓"


# --------------------------------------------------------------------------- #
# 7. 再循环（≥2 个独立不重叠回合）
# --------------------------------------------------------------------------- #
def test_recycle_multiple_rounds():
    """买入→清仓→再 KDJ 金叉→再买入：≥2 个独立回合，底仓区间不重叠（自检不抛 RuntimeError）。"""
    prices = ([10.0] * 18 + list(np.linspace(10.0, 12.0, 18)) + list(np.linspace(12.0, 11.0, 8))
              + list(np.linspace(11.0, 13.0, 18)) + list(np.linspace(13.0, 12.0, 8)))
    df, res = run_strategy(CFG, prices)  # 若区间重叠会抛 RuntimeError
    cl = clears(res)
    assert len(cl) >= 2, f"应至少有 2 个独立回合，实际 {len(cl)}"
    entry_dates = [c["entry_date"] for c in cl]
    assert len(set(entry_dates)) == len(entry_dates), "各回合 entry_date 应互不相同"
    # 底仓区间不重叠：上一回合 exit_date <= 下一回合 entry_date
    ordered = sorted(cl, key=lambda c: c["entry_date"])
    for prev, nxt in zip(ordered, ordered[1:]):
        assert prev["exit_date"] <= nxt["entry_date"], (
            f"底仓区间重叠：{prev['entry_date']}~{prev['exit_date']} 与 "
            f"{nxt['entry_date']}~{nxt['exit_date']}"
        )


# --------------------------------------------------------------------------- #
# 8. 期末强平
# --------------------------------------------------------------------------- #
def test_end_of_series_force_close():
    """持仓到序列末未触发清仓 → 末仓 label == '底仓(期末平仓)'。"""
    prices = [10.0] * 15 + list(np.linspace(10.0, 20.0, 50))  # 单调上行，无回撤无死叉
    df, res = run_strategy(CFG, prices)
    bs = bases(res)
    assert len(bs) >= 1, "应已建仓"
    last = bs[-1]
    assert last.get("exit_date"), "持仓到末尾应被强制平仓（含 exit_date）"
    assert last.get("label") == "底仓(期末平仓)", "末尾未清仓应为期末平仓"


# --------------------------------------------------------------------------- #
# 9. 字段结构
# --------------------------------------------------------------------------- #
def test_record_field_structure():
    """trade_history 记录字段齐全；positions 为列表且含关键字段；equity_curve 非空。"""
    prices = [10.0] * 18 + list(np.linspace(10.0, 11.0, 12)) + list(np.linspace(11.0, 10.4, 8))
    df, res = run_strategy(CFG, prices)
    need = {
        "entry_date", "exit_date", "side", "size", "entry_price", "exit_price",
        "pnl", "pnl_pct", "holding_bars", "symbol", "symbol_name", "label", "role", "position_id",
    }
    assert res["trade_history"], "trade_history 不应为空"
    missing = need - set(res["trade_history"][0].keys())
    assert not missing, f"trade_history 记录缺字段: {missing}"

    assert isinstance(res["positions"], list) and len(res["positions"]) >= 1
    pos_need = {
        "position_id", "entry_date", "exit_date", "side", "base_size", "entry_price",
        "exit_price", "holding_bars", "base_pnl", "base_pnl_pct", "total_pnl", "total_pnl_pct",
    }
    pmissing = pos_need - set(res["positions"][0].keys())
    assert not pmissing, f"positions 记录缺字段: {pmissing}"

    assert res["equity_curve"] and len(res["equity_curve"]) > 0
    assert "date" in res["equity_curve"][0] and "value" in res["equity_curve"][0]


# --------------------------------------------------------------------------- #
# 10. T+1 / 费用
# --------------------------------------------------------------------------- #
def test_t1_and_fee_in_pnl():
    """T+1：买入当日不平仓（holding_bars >= 1）；费用计入 pnl（公式复核一致）。"""
    prices = [10.0] * 18 + list(np.linspace(10.0, 11.0, 12)) + list(np.linspace(11.0, 10.0, 8))
    df, res = run_strategy(CFG, prices)
    cl = clears(res)
    assert len(cl) >= 1, "应产生清仓回合以校验 T+1 与费用"
    for rec in cl:
        # T+1：买入与清仓不在同一根 bar
        assert rec["holding_bars"] >= 1, "T+1 要求持仓≥1根（买入当日不可平仓）"
        # 费用公式复核：策略用原始(未舍入)价格计算 pnl 后舍入到 2 位，记录中的
        # entry/exit_price 只保留 4 位；用记录价复算的误差来自 价格舍入×size，取合理容差。
        gross = rec["size"] * (rec["exit_price"] - rec["entry_price"])           # 无费毛收益
        fees = rec["size"] * (rec["entry_price"] * COMMISSION
                              + rec["exit_price"] * (COMMISSION + STAMP_TAX))     # 代扣费用
        expected_net = gross - fees
        assert abs(rec["pnl"] - round(expected_net, 2)) <= 1.5, (
            f"pnl={rec['pnl']} 与费用公式(毛收益-费用)={round(expected_net, 2)} 不一致（费用未计入？）"
        )
        # 方向校验：含费 pnl 必严格小于无费毛收益（费用确被扣除，非偶然相等）
        assert rec["pnl"] < gross + 1e-6, "含费 pnl 应小于无费毛收益（费用未计入？）"


if __name__ == "__main__":
    # 无 pytest 时的兜底运行器
    import traceback

    tests = [
        ("test_auto_registration", test_auto_registration),
        ("test_toml_loads_correctly", test_toml_loads_correctly),
        ("test_no_kdj_gold_never_buys", test_no_kdj_gold_never_buys),
        ("test_kdj_armed_but_no_macd_gold_never_buys", test_kdj_armed_but_no_macd_gold_never_buys),
        ("test_macd_gold_buys_whole_lot", test_macd_gold_buys_whole_lot),
        ("test_macd_death_cross_exits_at_profit", test_macd_death_cross_exits_at_profit),
        ("test_drawdown_threshold_triggers_clear", test_drawdown_threshold_triggers_clear),
        ("test_drawdown_below_threshold_does_not_clear", test_drawdown_below_threshold_does_not_clear),
        ("test_recycle_multiple_rounds", test_recycle_multiple_rounds),
        ("test_end_of_series_force_close", test_end_of_series_force_close),
        ("test_record_field_structure", test_record_field_structure),
        ("test_t1_and_fee_in_pnl", test_t1_and_fee_in_pnl),
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
