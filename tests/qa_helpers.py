# -*- coding: utf-8 -*-
"""QA 共享工具：临时 SQLite 库管理、v1 库构造、构造测试数据。

所有 DB 测试都用临时文件库（避免污染项目 strategy_lab.db）。
通过重置 storage.db 的模块级引擎单例来切换 DATABASE_URL。
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

# 让 tests/ 能 import 到 src/strategylab
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import strategylab.engine.storage.db as db
import strategylab.engine.storage.repository as repo


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"


@contextlib.contextmanager
def temp_db(url: str | None = None):
    """切换到一个临时 SQLite 库（文件或内存），测试后还原环境并清理。"""
    if url is None:
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        url = "sqlite:///" + path

    orig = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    # 重置引擎单例 + 板块缓存，确保指向新库
    db._engine = None
    db._SessionFactory = None
    repo._SECTORS_CACHE = None
    repo._SECTOR_STOCKS_CACHE = None
    try:
        db.init_db()
        yield url
    finally:
        if orig is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = orig
        db._engine = None
        db._SessionFactory = None
        repo._SECTORS_CACHE = None
        repo._SECTOR_STOCKS_CACHE = None
        if url.startswith("sqlite:///") and url != "sqlite:///:memory:":
            p = url[len("sqlite:///"):]
            try:
                os.unlink(p)
            except OSError:
                pass


def make_v1_db(path: str) -> None:
    """构造一个 v1 老库：backtest_runs 表（无 params_hash 列），无 batches/batch_items。"""
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE backtest_runs (
            run_id VARCHAR(36) PRIMARY KEY,
            batch_id VARCHAR(36),
            strategy_type VARCHAR(64),
            strategy_name VARCHAR(128),
            symbol VARCHAR(32),
            symbol_name VARCHAR(128),
            start DATE,
            end DATE,
            initial_cash NUMERIC(18,4),
            params_json TEXT,
            positions_json TEXT,
            meta_json TEXT,
            created_at DATETIME
        )
        """
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# 测试数据构造（与既有 test_storage.py 类似，但适配 v2 字段）
# ---------------------------------------------------------------------------
def run_meta(extra: dict | None = None) -> dict:
    base = {
        "batch_id": "batch-qa-0001",
        "strategy_type": "kdj_macd_dual_entry",
        "strategy_name": "测试策略A",
        "symbol": "600216.SH",
        "symbol_name": "浙江医药",
        "start": "2023-01-01",
        "end": "2024-01-01",
        "initial_cash": 1000000.0,
        "params_json": '{"type":"kdj_macd_dual_entry","name":"测试策略A","params":{"initial_cash":1000000.0}}',
        "positions_json": "[]",
        "meta_json": '{"market":"china_a","window_start_value":1000000.0,"final_value":1100000.0}',
    }
    if extra:
        base.update(extra)
    return base


def equity_curve() -> list[dict]:
    return [
        {"date": "2023-01-01", "value": 1000000.0},
        {"date": "2023-06-01", "value": 1050000.0},
        {"date": "2024-01-01", "value": 1100000.0},
    ]


def trades() -> list[dict]:
    return [
        {
            "entry_date": "2023-01-01",
            "exit_date": "2023-06-01",
            "side": "long",
            "role": "底仓",
            "position_id": "pos-1",
            "size": 1000,
            "entry_price": 10.0,
            "exit_price": 10.5,
            "pnl": 500.0,
            "pnl_pct": 5.0,
            "holding_bars": 2,
            "symbol": "600216.SH",
            "symbol_name": "浙江医药",
            "display_symbol": "浙江医药",
            "label": "建仓",
        }
    ]


def summary(total_return_pct: float = 2.0) -> dict:
    return {
        "total_return_pct": total_return_pct,
        "annual_return_pct": 1.95,
        "max_drawdown_pct": -1.0,
        "sharpe": 1.2,
        "win_rate_pct": 100.0,
        "total_trades": 1,
    }
