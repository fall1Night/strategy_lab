# -*- coding: utf-8 -*-
"""批量扫描后台执行器（v2.0 核心闭环）。

职责：
  - ``submit_batch(...)``：生成 batch_id → 命中预查 → 写 batches/batch_items
    → 后台 ``ThreadPoolExecutor(max_workers=8)`` 并发跑 → 返回 (batch_id, hit_count)。
  - 每个任务 ``_run_one``：检查取消标志 → 置 running → 调 ``backtest.run_symbol``
    → 成功 done + run_id / 失败 failed + error_msg → 原子 SQL 递增 batches 计数。
  - ``cancel_batch``：内存 ``threading.Event`` 取消标志。
  - ``get_progress``：返回 batches 计数 + 当前 running 标的 + 状态 + ETA。
  - ``init_batch_runner``：进程启动时扫描 running 批次 → interrupted（重启处理）。

架构铁律：仅用 Python 标准库（concurrent.futures / threading），不引入 Redis/Celery。
"""
from __future__ import annotations

import datetime
import os
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .storage import repository
from .backtest import run_symbol
from .data_feed import normalize_symbol
from .datasource.config import DataSourceConfig
from .datasource.factory import DataSourceFactory

# 并发度（环境变量可配；IO 密集 8 路压满网络）
WORKERS = int(os.environ.get("STRATEGALAB_BATCH_WORKERS", "8"))

# 内存取消标志：batch_id -> threading.Event
_CANCEL_FLAGS: dict[str, threading.Event] = {}
_CANCEL_LOCK = threading.Lock()

# 后台线程池（按批次持有，便于收尾时 shutdown）
_EXECUTORS: dict[str, ThreadPoolExecutor] = {}
_EXEC_LOCK = threading.Lock()


def submit_batch(
    strategy_cfg: dict[str, Any],
    strategy_name: str,
    params_hash: str,
    scope_type: str,
    scope_value: str,
    out_dir: str,
    items: list[dict[str, Any]],
) -> tuple[str, int]:
    """提交一批批量回测。

    Args:
        strategy_cfg: 策略完整配置（dict）。
        strategy_name: 策略展示名（用于批次表与命中预查）。
        params_hash: 参数指纹（与策略+标的组成命中复用键）。
        scope_type: ``sector`` / ``pool`` / ``all_market``。
        scope_value: 板块 codes / 池 symbols / ``"ALL"``。
        out_dir: 行情缓存 / 落库目录。
        items: 元素 ``{"symbol", "symbol_name", "sector_code": Optional}``。

    Returns:
        ``(batch_id, hit_count)``：hit_count 为命中的 skipped 数量。
    """
    batch_id = repository.create_batch_id()
    # 归一化 symbol：run_symbol 内部会用 data_feed.normalize_symbol 把标的写成
    # "600216.SH" 格式落库（backtest_runs.symbol），命中复用必须以同一格式比对，
    # 否则 batch_items.symbol（如 "bj920000"）永远匹配不上 backtest_runs.symbol
    # （如 "920000.BJ"），导致命中复用/跳过全部失效。
    for it in items:
        it["symbol"] = normalize_symbol(it["symbol"])["symbol"]
    symbols = [it["symbol"] for it in items]

    # P0-4：计算每个标的的 effective source（用于命中复用键扩展 + 落库 data_source）
    ds_cfg = DataSourceConfig.from_env()
    ds_factory = DataSourceFactory(ds_cfg)
    symbol_source_map: dict[str, str] = {}
    for sym in symbols:
        symbol_source_map[sym] = ds_factory.get_effective_source(sym)
    # 用 majority source 查询复用（同批大多数标的同源）
    source_for_lookup = (
        max(set(symbol_source_map.values()), key=list(symbol_source_map.values()).count)
        if symbol_source_map
        else ds_cfg.default_source
    )

    # 命中预查：strategy_name + source + symbol + params_hash 一致的最新 run
    existing = repository.find_existing_runs(
        strategy_name, params_hash, symbols, data_source=source_for_lookup
    )
    hit_count = 0

    batch_items: list[dict[str, Any]] = []
    pending_items: list[dict[str, Any]] = []
    for it in items:
        sym = it["symbol"]
        if sym in existing:
            hit_count += 1
            batch_items.append(
                {
                    "batch_id": batch_id,
                    "symbol": sym,
                    "symbol_name": it.get("symbol_name") or sym,
                    "sector_code": it.get("sector_code"),
                    "status": "skipped",
                    "run_id": existing[sym],
                    "is_reused": True,
                }
            )
        else:
            batch_items.append(
                {
                    "batch_id": batch_id,
                    "symbol": sym,
                    "symbol_name": it.get("symbol_name") or sym,
                    "sector_code": it.get("sector_code"),
                    "status": "pending",
                    "run_id": None,
                    "is_reused": False,
                }
            )
            pending_items.append(it)

    # 写批次 + 批量写 items（skipped 计数在 create_batch 时一并写入）
    repository.create_batch(
        batch_id=batch_id,
        strategy_name=strategy_name,
        strategy_type=strategy_cfg.get("type", ""),
        params_hash=params_hash,
        scope_type=scope_type,
        scope_value=scope_value,
        total_count=len(items),
        skipped_count=hit_count,
    )
    repository.bulk_create_batch_items(batch_items)

    # 取消标志
    with _CANCEL_LOCK:
        _CANCEL_FLAGS[batch_id] = threading.Event()

    # 后台并发执行（仅 pending 项入队）
    ex = ThreadPoolExecutor(max_workers=WORKERS)
    with _EXEC_LOCK:
        _EXECUTORS[batch_id] = ex
    for it in pending_items:
        ex.submit(_run_one, batch_id, strategy_cfg, params_hash, out_dir, it)

    # 收尾线程：等所有任务完成后置最终状态
    threading.Thread(target=_watch, args=(batch_id, ex), daemon=True).start()

    return batch_id, hit_count


def _run_one(
    batch_id: str,
    strategy_cfg: dict[str, Any],
    params_hash: str,
    out_dir: str,
    item: dict[str, Any],
) -> None:
    """单只标的执行（后台线程）。命中复用已跳过，这里只跑未命中的。"""
    symbol = item["symbol"]
    symbol_name = item.get("symbol_name") or symbol

    ev = _CANCEL_FLAGS.get(batch_id)
    if ev is not None and ev.is_set():
        repository.update_batch_item(batch_id, symbol, status="cancelled")
        return

    repository.update_batch_item(
        batch_id,
        symbol,
        status="running",
        started_at=datetime.datetime.now(datetime.timezone.utc),
    )
    try:
        start = "2020-01-01"
        end = datetime.date.today().isoformat()
        # P0-4：计算该标的的 effective source 并传入 run_symbol 落库
        ds_cfg = DataSourceConfig.from_env()
        ds_factory = DataSourceFactory(ds_cfg)
        effective_source = ds_factory.get_effective_source(symbol)
        res = run_symbol(
            strategy_cfg, symbol, symbol_name, start, end, out_dir, batch_id=batch_id,
            data_source=effective_source,
        )
        repository.update_batch_item(
            batch_id,
            symbol,
            status="done",
            run_id=res.get("run_id"),
            finished_at=datetime.datetime.now(datetime.timezone.utc),
        )
        repository.inc_batch_count(batch_id, "done_count")
    except Exception as e:  # noqa: BLE001
        repository.update_batch_item(
            batch_id,
            symbol,
            status="failed",
            error_msg=str(e)[:2000],
            finished_at=datetime.datetime.now(datetime.timezone.utc),
        )
        repository.inc_batch_count(batch_id, "failed_count")


def _watch(batch_id: str, ex: ThreadPoolExecutor) -> None:
    """等所有任务完成，置批次最终状态，清理资源。"""
    ex.shutdown(wait=True)
    ev = _CANCEL_FLAGS.get(batch_id)
    cancelled = ev is not None and ev.is_set()
    repository.finalize_batch(batch_id, "cancelled" if cancelled else "done")
    with _EXEC_LOCK:
        _EXECUTORS.pop(batch_id, None)
    # 取消标志保留一小段时间供前端查询，随后清除
    _CANCEL_FLAGS.pop(batch_id, None)


def cancel_batch(batch_id: str) -> bool:
    """设置取消标志（内存 Event）。返回是否成功设置。"""
    with _CANCEL_LOCK:
        ev = _CANCEL_FLAGS.get(batch_id)
        if ev is None:
            ev = threading.Event()
            _CANCEL_FLAGS[batch_id] = ev
        ev.set()
    # 立即把尚未开始的 pending 项标记为 cancelled（running 项会跑完/被标志拦截）
    try:
        repository.mark_pending_cancelled(batch_id)
    except Exception:  # noqa: BLE001
        pass
    return True


def get_progress(batch_id: str) -> dict[str, Any]:
    """返回批次进度：计数 + 状态 + 当前 running 标的 + ETA。"""
    b = repository.get_batch(batch_id)
    if b is None:
        return {
            "batch_id": batch_id,
            "status": "not_found",
            "total": 0,
            "done": 0,
            "failed": 0,
            "skipped": 0,
            "running": 0,
            "current_symbol": None,
            "eta_seconds": None,
        }
    running = repository.count_running_items(batch_id)
    current = repository.get_running_symbol(batch_id)

    eta_seconds: float | None = None
    started = b.get("started_at")
    if b["status"] == "running" and started and b["done_count"] > 0:
        try:
            st = datetime.datetime.fromisoformat(started)
        except (ValueError, TypeError):
            st = None
        if st is not None:
            # SQLite/MySQL DATETIME 列回程会把时区信息剥离成 naive（存储层已知限制）。
            # 入库时统一用 datetime.timezone.utc 写入，故此处把 naive 当成 UTC 再统一为
            # aware 后再相减，避免 "can't subtract offset-naive and offset-aware" 的 TypeError。
            if st.tzinfo is None:
                st = st.replace(tzinfo=datetime.timezone.utc)
            now = datetime.datetime.now(datetime.timezone.utc)
            elapsed = (now - st).total_seconds()
            per = elapsed / b["done_count"]
            remaining = (
                b["total_count"]
                - b["done_count"]
                - b["failed_count"]
                - b["skipped_count"]
                - running
            )
            if remaining < 0:
                remaining = 0
            eta_seconds = per * remaining

    return {
        "batch_id": batch_id,
        "status": b["status"],
        "total": b["total_count"],
        "done": b["done_count"],
        "failed": b["failed_count"],
        "skipped": b["skipped_count"],
        "running": running,
        "current_symbol": current,
        "eta_seconds": eta_seconds,
    }


def init_batch_runner() -> None:
    """进程启动时调用：把残留的 running 批次标记为 interrupted（重启处理）。"""
    n_batches = repository.mark_interrupted_batches()
    n_items = repository.mark_interrupted_items()
    if n_batches:
        import logging

        logging.getLogger(__name__).info(
            "重启处理：%d 个 running 批次标记为 interrupted，%d 个 item 标记为 cancelled",
            n_batches,
            n_items,
        )
