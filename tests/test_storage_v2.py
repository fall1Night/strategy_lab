# -*- coding: utf-8 -*-
"""FR-13/14/15/20/22 测试：save_run 落库、命中复用、排名、板块状态。

B. save_run bulk_insert（FR-15）+ run_symbol 注入 params_hash（FR-13 端到端，去网络）
C. 命中复用 find_existing_runs（FR-14）
D. 排名 rank_runs（FR-20）+ 板块状态 sector_status（FR-22）
"""
from __future__ import annotations

import time
from pathlib import Path

from strategylab.engine import backtest
from strategylab.engine.storage import repository

from qa_helpers import (
    DATA_DIR,
    equity_curve,
    run_meta,
    summary,
    temp_db,
    trades,
)


# ---------------------------------------------------------------------------
# B. save_run + run_symbol (FR-13/15)
# ---------------------------------------------------------------------------
def test_save_run_roundtrip_with_params_hash():
    with temp_db():
        meta = run_meta(
            {
                "params_hash": repository.compute_params_hash(
                    {"type": "kdj_macd_dual_entry", "name": "测试策略A", "params": {"initial_cash": 1000000.0}}
                ),
                "symbol": "600216.SH",
                "symbol_name": "浙江医药",
            }
        )
        rid = repository.save_run(meta, equity_curve(), trades(), summary(), [])
        assert rid and len(rid) == 36

        r = repository.get_run(rid)
        assert r is not None
        assert r["run_id"] == rid
        assert len(r["equity_curve"]) == 3
        assert r["equity_curve"][0] == {"date": "2023-01-01", "value": 1000000.0}
        assert len(r["trade_history"]) == 1
        assert r["trade_history"][0]["side"] == "long"
        assert r["trade_history"][0]["pnl"] == 500.0
        assert r["summary"]["total_return_pct"] == 2.0
        assert r["summary"]["sharpe"] == 1.2
        assert r["positions"] == []
        assert r["symbol"] == "600216.SH"
        assert r["initial_cash"] == 1000000.0
        # params_hash 已落库（get_run 不含 params_hash，通过列表摘要校验）
        rows = repository.list_runs(symbol="600216.SH")
        assert rows and rows[0]["params_hash"] == meta["params_hash"]


def test_run_symbol_injects_params_hash(monkeypatch):
    """FR-13 端到端：run_symbol 把 params_hash 注入 run_meta 并落库（去掉网络取数）。

    用项目 data/ 里真实缓存的 K 线 CSV（600216.SH）喂给真实策略，仅 stub 掉
    ensure_data（网络取数）。证明 run_symbol → 注入 params_hash → save_run 链路有效。
    """
    import strategylab.engine.data_feed as data_feed

    sym = "600216.SH"
    prefix = data_feed.normalize_symbol(sym)["prefix"]

    # stub 掉网络取数：直接返回本地缓存的 csv 路径
    def fake_ensure_data(symbol_cfg, out_dir, **kwargs):
        d = Path(out_dir)
        return d / f"{prefix}_daily.csv", d / f"{prefix}_weekly.csv"

    monkeypatch.setattr(data_feed, "ensure_data", fake_ensure_data)

    with temp_db():
        from strategylab.engine.config import load_strategy_by_arg
        from strategylab.engine.storage.repository import compute_params_hash

        cfg = load_strategy_by_arg("kdj_macd_dual_entry")
        ph = compute_params_hash(cfg)

        res = backtest.run_symbol(
            cfg, sym, "浙江医药", "2023-01-01", "2024-01-01", str(DATA_DIR), batch_id=None
        )
        assert res.get("run_id"), "run_symbol 应返回 run_id"
        rid = res["run_id"]

        saved = repository.get_run(rid)
        assert saved is not None
        assert saved["symbol"] == "600216.SH"
        assert saved["summary"]["total_return_pct"] is not None
        # params_hash 已注入并落库（get_run 不含 params_hash，用列表摘要校验）
        rows = repository.list_runs(symbol="600216.SH")
        assert rows, "run_symbol 应已落库"
        assert rows[0]["params_hash"] == ph, "run_symbol 注入的 params_hash 应与 compute_params_hash 一致"


# ---------------------------------------------------------------------------
# C. 命中复用 find_existing_runs（FR-14）
# ---------------------------------------------------------------------------
def _save_with(ph: str, symbol: str, name: str, sleep: float = 0.0):
    if sleep:
        time.sleep(sleep)
    return repository.save_run(
        run_meta({"params_hash": ph, "symbol": symbol, "symbol_name": name}),
        equity_curve(),
        trades(),
        summary(),
        [],
    )


def test_find_existing_runs_latest_per_symbol():
    with temp_db():
        strategy = "测试策略A"
        ph = "a" * 40
        # symA 两条（不同 created_at），symB 一条
        r_a1 = _save_with(ph, "600216.SH", "浙江医药")
        time.sleep(0.02)
        r_a2 = _save_with(ph, "600216.SH", "浙江医药")
        r_b1 = _save_with(ph, "000001.SZ", "平安银行")

        found = repository.find_existing_runs(strategy, ph, ["600216.SH", "000001.SZ"])
        # 每个 symbol 返回最新（created_at MAX）的 run_id
        assert found.get("600216.SH") == r_a2, "应返回 symA 最新 run"
        assert found.get("000001.SZ") == r_b1
        # 未保存的 symbol 不应出现
        assert "300765.SZ" not in found


def test_find_existing_runs_empty_when_params_hash_changes():
    with temp_db():
        strategy = "测试策略A"
        ph1 = "a" * 40
        ph2 = "b" * 40
        _save_with(ph1, "600216.SH", "浙江医药")

        # 改 params_hash → 不应命中
        found = repository.find_existing_runs(strategy, ph2, ["600216.SH"])
        assert found == {}, "params_hash 不匹配时应返回空"


def test_find_existing_runs_empty_symbols():
    with temp_db():
        assert repository.find_existing_runs("x", "y", []) == {}


# ---------------------------------------------------------------------------
# D. 排名 rank_runs（FR-20）+ 板块状态 sector_status（FR-22）
# ---------------------------------------------------------------------------
def test_rank_runs_order_and_pagination():
    with temp_db():
        strategy = "排名测试策略"
        ph = "r" * 40
        returns = [50.0, 40.0, 30.0, 20.0, 10.0]
        syms = ["600001.SH", "600002.SH", "600003.SH", "600004.SH", "600005.SH"]
        for ret, sym in zip(returns, syms):
            repository.save_run(
                run_meta({"params_hash": ph, "strategy_name": strategy, "symbol": sym, "symbol_name": sym}),
                equity_curve(),
                trades(),
                summary(total_return_pct=ret),
                [],
            )

        # 总收益降序
        r1 = repository.rank_runs(strategy, ph, page=1, size=2)
        assert r1["total"] == 5
        assert len(r1["items"]) == 2
        assert r1["items"][0]["total_return_pct"] == 50.0
        assert r1["items"][1]["total_return_pct"] == 40.0
        # 关键字段齐全
        it = r1["items"][0]
        for k in ("run_id", "symbol", "symbol_name", "total_return_pct", "max_drawdown_pct", "sharpe"):
            assert k in it, f"排名项缺少字段 {k}"

        # 分页
        r2 = repository.rank_runs(strategy, ph, page=2, size=2)
        assert [i["total_return_pct"] for i in r2["items"]] == [30.0, 20.0]
        r3 = repository.rank_runs(strategy, ph, page=3, size=2)
        assert [i["total_return_pct"] for i in r3["items"]] == [10.0]


def test_rank_runs_scoped_by_params_hash():
    with temp_db():
        strategy = "双参数策略"
        ph1 = "p" * 40
        ph2 = "q" * 40
        repository.save_run(
            run_meta({"params_hash": ph1, "strategy_name": strategy, "symbol": "600001.SH"}),
            equity_curve(), trades(), summary(total_return_pct=99.0), [],
        )
        repository.save_run(
            run_meta({"params_hash": ph2, "strategy_name": strategy, "symbol": "600002.SH"}),
            equity_curve(), trades(), summary(total_return_pct=1.0), [],
        )
        # 只返回 ph1 的 run
        r = repository.rank_runs(strategy, ph1, page=1, size=50)
        assert r["total"] == 1
        assert r["items"][0]["symbol"] == "600001.SH"


def test_sector_status_no_items_returns_unrun():
    """FR-22：无 batch_items 时返回（31）板块且状态为未跑，不报错。"""
    with temp_db():
        sectors = repository.get_sectors()
        strategy = "板块测试策略"
        ph = "s" * 40
        result = repository.sector_status(strategy, ph)
        assert isinstance(result, list)
        if sectors:
            assert len(result) == len(sectors), "板块状态应覆盖全部板块"
            for sec in result:
                for k in ("code", "name", "status", "done", "skipped", "failed", "total"):
                    assert k in sec, f"板块状态项缺少字段 {k}"
                assert sec["status"] == "none", "无 batch_items 时应为未跑"


def test_sector_status_reflects_items():
    """FR-22 正面：插入一个 done 的 batch_item，对应板块状态应为 done/partial。"""
    with temp_db():
        sectors = repository.get_sectors()
        if not sectors:
            return
        code = sectors[0]["code"]
        strategy = "板块测试策略"
        ph = "s" * 40
        # 先落一个 run（供 batch_item.run_id 关联）
        rid = repository.save_run(
            run_meta({"params_hash": ph, "strategy_name": strategy, "symbol": "699999.SH"}),
            equity_curve(), trades(), summary(), [],
        )
        batch_id = repository.create_batch_id()
        repository.create_batch(
            batch_id=batch_id, strategy_name=strategy, strategy_type="kdj_macd_dual_entry",
            params_hash=ph, scope_type="sector", scope_value=code, total_count=1, skipped_count=0,
        )
        repository.bulk_create_batch_items([
            {"batch_id": batch_id, "symbol": "699999.SH", "symbol_name": "测试",
             "sector_code": code, "status": "done", "run_id": rid, "is_reused": False},
        ])
        result = repository.sector_status(strategy, ph)
        by_code = {s["code"]: s for s in result}
        assert by_code[code]["done"] >= 1
        assert by_code[code]["status"] in ("done", "partial")
