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
from .strategies import get_strategy_class


# --------------------------------------------------------------------------
# 策略说明 / 局限 文本（由配置生成，参数变更时同步更新）
# --------------------------------------------------------------------------
def _note_texts(cfg: dict[str, Any]) -> tuple[str, str, str]:
    """生成「策略实现要点」文案，并复用通用的「已知局限与偏差」「免责声明」。

    分发规则（接口驱动，无 type 字符串硬编码）：
      - 依据 ``cfg.get("type")`` 取得策略类；若类实现了 ``describe(params)``
        则由策略类自述「实现要点」；否则回退到通用文案 ``_note_texts_generic``。
      - 任意取类 / 描述失败都不抛异常，统一回退 generic，保证展示层稳健。
    """
    params = cfg.get("params") or {}
    strategy_type = str(cfg.get("type") or "").strip().lower()

    # 通用：已知局限与偏差（对所有策略一致）
    limit = (
        "- 当日收盘执行存在 look-ahead 偏差（实盘需次日开盘执行）。\n"
        "- 涨跌停未建模（±10%）：极端行情下当日收盘可能无法成交，本回测忽略。\n"
        "- 佣金按配置比例单边计，未设最低5元收费；印花税仅卖方。\n"
        "- 单标的回测，不含选股截面 / 存活偏差讨论。"
    )
    disclaimer = (
        "⚠️ 以上内容由 AI 基于公开信息整理生成，仅供参考，不构成任何投资建议或个股推荐。"
        "投资有风险，决策需谨慎。"
    )

    # 接口驱动：优先用策略类自描述，失败（未知 type / 无 describe）回退 generic
    note = _note_texts_generic(cfg, params)
    try:
        cls = get_strategy_class(strategy_type)
        if hasattr(cls, "describe"):
            note = cls.describe(params)
    except KeyError:
        # get_strategy_class 对未知 type 抛 KeyError → 已用 generic 兜底
        pass

    return note, limit, disclaimer




def _note_texts_generic(cfg: dict[str, Any], params: dict[str, Any]) -> str:
    """未知 / 通用策略的兜底文案：绝不因缺键崩溃。"""
    name = (str(cfg.get("name") or "").strip()) or "本策略"
    desc = str(cfg.get("description") or "").strip()
    if desc:
        core = (
            f"- 策略：{name}\n"
            f"- 说明：{desc}\n"
        )
    else:
        core = (
            f"- 策略：{name}\n"
            "- 说明：未提供策略描述（description 为空），以通用规则回测；"
            "具体买卖逻辑与参数请参考策略源码。\n"
        )
    return (
        core
        + "- 执行价：当日信号 + 当日收盘（含 look-ahead 偏差）；A股 T+1 / 100股整手 / "
        "佣金双边 / 印花税卖方 / 前复权 qfq。"
    )


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
            "initial_cash": float(cfg.get("params", {}).get("initial_cash", 0) or 0),
            "strategy_name": cfg.get("name", ""),
            "market": cfg.get("market", "china_a"),
        },
        language="zh",
        market=cfg.get("market", "china_a"),
        extra_modules=extra,
        price_curve=result.get("price_curve"),
    )
    return render_dashboard(rd, output_path=out_path)


# --------------------------------------------------------------------------
# 多标的对比仪表盘
# --------------------------------------------------------------------------
def build_compare_dashboard(results: dict[str, dict[str, Any]], cfg: dict[str, Any],
                            out_path: str | Path, start: str, end: str) -> Path:
    note, limit, disc = _note_texts(cfg)
    init_cash = float(cfg.get("params", {}).get("initial_cash", 0) or 0)

    # 各标的 report_data（仅取 modules，并改 tab 为该标的 prefix）
    sym_mods = {}
    i18n = None
    for prefix, r in results.items():
        pos_mod = _position_module(r)
        rd = build_dashboard_data(
            equity_curve=r["equity_curve"], trade_history=r["trade_history"],
            summary=r["summary"], meta={"initial_cash": init_cash, "strategy_name": cfg.get("name", "")},
            language="zh", market=cfg.get("market", "china_a"), extra_modules=[pos_mod],
            price_curve=r.get("price_curve"),
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
            price_curve=r.get("price_curve"),
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
