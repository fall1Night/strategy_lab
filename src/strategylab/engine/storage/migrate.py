# -*- coding: utf-8 -*-
"""轻量数据库迁移封装（不引入 Alembic）。

  - ``init_db()``：建表 + 写入 schema_version（幂等，仅首次写入）。
  - ``get_version()``：读取当前 schema 版本；表不存在返回 None。

后续演进可用少量 ALTER 脚本 + 提升 SCHEMA_VERSION 实现，无需引入迁移框架。
"""
from __future__ import annotations

from typing import Any

from .db import SessionLocal, get_engine, init_db as _db_init_db
from .schema import SchemaVersion


def init_db() -> None:
    """创建所有表并登记 schema 版本。

    直接委托 ``db.init_db()``（建表 + 写入 schema_version 的唯一实现），
    避免与 db 层重复逻辑，保证单一来源。
    """
    _db_init_db()


def get_version() -> int | None:
    """返回当前 schema 版本；表不存在则返回 None。"""
    from sqlalchemy import inspect

    engine = get_engine()
    if not inspect(engine).has_table("schema_version"):
        return None
    with SessionLocal() as s:
        row = s.query(SchemaVersion).order_by(SchemaVersion.id.desc()).first()
    return int(row.version) if row else None
