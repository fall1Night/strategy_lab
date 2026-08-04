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
from pathlib import Path
from typing import Any

from .storage import repository
from .backtest import run_symbol
from .data_feed import normalize_symbol, ensure_data
from .datasource.config import DataSourceConfig
from .datasource.factory import DataSourceFactory
from .datasource.cache import KlineCache

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
        ``(batch_id, hit_count)``：FR-42 起不再命中跳过，hit_count 恒为 0（全量重跑）。
    """
    batch_id = repository.create_batch_id()
    # 归一化 symbol：run_symbol 内部会用 data_feed.normalize_symbol 把标的写成
    # "600216.SH" 格式落库（backtest_runs.symbol），清空维度必须以同一格式比对。
    for it in items:
        it["symbol"] = normalize_symbol(it["symbol"])["symbol"]
    symbols = [it["symbol"] for it in items]

    # 计算每个标的的 effective source（用于清空维度 + 落库 data_source）
    ds_cfg = DataSourceConfig.from_env()
    ds_factory = DataSourceFactory(ds_cfg)
    symbol_source_map: dict[str, str] = {}
    for sym in symbols:
        symbol_source_map[sym] = ds_factory.get_effective_source(sym)

    # FR-42：回测前置清空该策略维度历史结果（策略全维度，不限所选范围），再全量重跑。
    # 修复（2026-08-04）：实际落库的 data_source 是「缓存优先」源（可能因容灾切换
    # 与轮询源不同），只清 majority 会漏掉切换过源的标的旧结果 → 对**所有出现的源**
    # 逐一清空，保证无残留。
    for src in set(symbol_source_map.values()):
        repository.clear_strategy_runs(strategy_name, params_hash, src)

    # 全量重跑：所有标的均为 pending（不再命中复用）
    batch_items: list[dict[str, Any]] = []
    for it in items:
        sym = it["symbol"]
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

    # 写批次 + 批量写 items（FR-42 不再有 skipped）
    repository.create_batch(
        batch_id=batch_id,
        strategy_name=strategy_name,
        strategy_type=strategy_cfg.get("type", ""),
        params_hash=params_hash,
        scope_type=scope_type,
        scope_value=scope_value,
        total_count=len(items),
        skipped_count=0,
        batch_type="backtest",
    )
    repository.bulk_create_batch_items(batch_items)

    # 取消标志
    with _CANCEL_LOCK:
        _CANCEL_FLAGS[batch_id] = threading.Event()

    # 后台并发执行（全量所选范围）
    ex = ThreadPoolExecutor(max_workers=WORKERS)
    with _EXEC_LOCK:
        _EXECUTORS[batch_id] = ex
    for it in items:
        ex.submit(_run_one, batch_id, strategy_cfg, params_hash, out_dir, it)

    # 收尾线程：等所有任务完成后置最终状态
    threading.Thread(target=_watch, args=(batch_id, ex), daemon=True).start()

    return batch_id, 0


def submit_data_batch(
    scope_type: str,
    scope_value: str,
    out_dir: str,
    items: list[dict[str, Any]],
) -> tuple[str, int]:
    """提交一个 data-only 批次（仅更新数据源，不回测、不落库 backtest_runs）。

    FR-38：复用 batches/batch_items 全套机制（batch_type='data'），进度 / 取消 /
    板块总览 / 重启-interrupted 全部复用，不新建表；data 批次 ``batch_items.run_id``
    恒为 ``None``。

    Args:
        scope_type: ``sector`` / ``pool`` / ``all_market``。
        scope_value: 板块 codes / 池 symbols / ``"ALL"``。
        out_dir: 行情缓存目录。
        items: 元素 ``{"symbol", "symbol_name", "sector_code": Optional}``。

    Returns:
        ``(batch_id, hit_count)``：hit_count = 已是最新（无需取数）的标的数。
    """
    batch_id = repository.create_batch_id()
    for it in items:
        it["symbol"] = normalize_symbol(it["symbol"])["symbol"]

    # 预先判定「已是最新」数（不触发取数）：日线/周线增量窗口均为 None 即无需更新。
    latest_syms: set[str] = set()
    try:
        from .datasource.provider import get_provider

        prov = get_provider()
        out_dir_p = Path(out_dir)
        today = datetime.date.today().strftime("%Y%m%d")
        for it in items:
            sym = it["symbol"]
            prefix = normalize_symbol(sym)["prefix"]
            # 生效源 = 已有缓存源优先，与 provider.ensure_data 的「缓存优先、同股同源」
            # 逻辑保持一致；无缓存才按 active_sources 轮询。
            eff = prov._factory.get_effective_source(sym)
            existing = prov._cache.find_existing_source(
                out_dir_p, prefix, prov._config.active_sources
            )
            if existing is not None:
                eff = existing
            d_meta, *_ = prov._cache._read_any_meta(out_dir_p, prefix, eff, "daily")
            w_meta, *_ = prov._cache._read_any_meta(out_dir_p, prefix, eff, "weekly")
            d_need = prov._cache._incremental_window(d_meta, today, "20200101") is not None
            w_need = prov._cache._incremental_window(w_meta, today, "20200101") is not None
            if not d_need and not w_need:
                latest_syms.add(sym)
    except Exception:  # noqa: BLE001
        # 预判定失败不应阻断提交，仅视为「需要更新」
        latest_syms = set()

    hit_count = len(latest_syms)
    batch_items: list[dict[str, Any]] = []
    for it in items:
        is_latest = it["symbol"] in latest_syms
        batch_items.append(
            {
                "batch_id": batch_id,
                "symbol": it["symbol"],
                "symbol_name": it.get("symbol_name") or it["symbol"],
                "sector_code": it.get("sector_code"),
                "status": "skipped" if is_latest else "pending",
                "run_id": None,
                "is_reused": False,
                "error_msg": "已是最新" if is_latest else None,
            }
        )

    repository.create_batch(
        batch_id=batch_id,
        strategy_name="行情更新",
        strategy_type="data",
        params_hash="__data__",
        scope_type=scope_type,
        scope_value=scope_value,
        total_count=len(items),
        skipped_count=hit_count,
        batch_type="data",
    )
    repository.bulk_create_batch_items(batch_items)

    with _CANCEL_LOCK:
        _CANCEL_FLAGS[batch_id] = threading.Event()

    ex = ThreadPoolExecutor(max_workers=WORKERS)
    with _EXEC_LOCK:
        _EXECUTORS[batch_id] = ex
    for it in items:
        if it["symbol"] not in latest_syms:
            ex.submit(_run_one_data, batch_id, out_dir, it)
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
        # 计算该标的的 effective source 并传入 run_symbol 落库。
        # 注意与 provider.ensure_data 保持一致：**缓存优先、同股同源**——
        # 已有缓存的标的用缓存源（轮询源可能与之不同，落库字段必须反映实际取数源）。
        ds_cfg = DataSourceConfig.from_env()
        ds_factory = DataSourceFactory(ds_cfg)
        effective_source = ds_factory.get_effective_source(symbol)
        sym_cfg = normalize_symbol(symbol)
        existing = KlineCache().find_existing_source(
            Path(out_dir), sym_cfg["prefix"], ds_cfg.active_sources
        )
        if existing is not None:
            effective_source = existing
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


def _run_one_data(
    batch_id: str,
    out_dir: str,
    item: dict[str, Any],
) -> None:
    """数据源更新单只执行（后台线程）：增量取数并 merge 写回，不落库回测 run。

    FR-38：调用 ``ensure_data(mode='update')`` 仅刷新行情缓存 CSV；任何失败
    标记 batch_item 为 failed，但绝不触发回测逻辑。
    """
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
        sym_cfg = normalize_symbol(symbol)
        # FR-39：增量取数 + merge 写回，仅更新数据源
        ensure_data(sym_cfg, out_dir, mode="update")
        repository.update_batch_item(
            batch_id,
            symbol,
            status="done",
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
