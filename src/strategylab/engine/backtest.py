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
from .datasource.exceptions import DataMissingError
from .strategies import get_strategy_class


# ---------------------------------------------------------------------------
# 日志辅助
# ---------------------------------------------------------------------------
def _read_meta_json(meta_path: Path) -> dict[str, Any] | None:
    """读取缓存 meta JSON；不存在或损坏返回 None。"""
    try:
        if meta_path.exists():
            return json.loads(meta_path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        pass
    return None


def _fmt_date_ymd(yyyymmdd: str) -> str:
    """YYYYMMDD → YYYY.M.D（无前导零月/日，如 2020.1.1）。"""
    if not yyyymmdd or len(yyyymmdd) < 8:
        return yyyymmdd or "N/A"
    y = yyyymmdd[:4]
    m = str(int(yyyymmdd[4:6]))
    d = str(int(yyyymmdd[6:8]))
    return f"{y}.{m}.{d}"


def _log_data_status(
    symbol: str,
    daily: pd.DataFrame,
    weekly: pd.DataFrame,
    daily_csv: Path,
    weekly_csv: Path,
    fetched: bool,
    range_beg: str,
    range_end: str,
) -> None:
    """打印统一的增量取数状态日志。

    行数从 meta 文件读取（缓存实际条数）；日期区间用传入的回测所需范围
    （而非缓存全量范围），确保与"更新数据源"的 genesis 一致。
    """
    daily_meta_path = daily_csv.parent / (daily_csv.stem + "_meta.json")
    weekly_meta_path = weekly_csv.parent / (weekly_csv.stem + "_meta.json")
    daily_meta = _read_meta_json(daily_meta_path)
    weekly_meta = _read_meta_json(weekly_meta_path)

    # 日线：优先 meta.rows，否则 DataFrame 实际行数
    n_daily = daily_meta.get("rows") if daily_meta else None
    if n_daily is None:
        n_daily = len(daily)

    # 周线
    n_weekly = weekly_meta.get("rows") if weekly_meta else None
    if n_weekly is None:
        n_weekly = len(weekly)

    # 日期区间使用回测所需范围（YYYYMMDD → YYYY.M.D）
    beg_label = _fmt_date_ymd(range_beg)
    end_label = _fmt_date_ymd(range_end)

    fetch_label = "需要拉取" if fetched else "无需拉取"
    print(
        f"  [增量取数] {symbol}: "
        f"日线 {n_daily} 条 / 周线 {n_weekly} 条，"
        f"当前范围为{beg_label}-{end_label}，"
        f"{fetch_label}，正在本地计算"
    )


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

    # 取数区间：[GENESIS, end] 与更新数据源完全一致；日线/周线的 warmup 提前量
    # 仅在不早于 GENESIS 的范围内生效（钳住下限，避免前推到 2019 年导致与缓存不一致）。
    # verify 仅校验缓存是否覆盖回测所需区间；缓存末日落后于今天不触发重拉。
    # update 模式则取到 max(end, 今天)，确保补足后数据尽量新。
    GENESIS = "20200101"
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    today = pd.Timestamp.now().normalize()
    fetch_end = max(end_ts, today)
    daily_beg = max(
        GENESIS,
        (start_ts - pd.DateOffset(years=1)).strftime("%Y%m%d"),
    )
    weekly_beg = max(
        GENESIS,
        (start_ts - pd.DateOffset(years=1, months=6)).strftime("%Y%m%d"),
    )
    end_str = end_ts.strftime("%Y%m%d")
    fetch_end_str = fetch_end.strftime("%Y%m%d")
    fetched = False
    try:
        daily_csv, weekly_csv = data_feed.ensure_data(
            sym_cfg, out_dir,
            daily_beg=daily_beg, daily_end=end_str,
            weekly_beg=weekly_beg, weekly_end=end_str,
            mode="verify",
            required_beg=daily_beg,
            required_end=end_str,
        )
    except DataMissingError:
        fetched = True
        # 行情不足：自动补足（从 daily_beg/weekly_beg 起点双向补到今天）后再回测
        daily_csv, weekly_csv = data_feed.ensure_data(
            sym_cfg, out_dir,
            daily_beg=daily_beg, daily_end=fetch_end_str,
            weekly_beg=weekly_beg, weekly_end=fetch_end_str,
            mode="update",
        )
    daily = data_feed.load_bars(daily_csv)
    weekly = data_feed.load_bars(weekly_csv)

    # 构造回测窗口内的价格曲线（K 线 + 成交量），供明细页走势图与落库使用。
    # 仅取 [start, end] 窗口，保证与 equity_curve / trade_history 日期轴对齐。
    window_mask = (daily["date"] >= start_ts) & (daily["date"] <= end_ts)
    window_df = daily.loc[window_mask]
    price_curve: list[dict[str, Any]] = []
    has_vol = "vol" in daily.columns
    for _, row in window_df.iterrows():
        raw_vol = row.get("vol") if has_vol else None
        # NaN / None → None（旧缓存无成交量时副图显示占位）
        vol_val: float | None = None
        if raw_vol is not None and not (
            isinstance(raw_vol, float) and pd.isna(raw_vol)
        ):
            vol_val = float(raw_vol)
        dt = row["date"]
        date_str = dt.strftime("%Y-%m-%d") if hasattr(dt, "strftime") else str(dt)[:10]
        price_curve.append(
            {
                "date": date_str,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": vol_val,
            }
        )

    # 统一日志：该标的的缓存状态 & 是否需要联网取数
    _log_data_status(sym_cfg["symbol"], daily, weekly, daily_csv, weekly_csv,
                     fetched, daily_beg, end_str)

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
        run_meta, equity_curve, trade_history, summary, res.get("positions"),
        price_curve=price_curve,
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
        "price_curve": price_curve,
        "run_id": run_id,
    }
