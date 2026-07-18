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
SCHEMA_VERSION: int = 1


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
            # 远程库开启连接健康检查
            kwargs["pool_pre_ping"] = True
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

    建表后补登 ``schema_version``（v1 仅首次写入，后续重复调用不会新增行），
    避免历史上该表被 ``create_all`` 建出却恒为空的问题。
    生产库建议改用 Alembic；v1 用 create_all + 单行版本号足矣。
    """
    from .schema import Base, SchemaVersion

    Base.metadata.create_all(get_engine())

    # 登记/补登 schema 版本：仅当表为空时插入 version=1
    with SessionLocal() as s:
        if s.query(SchemaVersion).first() is None:
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
