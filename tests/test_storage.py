# -*- coding: utf-8 -*-
"""存储层单元测试：SQLite 内存库验证建表 / 读写 / 查询 / 过滤 / 删除。

运行：
    cd strategy_lab
    python -m pytest tests/ -q

说明：必须在 import storage 之前设置 DATABASE_URL=sqlite:///:memory:
（配合 engine/storage/db.py 中的 StaticPool 配置，保证跨会话可见）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# 让 tests/ 能 import 到 src/strategylab 包（已 pip install -e . 则无需，此处为兜底）
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from strategylab.engine.storage import db, repository  # noqa: E402
from strategylab.engine.storage.schema import Base, BacktestRun, EquityPoint, Trade, Summary  # noqa: E402


def _reset_db() -> None:
    """清空所有表并重建，保证每个测试在干净状态开始（内存库跨测试复用连接）。"""
    engine = db.get_engine()
    Base.metadata.drop_all(engine)
    db.init_db()


# ---------------------------------------------------------------------------
# 测试数据构造
# ---------------------------------------------------------------------------
def _run_meta(extra: dict | None = None) -> dict:
    base = {
        "batch_id": "batch-0001",
        "strategy_type": "kdj_macd_dual_entry",
        "strategy_name": "测试策略A",
        "symbol": "600216.SH",
        "symbol_name": "浙江医药",
        "start": "2023-07-18",
        "end": "2024-07-18",
        "initial_cash": 1000000.0,
        "params_json": '{"type":"kdj_macd_dual_entry","name":"测试策略A","params":{"initial_cash":1000000.0}}',
        "positions_json": "[]",
        "meta_json": '{"market":"china_a","window_start_value":1000000.0,"final_value":1100000.0}',
    }
    if extra:
        base.update(extra)
    return base


def _equity() -> list[dict]:
    return [
        {"date": "2023-07-18", "value": 1000000.0},
        {"date": "2023-07-19", "value": 1010000.0},
        {"date": "2023-07-20", "value": 1020000.0},
    ]


def _trades() -> list[dict]:
    return [
        {
            "entry_date": "2023-07-18",
            "exit_date": "2023-07-19",
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


def _summary() -> dict:
    return {
        "total_return_pct": 2.0,
        "annual_return_pct": 1.95,
        "max_drawdown_pct": -1.0,
        "sharpe": 1.2,
        "win_rate_pct": 100.0,
        "total_trades": 1,
    }


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------
def test_create_all_and_save():
    _reset_db()
    # 验证 4 张表均已创建
    from sqlalchemy import inspect

    names = set(inspect(db.get_engine()).get_table_names())
    for t in ("backtest_runs", "equity_points", "trades", "summary", "schema_version"):
        assert t in names, f"缺少表: {t}"

    rid = repository.save_run(_run_meta(), _equity(), _trades(), _summary(), [])
    assert rid and len(rid) == 36
    return rid


def test_get_run_roundtrip():
    rid = test_create_all_and_save()
    r = repository.get_run(rid)
    assert r is not None
    assert r["run_id"] == rid
    assert len(r["equity_curve"]) == 3
    assert r["equity_curve"][0] == {"date": "2023-07-18", "value": 1000000.0}
    assert len(r["trade_history"]) == 1
    assert r["trade_history"][0]["side"] == "long"
    assert r["trade_history"][0]["pnl"] == 500.0
    assert r["summary"]["total_return_pct"] == 2.0
    assert r["summary"]["sharpe"] == 1.2
    assert r["positions"] == []
    assert r["symbol"] == "600216.SH"
    assert r["initial_cash"] == 1000000.0
    # 不存在返回 None
    assert repository.get_run("nonexistent-id") is None


def test_list_runs_and_filters():
    _reset_db()
    test_create_all_and_save()
    repository.save_run(
        _run_meta({"symbol": "000001.SZ", "symbol_name": "平安银行", "strategy_name": "测试策略B"}),
        _equity(),
        _trades(),
        _summary(),
        [],
    )

    all_runs = repository.list_runs()
    assert len(all_runs) == 2

    # 按标的过滤
    assert len(repository.list_runs(symbol="600216.SH")) == 1
    assert len(repository.list_runs(symbol="000001.SZ")) == 1
    assert len(repository.list_runs(symbol="999999.SZ")) == 0

    # 按策略过滤
    assert len(repository.list_runs(strategy="测试策略B")) == 1
    assert len(repository.list_runs(strategy="不存在策略")) == 0

    # 限制条数
    assert len(repository.list_runs(limit=1)) == 1

    # 摘要字段完整
    r0 = all_runs[0]
    assert set(
        ["run_id", "symbol", "symbol_name", "start", "end", "total_return_pct", "sharpe"]
    ).issubset(r0.keys())


def test_list_runs_by_ids_order_and_dedup():
    _reset_db()
    rid1 = repository.save_run(
        _run_meta({"symbol": "600001.SH"}), _equity(), _trades(), _summary(), []
    )
    rid2 = repository.save_run(
        _run_meta({"symbol": "600002.SH"}), _equity(), _trades(), _summary(), []
    )
    # 顺序保持、去重
    out = repository.list_runs_by_ids([rid2, rid1, rid2])
    assert [r["run_id"] for r in out] == [rid2, rid1]
    assert len(out) == 2
    # 空列表
    assert repository.list_runs_by_ids([]) == []


def test_delete_run_cascade():
    _reset_db()
    rid = test_create_all_and_save()
    repository.delete_run(rid)
    assert repository.get_run(rid) is None
    # 关联 equity/trades/summary 也应被 cascade 删除
    with db.SessionLocal() as s:
        assert s.query(EquityPoint).filter(EquityPoint.run_id == rid).count() == 0
        assert s.query(Trade).filter(Trade.run_id == rid).count() == 0
        assert s.query(Summary).filter(Summary.run_id == rid).count() == 0


if __name__ == "__main__":
    test_create_all_and_save()
    test_get_run_roundtrip()
    test_list_runs_and_filters()
    test_list_runs_by_ids_order_and_dedup()
    test_delete_run_cascade()
    print("\nALL TESTS PASSED")
