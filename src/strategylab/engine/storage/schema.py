# -*- coding: utf-8 -*-
"""回测结果存储 ORM 模型（SQLAlchemy 2.0，DB 无关）。

字段类型约定（兼顾精度与跨库一致）：
  - 金额 / 价格：``Numeric(18, 4)``
  - 百分比：     ``Numeric(10, 4)``
  - 比率(sharpe)：``Float``
  - 日期：       ``Date``（评估 / 交易日期，无时区）
  - 时间戳：     ``DateTime``（``created_at`` 存 UTC）
  - ``run_id``： ``String(36)`` 存 uuid4（比原生 UUID 更跨库）

关系：``BacktestRun`` 1—* ``EquityPoint`` / ``Trade``，1—1 ``Summary``，
删除 run 时 cascade 删除其关联数据。
"""
from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import (
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
)


class Base(DeclarativeBase):
    """所有 ORM 模型的声明基类。"""


class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    run_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    batch_id: Mapped[str | None] = mapped_column(String(36), index=True)  # 同批提交共享
    strategy_type: Mapped[str] = mapped_column(String(64), index=True)
    strategy_name: Mapped[str] = mapped_column(String(128), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)  # 600216.SH
    symbol_name: Mapped[str] = mapped_column(String(128))
    start: Mapped[datetime.date] = mapped_column(Date, index=True)
    end: Mapped[datetime.date] = mapped_column(Date, index=True)
    initial_cash: Mapped[float] = mapped_column(Numeric(18, 4))
    params_json: Mapped[str] = mapped_column(Text)  # 策略配置完整快照（可复跑）
    positions_json: Mapped[str | None] = mapped_column(Text)  # 合并持仓明细（派生视图）
    meta_json: Mapped[str | None] = mapped_column(
        Text
    )  # market/generated_at/window_start_value/final_value
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
        index=True,
    )

    equity = relationship("EquityPoint", cascade="all,delete-orphan")
    trades = relationship("Trade", cascade="all,delete-orphan")
    summary = relationship("Summary", cascade="all,delete-orphan", uselist=False)


class EquityPoint(Base):
    __tablename__ = "equity_points"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("backtest_runs.run_id"), index=True
    )
    date: Mapped[datetime.date] = mapped_column(Date, index=True)
    value: Mapped[float] = mapped_column(Numeric(18, 4))


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("backtest_runs.run_id"), index=True
    )
    entry_date: Mapped[datetime.date] = mapped_column(Date)
    exit_date: Mapped[datetime.date] = mapped_column(Date)
    side: Mapped[str] = mapped_column(String(8))  # long
    role: Mapped[str | None] = mapped_column(String(16))  # 底仓 / 做T
    position_id: Mapped[str | None] = mapped_column(String(36), index=True)
    size: Mapped[int] = mapped_column(Integer)
    entry_price: Mapped[float] = mapped_column(Numeric(18, 4))
    exit_price: Mapped[float] = mapped_column(Numeric(18, 4))
    pnl: Mapped[float] = mapped_column(Numeric(18, 4))
    pnl_pct: Mapped[float] = mapped_column(Numeric(10, 4))
    holding_bars: Mapped[int] = mapped_column(Integer)
    symbol: Mapped[str] = mapped_column(String(32))
    symbol_name: Mapped[str | None] = mapped_column(String(128))
    display_symbol: Mapped[str | None] = mapped_column(String(128))
    label: Mapped[str | None] = mapped_column(String(64))


class Summary(Base):
    __tablename__ = "summary"

    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("backtest_runs.run_id"), primary_key=True
    )
    total_return_pct: Mapped[float | None] = mapped_column(Numeric(10, 4))
    annual_return_pct: Mapped[float | None] = mapped_column(Numeric(10, 4))
    max_drawdown_pct: Mapped[float | None] = mapped_column(Numeric(10, 4))
    sharpe: Mapped[float | None] = mapped_column(Float)
    win_rate_pct: Mapped[float | None] = mapped_column(Numeric(10, 4))
    total_trades: Mapped[int] = mapped_column(Integer)
    meta_json: Mapped[str | None] = mapped_column(Text)


class SchemaVersion(Base):
    """轻量版本表（migrate.init_db 写入当前版本，便于后续演进）。"""

    __tablename__ = "schema_version"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    applied_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
