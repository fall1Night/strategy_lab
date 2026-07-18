# -*- coding: utf-8 -*-
"""回测结果 CRUD 仓储层（DB 无关）。

约定：
  - 所有方法使用「短生命周期 Session」：``with get_session() as s: ...``
  - 结果 dict 与 ``engine.backtest.run_symbol`` 返回「同构」，dashboard / web
    无需感知数据来自文件还是数据库。
  - 失败一律向上抛（调用方负责明确报错，不再静默回退）。

方法：
  - ``save_run(meta, equity, trades, summary, positions) -> run_id``
  - ``get_run(run_id) -> dict | None``（含 equity / trades / summary / positions）
  - ``list_runs(symbol, strategy, limit) -> list[dict]``（轻量摘要，无明细）
  - ``list_runs_by_ids(ids) -> list[dict]``（完整结果，保序去重）
  - ``delete_run(run_id) -> None``（cascade 删除关联数据）
"""
from __future__ import annotations

import datetime
import json
import math
import uuid
from typing import Any

from sqlalchemy.orm import joinedload

from .db import SessionLocal, get_session, init_db
from .schema import BacktestRun, EquityPoint, Trade, Summary
from .serializers import db_row_to_result


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _parse_date(value: Any) -> datetime.date | None:
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.date.fromisoformat(text[:10])
    except ValueError:
        return None


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(result) or math.isinf(result):
        return None
    return result


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
def save_run(
    run_meta: dict[str, Any],
    equity_curve: list[dict[str, Any]],
    trade_history: list[dict[str, Any]],
    summary: dict[str, Any],
    positions: Any = None,
) -> str:
    """保存一次完整回测结果，返回 run_id。失败向上抛（由调用方明确报错）。"""
    init_db()
    run_id = str(uuid.uuid4())

    with get_session() as s:
        run = BacktestRun(
            run_id=run_id,
            batch_id=run_meta.get("batch_id"),
            strategy_type=run_meta.get("strategy_type", ""),
            strategy_name=run_meta.get("strategy_name", ""),
            symbol=run_meta.get("symbol", ""),
            symbol_name=run_meta.get("symbol_name", ""),
            start=_parse_date(run_meta.get("start")),
            end=_parse_date(run_meta.get("end")),
            initial_cash=_safe_float(run_meta.get("initial_cash")) or 0.0,
            params_json=run_meta.get("params_json") or "{}",
            positions_json=run_meta.get("positions_json"),
            meta_json=run_meta.get("meta_json"),
        )

        for pt in equity_curve or []:
            run.equity.append(
                EquityPoint(
                    date=_parse_date(pt.get("date")),
                    value=_safe_float(pt.get("value")) or 0.0,
                )
            )

        for t in trade_history or []:
            run.trades.append(
                Trade(
                    entry_date=_parse_date(t.get("entry_date")),
                    exit_date=_parse_date(t.get("exit_date")),
                    side=str(t.get("side") or "long"),
                    role=t.get("role"),
                    position_id=t.get("position_id"),
                    size=int(_safe_float(t.get("size")) or 0),
                    entry_price=_safe_float(t.get("entry_price")) or 0.0,
                    exit_price=_safe_float(t.get("exit_price")) or 0.0,
                    pnl=_safe_float(t.get("pnl")) or 0.0,
                    pnl_pct=_safe_float(t.get("pnl_pct")) or 0.0,
                    holding_bars=int(_safe_float(t.get("holding_bars")) or 0),
                    symbol=str(t.get("symbol") or run_meta.get("symbol") or ""),
                    symbol_name=t.get("symbol_name"),
                    display_symbol=t.get("display_symbol"),
                    label=t.get("label"),
                )
            )

        summ = summary or {}
        run.summary = Summary(
            run_id=run_id,
            total_return_pct=_safe_float(summ.get("total_return_pct")),
            annual_return_pct=_safe_float(summ.get("annual_return_pct")),
            max_drawdown_pct=_safe_float(summ.get("max_drawdown_pct")),
            sharpe=_safe_float(summ.get("sharpe")),
            win_rate_pct=_safe_float(summ.get("win_rate_pct")) or 0.0,
            total_trades=int(_safe_float(summ.get("total_trades")) or 0),
            meta_json=run_meta.get("meta_json"),
        )

        s.add(run)
    return run_id


def get_run(run_id: str) -> dict[str, Any] | None:
    """按 run_id 取完整结果（含 equity/trades/summary/positions），不存在返回 None。"""
    init_db()
    with get_session() as s:
        run = (
            s.query(BacktestRun)
            .options(
                joinedload(BacktestRun.equity),
                joinedload(BacktestRun.trades),
                joinedload(BacktestRun.summary),
            )
            .filter(BacktestRun.run_id == run_id)
            .first()
        )
        if run is None:
            return None
        # 在 session 内完成「对象 → dict」转换，避免 detached 实例访问关系
        return db_row_to_result(run)


def list_runs(
    symbol: str | None = None,
    strategy: str | None = None,
    strategy_type: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """轻量摘要列表（不含 equity/trades 明细），用于历史页 / API。"""
    init_db()
    with get_session() as s:
        q = s.query(BacktestRun).options(joinedload(BacktestRun.summary))
        if symbol:
            q = q.filter(BacktestRun.symbol == symbol)
        if strategy:
            q = q.filter(BacktestRun.strategy_name == strategy)
        if strategy_type:
            q = q.filter(BacktestRun.strategy_type == strategy_type)
        q = q.order_by(BacktestRun.created_at.desc())
        if limit:
            q = q.limit(int(limit))
        return [_run_to_summary_dict(r) for r in q.all()]


def list_runs_by_ids(run_ids: list[str]) -> list[dict[str, Any]]:
    """按给定 run_ids（保持顺序、去重）返回完整结果列表，用于跨 run 对比。"""
    init_db()
    if not run_ids:
        return []
    uniq = list(dict.fromkeys(run_ids))  # 去重保序
    with get_session() as s:
        runs = (
            s.query(BacktestRun)
            .options(
                joinedload(BacktestRun.equity),
                joinedload(BacktestRun.trades),
                joinedload(BacktestRun.summary),
            )
            .filter(BacktestRun.run_id.in_(uniq))
            .all()
        )
        by_id = {r.run_id: r for r in runs}
        return [db_row_to_result(by_id[rid]) for rid in uniq if rid in by_id]


def delete_run(run_id: str) -> None:
    """删除一次回测及其关联数据（cascade）。"""
    init_db()
    with get_session() as s:
        run = s.query(BacktestRun).filter(BacktestRun.run_id == run_id).first()
        if run is not None:
            s.delete(run)


def _run_to_summary_dict(run: BacktestRun) -> dict[str, Any]:
    """把一行 BacktestRun（+Summary）转成轻量摘要 dict。"""
    summ = run.summary
    return {
        "run_id": run.run_id,
        "batch_id": run.batch_id,
        "strategy_type": run.strategy_type,
        "strategy_name": run.strategy_name,
        "symbol": run.symbol,
        "symbol_name": run.symbol_name,
        "start": run.start.isoformat() if run.start else None,
        "end": run.end.isoformat() if run.end else None,
        "initial_cash": float(run.initial_cash) if run.initial_cash is not None else None,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "total_return_pct": (
            float(summ.total_return_pct)
            if summ and summ.total_return_pct is not None
            else None
        ),
        "annual_return_pct": (
            float(summ.annual_return_pct)
            if summ and summ.annual_return_pct is not None
            else None
        ),
        "max_drawdown_pct": (
            float(summ.max_drawdown_pct)
            if summ and summ.max_drawdown_pct is not None
            else None
        ),
        "sharpe": float(summ.sharpe) if summ and summ.sharpe is not None else None,
        "win_rate_pct": (
            float(summ.win_rate_pct)
            if summ and summ.win_rate_pct is not None
            else None
        ),
        "total_trades": int(summ.total_trades) if summ else 0,
    }
