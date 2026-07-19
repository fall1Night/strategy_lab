# -*- coding: utf-8 -*-
"""Edward（QA）独立回归验证：海龟「查看详情」不再因缺键崩溃（KeyError: 'entry'）。

设计原则（与工程师脚本 qa_verify_turtle_detail.py 区分，独立视角）：
  - 不依赖 config 名称匹配来“间接”验证；直接断言 run['params']（=params_json）
    确实携带分发契约所需的 type/name，这是本次修复能否在真实 DB 路径生效的关键。
  - 优先使用工程师给定的确切 run_id 做交叉验证；若不存在再回退按名查询。
  - 健壮性用例额外覆盖「turtle 完全无 params 键」「仅靠 name 兜底命中」两条路径。
  - KDJ 回归额外校验关键默认数值（J<50 / 周J线<30 / 回落≥30 / J>80）未被改坏。
  - 对 render_dashboard.py 做 KDJ 专用键硬下标静态扫描。
只读真实库（仅 SELECT / 纯函数），不写数据、不重启服务、不触碰运行中进程。
"""
import os
import re
import sys

# 与运行服务一致的 MySQL（独立连接，只读）
os.environ.setdefault(
    "DATABASE_URL",
    "mysql+pymysql://root:root@localhost:3306/strategylab?charset=utf8mb4",
)

ROOT = r"E:\量化交易\strategy_lab"
sys.path.insert(0, os.path.join(ROOT, "src"))

from strategylab.engine import config
from strategylab.engine.dashboard import build_compare_from_runs, _note_texts
from strategylab.engine.storage import repository as repo

# 工程师离线验证用过的确切 run_id
TURTLE_RUN = "cd1ba01d-5c9f-4c3a-b666-0e2fcfc3e5d8"
KDJ_RUN = "a75b99b9-170a-44df-835c-1405f22eeac6"

results = []  # (label, passed, detail)


def check(label, cond, detail=""):
    results.append((label, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {label} {detail}")


# ---------------------------------------------------------------------------
# Part 0: _note_texts 健壮性（缺键 / 非通用结构 / 未知类型 绝不崩溃）
# ---------------------------------------------------------------------------
print("=" * 70)
print("Part 0: _note_texts 健壮性（缺键绝不崩溃）")
print("=" * 70)

# a) 空配置
try:
    n0, l0, d0 = _note_texts({})
    check("空配置不崩溃且返回非空文案", bool(n0) and bool(l0) and bool(d0), repr(n0[:24]))
except Exception as e:
    check("空配置不崩溃且返回非空文案", False, f"{type(e).__name__}: {e}")

# b) turtle 类型，params 存在但缺 [params.turtle] 子键
try:
    cfg = {"type": "turtle", "name": "海龟交易法则(唐奇安突破+ATR止损)",
           "params": {"initial_cash": 200000, "commission": 0.0003}}
    n, _, _ = _note_texts(cfg)
    check("turtle 缺[params.turtle]子键不崩溃且产出海龟文案",
          "唐奇安" in n and "海龟单位" in n, repr(n[:40]))
except Exception as e:
    check("turtle 缺[params.turtle]子键不崩溃", False, f"{type(e).__name__}: {e}")

# c) turtle 类型，完全无 params 键
try:
    cfg = {"type": "turtle", "name": "海龟交易法则(唐奇安突破+ATR止损)"}
    n, _, _ = _note_texts(cfg)
    check("turtle 完全无 params 键不崩溃且产出海龟文案",
          "唐奇安" in n and "海龟单位" in n, repr(n[:40]))
except Exception as e:
    check("turtle 完全无 params 键不崩溃", False, f"{type(e).__name__}: {e}")

# d) kdj 类型，缺 entry / t_trade
try:
    cfg = {"type": "kdj_macd_dual_entry", "name": "周线MACD + 日线KDJ 双入口做T",
           "params": {"initial_cash": 200000, "commission": 0.0003, "stamp_tax": 0.0005}}
    n, _, _ = _note_texts(cfg)
    check("kdj 缺 entry/t_trade 不崩溃", "建仓" in n, repr(n[:40]))
except Exception as e:
    check("kdj 缺 entry/t_trade 不崩溃", False, f"{type(e).__name__}: {e}")

# e) 未知类型 + description
try:
    cfg = {"type": "mystery", "name": "神秘策略", "description": "这是测试描述。", "params": {}}
    n, _, _ = _note_texts(cfg)
    check("未知类型+description 不崩溃", "测试描述" in n, repr(n[:40]))
except Exception as e:
    check("未知类型+description 不崩溃", False, f"{type(e).__name__}: {e}")

# f) 未知类型 + 无 description
try:
    cfg = {"type": "mystery", "name": "神秘策略2", "params": {}}
    n, _, _ = _note_texts(cfg)
    check("未知类型无 description 不崩溃", "神秘策略2" in n, repr(n[:40]))
except Exception as e:
    check("未知类型无 description 不崩溃", False, f"{type(e).__name__}: {e}")

# g) 仅靠 name 兜底命中（无 type）—— 验证 cfg.get('name') 兜底分支
try:
    cfg = {"name": "海龟交易法则(唐奇安突破+ATR止损)", "params": {"initial_cash": 200000}}
    n, _, _ = _note_texts(cfg)
    check("仅靠 name(无type) 命中海龟文案",
          "唐奇安" in n and "海龟单位" in n, repr(n[:40]))
except Exception as e:
    check("仅靠 name(无type) 命中海龟文案", False, f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# Part 1: 真实 DB 海龟 run -> build_compare_from_runs 产出海龟文案
# ---------------------------------------------------------------------------
print()
print("=" * 70)
print("Part 1: 真实 DB 海龟 run -> build_compare_from_runs")
print("=" * 70)

turtle_cfg = config.load_strategy_by_arg("turtle")
turtle_name = turtle_cfg.get("name", "")
print("turtle strategy_name =", repr(turtle_name))

# 优先使用工程师给定 run_id，否则按名回退
turtle_runs_by_id = repo.list_runs_by_ids([TURTLE_RUN])
if turtle_runs_by_id:
    turtle_run_id = TURTLE_RUN
    print("使用工程师指定 turtle run_id =", turtle_run_id)
else:
    t_runs = repo.list_runs(strategy=turtle_name, limit=5)
    turtle_run_id = t_runs[0]["run_id"] if t_runs else None
    print("按名回退 turtle run_id   =", turtle_run_id)

check("存在海龟 run", turtle_run_id is not None)
if turtle_run_id:
    run_full = repo.list_runs_by_ids([turtle_run_id])[0]
    rparams = run_full.get("params") or {}
    # 关键契约：分发依赖 cfg.get('type') / cfg.get('name')，二者必须来自 params_json
    has_type = "type" in rparams
    has_name = "name" in rparams
    check("run['params'] 含 type(分发契约)", has_type,
          f"keys_sample={list(rparams.keys())[:8]}")
    check("run['params'] 含 name(兜底契约)", has_name)

    # 直接对真实 cfg 调用 _note_texts：必须产出海龟文案（而非 generic）
    try:
        n_note, _, _ = _note_texts(rparams)
        is_turtle = ("唐奇安" in n_note) and ("海龟单位" in n_note) and ("ATR" in n_note)
        check("真实cfg经 _note_texts 产出海龟文案(非generic)", is_turtle, repr(n_note[:40]))
    except Exception as e:
        check("真实cfg经 _note_texts 产出海龟文案(非generic)", False, f"{type(e).__name__}: {e}")

    # 端到端：build_compare_from_runs（不传 out_path → 不落盘、不写库）
    try:
        rd = build_compare_from_runs([turtle_run_id])
        check("build_compare_from_runs 不抛异常", True)
    except Exception as e:
        rd = None
        check("build_compare_from_runs 不抛异常", False, f"{type(e).__name__}: {e}")

    if rd is not None:
        modules = rd.get("modules", [])
        note_mod = next((m for m in modules
                         if m.get("type") == "text" and m.get("title") == "策略实现要点"), None)
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
            check("内容含「唐奇安通道 shift(1) 无未来函数」",
                  "唐奇安" in text and "未来函数" in text)
        limit_mod = next((m for m in modules
                          if m.get("type") == "text" and m.get("title") == "已知局限与偏差"), None)
        disc_mod = next((m for m in modules
                         if m.get("type") == "text" and m.get("title") == "免责声明"), None)
        check("modules 含「已知局限与偏差」", limit_mod is not None)
        check("modules 含「免责声明」", disc_mod is not None)
        has_equity = any(m.get("type") == "line_chart" for m in modules)
        has_position = any(m.get("type") == "position_table" for m in modules)
        check("modules 含权益曲线(line_chart)", has_equity)
        check("modules 含持仓成交明细(position_table)", has_position)


# ---------------------------------------------------------------------------
# Part 2: 真实 DB KDJ run -> 文案保持不变（双入口/做T/backward merge + 默认数值）
# ---------------------------------------------------------------------------
print()
print("=" * 70)
print("Part 2: 真实 DB KDJ run -> 文案保持不变")
print("=" * 70)

kdj_cfg = config.load_strategy_by_arg("kdj_macd_dual_entry")
kdj_name = kdj_cfg.get("name", "")
print("kdj strategy_name =", repr(kdj_name))

kdj_runs_by_id = repo.list_runs_by_ids([KDJ_RUN])
if kdj_runs_by_id:
    kdj_run_id = KDJ_RUN
    print("使用工程师指定 kdj run_id =", kdj_run_id)
else:
    k_runs = repo.list_runs(strategy=kdj_name, limit=5)
    kdj_run_id = k_runs[0]["run_id"] if k_runs else None
    print("按名回退 kdj run_id   =", kdj_run_id)

check("存在 KDJ run", kdj_run_id is not None)
if kdj_run_id:
    run_full = repo.list_runs_by_ids([kdj_run_id])[0]
    rparams = run_full.get("params") or {}
    try:
        nk, _, _ = _note_texts(rparams)
        check("KDJ 真实cfg _note_texts 不崩溃", True)
        check("KDJ 文案保留「两条平行入口」", "两条平行入口" in nk)
        check("KDJ 文案保留「做T（仅看日线J线）」", "做T" in nk and "日线J线" in nk)
        check("KDJ 文案保留「backward merge」", "backward merge" in nk)
        # 关键默认数值回归不变
        check("KDJ 文案含 路径A 默认 J线<50", "J线<50" in nk or "J<50" in nk)
        check("KDJ 文案含 路径B 默认 周J线<30", "周J线<30" in nk)
        check("KDJ 文案含 做T 默认 回落≥30", "回落≥30" in nk)
        check("KDJ 文案含 默认 J>80 卖光加仓", "J>80" in nk)
        print("---- 策略实现要点（KDJ）预览 ----")
        print(nk)
        print("-------------------------------")
    except Exception as e:
        check("KDJ 真实cfg _note_texts 不崩溃", False, f"{type(e).__name__}: {e}")

    try:
        rd_k = build_compare_from_runs([kdj_run_id])
        check("KDJ build_compare_from_runs 不抛异常", True)
    except Exception as e:
        check("KDJ build_compare_from_runs 不抛异常", False, f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# Part 3: render_dashboard.py 写死 KDJ 专用键静态扫描（独立复核工程师结论）
# ---------------------------------------------------------------------------
print()
print("=" * 70)
print("Part 3: render_dashboard.py 写死 KDJ 专用键扫描")
print("=" * 70)

render_path = os.path.join(ROOT, "src", "strategylab", "engine", "vendor", "render_dashboard.py")
with open(render_path, encoding="utf-8") as f:
    rsrc = f.read()

# 1) 危险硬下标：params["entry"] / ["t_trade"] / ["t_buy_amount"] / ["path_a"] / ["path_b"]
hard = re.findall(r'\[["\'](?:entry|t_trade|t_buy_amount|path_a|path_b)["\']\]', rsrc)
check("render_dashboard.py 无 KDJ 专用键硬下标", len(hard) == 0, f"命中={hard}")

# 2) 裸 .get('entry')（非 entry_date/exit_date）亦不应出现
bare_entry = re.findall(r'\.get\(["\']entry["\']\)', rsrc)
check("render_dashboard.py 无 .get('entry') 裸键(仅允许 entry_date 等)",
      len(bare_entry) == 0, f"命中={bare_entry}")

# 3) 对 trade_history / positions 的访问应全部走 .get（不应出现 trade_history['...']）
hard_trade = re.findall(r'trade_history\[', rsrc)
check("render_dashboard.py 无 trade_history['...'] 硬下标", len(hard_trade) == 0,
      f"命中={hard_trade}")


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
print()
print("=" * 70)
passed = sum(1 for _, ok, _ in results if ok)
failed = len(results) - passed
print(f"Edward 独立验证: 总计 {len(results)} | PASS {passed} | FAIL {failed}")
print("=" * 70)
sys.exit(0 if failed == 0 else 1)
