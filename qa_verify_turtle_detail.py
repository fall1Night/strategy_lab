# -*- coding: utf-8 -*-
"""QA 离线验证：海龟策略「查看详情」不再因缺键崩溃（KeyError: 'entry'）。

仅做 SELECT / 纯函数调用，不修改任何数据，不触碰运行中进程（独立连接，
指向与运行服务相同的 MySQL 实例）。运行：
  python qa_verify_turtle_detail.py
"""
import os
import sys

# 与运行服务一致的 MySQL 连接（独立连接，只读）
os.environ.setdefault(
    "DATABASE_URL",
    "mysql+pymysql://root:root@localhost:3306/strategylab?charset=utf8mb4",
)

ROOT = r"E:\量化交易\strategy_lab"
sys.path.insert(0, os.path.join(ROOT, "src"))

from strategylab.engine import config
from strategylab.engine.dashboard import build_compare_from_runs, _note_texts
from strategylab.engine.storage import repository as repo

ok = True


def check(label, cond, detail=""):
    global ok
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {label} {detail}")
    if not cond:
        ok = False


# ---------------------------------------------------------------------------
# Part 0: 单元测试 _note_texts 健壮性（缺键绝不崩溃）
# ---------------------------------------------------------------------------
print("=" * 70)
print("Part 0: _note_texts 健壮性（缺键 / 非通用结构）")
print("=" * 70)

# a) 空配置
try:
    n0, l0, d0 = _note_texts({})
    check("空配置不崩溃", bool(n0) and bool(l0) and bool(d0), repr(n0[:30]))
except Exception as e:
    check("空配置不崩溃", False, f"{type(e).__name__}: {e}")

# b) turtle 类型但缺少 [params.turtle] 子键
try:
    cfg_turtle_partial = {
        "type": "turtle",
        "name": "海龟交易法则(唐奇安突破+ATR止损)",
        "params": {"initial_cash": 200000, "commission": 0.0003},
    }
    n1, _, _ = _note_texts(cfg_turtle_partial)
    check("turtle 缺子键不崩溃", "唐奇安" in n1 and "海龟单位" in n1, repr(n1[:40]))
except Exception as e:
    check("turtle 缺子键不崩溃", False, f"{type(e).__name__}: {e}")

# c) kdj 类型但缺 entry / t_trade（模拟畸形配置）
try:
    cfg_kdj_partial = {
        "type": "kdj_macd_dual_entry",
        "name": "周线MACD+日线KDJ 双入口做T",
        "params": {"initial_cash": 200000, "commission": 0.0003, "stamp_tax": 0.0005},
    }
    n2, _, _ = _note_texts(cfg_kdj_partial)
    check("kdj 缺 entry/t_trade 不崩溃", "建仓" in n2, repr(n2[:40]))
except Exception as e:
    check("kdj 缺 entry/t_trade 不崩溃", False, f"{type(e).__name__}: {e}")

# d) 完全未知类型 + description
try:
    cfg_unknown = {
        "type": "mystery",
        "name": "神秘策略",
        "description": "这是一个测试描述。",
        "params": {},
    }
    n3, _, _ = _note_texts(cfg_unknown)
    check("未知类型+description 不崩溃", "测试描述" in n3, repr(n3[:40]))
except Exception as e:
    check("未知类型+description 不崩溃", False, f"{type(e).__name__}: {e}")

# e) 完全未知类型 + 无 description
try:
    cfg_unknown2 = {"type": "mystery", "name": "神秘策略2", "params": {}}
    n4, _, _ = _note_texts(cfg_unknown2)
    check("未知类型无 description 不崩溃", "神秘策略2" in n4, repr(n4[:40]))
except Exception as e:
    check("未知类型无 description 不崩溃", False, f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# Part 1: 真实 DB — 海龟 run 详情构建不再抛 KeyError
# ---------------------------------------------------------------------------
print()
print("=" * 70)
print("Part 1: 真实 DB 海龟 run -> build_compare_from_runs")
print("=" * 70)

turtle_cfg = config.load_strategy_by_arg("turtle")
turtle_name = turtle_cfg.get("name", "")
print("turtle strategy_name =", repr(turtle_name))

turtle_runs = repo.list_runs(strategy=turtle_name, limit=1)
check("存在海龟 run", len(turtle_runs) > 0, f"count={len(turtle_runs)}")
if turtle_runs:
    turtle_run_id = turtle_runs[0]["run_id"]
    print("turtle run_id        =", turtle_run_id)
    try:
        rd = build_compare_from_runs([turtle_run_id])
        check("build_compare_from_runs 不抛异常", True)
    except Exception as e:
        rd = None
        check("build_compare_from_runs 不抛异常", False, f"{type(e).__name__}: {e}")

    if rd is not None:
        modules = rd.get("modules", [])
        note_mod = next(
            (m for m in modules if m.get("type") == "text" and m.get("title") == "策略实现要点"),
            None,
        )
        check("modules 含「策略实现要点」text 模块", note_mod is not None)
        if note_mod:
            text = note_mod.get("text", "")
            print("---- 策略实现要点（海龟）预览 ----")
            print(text)
            print("---------------------------------")
            check("内容含「唐奇安/突破」入场描述", "突破" in text or "唐奇安" in text)
            check("内容含「ATR 止损」离场描述", "ATR" in text and "止损" in text)
            check("内容含「海龟单位 N」仓位描述", "海龟单位" in text)
            check("内容含「T+1 / 100股整手」执行价描述", "T+1" in text and "100" in text)
        # 通用模块也应存在
        limit_mod = next(
            (m for m in modules if m.get("type") == "text" and m.get("title") == "已知局限与偏差"),
            None,
        )
        disc_mod = next(
            (m for m in modules if m.get("type") == "text" and m.get("title") == "免责声明"),
            None,
        )
        check("modules 含「已知局限与偏差」", limit_mod is not None)
        check("modules 含「免责声明」", disc_mod is not None)
        # 权益曲线 / 持仓明细模块
        has_equity = any(m.get("type") == "line_chart" for m in modules)
        has_position = any(m.get("type") == "position_table" for m in modules)
        check("modules 含权益曲线(line_chart)", has_equity)
        check("modules 含持仓成交明细(position_table)", has_position)


# ---------------------------------------------------------------------------
# Part 2: 真实 DB — KDJ 双入口 run 文案不被破坏
# ---------------------------------------------------------------------------
print()
print("=" * 70)
print("Part 2: 真实 DB KDJ run -> 文案保持不变")
print("=" * 70)

kdj_cfg = config.load_strategy_by_arg("kdj_macd_dual_entry")
kdj_name = kdj_cfg.get("name", "")
print("kdj strategy_name =", repr(kdj_name))

kdj_runs = repo.list_runs(strategy=kdj_name, limit=1)
check("存在 KDJ run", len(kdj_runs) > 0, f"count={len(kdj_runs)}")
if kdj_runs:
    kdj_run_id = kdj_runs[0]["run_id"]
    print("kdj run_id          =", kdj_run_id)
    try:
        rd_k = build_compare_from_runs([kdj_run_id])
        check("KDJ build_compare_from_runs 不抛异常", True)
    except Exception as e:
        rd_k = None
        check("KDJ build_compare_from_runs 不抛异常", False, f"{type(e).__name__}: {e}")

    if rd_k is not None:
        note_mod_k = next(
            (m for m in rd_k.get("modules", []) if m.get("type") == "text" and m.get("title") == "策略实现要点"),
            None,
        )
        check("KDJ 含「策略实现要点」模块", note_mod_k is not None)
        if note_mod_k:
            text_k = note_mod_k.get("text", "")
            print("---- 策略实现要点（KDJ）预览 ----")
            print(text_k)
            print("-------------------------------")
            check("KDJ 文案保留「建仓（两条平行入口」", "两条平行入口" in text_k)
            check("KDJ 文案保留「做T（仅看日线J线）」", "做T" in text_k and "日线J线" in text_k)
            check("KDJ 文案保留「周线信号对齐 backward merge」", "backward merge" in text_k)


print()
print("=" * 70)
print("总体:", "ALL PASS ✓" if ok else "HAS FAIL ✗")
print("=" * 70)
sys.exit(0 if ok else 1)
