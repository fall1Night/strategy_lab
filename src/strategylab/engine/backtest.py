# -*- coding: utf-8 -*-
"""单标的回测编排：取数 → 策略运行 → 内存校验/汇总 → 落库 → 返回结果。

结果不再写三件套文件：防御校验与 summary 计算交给
``vendor.export_results.export_results(write_files=False)`` 在内存完成，
随后通过 ``storage.repository.save_run`` 落库（DB 无关，连接串走 DATABASE_URL）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from . import data_feed
from .strategies import get_strategy_class


def run_symbol(strategy_cfg: dict[str, Any], symbol: str, name: str | None,
              start: str, end: str, out_dir: str | Path,
              batch_id: str | None = None,
              data_source: str | None = None) -> dict[str, Any]:
    """对单个标的跑回测，把结果落库（SQLAlchemy，DB 无关），返回结果 dict（含 run_id）。

    不再写三件套文件：防御校验与 summary 计算交给
    ``vendor.export_results.export_results(write_files=False)`` 在内存完成，
    随后通过 ``storage.repository.save_run`` 落库。DB 不可用时会向上抛错
    （由调用方明确报错，不再静默回退文件）。
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    sym_cfg = data_feed.normalize_symbol(symbol)
    if name:
        sym_cfg["symbol_name"] = name
    else:
        sym_cfg.setdefault("symbol_name", sym_cfg["symbol"])

    # 取数 warmup 区间：日线早 1 年、周线早约 1.5 年；结束取到 max(评估结束, 今天)
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    today = pd.Timestamp.now().normalize()
    fetch_end = max(end_ts, today)
    daily_beg = (start_ts - pd.DateOffset(years=1)).strftime("%Y%m%d")
    weekly_beg = (start_ts - pd.DateOffset(years=1, months=6)).strftime("%Y%m%d")
    daily_csv, weekly_csv = data_feed.ensure_data(
        sym_cfg, out_dir,
        daily_beg=daily_beg, daily_end=fetch_end.strftime("%Y%m%d"),
        weekly_beg=weekly_beg, weekly_end=fetch_end.strftime("%Y%m%d"),
    )
    daily = data_feed.load_bars(daily_csv)
    weekly = data_feed.load_bars(weekly_csv)

    cls = get_strategy_class(strategy_cfg["type"])
    strat = cls(strategy_cfg)
    res = strat.run(
        daily, weekly, start, end,
        symbol=sym_cfg["symbol"], symbol_name=sym_cfg["symbol_name"],
    )
    equity_curve = res["equity_curve"]
    trade_history = res["trade_history"]

    # 防御校验 + 内存 summary（不写文件，只存数据库）
    from .vendor.export_results import export_results
    from .storage import repository
    prefix = sym_cfg["prefix"]
    er = export_results(
        equity_curve=equity_curve,
        trade_history=trade_history,
        prefix=prefix,
        initial_cash=float(strategy_cfg["params"]["initial_cash"]),
        start=start, end=end,
        market=strategy_cfg.get("market", "china_a"),
        output_dir=out_dir,
        strategy_name=f"{sym_cfg['symbol_name']} {strategy_cfg.get('name','')}",
        symbol=sym_cfg["symbol"],
        is_flat_at_end=True,
        write_files=False,
    )
    summary = er.get("summary_data", {})
    meta = er.get("meta", {})

    # 落库（DB 不可用会向上抛错，由调用方明确报错，不再静默回退文件）
    params_json = json.dumps(strategy_cfg, ensure_ascii=False, default=str)
    from .storage import repository

    run_meta = {
        "batch_id": batch_id,
        "strategy_type": strategy_cfg.get("type", ""),
        "strategy_name": strategy_cfg.get("name", ""),
        "symbol": sym_cfg["symbol"],
        "symbol_name": sym_cfg["symbol_name"],
        "start": start,
        "end": end,
        "initial_cash": float(strategy_cfg["params"]["initial_cash"]),
        "params_json": params_json,
        "params_hash": repository.compute_params_hash(strategy_cfg),
        "positions_json": json.dumps(res.get("positions"), ensure_ascii=False, default=str),
        "meta_json": json.dumps(meta, ensure_ascii=False, default=str),
        "data_source": data_source,
    }
    run_id = repository.save_run(
        run_meta, equity_curve, trade_history, summary, res.get("positions")
    )

    return {
        "symbol": sym_cfg["symbol"],
        "name": sym_cfg["symbol_name"],
        "prefix": prefix,
        "equity_csv": None,
        "trades_csv": None,
        "summary_json": None,
        "summary": summary,
        "positions": res["positions"],
        "equity_curve": equity_curve,
        "trade_history": trade_history,
        "run_id": run_id,
    }
