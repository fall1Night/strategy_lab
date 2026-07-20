# -*- coding: utf-8 -*-
"""轻量数据库迁移封装（不引入 Alembic）。

  - ``init_db()``：建表 + 写入 schema_version（幂等，仅首次写入）。
  - ``get_version()``：读取当前 schema 版本；表不存在返回 None。
  - ``migrate_v1_to_v2(engine)``：v1 → v2 迁移（加 params_hash 列 + 建批次表 + 回填 + 写版本），幂等。
  - ``migrate_v2_to_v3(engine)``：v2 → v3 迁移（加 data_source 列 + 回填 'eastmoney' + 建索引），幂等。

后续演进可用少量 ALTER 脚本 + 提升 SCHEMA_VERSION 实现，无需引入迁移框架。
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import inspect, text

from .db import SessionLocal, get_engine, init_db as _db_init_db
from .schema import Base, SchemaVersion


def init_db() -> None:
    """创建所有表并登记 schema 版本。

    直接委托 ``db.init_db()``（建表 + 写入 schema_version 的唯一实现），
    避免与 db 层重复逻辑，保证单一来源。
    """
    _db_init_db()


def get_version() -> int | None:
    """返回当前 schema 版本；表不存在则返回 None。"""
    engine = get_engine()
    if not inspect(engine).has_table("schema_version"):
        return None
    with SessionLocal() as s:
        row = s.query(SchemaVersion).order_by(SchemaVersion.id.desc()).first()
    return int(row.version) if row else None


def _compute_params_hash(strategy_cfg: dict) -> str:
    """规范化 params_json 的 sha1 指纹（与 repository.compute_params_hash 保持一致）。"""
    import hashlib

    blob = json.dumps(
        strategy_cfg,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha1(blob).hexdigest()


def _ensure_index(engine: Any, table: str, index_name: str, column: str) -> None:
    """若该索引不存在则创建（SQLite/MySQL 通用，幂等）。"""
    existing = {idx["name"] for idx in inspect(engine).get_indexes(table)}
    if index_name in existing:
        return
    with engine.connect() as conn:
        conn.execute(
            text(f"CREATE INDEX {index_name} ON {table} ({column})")
        )
        conn.commit()


def migrate_v1_to_v2(engine: Any) -> None:
    """v1 → v2 迁移（幂等）。

    1. 若 ``backtest_runs`` 缺少 ``params_hash`` 列 → ALTER 加列 + 建索引；
    2. ``Base.metadata.create_all`` 建新表（batches / batch_items，已存在则跳过）；
    3. 回填老数据 ``params_hash``（从 ``params_json`` 计算）；
    4. ``schema_version`` 写入 2（仅当当前版本 < 2）。
    """
    insp = inspect(engine)
    if not insp.has_table("backtest_runs"):
        # 全新库：直接建表并登记版本，无需 ALTER/回填
        Base.metadata.create_all(engine)
        with SessionLocal() as s:
            if s.query(SchemaVersion).first() is None:
                s.add(SchemaVersion(version=2))
            s.commit()
        return

    columns = [c["name"] for c in insp.get_columns("backtest_runs")]
    if "params_hash" not in columns:
        with engine.connect() as conn:
            conn.execute(
                text("ALTER TABLE backtest_runs ADD COLUMN params_hash VARCHAR(40)")
            )
            conn.commit()
        _ensure_index(engine, "backtest_runs", "ix_backtest_runs_params_hash", "params_hash")

    # 建新表（batches / batch_items）；已存在则无操作
    Base.metadata.create_all(engine)

    # 回填老数据：params_json 非空且 params_hash 为空
    from . import repository

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT run_id, params_json FROM backtest_runs "
                "WHERE params_json IS NOT NULL AND params_json <> '' "
                "AND (params_hash IS NULL OR params_hash = '')"
            )
        ).fetchall()
        for run_id, params_json in rows:
            try:
                cfg = json.loads(params_json)
            except (ValueError, TypeError):
                continue
            ph = _compute_params_hash(cfg)
            conn.execute(
                text("UPDATE backtest_runs SET params_hash = :ph WHERE run_id = :rid"),
                {"ph": ph, "rid": run_id},
            )
        conn.commit()

    # 写版本：单一当前版本行不变式——更新已有最新行，不新增行（N=2）
    with SessionLocal() as s:
        cur = s.query(SchemaVersion).order_by(SchemaVersion.id.desc()).first()
        if cur is None:
            s.add(SchemaVersion(version=2))
        elif int(cur.version) < 2:
            cur.version = 2  # 更新已有行，不新增
        s.commit()


def migrate_v2_to_v3(engine: Any) -> None:
    """v2 → v3 迁移（幂等）：加 ``data_source`` 列 + 回填 ``'eastmoney'`` + 建索引。

    1. 若 ``backtest_runs`` 缺少 ``data_source`` 列 → ALTER 加列 + 建索引；
    2. 回填历史 run 的 ``data_source`` 为 ``'eastmoney'``；
    3. ``schema_version`` 写入 3（仅当当前版本 < 3）。
    """
    insp = inspect(engine)

    if not insp.has_table("backtest_runs"):
        return

    columns = [c["name"] for c in insp.get_columns("backtest_runs")]

    if "data_source" not in columns:
        with engine.connect() as conn:
            conn.execute(
                text("ALTER TABLE backtest_runs ADD COLUMN data_source VARCHAR(32)")
            )
            conn.commit()
        _ensure_index(engine, "backtest_runs", "ix_backtest_runs_data_source", "data_source")

    # 回填历史数据：data_source 为 NULL 或空的 → 'eastmoney'
    with engine.connect() as conn:
        conn.execute(
            text(
                "UPDATE backtest_runs SET data_source = 'eastmoney' "
                "WHERE data_source IS NULL OR data_source = ''"
            )
        )
        conn.commit()

    # 写版本 3：单一当前版本行不变式——更新已有最新行，不新增行（N=3）
    with SessionLocal() as s:
        cur = s.query(SchemaVersion).order_by(SchemaVersion.id.desc()).first()
        if cur is None:
            s.add(SchemaVersion(version=3))
        elif int(cur.version) < 3:
            cur.version = 3  # 更新已有行，不新增
        s.commit()


def migrate_v3_to_v4(engine: Any) -> None:
    """v3 → v4 迁移（幂等）：``batches`` 表加 ``batch_type`` 列（data/backtest 隔离）。

    仅 ALTER 加列（NOT NULL DEFAULT 'backtest'），不新增 schema_version 行，
    以兼容既有 v1→v2→v3 迁移链与已有版本断言（保持与现有迁移链一致）。

    FR-37：回测批次与「更新数据源」批次共用 ``batches`` 表，用 ``batch_type``
    区分（``'backtest'`` / ``'data'``），清空回测结果时限定该维度避免误删 data 批次。
    """
    insp = inspect(engine)
    if not insp.has_table("batches"):
        return
    columns = [c["name"] for c in insp.get_columns("batches")]
    if "batch_type" in columns:
        return  # 已存在（如全新库由 create_all 建出）→ 幂等跳过
    with engine.connect() as conn:
        conn.execute(
            text(
                "ALTER TABLE batches ADD COLUMN batch_type VARCHAR(16) "
                "NOT NULL DEFAULT 'backtest'"
            )
        )
        conn.commit()
