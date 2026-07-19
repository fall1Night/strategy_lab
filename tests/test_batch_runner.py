# -*- coding: utf-8 -*-
"""FR-16/17/25 测试：批量执行器 submit_batch / 命中复用 / cancel / 重启 interrupted。

E. 批量执行（FR-16/17）：
   - submit_batch（scope_type='pool'，3~5 标的，部分命中复用、其余 pending）；
     断言 batch_id、total_count、命中项 skipped+is_reused+run_id、pending 项 done+run_id、
     get_progress 计数合理。
   - cancel_batch：提交后立刻取消，未跑完项转 cancelled（用可暂停执行器消除竞态）。
   - 重启处理：running 批次 → interrupted，其 pending/running item → cancelled。
"""
from __future__ import annotations

import datetime
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from strategylab.engine import batch_runner
from strategylab.engine.storage import db, repository
from strategylab.engine.storage.schema import BatchItem

from qa_helpers import equity_curve, run_meta, summary, temp_db, trades


# ---------------------------------------------------------------------------
# 可暂停执行器（消除并发竞态，使 cancel 测试确定性）
# ---------------------------------------------------------------------------
_pause = {"value": False}
_captured: dict = {}


class PausableExecutor:
    def __init__(self, max_workers):
        self._ex = ThreadPoolExecutor(max_workers=max_workers)
        self._go = threading.Event()
        if not _pause["value"]:
            self._go.set()

    def submit(self, fn, *args, **kwargs):
        if self._go.is_set():
            return self._ex.submit(fn, *args, **kwargs)
        return self._ex.submit(self._wrap, fn, args, kwargs)

    def _wrap(self, fn, args, kwargs):
        self._go.wait()
        return fn(*args, **kwargs)

    def shutdown(self, wait=True):
        self._go.set()
        return self._ex.shutdown(wait=wait)


def _make_executor(max_workers):
    ex = PausableExecutor(max_workers)
    _captured["ex"] = ex
    return ex


def _fake_run_symbol(strategy_cfg, symbol, symbol_name, start, end, out_dir, batch_id=None):
    """不联网：直接落库一条 run 并返回 run_id（模拟回测成功）。"""
    ph = repository.compute_params_hash(strategy_cfg)
    rid = repository.save_run(
        run_meta(
            {
                "params_hash": ph,
                "symbol": symbol,
                "symbol_name": symbol_name,
                "strategy_name": strategy_cfg.get("name", ""),
            }
        ),
        equity_curve(),
        trades(),
        summary(),
        [],
    )
    return {"run_id": rid}


def _wait_batch_done(batch_id: str, timeout: float = 20.0) -> dict:
    """轮询批次状态（直接用 repository，避免 get_progress 的 ETA 路径）。

    注：get_progress 在 running+done_count>0 时会因 aware/naive 时间相减崩溃
    （见 test_get_progress_eta_computation），故此处用 repository.get_batch 轮询，
    以保证批次功能测试不被该已知 bug 干扰。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        b = repository.get_batch(batch_id)
        if b and b["status"] in ("done", "cancelled", "interrupted"):
            return b
        time.sleep(0.05)
    return repository.get_batch(batch_id)


def _items_for(batch_id):
    with db.SessionLocal() as s:
        return [
            (it.symbol, it.status, it.is_reused, it.run_id)
            for it in s.query(BatchItem)
            .filter(BatchItem.batch_id == batch_id)
            .order_by(BatchItem.symbol)
            .all()
        ]


# ---------------------------------------------------------------------------
# E1. 正常批量：命中复用 + pending 执行
# ---------------------------------------------------------------------------
def test_submit_batch_hit_and_pending(monkeypatch):
    import strategylab.engine.config as config

    monkeypatch.setattr(batch_runner, "run_symbol", _fake_run_symbol)
    monkeypatch.setattr(batch_runner, "WORKERS", 2)
    monkeypatch.setattr(batch_runner, "ThreadPoolExecutor", _make_executor)
    _pause["value"] = False

    with temp_db():
        cfg = config.load_strategy_by_arg("kdj_macd_dual_entry")
        ph = repository.compute_params_hash(cfg)
        sname = cfg["name"]

        # 预存 2 个命中 run（symA / symB），让它们走复用
        hit_a = repository.save_run(
            run_meta({"params_hash": ph, "strategy_name": sname, "symbol": "600216.SH", "symbol_name": "浙江医药"}),
            equity_curve(), trades(), summary(), [],
        )
        hit_b = repository.save_run(
            run_meta({"params_hash": ph, "strategy_name": sname, "symbol": "000001.SZ", "symbol_name": "平安银行"}),
            equity_curve(), trades(), summary(), [],
        )

        items = [
            {"symbol": "600216.SH", "symbol_name": "浙江医药", "sector_code": None},      # 命中
            {"symbol": "000001.SZ", "symbol_name": "平安银行", "sector_code": None},      # 命中
            {"symbol": "300765.SZ", "symbol_name": "新宙邦", "sector_code": None},        # pending
            {"symbol": "002001.SZ", "symbol_name": "新和成", "sector_code": None},        # pending
        ]
        batch_id, hit_count = batch_runner.submit_batch(
            cfg, sname, ph, "pool", "600216.SH,000001.SZ,300765.SZ,002001.SZ",
            str(__import__("tempfile").mkdtemp()), items,
        )

        assert batch_id and len(batch_id) == 36
        assert hit_count == 2, f"命中数应为 2，实际 {hit_count}"

        b = _wait_batch_done(batch_id, timeout=20)
        assert b["status"] == "done", f"批次应完成，实际 {b['status']}"
        assert b["total_count"] == 4
        assert b["skipped_count"] == 2
        assert b["done_count"] == 2

        # 终态下 get_progress 应正常返回（running+done 的 ETA 窗口崩溃见 test_get_progress_eta_computation）
        prog = batch_runner.get_progress(batch_id)
        assert prog["status"] == "done"
        assert prog["total"] == 4
        assert prog["skipped"] == 2
        assert prog["done"] == 2

        rows = _items_for(batch_id)
        by_sym = {sym: (status, reused, rid) for sym, status, reused, rid in rows}
        # 命中项
        assert by_sym["600216.SH"][0] == "skipped"
        assert by_sym["600216.SH"][1] is True
        assert by_sym["600216.SH"][2] == hit_a
        assert by_sym["000001.SZ"][0] == "skipped"
        assert by_sym["000001.SZ"][2] == hit_b
        # pending 项（被线程池执行）
        assert by_sym["300765.SZ"][0] == "done"
        assert by_sym["300765.SZ"][1] is False
        assert by_sym["300765.SZ"][2] is not None and len(by_sym["300765.SZ"][2]) == 36
        assert by_sym["002001.SZ"][0] == "done"
        assert by_sym["002001.SZ"][2] is not None


# ---------------------------------------------------------------------------
# E2. 取消：提交后立刻 cancel，未跑完项转 cancelled
# ---------------------------------------------------------------------------
def test_cancel_batch(monkeypatch):
    import strategylab.engine.config as config

    monkeypatch.setattr(batch_runner, "run_symbol", _fake_run_symbol)
    monkeypatch.setattr(batch_runner, "WORKERS", 2)
    monkeypatch.setattr(batch_runner, "ThreadPoolExecutor", _make_executor)
    _pause["value"] = True  # 提交后执行器先暂停，确保 cancel 在任务开始前生效

    with temp_db():
        cfg = config.load_strategy_by_arg("kdj_macd_dual_entry")
        ph = repository.compute_params_hash(cfg)
        sname = cfg["name"]

        items = [
            {"symbol": "300765.SZ", "symbol_name": "新宙邦", "sector_code": None},
            {"symbol": "002001.SZ", "symbol_name": "新和成", "sector_code": None},
            {"symbol": "600216.SH", "symbol_name": "浙江医药", "sector_code": None},
        ]
        batch_id, hit_count = batch_runner.submit_batch(
            cfg, sname, ph, "pool", "300765.SZ,002001.SZ,600216.SH",
            str(__import__("tempfile").mkdtemp()), items,
        )
        assert hit_count == 0

        # 任务此刻被暂停在 PausableExecutor 中（尚未置 running）
        cancel_ok = batch_runner.cancel_batch(batch_id)
        assert cancel_ok is True

        # 释放执行器：_run_one 看到取消标志 → 直接转 cancelled
        ex = _captured.get("ex")
        assert ex is not None
        ex._go.set()

        b = _wait_batch_done(batch_id, timeout=20)
        assert b["status"] == "cancelled", f"被取消批次状态应为 cancelled，实际 {b['status']}"

        rows = _items_for(batch_id)
        assert len(rows) == 3
        for sym, status, reused, rid in rows:
            assert status == "cancelled", f"未跑完项 {sym} 应为 cancelled，实际 {status}"


# ---------------------------------------------------------------------------
# E3. 重启处理：running 批次 → interrupted，pending/running item → cancelled
# ---------------------------------------------------------------------------
def test_init_batch_runner_interrupted(monkeypatch):
    import strategylab.engine.config as config

    # 不需要真实执行器，但要避免任何残留执行器干扰
    _pause["value"] = False
    monkeypatch.setattr(batch_runner, "ThreadPoolExecutor", _make_executor)

    with temp_db():
        cfg = config.load_strategy_by_arg("kdj_macd_dual_entry")
        ph = repository.compute_params_hash(cfg)
        sname = cfg["name"]

        batch_id = repository.create_batch_id()
        repository.create_batch(
            batch_id=batch_id, strategy_name=sname, strategy_type=cfg["type"],
            params_hash=ph, scope_type="pool", scope_value="ALL", total_count=3, skipped_count=0,
        )
        rid = repository.save_run(
            run_meta({"params_hash": ph, "strategy_name": sname, "symbol": "600216.SH"}),
            equity_curve(), trades(), summary(), [],
        )
        repository.bulk_create_batch_items([
            {"batch_id": batch_id, "symbol": "600216.SH", "symbol_name": "浙江医药",
             "sector_code": None, "status": "pending", "run_id": None, "is_reused": False},
            {"batch_id": batch_id, "symbol": "000001.SZ", "symbol_name": "平安银行",
             "sector_code": None, "status": "running", "run_id": None, "is_reused": False},
            {"batch_id": batch_id, "symbol": "300765.SZ", "symbol_name": "新宙邦",
             "sector_code": None, "status": "skipped", "run_id": rid, "is_reused": True},
        ])

        # 模拟重启
        batch_runner.init_batch_runner()

        b = repository.get_batch(batch_id)
        assert b["status"] == "interrupted", "running 批次重启后应标记为 interrupted"

        rows = {sym: status for sym, status, reused, rid in _items_for(batch_id)}
        assert rows["600216.SH"] == "cancelled", "pending item 应转 cancelled"
        assert rows["000001.SZ"] == "cancelled", "running item 应转 cancelled"
        assert rows["300765.SZ"] == "skipped", "skipped item 应保持 skipped"


# ---------------------------------------------------------------------------
# E-bug. get_progress ETA 崩溃（known source bug，路由工程师修复）
# ---------------------------------------------------------------------------
def test_get_progress_eta_computation():
    """复现 get_progress 的 ETA 崩溃（FR-17/进度轮询）：

    started_at 经 SQLite DATETIME 列往返后变 naive，fromisoformat 解析为 naive，
    与 datetime.now(timezone.utc)（aware）相减 → TypeError。
    该崩溃发生在「running 且 done_count>0」的轮询窗口，导致 /api/batch/<id>/progress 500。
    保留为回归守卫：修复后应返回含 eta_seconds 的 dict。
    """
    with temp_db():
        batch_id = repository.create_batch_id()
        repository.create_batch(
            batch_id=batch_id, strategy_name="ETA测试", strategy_type="kdj_macd_dual_entry",
            params_hash="e" * 40, scope_type="pool", scope_value="ALL", total_count=2, skipped_count=0,
        )
        rid = repository.save_run(
            run_meta({"params_hash": "e" * 40, "strategy_name": "ETA测试", "symbol": "600216.SH"}),
            equity_curve(), trades(), summary(), [],
        )
        repository.bulk_create_batch_items([
            {"batch_id": batch_id, "symbol": "600216.SH", "symbol_name": "浙江医药",
             "sector_code": None, "status": "pending", "run_id": None, "is_reused": False},
            {"batch_id": batch_id, "symbol": "000001.SZ", "symbol_name": "平安银行",
             "sector_code": None, "status": "pending", "run_id": None, "is_reused": False},
        ])
        # 模拟「1 个完成、批次仍 running」的窗口
        repository.update_batch_item(
            batch_id, "600216.SH", status="done", run_id=rid,
            finished_at=datetime.datetime.now(datetime.timezone.utc),
        )
        repository.inc_batch_count(batch_id, "done_count")

        # 当前实现在此处抛 TypeError；修复后应返回含 eta_seconds 的字典
        p = batch_runner.get_progress(batch_id)
        assert isinstance(p, dict)
        assert p["status"] == "running"
        assert "eta_seconds" in p
