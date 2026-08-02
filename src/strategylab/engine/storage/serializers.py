# -*- coding: utf-8 -*-
"""DB 行 → dashboard result dict（与 ``engine.backtest.run_symbol`` 返回同构）。

dashboard / web 拿到该 dict 后，无需关心数据来自文件还是数据库：
  - ``equity_curve`` / ``trade_history`` / ``positions`` / ``summary`` 直接填充
    内存字段；
  - ``equity_csv`` / ``trades_csv`` / ``summary_json`` 置 ``None``（不再依赖文件路径）。
  - 额外附带 ``run_id`` / ``start`` / ``end`` / ``initial_cash`` / ``params`` /
    ``meta`` / ``label`` 等字段，便于跨 run 对比与展示。
"""
from __future__ import annotations

import json
from typing import Any

from .schema import BacktestRun


def db_row_to_result(run: BacktestRun) -> dict[str, Any]:
    """把一条已加载关联（equity/trades/summary）的 BacktestRun 转成 result dict。"""
    summ = run.summary
    summary = {
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

    equity_curve = [
        {"date": p.date.isoformat(), "value": float(p.value)}
        for p in (run.equity or [])
    ]
    trade_history = [
        {
            "entry_date": t.entry_date.isoformat() if t.entry_date else None,
            "exit_date": t.exit_date.isoformat() if t.exit_date else None,
            "side": t.side,
            "role": t.role,
            "position_id": t.position_id,
            "size": int(t.size),
            "entry_price": float(t.entry_price),
            "exit_price": float(t.exit_price),
            "pnl": float(t.pnl),
            "pnl_pct": float(t.pnl_pct),
            "holding_bars": int(t.holding_bars),
            "symbol": t.symbol,
            "symbol_name": t.symbol_name,
            "display_symbol": t.display_symbol,
            "label": t.label,
        }
        for t in (run.trades or [])
    ]
    positions = json.loads(run.positions_json) if run.positions_json else []

    # 价格曲线（K 线）：按 date 排序；volume 为 None 表示旧缓存无成交量
    price_curve = [
        {
            "date": p.date.isoformat(),
            "open": float(p.open),
            "high": float(p.high),
            "low": float(p.low),
            "close": float(p.close),
            "volume": float(p.volume) if p.volume is not None else None,
        }
        for p in sorted(run.price or [], key=lambda x: x.date)
    ]

    start = run.start.isoformat() if run.start else None
    end = run.end.isoformat() if run.end else None
    initial_cash = float(run.initial_cash) if run.initial_cash is not None else None
    created_at = run.created_at.isoformat() if run.created_at else None
    meta = json.loads(run.meta_json) if run.meta_json else {}
    params = json.loads(run.params_json) if run.params_json else {}
    label = f"{run.symbol_name} {run.symbol} ({start}~{end})"

    return {
        "symbol": run.symbol,
        "name": run.symbol_name,
        "prefix": run.run_id,  # 作为 dashboard tab 的唯一键
        "equity_csv": None,
        "trades_csv": None,
        "summary_json": None,
        "summary": summary,
        "positions": positions,
        "equity_curve": equity_curve,
        "trade_history": trade_history,
        "price_curve": price_curve,
        # 附加字段（run_symbol 不提供，但对比 / 展示有用）
        "run_id": run.run_id,
        "start": start,
        "end": end,
        "initial_cash": initial_cash,
        "strategy_type": run.strategy_type,
        "strategy_name": run.strategy_name,
        "batch_id": run.batch_id,
        "created_at": created_at,
        "meta": meta,
        "params": params,
        "label": label,
    }
