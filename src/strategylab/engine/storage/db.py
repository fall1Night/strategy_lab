# -*- coding: utf-8 -*-
"""数据库连接与会话管理（DB 无关，SQLAlchemy 2.0）。

连接来源：环境变量 ``DATABASE_URL``。

  - 本地默认：``sqlite:///./strategy_lab.db``（零额外依赖，仅标准库）
  - MySQL：    ``mysql+pymysql://user:pass@host:3306/strategylab``
  - Postgres： ``postgresql+psycopg2://user:pass@host:5432/strategylab``

设计要点：
  - ``get_engine()`` 模块级懒加载单例，首次使用时按 ``DATABASE_URL`` 创建。
  - SQLite 多线程（``ThreadingHTTPServer``）需 ``check_same_thread=False``；
    内存库用 ``StaticPool`` 保证跨会话可见。
  - 远程库开启 ``pool_pre_ping=True``，避免陈旧连接。
  - ``SessionLocal()`` 返回短生命周期 ``Session``；``get_session()`` 提供
    「自动提交 / 异常回滚 / 结束关闭」上下文。
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

_DEFAULT_URL = "sqlite:///./strategy_lab.db"

_engine: Engine | None = None
_SessionFactory: sessionmaker | None = None

# 当前 schema 版本（与 migrate.SCHEMA_VERSION 同源；db.init_db 负责写入）。
SCHEMA_VERSION: int = 3


def _database_url() -> str:
    """读取 DATABASE_URL，缺省回落到本地 SQLite 文件。"""
    return os.environ.get("DATABASE_URL", _DEFAULT_URL)


def get_engine() -> Engine:
    """模块级懒加载单例：首次使用时按 DATABASE_URL 创建引擎。"""
    global _engine
    if _engine is None:
        url = _database_url()
        kwargs: dict = {}
        if url.startswith("sqlite"):
            # 多线程（Web 服务）需关闭线程检查
            kwargs["connect_args"] = {"check_same_thread": False}
            # 内存库每个连接独立；用 StaticPool 复用同一连接，跨会话可见
            if url == "sqlite:///:memory:":
                from sqlalchemy.pool import StaticPool

                kwargs["poolclass"] = StaticPool
        else:
            # 远程库（MySQL/Postgres）开启连接池调优：健康检查 + 回收 + 溢出
            kwargs["pool_pre_ping"] = True
            kwargs["pool_size"] = 10
            kwargs["max_overflow"] = 5
            kwargs["pool_recycle"] = 1800
        _engine = create_engine(url, future=True, **kwargs)
    return _engine


def _get_session_factory() -> sessionmaker:
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(
            bind=get_engine(), expire_on_commit=False, future=True
        )
    return _SessionFactory


def SessionLocal() -> Session:
    """返回一个全新的短生命周期 Session（请用 ``with`` 管理生命周期）。"""
    return _get_session_factory()()


def init_db() -> None:
    """创建所有表并登记 schema 版本（幂等，单一来源）。

    建表后：
      - 若 ``backtest_runs`` 已存在但缺少 ``params_hash`` 列 → 旧 v1 库，
        触发 ``migrate.migrate_v1_to_v2``（加列 / 建新表 / 回填 / 写版本）。
      - 否则为新库，直接补登 ``schema_version`` 为当前版本。
    生产库建议改用 Alembic；v1 用 create_all + 单行版本号足矣。
    """
    from sqlalchemy import inspect

    from .schema import Base, SchemaVersion

    engine = get_engine()
    Base.metadata.create_all(engine)

    # 检测是否需要迁移
    insp = inspect(engine)
    columns = [c["name"] for c in insp.get_columns("backtest_runs")] if insp.has_table("backtest_runs") else []

    if insp.has_table("backtest_runs") and "params_hash" not in columns:
        # v1 → v2 迁移：加 params_hash 列
        from . import migrate
        migrate.migrate_v1_to_v2(engine)
        # 重新检测列（迁移后可能有 params_hash 但无 data_source）
        columns = [c["name"] for c in inspect(engine).get_columns("backtest_runs")]

    # v2 → v3：加 data_source 列 + 回填历史 'eastmoney'
    if insp.has_table("backtest_runs") and "data_source" not in columns:
        from . import migrate
        migrate.migrate_v2_to_v3(engine)
    else:
        # 新库或已是 v3：补登/跳过 schema_version
        with SessionLocal() as s:
            cur = s.query(SchemaVersion).order_by(SchemaVersion.id.desc()).first()
            if cur is None or int(cur.version) < SCHEMA_VERSION:
                s.add(SchemaVersion(version=SCHEMA_VERSION))
            s.commit()


@contextmanager
def get_session() -> Iterator[Session]:
    """上下文管理器：自动提交 / 异常回滚 / 结束时关闭。

    用法：
        with get_session() as s:
            s.add(obj)
    """
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
