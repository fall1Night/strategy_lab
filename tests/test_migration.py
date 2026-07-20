# -*- coding: utf-8 -*-
"""FR-12/13 测试：v1→v2 迁移、params_hash 回填、幂等。

A. 迁移（FR-12/13）：
   - 老 v1 库（backtest_runs 无 params_hash）→ init_db() 自动迁移到 v2
     （params_hash 列存在、batches/batch_items 表存在、schema_version=2）。
   - 老 run 的 params_hash 被回填（len==40，与 compute_params_hash 一致）。
   - 幂等：再跑 init_db() 不报错、不重复插版本行。
   - repository.backfill_params_hash() 单独验证（空 params_hash 的 run 被回填）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile

from sqlalchemy import inspect, text

from strategylab.engine.storage import db, repository
from strategylab.engine.storage.schema import Base, SchemaVersion

from qa_helpers import make_v1_db, temp_db


def _insert_v1_runs(path: str, rows: list[dict]) -> None:
    conn = sqlite3.connect(path)
    for r in rows:
        conn.execute(
            """
            INSERT INTO backtest_runs
              (run_id, strategy_type, strategy_name, symbol, symbol_name,
               start, end, initial_cash, params_json, positions_json, meta_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                r["run_id"],
                r.get("strategy_type", "kdj_macd_dual_entry"),
                r["strategy_name"],
                r["symbol"],
                r["symbol_name"],
                r["start"],
                r["end"],
                r["initial_cash"],
                r["params_json"],
                r.get("positions_json", "[]"),
                r.get("meta_json", "{}"),
            ),
        )
    conn.commit()
    conn.close()


V1_ROWS = [
    {
        "run_id": "aaaaaaaa-0000-0000-0000-000000000001",
        "strategy_name": "测试策略A",
        "symbol": "600216.SH",
        "symbol_name": "浙江医药",
        "start": "2023-01-01",
        "end": "2024-01-01",
        "initial_cash": 1000000.0,
        "params_json": '{"type":"kdj_macd_dual_entry","name":"测试策略A","params":{"initial_cash":1000000.0}}',
    },
    {
        "run_id": "aaaaaaaa-0000-0000-0000-000000000002",
        "strategy_name": "测试策略A",
        "symbol": "000001.SZ",
        "symbol_name": "平安银行",
        "start": "2023-01-01",
        "end": "2024-01-01",
        "initial_cash": 1000000.0,
        "params_json": '{"type":"kdj_macd_dual_entry","name":"测试策略A","params":{"initial_cash":1000000.0}}',
    },
    {
        "run_id": "aaaaaaaa-0000-0000-0000-000000000003",
        "strategy_name": "测试策略B",
        "symbol": "600216.SH",
        "symbol_name": "浙江医药",
        "start": "2023-01-01",
        "end": "2024-01-01",
        "initial_cash": 500000.0,
        "params_json": '{"type":"kdj_macd_dual_entry","name":"测试策略B","params":{"initial_cash":500000.0}}',
    },
]


def test_migrate_v1_to_v2():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    make_v1_db(path)
    _insert_v1_runs(path, V1_ROWS)

    with temp_db("sqlite:///" + path):
        # 触发迁移
        db.init_db()

        engine = db.get_engine()
        insp = inspect(engine)
        cols = [c["name"] for c in insp.get_columns("backtest_runs")]

        # 1) params_hash 列已存在
        assert "params_hash" in cols, "迁移后 backtest_runs 缺少 params_hash 列"

        # 2) 新表已建立
        for t in ("batches", "batch_items", "schema_version"):
            assert insp.has_table(t), f"迁移后缺少表: {t}"

        # 3) schema_version = 3（3.0 当前版本；v1→v3 迁移链完成后 MAX 版本应为 3）
        from strategylab.engine.storage import migrate as migrate_mod

        assert migrate_mod.get_version() == 3, "schema_version 应为 3"

        # 4) 老 run 的 params_hash 被回填（len==40，与 compute_params_hash 一致）
        with db.SessionLocal() as s:
            rows = s.query(
                repository.BacktestRun.run_id,
                repository.BacktestRun.params_json,
                repository.BacktestRun.params_hash,
            ).all()
        assert len(rows) == 3
        for run_id, pj, ph in rows:
            assert ph is not None and ph != "", f"run {run_id} 的 params_hash 未回填"
            assert len(ph) == 40, f"params_hash 长度应为 40，实际 {len(ph)}"
            expected = repository.compute_params_hash(json.loads(pj))
            assert ph == expected, "回填的 params_hash 与 compute_params_hash 不一致"


def test_backfill_params_hash_function():
    """单独验证 repository.backfill_params_hash()：空 params_hash 的 run 被回填。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    make_v1_db(path)
    # 手动加回 params_hash 列（模拟「有列但为空」的库，不经过迁移回填）
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE backtest_runs ADD COLUMN params_hash VARCHAR(40)")
    conn.commit()
    conn.close()
    _insert_v1_runs(path, V1_ROWS)

    with temp_db("sqlite:///" + path):
        # 先确认此时 params_hash 为空
        with db.SessionLocal() as s:
            n_empty = (
                s.query(repository.BacktestRun)
                .filter(
                    (repository.BacktestRun.params_hash.is_(None))
                    | (repository.BacktestRun.params_hash == "")
                )
                .count()
            )
        assert n_empty == 3

        filled = repository.backfill_params_hash()
        assert filled == 3, f"backfill_params_hash 应回填 3 条，实际 {filled}"

        with db.SessionLocal() as s:
            rows = s.query(
                repository.BacktestRun.params_json,
                repository.BacktestRun.params_hash,
            ).all()
        for pj, ph in rows:
            assert ph and len(ph) == 40
            assert ph == repository.compute_params_hash(json.loads(pj))


def test_migration_idempotent():
    """幂等：再跑 init_db() 不报错、不重复插版本行。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    make_v1_db(path)
    _insert_v1_runs(path, V1_ROWS[:1])

    with temp_db("sqlite:///" + path):
        db.init_db()  # 第一次：迁移
        with db.SessionLocal() as s:
            first_count = s.query(SchemaVersion).count()
        assert first_count == 1

        # 再跑多次 init_db()
        db.init_db()
        db.init_db()

        with db.SessionLocal() as s:
            vs = s.query(SchemaVersion).all()
            count = len(vs)
            max_ver = max(int(v.version) for v in vs)
        assert count == 1, f"版本行不应重复插入，实际 {count} 行"
        assert max_ver == 3
