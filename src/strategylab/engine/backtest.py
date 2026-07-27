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
) -> None:
    """打印统一的增量取数状态日志。

    从 meta 文件读取缓存行数与日期区间；meta 缺失时回退到 DataFrame。
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

    # 日期区间：优先用 meta.beg/meta.end，缺失则取首末日期的自然格式
    if daily_meta and daily_meta.get("beg") and daily_meta.get("end"):
        range_beg = _fmt_date_ymd(str(daily_meta["beg"]))
        range_end = _fmt_date_ymd(str(daily_meta["end"]))
    elif len(daily) > 0:
        range_beg = pd.Timestamp(daily["date"].iloc[0]).strftime("%Y.%-m.%-d")
        range_end = pd.Timestamp(daily["date"].iloc[-1]).strftime("%Y.%-m.%-d")
    else:
        range_beg = range_end = "N/A"

    fetch_label = "需要拉取" if fetched else "无需拉取"
    print(
        f"  [增量取数] {symbol}: "
        f"日线 {n_daily} 条 / 周线 {n_weekly} 条，"
        f"当前范围为{range_beg}-{range_end}，"
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

    # 取数 warmup 区间：日线早 1 年、周线早约 1.5 年；结束取到 max(评估结束, 今天)
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    today = pd.Timestamp.now().normalize()
    fetch_end = max(end_ts, today)
    daily_beg = (start_ts - pd.DateOffset(years=1)).strftime("%Y%m%d")
    weekly_beg = (start_ts - pd.DateOffset(years=1, months=6)).strftime("%Y%m%d")
    # FR-40 修复：回测先仅校验缓存覆盖（verify）；若行情不足（warmup 历史缺失或
    # 末日早于今天）则自动以 update 双向补（往前补历史 + 往后补到今天）后继续回测，
    # 而非仅提示手动更新。若 update 仍失败（如网络彻底不可用）则保持 DataMissingError
    # 上浮，由上层标记 failed —— 不改变既有"缺数即失败"的兜底语义。
    fetch_end_str = fetch_end.strftime("%Y%m%d")
    fetched = False
    try:
        daily_csv, weekly_csv = data_feed.ensure_data(
            sym_cfg, out_dir,
            daily_beg=daily_beg, daily_end=fetch_end_str,
            weekly_beg=weekly_beg, weekly_end=fetch_end_str,
            mode="verify",
            required_beg=daily_beg,
            required_end=fetch_end_str,
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

    # 统一日志：该标的的缓存状态 & 是否需要联网取数
    _log_data_status(sym_cfg["symbol"], daily, weekly, daily_csv, weekly_csv, fetched)

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
