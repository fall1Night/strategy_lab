# -*- coding: utf-8 -*-
"""仪表盘构建：单标的 / 多标的对比。

复用 skill 的 build_dashboard_data + render_dashboard（已支持 position_table 合并成交表）。
- 单标的：overview 一个 Tab，含权益图 + 指标表 + 合并成交明细 + 策略说明/局限/免责。
- 多标的：Tab1「策略对比」（双/多权益曲线 + KPI 对比表 + 说明），其余各标的独立 Tab。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from .vendor.render_dashboard import build_dashboard_data, render_dashboard, numberFormatPy


# --------------------------------------------------------------------------
# 策略说明 / 局限 文本（由配置生成，参数变更时同步更新）
# --------------------------------------------------------------------------
def _note_texts(cfg: dict[str, Any]) -> tuple[str, str, str]:
    p = cfg["params"]
    pa = p["entry"]["path_a"]
    pb = p["entry"]["path_b"]
    tt = p["t_trade"]
    comm = p["commission"] * 10000
    tax = p["stamp_tax"] * 10000
    t_amt_wan = p["t_buy_amount"] / 10000

    note = (
        f"- 建仓（两条平行入口，同一时间仅持有一笔底仓）：\n"
        f"  路径A：周线MACD柱(hist)<0进入监控区；当某一周hist较上周上涨（动能筑底转强）开始判定；"
        f"日线KDJ的J线<{pa.get('j_buy', 50)}当日收盘买入底仓；hist回到0轴上方自动解除监控。\n"
        f"  路径B：周线hist<0 且 周J线<{pb.get('j_below', 30)} 且 本周周J线较上周上涨 "
        f"→ 当日收盘直接买入底仓（不卡日线J）。\n"
        f"- 做T（仅看日线J线）：J较近5日高点回落≥{tt.get('drop_from_high', 30)}且仍在下降（J<前日J）当日收盘买{t_amt_wan}万；"
        f"J反弹（J≥前日J）停止加仓；J>{tt.get('j_sell_threshold', 80)}当日收盘卖光全部加仓部分，底仓不动。\n"
        f"- 清仓：日线MACD水上死叉（DIF>0且DEA>0且DIF下穿DEA）当日收盘清仓全部。\n"
        f"- 执行价：当日信号+当日收盘（含 look-ahead 偏差）；A股 T+1 / {p['lot_size']}股整手 / "
        f"佣金{comm:.0f}bp双边 / 印花税{tax:.0f}bp卖方 / 前复权qfq。\n"
        f"- 周线信号对齐采用 backward merge（最新 weekly_date ≤ daily_date），周内不使用未来数据。"
    )
    limit = (
        "- 当日收盘执行存在 look-ahead 偏差（实盘需次日开盘执行）。\n"
        "- 涨跌停未建模（±10%）：极端行情下当日收盘可能无法成交，本回测忽略。\n"
        "- 佣金按配置比例单边计，未设最低5元收费；印花税仅卖方。\n"
        "- 清仓信号当日强制平仓所有仓位（含当日T加仓），严格T+1下当日买入份额无法卖出，"
        "本回测为满足期末平仓要求按清仓价强制卖出，该场景极少出现。\n"
        "- 单标的回测，不含选股截面 / 存活偏差讨论。"
    )
    disclaimer = (
        "⚠️ 以上内容由 AI 基于公开信息整理生成，仅供参考，不构成任何投资建议或个股推荐。"
        "投资有风险，决策需谨慎。"
    )
    return note, limit, disclaimer


def _position_module(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "position_table",
        "tab": result["prefix"],
        "title": f"{result['name']}（{result['symbol']}）持仓成交明细（建仓→清仓合并）",
        "subtitle": "每一组合并一笔底仓交易，下方缩进子行列出该持仓期间的每笔做T收益；做T发生在底仓持有期内，不是新建仓位。",
        "positions": result["positions"],
    }


# --------------------------------------------------------------------------
# 单标的仪表盘
# --------------------------------------------------------------------------
def build_single_dashboard(result: dict[str, Any], cfg: dict[str, Any],
                            out_path: str | Path, start: str, end: str) -> Path:
    note, limit, disc = _note_texts(cfg)
    extra = [
        _position_module(result),
        {"type": "text", "tab": "overview", "title": "策略实现要点", "text": note},
        {"type": "text", "tab": "overview", "title": "已知局限与偏差", "text": limit},
        {"type": "text", "tab": "overview", "title": "免责声明", "text": disc},
    ]
    rd = build_dashboard_data(
        equity_curve=result.get("equity_curve"),
        trade_history=result.get("trade_history"),
        summary=result.get("summary"),
        meta={
            "initial_cash": float(cfg["params"]["initial_cash"]),
            "strategy_name": cfg.get("name", ""),
            "market": cfg.get("market", "china_a"),
        },
        language="zh",
        market=cfg.get("market", "china_a"),
        extra_modules=extra,
    )
    return render_dashboard(rd, output_path=out_path)


# --------------------------------------------------------------------------
# 多标的对比仪表盘
# --------------------------------------------------------------------------
def build_compare_dashboard(results: dict[str, dict[str, Any]], cfg: dict[str, Any],
                            out_path: str | Path, start: str, end: str) -> Path:
    note, limit, disc = _note_texts(cfg)
    init_cash = float(cfg["params"]["initial_cash"])

    # 各标的 report_data（仅取 modules，并改 tab 为该标的 prefix）
    sym_mods = {}
    i18n = None
    for prefix, r in results.items():
        pos_mod = _position_module(r)
        rd = build_dashboard_data(
            equity_curve=r["equity_curve"], trade_history=r["trade_history"],
            summary=r["summary"], meta={"initial_cash": init_cash, "strategy_name": cfg.get("name", "")},
            language="zh", market=cfg.get("market", "china_a"), extra_modules=[pos_mod],
        )
        for m in rd["modules"]:
            m["tab"] = prefix
        sym_mods[prefix] = rd["modules"]
        i18n = rd["ui"]["i18n"]

    # 对比 Tab：多权益曲线（优先内存 equity_curve，避免读 CSV）
    eq_points = {}
    for prefix, r in results.items():
        ec = r.get("equity_curve")
        if ec:
            eq_points[prefix] = [
                {"date": str(p["date"]), "value": float(p["value"])} for p in ec
            ]
        else:
            df = pd.read_csv(r["equity_csv"], parse_dates=["date"])
            eq_points[prefix] = [
                {"date": row["date"].strftime("%Y-%m-%d"), "value": float(row["value"])}
                for _, row in df.iterrows()
            ]

    line_module = {
        "type": "line_chart", "tab": "compare",
        "title": "权益曲线对比（初始资金均为 %.0f 万）" % (init_cash / 10000),
        "subtitle": f"同一策略 · 同一窗口 {start} ~ {end} · 单位 元",
        "series": [
            {"name": f"{results[p]['name']} {results[p]['symbol']}", "points": eq_points[p]}
            for p in results
        ],
    }

    def _fmt(v, suffix=""):
        return numberFormatPy(v, suffix=suffix) if v is not None else "--"

    def _row(metric, vals, suffix=""):
        return {"metric": metric, "values": [{"main": _fmt(v, suffix), "raw": v} for v in vals]}

    first = next(iter(results.values()))
    cols = ["指标"] + [f"{results[p]['name']} {results[p]['symbol']}" for p in results]
    rows = [
        _row("总收益率", [results[p]["summary"].get("total_return_pct") for p in results], suffix="%"),
        _row("年化收益率", [results[p]["summary"].get("annual_return_pct") for p in results], suffix="%"),
        _row("最大回撤", [results[p]["summary"].get("max_drawdown_pct") for p in results], suffix="%"),
        _row("夏普比率", [results[p]["summary"].get("sharpe") for p in results]),
        _row("胜率", [results[p]["summary"].get("win_rate_pct") for p in results], suffix="%"),
        _row("交易笔数", [results[p]["summary"].get("total_trades") for p in results]),
        _row("期末权益(元)", [round(init_cash * (1 + (results[p]["summary"].get("total_return_pct") or 0) / 100.0), 2)
                              for p in results]),
    ]
    metric_module = {
        "type": "metric_table", "tab": "compare",
        "title": "关键指标对比", "columns": cols, "rows": rows,
    }

    text_modules = [
        {"type": "text", "tab": "compare", "title": "策略实现要点", "text": note},
        {"type": "text", "tab": "compare", "title": "已知局限与偏差", "text": limit},
        {"type": "text", "tab": "compare", "title": "免责声明", "text": disc},
    ]

    modules = [line_module, metric_module] + text_modules
    for prefix in results:
        modules.extend(sym_mods[prefix])

    tabs = [{"id": "compare", "label": "策略对比"}]
    for prefix in results:
        tabs.append({"id": prefix, "label": results[prefix]["name"]})

    report_data = {
        "meta": {
            "strategy_name": cfg.get("name", "策略对比回测"),
            "market": cfg.get("market", "china_a"), "start": start, "end": end,
            "initial_cash": init_cash, "generated_at": pd.Timestamp.now().isoformat(),
        },
        "summary": {},
        "equity_curve": eq_points[first["prefix"]],
        "ui": {
            "title": " · ".join(sorted({v["name"] for v in results.values()})),
            "subtitle": f"{start}~{end} · 初始资金{init_cash/10000:.0f}万 · {cfg.get('name','')}",
            "active_tab": "compare", "tabs": tabs,
            "language": "zh", "i18n": i18n,
        },
        "modules": modules,
    }
    return render_dashboard(report_data, output_path=out_path)


# --------------------------------------------------------------------------
# 跨回测对比（数据来自数据库，不依赖文件）
# --------------------------------------------------------------------------
def build_compare_from_runs(
    run_ids: list[str],
    out_path: str | Path | None = None,
    start: str | None = None,
    end: str | None = None,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """从数据库加载多个 run，复用现有模块渲染「跨回测对比」仪表盘。

    - 多权益曲线：每个 run 的窗口起点 **rebased 到 100** 再叠加（解决跨初始资金 /
      区间不可比问题）。
    - 指标对比表：保留各 run **原始百分比**。
    - 各 run 独立 Tab：复用 ``build_dashboard_data``（权益图 / 成交表 / 持仓明细）。

    Args:
        run_ids: 要对比的 run_id 列表（至少 1 个）。
        out_path: 若提供则把仪表盘渲染为该路径的 HTML 文件。
        start/end: 可选，用于报告头展示（默认取所选 run 的起止并集）。
        cfg: 策略配置（用于生成策略说明）。不传则取第一个 run 的 params_json。

    Returns:
        与 ``build_dashboard_data`` 同构的 ``report_data`` dict（供 ``render_dashboard`` 使用）。
    """
    from .storage.repository import list_runs_by_ids
    from .storage.serializers import db_row_to_result

    runs = list_runs_by_ids(run_ids)
    if not runs:
        raise ValueError("没有可用的回测结果（run_ids 为空或均不存在于数据库）。")

    # 以 run_id 为唯一键，避免同标的/同策略碰撞
    results: dict[str, dict[str, Any]] = {r["run_id"]: r for r in runs}

    # 策略配置：优先用传入 cfg，否则取第一个 run 的完整配置快照
    if cfg is None:
        cfg = results[next(iter(results))].get("params") or {}

    note, limit_text, disc = _note_texts(cfg)

    # 各 run 独立 Tab（使用原始权益曲线，不 rebased）
    sym_mods: dict[str, list[dict[str, Any]]] = {}
    i18n: dict[str, Any] | None = None
    for rid, r in results.items():
        pos_mod = _position_module(r)
        rd = build_dashboard_data(
            equity_curve=r["equity_curve"],
            trade_history=r["trade_history"],
            summary=r["summary"],
            meta={
                "initial_cash": r.get("initial_cash"),
                "strategy_name": r.get("strategy_name", ""),
            },
            language="zh",
            market=cfg.get("market", "china_a"),
            extra_modules=[pos_mod],
        )
        for m in rd["modules"]:
            m["tab"] = rid
        sym_mods[rid] = rd["modules"]
        i18n = rd["ui"]["i18n"]

    # 对比 Tab：多权益曲线（rebased 到 100 再叠加）
    eq_points: dict[str, list[dict[str, Any]]] = {}
    for rid, r in results.items():
        ec = r.get("equity_curve") or []
        if ec:
            base = float(ec[0]["value"]) or 1.0
            eq_points[rid] = [
                {"date": str(p["date"]), "value": float(p["value"]) / base * 100.0}
                for p in ec
            ]
        else:
            eq_points[rid] = []

    line_module = {
        "type": "line_chart",
        "tab": "compare",
        "title": "权益曲线对比（各 run 起点 rebased 到 100）",
        "subtitle": "跨初始资金 / 区间归一化对比 · 基准 = 100",
        "series": [
            {
                "name": r.get("label", r["name"]),
                "points": eq_points[rid],
            }
            for rid, r in results.items()
        ],
    }

    # 指标对比表（原始百分比，期末权益按各 run 自身 initial_cash 计算）
    def _fmt(v: float | None, suffix: str = "") -> str:
        return numberFormatPy(v, suffix=suffix) if v is not None else "--"

    def _row(metric: str, vals: list, suffix: str = "") -> dict[str, Any]:
        return {
            "metric": metric,
            "values": [{"main": _fmt(v, suffix), "raw": v} for v in vals],
        }

    init_cashes = {rid: (r.get("initial_cash") or 0.0) for rid, r in results.items()}
    first = next(iter(results.values()))
    cols = ["指标"] + [
        r.get("label", r["name"]) for r in results.values()
    ]
    rows = [
        _row("总收益率", [r["summary"].get("total_return_pct") for r in results.values()], suffix="%"),
        _row("年化收益率", [r["summary"].get("annual_return_pct") for r in results.values()], suffix="%"),
        _row("最大回撤", [r["summary"].get("max_drawdown_pct") for r in results.values()], suffix="%"),
        _row("夏普比率", [r["summary"].get("sharpe") for r in results.values()]),
        _row("胜率", [r["summary"].get("win_rate_pct") for r in results.values()], suffix="%"),
        _row("交易笔数", [r["summary"].get("total_trades") for r in results.values()]),
        _row(
            "期末权益(元)",
            [
                round(init_cashes[rid] * (1 + (r["summary"].get("total_return_pct") or 0) / 100.0), 2)
                for rid, r in results.items()
            ],
        ),
    ]
    metric_module = {
        "type": "metric_table",
        "tab": "compare",
        "title": "关键指标对比",
        "columns": cols,
        "rows": rows,
    }

    text_modules = [
        {"type": "text", "tab": "compare", "title": "策略实现要点", "text": note},
        {"type": "text", "tab": "compare", "title": "已知局限与偏差", "text": limit_text},
        {"type": "text", "tab": "compare", "title": "免责声明", "text": disc},
    ]

    modules = [line_module, metric_module] + text_modules
    for rid in results:
        modules.extend(sym_mods[rid])

    overall_start = min(
        (r.get("start") for r in results.values() if r.get("start")), default=start or ""
    )
    overall_end = max(
        (r.get("end") for r in results.values() if r.get("end")), default=end or ""
    )
    tabs = [{"id": "compare", "label": "跨回测对比"}]
    for rid, r in results.items():
        tabs.append({"id": rid, "label": r.get("label", r["name"])})

    report_data = {
        "meta": {
            "strategy_name": cfg.get("name", "跨回测对比"),
            "market": cfg.get("market", "china_a"),
            "start": overall_start,
            "end": overall_end,
            "initial_cash": None,
            "generated_at": pd.Timestamp.now().isoformat(),
        },
        "summary": {},
        "equity_curve": (
            eq_points.get(first["run_id"]) or next(iter(eq_points.values()), [])
        ),
        "ui": {
            "title": " · ".join(sorted({r["name"] for r in results.values()})),
            "subtitle": f"跨回测对比 · {overall_start}~{overall_end}",
            "active_tab": "compare",
            "tabs": tabs,
            "language": "zh",
            "i18n": i18n,
        },
        "modules": modules,
    }

    if out_path:
        render_dashboard(report_data, output_path=out_path)
    return report_data
