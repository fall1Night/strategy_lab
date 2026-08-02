# -*- coding: utf-8 -*-
"""QA 独立验证 #1：数据契约核验（不依赖工程师测试）"""
import sys, os, json, tempfile
from datetime import date, timedelta

VENDOR = r"E:\量化交易\strategy_lab\src\strategylab\engine\vendor"
sys.path.insert(0, VENDOR)
sys.path.insert(0, os.path.dirname(VENDOR))  # 使 strategylab.engine.vendor 可导入

from render_dashboard import build_dashboard_data, _build_default_modules  # noqa: E402

PASS = []
FAIL = []

def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  [PASS] {name}")
    else:
        FAIL.append(name)
        print(f"  [FAIL] {name} {detail}")

def trading_days(start: date, n: int):
    """生成连续交易日（跳过周末）"""
    out = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out

def make_equity_curve(start: date, n: int):
    days = trading_days(start, n)
    return [{"date": d.isoformat(), "value": 1_000_000 + i * 137.5} for i, d in enumerate(days)]

def make_price_curve(start: date, n: int):
    days = trading_days(start, n)
    out = []
    price = 50.0
    for i, d in enumerate(days):
        o = price
        c = o + (1 if i % 3 else -1) * 0.8
        h = max(o, c) + 0.4
        l = min(o, c) - 0.4
        out.append({"date": d.isoformat(), "open": o, "high": h, "low": l, "close": c,
                    "volume": 1_000_000 + i * 1000})
        price = c
    return out

def make_trades():
    return [
        {"entry_date": "2023-03-01", "exit_date": "2023-03-10", "side": "buy",
         "size": 100, "entry_price": 50.0, "exit_price": 52.0, "pnl": 200.0,
         "pnl_pct": 4.0, "holding_bars": 7},
        {"entry_date": "2023-04-05", "exit_date": "2023-04-20", "side": "sell",
         "size": 100, "entry_price": 55.0, "exit_price": 53.0, "pnl": -200.0,
         "pnl_pct": -3.6, "holding_bars": 11},
    ]

# ---------------------------------------------------------------- 场景 A：策略 + price_curve
print("== 场景 A：strategy 报告 + 带 price_curve（equity 250 点 / price 180 根，部分重叠）==")
equity = make_equity_curve(date(2023, 1, 2), 250)
price = make_price_curve(date(2023, 2, 27), 180)
summary = {
    "total_return_pct": 18.5, "max_drawdown_pct": -6.2, "total_trades": 2,
    "win_rate_pct": 50.0, "sharpe": 1.35, "annual_return_pct": 22.0,
    "final_value": 1185000.0,
}
meta = {"strategy_name": "QA_Test_Strategy", "market": "china_a", "symbol": "600519.SH",
        "report_kind": "strategy"}

report = build_dashboard_data(
    equity_curve=equity,
    trade_history=make_trades(),
    summary=summary,
    meta=meta,
    language="zh",
    price_curve=price,
)
modules = report["modules"]
print(f"  modules count = {len(modules)}")

ov = [m for m in modules if m.get("type") == "overview_chart"]
pc = [m for m in modules if m.get("type") == "price_chart"]
check("A1. 存在 overview_chart 模块", len(ov) == 1, f"got {len(ov)}")
check("A2. 存在 price_chart 模块", len(pc) == 1, f"got {len(pc)}")
check("A3. overview_chart.zoom_group == 'price_equity'",
      len(ov) == 1 and ov[0].get("zoom_group") == "price_equity",
      f"got {ov[0].get('zoom_group') if ov else None}")
check("A4. price_chart.zoom_group == 'price_equity'",
      len(pc) == 1 and pc[0].get("zoom_group") == "price_equity",
      f"got {pc[0].get('zoom_group') if pc else None}")
ov_points = ov[0].get("points", []) if ov else []
check("A5. overview_chart.points 非空", len(ov_points) > 0, f"got {len(ov_points)}")
check("A6. overview_chart.points 每项都有 date 字段",
      len(ov_points) > 0 and all(isinstance(p.get("date"), str) and p["date"] for p in ov_points),
      f"missing: {[i for i,p in enumerate(ov_points) if not isinstance(p.get('date'), str) or not p['date']][:3]}")
ohlc = pc[0].get("ohlc", []) if pc else []
check("A7. price_chart.ohlc 非空", len(ohlc) > 0, f"got {len(ohlc)}")
check("A8. price_chart.ohlc 每项都有 date 字段",
      len(ohlc) > 0 and all(isinstance(d.get("date"), str) and d["date"] for d in ohlc),
      f"missing: {[i for i,d in enumerate(ohlc) if not isinstance(d.get('date'), str) or not d['date']][:3]}")

# 日期区间部分重叠断言
eq_dates = {p["date"] for p in ov_points}
pr_dates = {d["date"] for d in ohlc}
check("A9. equity 与 price 日期区间部分重叠（既有交集又有差异）",
      bool(eq_dates & pr_dates) and bool(eq_dates - pr_dates) and bool(pr_dates - eq_dates),
      f"intersect={len(eq_dates & pr_dates)} only_eq={len(eq_dates - pr_dates)} only_pr={len(pr_dates - eq_dates)}")
check("A10. equity 起点早于 price 起点（错开）",
      min(eq_dates) < min(pr_dates), f"{min(eq_dates)} vs {min(pr_dates)}")

# ---------------------------------------------------------------- 场景 B：事件研究（无 price_curve → 兼容）
print("== 场景 B：event_study（事件研究），不传 price_curve ==")
event_trades = make_trades()
for t in event_trades:
    t["label"] = "事件-01" if t is event_trades[0] else "事件-02"
event_summary = {
    "total_return_pct": 4.0, "avg_return_pct": 0.2, "median_return_pct": 0.1,
    "best_trade_pct": 4.0, "worst_trade_pct": -3.6, "total_trades": 2,
    "win_rate_pct": 50.0,
}
event_meta = {"strategy_name": "QA_Event", "market": "china_a",
              "report_kind": "event_study", "event_overview_mode": "timeline"}

report_b = build_dashboard_data(
    equity_curve=equity[:80],  # 事件研究也带权益曲线(timeline 模式)
    trade_history=event_trades,
    summary=event_summary,
    meta=event_meta,
    language="zh",
    price_curve=None,  # 事件研究场景不传 price_curve
)
mods_b = report_b["modules"]
pc_b = [m for m in mods_b if m.get("type") == "price_chart"]
ov_b = [m for m in mods_b if m.get("type") == "overview_chart"]
check("B1. 事件研究不产生 price_chart 模块", len(pc_b) == 0, f"got {len(pc_b)}")
check("B2. 事件研究 overview_chart 存在", len(ov_b) == 1, f"got {len(ov_b)}")
check("B3. 事件研究 overview_chart 不带 zoom_group（行为兼容）",
      len(ov_b) == 1 and ov_b[0].get("zoom_group") is None,
      f"got {ov_b[0].get('zoom_group') if ov_b else None}")

# 场景 B2：事件研究即使误传 price_curve 也不应生成 price_chart（防御）
report_b2 = build_dashboard_data(
    equity_curve=equity[:80],
    trade_history=event_trades,
    summary=event_summary,
    meta=dict(event_meta),
    language="zh",
    price_curve=price[:10],  # 误传
)
pc_b2 = [m for m in report_b2["modules"] if m.get("type") == "price_chart"]
check("B4. 事件研究误传 price_curve 也不生成 price_chart", len(pc_b2) == 0, f"got {len(pc_b2)}")

# ---------------------------------------------------------------- 场景 C：strategy 但无 price_curve（兼容旧行为）
print("== 场景 C：strategy 报告但不传 price_curve（无 K 线）==")
report_c = build_dashboard_data(
    equity_curve=equity,
    trade_history=make_trades(),
    summary=summary,
    meta=dict(meta),
    language="zh",
    price_curve=None,
)
pc_c = [m for m in report_c["modules"] if m.get("type") == "price_chart"]
ov_c = [m for m in report_c["modules"] if m.get("type") == "overview_chart"]
check("C1. 无 price_curve 不产生 price_chart", len(pc_c) == 0, f"got {len(pc_c)}")
check("C2. overview_chart 仍带 zoom_group（strategy 场景有 K 线潜在联动，保留字段）",
      len(ov_c) == 1 and ov_c[0].get("zoom_group") == "price_equity",
      f"got {ov_c[0].get('zoom_group') if ov_c else None}")

# 汇总
print()
print(f"SUMMARY: PASS={len(PASS)} FAIL={len(FAIL)}")
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
print("ALL CONTRACT CHECKS PASSED")
