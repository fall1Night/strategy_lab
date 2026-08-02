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
import hashlib
import json
import math
import os
import uuid
from typing import Any

from sqlalchemy import case, func, text
from sqlalchemy.orm import joinedload

from .db import SessionLocal, get_session, init_db
from .schema import BacktestRun, EquityPoint, PricePoint, Trade, Summary, Batch, BatchItem
from .serializers import db_row_to_result

# FR-41：数据时效阈值（天），与 FR-31 共用单一真相源，可配 STRATEGALAB_STALE_DAYS。
# 用于分析页标注「数据较旧」，引导用户先点『更新数据源』刷新行情再重跑。
STALE_DAYS: int = int(os.environ.get("STRATEGALAB_STALE_DAYS", "30"))


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


def compute_params_hash(strategy_cfg: dict[str, Any]) -> str:
    """对策略配置做规范化 sha1 指纹（40 位十六进制）。

    规范化：``sort_keys`` + 压缩空白 + ``ensure_ascii=False`` + ``default=str``，
    保证「参数完全一致」则 hash 一致，否则视为新 run（触发重跑）。
    """
    blob = json.dumps(
        strategy_cfg,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha1(blob).hexdigest()


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
def save_run(
    run_meta: dict[str, Any],
    equity_curve: list[dict[str, Any]],
    trade_history: list[dict[str, Any]],
    summary: dict[str, Any],
    positions: Any = None,
    price_curve: list[dict[str, Any]] | None = None,
) -> str:
    """保存一次完整回测结果，返回 run_id。失败向上抛（由调用方明确报错）。

    FR-15：用 ``bulk_insert_mappings`` 批量写入 equity / trades / price，比逐条
    ORM append 快 10-50 倍；权益点 / 成交明细 / 价格点量大时尤为明显。
    """
    init_db()
    run_id = str(uuid.uuid4())

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
        params_hash=run_meta.get("params_hash"),
        positions_json=run_meta.get("positions_json"),
        meta_json=run_meta.get("meta_json"),
        data_source=run_meta.get("data_source"),
    )

    equity_maps: list[dict[str, Any]] = []
    for pt in equity_curve or []:
        equity_maps.append(
            {
                "run_id": run_id,
                "date": _parse_date(pt.get("date")),
                "value": _safe_float(pt.get("value")) or 0.0,
            }
        )

    trade_maps: list[dict[str, Any]] = []
    for t in trade_history or []:
        trade_maps.append(
            {
                "run_id": run_id,
                "entry_date": _parse_date(t.get("entry_date")),
                "exit_date": _parse_date(t.get("exit_date")),
                "side": str(t.get("side") or "long"),
                "role": t.get("role"),
                "position_id": t.get("position_id"),
                "size": int(_safe_float(t.get("size")) or 0),
                "entry_price": _safe_float(t.get("entry_price")) or 0.0,
                "exit_price": _safe_float(t.get("exit_price")) or 0.0,
                "pnl": _safe_float(t.get("pnl")) or 0.0,
                "pnl_pct": _safe_float(t.get("pnl_pct")) or 0.0,
                "holding_bars": int(_safe_float(t.get("holding_bars")) or 0),
                "symbol": str(t.get("symbol") or run_meta.get("symbol") or ""),
                "symbol_name": t.get("symbol_name"),
                "display_symbol": t.get("display_symbol"),
                "label": t.get("label"),
            }
        )

    summ = summary or {}
    summary_obj = Summary(
        run_id=run_id,
        total_return_pct=_safe_float(summ.get("total_return_pct")),
        annual_return_pct=_safe_float(summ.get("annual_return_pct")),
        max_drawdown_pct=_safe_float(summ.get("max_drawdown_pct")),
        sharpe=_safe_float(summ.get("sharpe")),
        win_rate_pct=_safe_float(summ.get("win_rate_pct")) or 0.0,
        total_trades=int(_safe_float(summ.get("total_trades")) or 0),
        meta_json=run_meta.get("meta_json"),
    )

    price_maps: list[dict[str, Any]] = []
    for p in price_curve or []:
        price_maps.append(
            {
                "run_id": run_id,
                "date": _parse_date(p.get("date")),
                "open": _safe_float(p.get("open")) or 0.0,
                "high": _safe_float(p.get("high")) or 0.0,
                "low": _safe_float(p.get("low")) or 0.0,
                "close": _safe_float(p.get("close")) or 0.0,
                # 成交量允许 None（旧缓存无 vol 时为 NULL）
                "volume": _safe_float(p.get("volume")),
            }
        )

    with get_session() as s:
        s.add(run)
        s.flush()  # 先落 run，确保外键父行存在
        if equity_maps:
            s.bulk_insert_mappings(EquityPoint, equity_maps)
        if price_maps:
            s.bulk_insert_mappings(PricePoint, price_maps)
        if trade_maps:
            s.bulk_insert_mappings(Trade, trade_maps)
        s.add(summary_obj)
    return run_id


def get_run(run_id: str) -> dict[str, Any] | None:
    """按 run_id 取完整结果（含 equity/trades/summary/positions），不存在返回 None。"""
    init_db()
    with get_session() as s:
        run = (
            s.query(BacktestRun)
            .options(
                joinedload(BacktestRun.equity),
                joinedload(BacktestRun.price),
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
    params_hash: str | None = None,
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
        if params_hash:
            q = q.filter(BacktestRun.params_hash == params_hash)
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
                joinedload(BacktestRun.price),
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
        "params_hash": run.params_hash,
    }


# ---------------------------------------------------------------------------
# FR-13 老数据回填 params_hash
# ---------------------------------------------------------------------------
def backfill_params_hash() -> int:
    """遍历 params_json 非空且 params_hash 为空的 run，计算并回填。返回回填条数。"""
    init_db()
    n = 0
    with get_session() as s:
        rows = (
            s.query(BacktestRun.run_id, BacktestRun.params_json)
            .filter(
                BacktestRun.params_json.isnot(None),
                BacktestRun.params_json != "",
                (BacktestRun.params_hash.is_(None) | (BacktestRun.params_hash == "")),
            )
            .all()
        )
        for run_id, pj in rows:
            try:
                cfg = json.loads(pj)
            except (ValueError, TypeError):
                continue
            ph = compute_params_hash(cfg)
            s.query(BacktestRun).filter(BacktestRun.run_id == run_id).update(
                {"params_hash": ph}
            )
            n += 1
    return n


# ---------------------------------------------------------------------------
# FR-14 命中复用预查
# ---------------------------------------------------------------------------
def find_existing_runs(
    strategy_name: str,
    params_hash: str,
    symbols: list[str],
    data_source: str | None = None,
) -> dict[str, str]:
    """返回每个 symbol 对应最新已存在 run 的 run_id（按 created_at 取 MAX）。

    命中条件：``strategy_name + data_source + symbol + params_hash`` 一致
    （start 固定、end 不严格匹配，复用已有 run 的 end）。
    P0-4：运行复用键扩展含 data_source。
    """
    result: dict[str, str] = {}
    if not symbols:
        return result
    with get_session() as s:
        from sqlalchemy import and_

        filters = [
            BacktestRun.strategy_name == strategy_name,
            BacktestRun.params_hash == params_hash,
            BacktestRun.symbol.in_(symbols),
        ]
        if data_source:
            filters.append(BacktestRun.data_source == data_source)

        sub = (
            s.query(BacktestRun.symbol, func.max(BacktestRun.created_at).label("mc"))
            .filter(*filters)
            .group_by(BacktestRun.symbol)
            .subquery()
        )
        rows = (
            s.query(BacktestRun.symbol, BacktestRun.run_id)
            .join(
                sub,
                and_(
                    BacktestRun.symbol == sub.c.symbol,
                    BacktestRun.created_at == sub.c.mc,
                ),
            )
            .filter(BacktestRun.params_hash == params_hash)
            .all()
        )
        for sym, rid in rows:
            result[sym] = rid
    return result


# ---------------------------------------------------------------------------
# FR-42 回测清空重跑：按维度删除历史结果（限定 batch_type='backtest'）
# ---------------------------------------------------------------------------
def clear_strategy_runs(
    strategy_name: str,
    params_hash: str,
    data_source: str,
) -> int:
    """删除该维度下历史回测结果及关联明细，返回被删 run 数。

    FR-42：回测入口 ``submit_batch`` 在 ``create_batch`` 之前调用本函数，
    先清空该策略（``strategy_name + params_hash + data_source`` 全维度）此前产生的
    全部 ``backtest_runs`` 及关联 ``equity_points`` / ``trades`` / ``summary`` /
    ``batch_items``，再全量重跑所选范围。

    隔离原则：``batches`` / ``batch_items`` 为 data/backtest 共用表，删除 **限定
    ``batch_type='backtest'``**，绝不波及「更新数据源」写入的 data 批次与缓存 CSV。

    Args:
        strategy_name: 策略展示名。
        params_hash: 参数指纹。
        data_source: 数据源标识（如 ``eastmoney`` / ``akshare``）。

    Returns:
        被删 ``backtest_runs`` 行数。
    """
    with get_session() as s:
        runs = (
            s.query(BacktestRun.run_id)
            .filter(
                BacktestRun.strategy_name == strategy_name,
                BacktestRun.params_hash == params_hash,
                BacktestRun.data_source == data_source,
            )
            .all()
        )
        run_ids = [r.run_id for r in runs]
        if not run_ids:
            return 0
        # 顺序 DELETE 关联明细（避免外键约束 / 触发器问题）
        s.query(EquityPoint).filter(EquityPoint.run_id.in_(run_ids)).delete(
            synchronize_session=False
        )
        s.query(PricePoint).filter(PricePoint.run_id.in_(run_ids)).delete(
            synchronize_session=False
        )
        s.query(Trade).filter(Trade.run_id.in_(run_ids)).delete(
            synchronize_session=False
        )
        s.query(Summary).filter(Summary.run_id.in_(run_ids)).delete(
            synchronize_session=False
        )
        # batch_items：仅删命中 run 的（data 批次 run_id 为 NULL，天然不受影响）；
        # 额外限定 batch_type='backtest' 作双保险，避免误删 data 批次。
        s.query(BatchItem).filter(
            BatchItem.run_id.in_(run_ids),
            BatchItem.batch_id.in_(
                s.query(Batch.batch_id).filter(Batch.batch_type == "backtest")
            ),
        ).delete(synchronize_session=False)
        # 删除 backtest_runs 主行
        n = (
            s.query(BacktestRun)
            .filter(BacktestRun.run_id.in_(run_ids))
            .delete(synchronize_session=False)
        )
        return int(n)


# ---------------------------------------------------------------------------
# FR-16 批次 CRUD
# ---------------------------------------------------------------------------
def create_batch(
    batch_id: str,
    strategy_name: str,
    strategy_type: str,
    params_hash: str,
    scope_type: str,
    scope_value: str,
    total_count: int,
    skipped_count: int = 0,
    batch_type: str = "backtest",
) -> None:
    """写入一条 batches 记录（status='running'）。

    Args:
        batch_type: ``"backtest"``（回测批次）或 ``"data"``（数据源更新批次）。
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    with get_session() as s:
        s.add(
            Batch(
                batch_id=batch_id,
                strategy_name=strategy_name,
                strategy_type=strategy_type,
                params_hash=params_hash,
                scope_type=scope_type,
                scope_value=scope_value,
                batch_type=batch_type,
                total_count=int(total_count),
                done_count=0,
                failed_count=0,
                skipped_count=int(skipped_count),
                status="running",
                started_at=now,
                created_at=now,
            )
        )


def create_batch_id() -> str:
    """生成一个新的批次 ID（uuid4）。"""
    return str(uuid.uuid4())


def mark_pending_cancelled(batch_id: str) -> int:
    """取消时：把尚未开始的 pending item 标记为 cancelled。"""
    with get_session() as s:
        n = (
            s.query(BatchItem)
            .filter(BatchItem.batch_id == batch_id, BatchItem.status == "pending")
            .update({"status": "cancelled"}, synchronize_session=False)
        )
    return int(n)


def bulk_create_batch_items(items: list[dict[str, Any]]) -> None:
    """批量写入 batch_items（bulk_insert_mappings，快）。"""
    if not items:
        return
    with get_session() as s:
        s.bulk_insert_mappings(BatchItem, items)


def update_batch_item(
    batch_id: str,
    symbol: str,
    status: str | None = None,
    run_id: str | None = None,
    error_msg: str | None = None,
    started_at: datetime.datetime | None = None,
    finished_at: datetime.datetime | None = None,
) -> None:
    """更新某 batch_item 的执行状态（按 batch_id + symbol 唯一行）。"""
    values: dict[str, Any] = {}
    if status is not None:
        values["status"] = status
    if run_id is not None:
        values["run_id"] = run_id
    if error_msg is not None:
        values["error_msg"] = error_msg
    if started_at is not None:
        values["started_at"] = started_at
    if finished_at is not None:
        values["finished_at"] = finished_at
    if not values:
        return
    with get_session() as s:
        s.query(BatchItem).filter(
            BatchItem.batch_id == batch_id, BatchItem.symbol == symbol
        ).update(values)


def inc_batch_count(batch_id: str, column: str) -> None:
    """原子递增 batches 的计数（done_count / failed_count / skipped_count）。"""
    allowed = {"done_count", "failed_count", "skipped_count"}
    if column not in allowed:
        return
    with get_session() as s:
        s.execute(
            text(f"UPDATE batches SET `{column}`=`{column}`+1 WHERE batch_id=:b"),
            {"b": batch_id},
        )


def finalize_batch(batch_id: str, status: str) -> None:
    """批次收尾：写最终 status + finished_at。"""
    with get_session() as s:
        s.execute(
            text(
                "UPDATE batches SET `status`=:st, finished_at=:fa WHERE batch_id=:b"
            ),
            {
                "st": status,
                "fa": datetime.datetime.now(datetime.timezone.utc),
                "b": batch_id,
            },
        )


def get_batch(batch_id: str) -> dict[str, Any] | None:
    """按 batch_id 取批次元信息（dict），不存在返回 None。"""
    with get_session() as s:
        b = s.query(Batch).filter(Batch.batch_id == batch_id).first()
        if b is None:
            return None
        return _batch_to_dict(b)


def _batch_to_dict(b: Batch) -> dict[str, Any]:
    return {
        "batch_id": b.batch_id,
        "strategy_name": b.strategy_name,
        "strategy_type": b.strategy_type,
        "params_hash": b.params_hash,
        "scope_type": b.scope_type,
        "scope_value": b.scope_value,
        "batch_type": b.batch_type,
        "total_count": int(b.total_count),
        "done_count": int(b.done_count),
        "failed_count": int(b.failed_count),
        "skipped_count": int(b.skipped_count),
        "status": b.status,
        "started_at": b.started_at.isoformat() if b.started_at else None,
        "finished_at": b.finished_at.isoformat() if b.finished_at else None,
        "created_at": b.created_at.isoformat() if b.created_at else None,
        "error_msg": b.error_msg,
    }


def count_running_items(batch_id: str) -> int:
    """统计某批次下 status='running' 的 item 数。"""
    with get_session() as s:
        return (
            s.query(func.count(BatchItem.id))
            .filter(BatchItem.batch_id == batch_id, BatchItem.status == "running")
            .scalar()
            or 0
        )


def get_running_symbol(batch_id: str) -> str | None:
    """返回当前正在跑的 item 的标的信息（symbol_name 优先）。"""
    with get_session() as s:
        it = (
            s.query(BatchItem)
            .filter(BatchItem.batch_id == batch_id, BatchItem.status == "running")
            .order_by(BatchItem.id)
            .first()
        )
        if it is None:
            return None
        return it.symbol_name or it.symbol


def list_batches(limit: int = 50) -> list[dict[str, Any]]:
    """近期批次列表（按创建时间倒序）。"""
    init_db()
    with get_session() as s:
        rows = (
            s.query(Batch).order_by(Batch.created_at.desc()).limit(int(limit)).all()
        )
        return [_batch_to_dict(b) for b in rows]


def latest_batch_for_strategy(
    strategy_name: str,
    params_hash: str | None = None,
) -> dict[str, Any] | None:
    """返回某策略（按 ``strategy_name`` + ``params_hash``）最新的批次摘要。

    用于分析查询页「无数据」时的预热引导：若该批次仍在处理中、或刚结束
    但完成数为 0，则提示用户预热仍在进行，而不是冷冰冰的「没有跑过数据」。

    返回 ``{status, done_count, total_count, created_at}``；若无匹配批次或
    查询过程中出错，一律返回 ``None``（防御性兜底，绝不影响主查询返回）。
    """
    try:
        init_db()
        with get_session() as s:
            q = s.query(Batch).filter(Batch.strategy_name == strategy_name)
            if params_hash:
                q = q.filter(Batch.params_hash == params_hash)
            b = q.order_by(Batch.created_at.desc()).first()
            if b is None:
                return None
            return {
                "status": b.status,
                "done_count": int(b.done_count),
                "total_count": int(b.total_count),
                "created_at": b.created_at.isoformat() if b.created_at else None,
            }
    except Exception:  # noqa: BLE001
        # 防御性兜底：查询失败绝不影响主查询（rank）的返回
        return None


def mark_interrupted_batches() -> int:
    """启动扫描：把 status='running' 的批次标记为 'interrupted'（重启处理）。"""
    with get_session() as s:
        n = (
            s.query(Batch)
            .filter(Batch.status == "running")
            .update(
                {
                    "status": "interrupted",
                    "finished_at": datetime.datetime.now(datetime.timezone.utc),
                }
            )
        )
    return int(n)


def mark_interrupted_items() -> int:
    """把 interrupted 批次下的 pending/running item 标记为 cancelled。"""
    with get_session() as s:
        n = (
            s.query(BatchItem)
            .filter(
                BatchItem.batch_id.in_(
                    s.query(Batch.batch_id).filter(Batch.status == "interrupted")
                ),
                BatchItem.status.in_(["pending", "running"]),
            )
            .update({"status": "cancelled"}, synchronize_session=False)
        )
    return int(n)


# ---------------------------------------------------------------------------
# FR-20 排名查询 / FR-22 板块状态
# ---------------------------------------------------------------------------
_SECTORS_CACHE: "tuple[float | None, list[dict[str, Any]]] | None" = None
_SECTOR_STOCKS_CACHE: "tuple[float | None, dict[str, list[dict[str, Any]]]] | None" = None


def _load_sectors() -> list[dict[str, Any]]:
    """读取 31 申万一级行业（带 mtime 失效缓存）。"""
    global _SECTORS_CACHE
    from ...settings import get_data_dir

    p = get_data_dir() / "sectors.json"
    mtime = p.stat().st_mtime if p.exists() else None
    if _SECTORS_CACHE is not None and _SECTORS_CACHE[0] == mtime:
        return _SECTORS_CACHE[1]
    data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    _SECTORS_CACHE = (mtime, data)
    return data


def _load_sector_stocks() -> dict[str, list[dict[str, Any]]]:
    """读取 板块 code → 成分股列表 映射（带 mtime 失效缓存）。"""
    global _SECTOR_STOCKS_CACHE
    from ...settings import get_data_dir

    p = get_data_dir() / "sector_stocks.json"
    mtime = p.stat().st_mtime if p.exists() else None
    if _SECTOR_STOCKS_CACHE is not None and _SECTOR_STOCKS_CACHE[0] == mtime:
        return _SECTOR_STOCKS_CACHE[1]
    data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    _SECTOR_STOCKS_CACHE = (mtime, data)
    return data


def get_sectors() -> list[dict[str, Any]]:
    """返回 31 申万一级行业 [{code, name}, ...]（带缓存）。"""
    return _load_sectors()


def get_sector_stocks() -> dict[str, list[dict[str, Any]]]:
    """返回 板块 code → 成分股列表 的映射（带缓存）。"""
    return _load_sector_stocks()


def rank_runs(
    strategy_name: str,
    params_hash: str,
    scope: str | None = None,
    symbols: list[str] | None = None,
    page: int = 1,
    size: int = 50,
    sort_by: str | None = None,
    order: str = "desc",
) -> dict[str, Any]:
    """排名查询（分析页核心）：已落库 run 分页排序，默认按总收益率降序。

    - ``scope``：板块 code（按该板块聚合过滤）或 None/空（全部已跑过的）。
    - ``symbols``：自定义池 symbols（与 scope 互斥，优先于 scope）。
    - ``sort_by``：排序字段白名单 ``symbol_name`` / ``total_return_pct`` /
      ``max_drawdown_pct`` / ``sharpe`` / ``win_rate_pct`` / ``last_buy_date``，
      支持逗号分隔的多键组合排序（如 ``"last_buy_date,win_rate_pct"``，最多 3 个，
      从左到右优先级递减）；``order`` 同步为逗号分隔的方向。为 None、空或全非法时
      保持默认排序（total_return_pct 降序，向后兼容）。
    - ``order``：方向，逗号分隔且与 ``sort_by`` 平行（``"asc"`` 升序 /
      ``"desc"`` 降序）；字段值为 None 的统一排到最后。
    - 返回 ``{items, total, miss_count}``；``miss_count`` 仅当 scope 为板块 code 时有效。
    """
    init_db()
    page = max(1, int(page))
    size = max(1, min(500, int(size)))

    scope_is_sector = False
    if not symbols and scope:
        scope_is_sector = scope in _load_sector_stocks()

    with get_session() as s:
        q = s.query(BacktestRun).filter(
            BacktestRun.strategy_name == strategy_name,
            BacktestRun.params_hash == params_hash,
        )
        if symbols:
            q = q.filter(BacktestRun.symbol.in_(symbols))
        elif scope_is_sector:
            q = q.join(BatchItem, BatchItem.run_id == BacktestRun.run_id).filter(
                BatchItem.sector_code == scope
            )
        # 仅取有 summary 的 run，并按总收益率排序
        q = q.join(Summary, Summary.run_id == BacktestRun.run_id)

        # 全量查询（数据量不大，方便后续去重）
        all_rows = (
            q.order_by(Summary.total_return_pct.desc())
            .all()
        )

        # FR-43：按 run_id 聚合 MAX(trades.entry_date)（一条分组聚合，避免 N+1）。
        # trades 仅 A 股多头（side='long' 固定），无需 side 过滤；无 trades 的 run → None。
        run_ids = [r.run_id for r in all_rows]
        buy_map: dict[str, str | None] = {}
        if run_ids:
            sub = (
                s.query(Trade.run_id, func.max(Trade.entry_date).label("last_buy_date"))
                .filter(Trade.run_id.in_(run_ids))
                .group_by(Trade.run_id)
                .all()
            )
            buy_map = {rid: (d.isoformat() if d else None) for rid, d in sub}

        items: list[dict[str, Any]] = []
        today = datetime.date.today()
        for r in all_rows:
            summ = r.summary
            end_d = r.end
            stale = bool(end_d is not None and (today - end_d).days > STALE_DAYS)
            items.append(
                {
                    "run_id": r.run_id,
                    "symbol": r.symbol,
                    "symbol_name": r.symbol_name,
                    "total_return_pct": (
                        float(summ.total_return_pct)
                        if summ and summ.total_return_pct is not None
                        else None
                    ),
                    "max_drawdown_pct": (
                        float(summ.max_drawdown_pct)
                        if summ and summ.max_drawdown_pct is not None
                        else None
                    ),
                    "sharpe": (
                        float(summ.sharpe) if summ and summ.sharpe is not None else None
                    ),
                    "win_rate_pct": (
                        float(summ.win_rate_pct)
                        if summ and summ.win_rate_pct is not None
                        else None
                    ),
                    "last_buy_date": buy_map.get(r.run_id),  # FR-43：最近买入日期（ISO，无则为 None）
                    "end": r.end.isoformat() if r.end else None,
                    "stale": stale,
                    "_created_at": r.created_at,  # 去重辅助字段
                }
            )

        # 去重：每个 symbol 只保留最新创建的一条
        seen: dict[str, dict] = {}
        for item in items:
            sym = item["symbol"]
            prev = seen.get(sym)
            if prev is None or item["_created_at"] > prev["_created_at"]:
                seen[sym] = item
        items = list(seen.values())

        # 排序（在去重后的 items 上做，支持多键组合排序；None 统一排末尾）
        _ALLOWED_SORT = {
            "symbol_name",
            "total_return_pct",
            "max_drawdown_pct",
            "sharpe",
            "win_rate_pct",
            "last_buy_date",  # FR-43：最近买入日期（NULL 统一排末尾，沿用 FR-20 规则）
        }

        # 解析多键排序规则：sort_by 逗号分隔取前 3 个，order 平行逗号分隔（不足默认 desc）
        _keys = [k.strip() for k in (sort_by or "").split(",")][:3] if sort_by else []
        _dirs = [d.strip() for d in order.split(",")]
        _rules: list[tuple[str, bool]] = []
        for _i, _key in enumerate(_keys):
            if _key not in _ALLOWED_SORT:
                continue
            _d = _dirs[_i] if _i < len(_dirs) else "desc"
            _rules.append((_key, _d == "desc"))
        if not _rules:
            # 单键非法 / 空 → 默认总收益率降序（保持向后兼容）
            _rules = [("total_return_pct", True)]

        # 多键稳定排序：
        # 1) 先取出「主键缺失」的行，保留原始查询顺序，最后统一附加到末尾
        #    （保证"主键缺失=末尾且不被次级键重排"，符合 FR-20/FR-43 设计）；
        # 2) 对剩余行按规则「逆序」逐个稳定排序——最低优先级先排、最高优先级(主键)最后排，
        #    靠 Python 稳定排序保住高优先级键的相对顺序，次级键仅在"高优先级相等"时破平。
        _primary_key = _rules[0][0]
        _null_primary: list[dict[str, Any]] = [x for x in items if x.get(_primary_key) is None]
        _pool: list[dict[str, Any]] = [x for x in items if x.get(_primary_key) is not None]

        head: list[dict[str, Any]] = list(_pool)
        tail: list[dict[str, Any]] = []  # 仅承载「次级键缺失」的行
        for _key, _rev in reversed(_rules):
            body = [x for x in head if x.get(_key) is not None]
            nils = [x for x in head if x.get(_key) is None]
            body.sort(key=lambda x: x[_key], reverse=_rev)
            head = body
            tail = nils + tail
        items = head + tail + _null_primary

        # 清理辅助字段
        for item in items:
            item.pop("_created_at", None)

        total = len(items)
        offset = (page - 1) * size
        page_items = items[offset:offset + size]

        miss_count: int | None = None
        if scope_is_sector:
            sector_total = len(_load_sector_stocks().get(scope, []))
            done_skipped = (
                s.query(func.count(BatchItem.id))
                .join(Batch, Batch.batch_id == BatchItem.batch_id)
                .filter(
                    Batch.strategy_name == strategy_name,
                    Batch.params_hash == params_hash,
                    BatchItem.sector_code == scope,
                    BatchItem.status.in_(["done", "skipped"]),
                )
                .scalar()
                or 0
            )
            miss_count = max(0, sector_total - int(done_skipped))

    return {"items": page_items, "total": int(total), "miss_count": miss_count}


def sector_status(strategy_name: str, params_hash: str) -> list[dict[str, Any]]:
    """跨批次板块状态总览（策略×板块维度，纯聚合，不建物化表）。"""
    init_db()
    with get_session() as s:
        rows = (
            s.query(
                BatchItem.sector_code,
                func.count(BatchItem.id),
                func.sum(BatchItem.status == "done"),
                func.sum(BatchItem.status == "skipped"),
                func.sum(BatchItem.status == "failed"),
            )
            .join(Batch, Batch.batch_id == BatchItem.batch_id)
            .filter(
                Batch.strategy_name == strategy_name,
                Batch.params_hash == params_hash,
                BatchItem.sector_code.isnot(None),
            )
            .group_by(BatchItem.sector_code)
            .all()
        )
        agg = {
            r[0]: {
                "total": int(r[1] or 0),
                "done": int(r[2] or 0),
                "skipped": int(r[3] or 0),
                "failed": int(r[4] or 0),
            }
            for r in rows
        }

    result: list[dict[str, Any]] = []
    for sec in _load_sectors():
        code = sec["code"]
        a = agg.get(code)
        if not a or (a["done"] + a["skipped"]) == 0:
            status = "none"
        elif (a["done"] + a["skipped"]) >= a["total"]:
            status = "done"
        else:
            status = "partial"
        result.append(
            {
                "code": code,
                "name": sec["name"],
                "status": status,
                "done": a["done"] if a else 0,
                "skipped": a["skipped"] if a else 0,
                "failed": a["failed"] if a else 0,
                "total": a["total"] if a else 0,
            }
        )
    return result


def batch_sector_status(batch_id: str) -> list[dict[str, Any]]:
    """单批次板块完成总览（按 batch_id 聚合）。"""
    with get_session() as s:
        rows = (
            s.query(
                BatchItem.sector_code,
                func.count(BatchItem.id),
                func.sum(BatchItem.status == "done"),
                func.sum(BatchItem.status == "skipped"),
                func.sum(BatchItem.status == "failed"),
            )
            .filter(BatchItem.batch_id == batch_id, BatchItem.sector_code.isnot(None))
            .group_by(BatchItem.sector_code)
            .all()
        )
        agg = {
            r[0]: {
                "total": int(r[1] or 0),
                "done": int(r[2] or 0),
                "skipped": int(r[3] or 0),
                "failed": int(r[4] or 0),
            }
            for r in rows
        }
    result: list[dict[str, Any]] = []
    for sec in _load_sectors():
        code = sec["code"]
        a = agg.get(code)
        if not a or (a["done"] + a["skipped"]) == 0:
            status = "none"
        elif (a["done"] + a["skipped"]) >= a["total"]:
            status = "done"
        else:
            status = "partial"
        result.append(
            {
                "code": code,
                "name": sec["name"],
                "status": status,
                "done": a["done"] if a else 0,
                "skipped": a["skipped"] if a else 0,
                "failed": a["failed"] if a else 0,
                "total": a["total"] if a else 0,
            }
        )
    return result
