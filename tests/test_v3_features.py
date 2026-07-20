# -*- coding: utf-8 -*-
"""FR-37~43（3.0 核心行为）回归测试。

覆盖：
  - T-39 增量更新：``cache.merge`` 幂等；``_incremental_window(last>=today)`` 返回 None；
        ``ensure_data(mode='update')`` 取数区间 = [last+1, today]。
  - T-38 data 批次：``submit_data_batch`` 执行后 ``backtest_runs`` 不新增 run
        （data-only，item.run_id=NULL，batch_type='data'）。
  - T-40 verify：``ensure_data(mode='verify')`` 区间未覆盖抛 ``DataMissingError``；
        已覆盖则不取数、不抛错。
  - T-42 清空重跑：``clear_strategy_runs`` 删该维度 backtest_runs/equity/trades/summary/batch_items，
        但 data 批次（batch_type='data'）不被删；``submit_batch`` 清空 backtest 但保留 data 批次。
  - T-37 双按钮：``POST /api/data/update`` 路由存在且走 data 批次；``Batch`` 模型有 ``batch_type`` 列；
        空库 init_db 可跑通且 ``batches.batch_type`` 存在（单一 schema_version 行）。
  - T-43 last_buy_date：``rank_runs`` 返回某 run 的 ``last_buy_date`` = 该 run
        ``trades.entry_date`` 的 MAX；无交易 run 为 None；``/api/rank sort_by='last_buy_date'``
        按该列排序且 None 排末尾；白名单含 ``last_buy_date``。
"""
from __future__ import annotations

import datetime
import json
import os
import threading
import urllib.parse
import urllib.request
from pathlib import Path
from tempfile import mkdtemp

import pandas as pd
import pytest
from http.server import ThreadingHTTPServer
from sqlalchemy import inspect

from strategylab.engine import batch_runner, data_feed
from strategylab.engine.datasource import provider as provider_mod
from strategylab.engine.datasource.base import normalize_symbol
from strategylab.engine.datasource.cache import KlineCache
from strategylab.engine.datasource.exceptions import DataMissingError
from strategylab.engine.storage import db, repository
from strategylab.engine.storage.schema import (
    BacktestRun,
    Batch,
    BatchItem,
    SchemaVersion,
    Trade,
    Summary,
    EquityPoint,
)

from qa_helpers import (
    PROJECT_ROOT,
    equity_curve,
    run_meta,
    summary,
    temp_db,
    trades,
)

# data_feed.ensure_data 需要 symbol_cfg 的「dict」形态（data_feed.normalize_symbol 返回 dict）；
# datasource.base.normalize_symbol 返回 SymbolSpec（不可下标），仅用于 cache 层操作。
SYM_DICT = data_feed.normalize_symbol("600216.SH")


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _count_backtest_runs() -> int:
    with db.SessionLocal() as s:
        return s.query(BacktestRun).count()


def _wait_batch_done(batch_id: str, timeout: float = 20.0) -> dict:
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        b = repository.get_batch(batch_id)
        if b and b["status"] in ("done", "cancelled", "interrupted"):
            return b
        time.sleep(0.05)
    return repository.get_batch(batch_id)


def _fake_run_symbol(strategy_cfg, symbol, symbol_name, start, end, out_dir, batch_id=None, data_source=None):
    """不联网：直接落库一条 run 并返回 run_id（模拟回测成功）。"""
    ph = repository.compute_params_hash(strategy_cfg)
    rid = repository.save_run(
        run_meta(
            {
                "params_hash": ph,
                "symbol": symbol,
                "symbol_name": symbol_name,
                "strategy_name": strategy_cfg.get("name", ""),
                "data_source": data_source,
            }
        ),
        equity_curve(),
        trades(),
        summary(),
        [],
    )
    return {"run_id": rid}


def _trades_with(entry_dates: list[str]) -> list[dict]:
    """按给定 entry_date 列表构造 trade 明细（复制模板，仅改 entry_date）。"""
    base = trades()[0]
    out = []
    for d in entry_dates:
        t = dict(base)
        t["entry_date"] = d
        out.append(t)
    return out


def _write_cover_meta(out: Path, spec, eff: str, d_beg, d_end, w_beg, w_end) -> None:
    """直接写 daily+weekly meta，使其覆盖给定区间（供 verify 成功用例）。"""
    cache = KlineCache()
    for period, beg, end in (("daily", d_beg, d_end), ("weekly", w_beg, w_end)):
        meta = {
            "beg": beg,
            "end": end,
            "rows": 1,
            "first": beg,
            "last": end,
            "version": 2,
            "source": eff,
        }
        cache._atomic_write_meta(meta, cache._meta_path(out, spec.prefix, eff, period))


# ===========================================================================
# T-39 增量更新
# ===========================================================================
def test_cache_merge_idempotent():
    """cache.merge 重复 merge 相同数据，末日/首日不变（幂等，历史不回退）。"""
    out = Path(mkdtemp())
    spec = normalize_symbol("600216.SH")
    cache = KlineCache()
    src = "eastmoney"
    df = pd.DataFrame(
        {
            "date": ["2022-01-01", "2022-02-01", "2022-03-01"],
            "open": [1.0, 2.0, 3.0],
            "high": [1.0, 2.0, 3.0],
            "low": [1.0, 2.0, 3.0],
            "close": [1.0, 2.0, 3.0],
        }
    )
    cache.merge(spec, src, "daily", df, out)
    meta1 = json.loads(
        Path(cache._meta_path(out, spec.prefix, src, "daily")).read_text(encoding="utf-8")
    )
    # 再次 merge 相同数据（幂等）
    cache.merge(spec, src, "daily", df, out)
    meta2 = json.loads(
        Path(cache._meta_path(out, spec.prefix, src, "daily")).read_text(encoding="utf-8")
    )
    assert meta2["last"] == meta1["last"], "重复 merge 末日应不变（幂等）"
    assert meta2["beg"] == meta1["beg"], "重复 merge 首日应不变（历史不回退）"


def test_incremental_window_returns_none_when_uptodate():
    """_incremental_window：last>=today 返回 None（幂等跳过）；last<today 返回 last+1；无缓存返回 genesis。"""
    cache = KlineCache()
    today = "20230101"
    assert cache._incremental_window({"last": "20230101"}, today, "20220706") is None
    assert cache._incremental_window({"last": "20230102"}, today, "20220706") is None
    assert (
        cache._incremental_window({"last": "20221231"}, today, "20220706") == "20230101"
    )
    assert cache._incremental_window(None, today, "20220706") == "20220706"
    assert cache._incremental_window({}, today, "20220706") == "20220706"


def test_ensure_data_update_interval_is_last_plus_one_to_today(monkeypatch):
    """ensure_data(mode='update') 取数区间 = [last+1, today]（FR-39 增量）。"""
    out = Path(mkdtemp())
    captured: list = []
    fetch = MagicMock_side_effect_capture(captured)
    monkeypatch.setattr(provider_mod.KlineProvider, "_try_fetch", fetch)

    # 预置「落后」缓存：daily last = 20230101（< 真实 today）
    spec = normalize_symbol("600216.SH")
    prov = provider_mod.get_provider()
    eff = prov._factory.get_effective_source(spec.symbol)
    cache = KlineCache()
    stale = pd.DataFrame(
        {
            "date": ["2022-01-01", "2023-01-01"],
            "open": [1.0, 1.0],
            "high": [1.0, 1.0],
            "low": [1.0, 1.0],
            "close": [1.0, 1.0],
        }
    )
    cache.merge(spec, eff, "daily", stale, out)
    cache.merge(spec, eff, "weekly", stale, out)

    today = datetime.date.today().strftime("%Y%m%d")
    data_feed.ensure_data(SYM_DICT, out, mode="update")

    daily = [c for c in captured if c[0] == "daily"]
    assert daily, "日线应触发取数"
    beg, end = daily[0][1], daily[0][2]
    assert beg == "20230102", f"增量起点应为 last+1=20230102，实际 {beg}"
    assert end == today, f"增量终点应为今天 {today}，实际 {end}"


def MagicMock_side_effect_capture(captured: list) -> object:
    from unittest.mock import MagicMock

    def fake(symbol, spec, period, beg, end, lmt):
        captured.append((period, beg, end))
        return pd.DataFrame(
            {
                "date": ["2022-01-01", "2023-01-01"],
                "open": [1.0, 1.0],
                "high": [1.0, 1.0],
                "low": [1.0, 1.0],
                "close": [1.0, 1.0],
            }
        )

    return MagicMock(side_effect=fake)


# ===========================================================================
# T-38 data 批次：不落库 backtest run
# ===========================================================================
def test_submit_data_batch_does_not_create_backtest_run(monkeypatch):
    """submit_data_batch 仅更新数据源：backtest_runs 不新增；batches 写入 data 批次（run_id=NULL）。"""
    monkeypatch.setattr(
        batch_runner, "ensure_data", lambda *a, **k: (Path("d.csv"), Path("w.csv"))
    )
    with temp_db():
        before = _count_backtest_runs()
        items = [{"symbol": "600216.SH", "symbol_name": "浙江医药", "sector_code": None}]
        batch_id, hit_count = batch_runner.submit_data_batch(
            "pool", "ALL", str(Path(mkdtemp())), items
        )
        assert isinstance(hit_count, int)
        _wait_batch_done(batch_id)  # data 批次跑完（ensure_data 已 mock）

        after = _count_backtest_runs()
        assert after == before, (
            f"data 批次不应新增回测 run（before={before}, after={after}）"
        )
        with db.SessionLocal() as s:
            data_batches = s.query(Batch).filter(Batch.batch_type == "data").all()
            assert len(data_batches) == 1, "应有 1 条 data 批次"
            its = s.query(BatchItem).filter(BatchItem.batch_id == batch_id).all()
            assert its and all(it.run_id is None for it in its), (
                "data 批次 item.run_id 应全为 NULL"
            )


# ===========================================================================
# T-40 verify：区间未覆盖抛 DataMissingError
# ===========================================================================
def test_ensure_data_verify_raises_when_missing():
    """ensure_data(mode='verify')：区间未覆盖抛 DataMissingError；已覆盖则不取数、不抛错。"""
    out = Path(mkdtemp())
    spec = normalize_symbol("600216.SH")
    prov = provider_mod.get_provider()
    eff = prov._factory.get_effective_source(spec.symbol)

    # 1) 完全无缓存 → 缺数据 → 抛 DataMissingError
    with pytest.raises(DataMissingError):
        data_feed.ensure_data(
            SYM_DICT, out, mode="verify", required_beg="20220101", required_end="20230101"
        )

    # 2) 区间已覆盖 → 不取数、不抛错，返回 (daily_csv, weekly_csv)
    _write_cover_meta(out, spec, eff, "20220101", "20230101", "20211210", "20260718")
    paths = data_feed.ensure_data(
        SYM_DICT, out, mode="verify", required_beg="20220101", required_end="20230101"
    )
    assert paths and len(paths) == 2, "已覆盖时应返回 (daily_csv, weekly_csv)"


# ===========================================================================
# T-42 清空重跑：clear_strategy_runs 隔离 data 批次
# ===========================================================================
def test_clear_strategy_runs_keeps_data_batch():
    """clear_strategy_runs 删该维度 backtest_runs/equity/trades/summary/batch_items，
    但 batch_type='data' 的 data 批次及其 item（run_id=NULL）不被删。"""
    with temp_db():
        sname = "测试策略A"
        ph = "a" * 40
        src = "eastmoney"
        # 预存 2 个 backtest_runs（含 equity/trades/summary）
        r1 = repository.save_run(
            run_meta(
                {
                    "strategy_name": sname,
                    "params_hash": ph,
                    "symbol": "600216.SH",
                    "symbol_name": "浙江医药",
                    "data_source": src,
                }
            ),
            equity_curve(),
            trades(),
            summary(),
            [],
        )
        r2 = repository.save_run(
            run_meta(
                {
                    "strategy_name": sname,
                    "params_hash": ph,
                    "symbol": "000001.SZ",
                    "symbol_name": "平安银行",
                    "data_source": src,
                }
            ),
            equity_curve(),
            trades(),
            summary(),
            [],
        )
        # 关联 backtest 批次（item.run_id 指向 run）
        bid_bt = repository.create_batch_id()
        repository.create_batch(
            batch_id=bid_bt,
            strategy_name=sname,
            strategy_type="x",
            params_hash=ph,
            scope_type="pool",
            scope_value="ALL",
            total_count=2,
            batch_type="backtest",
        )
        repository.bulk_create_batch_items(
            [
                {
                    "batch_id": bid_bt,
                    "symbol": "600216.SH",
                    "symbol_name": "浙江医药",
                    "sector_code": None,
                    "status": "done",
                    "run_id": r1,
                    "is_reused": False,
                },
                {
                    "batch_id": bid_bt,
                    "symbol": "000001.SZ",
                    "symbol_name": "平安银行",
                    "sector_code": None,
                    "status": "done",
                    "run_id": r2,
                    "is_reused": False,
                },
            ]
        )
        # 关联 data 批次（item.run_id = NULL，不应被删）
        bid_data = repository.create_batch_id()
        repository.create_batch(
            batch_id=bid_data,
            strategy_name="行情更新",
            strategy_type="data",
            params_hash="__data__",
            scope_type="pool",
            scope_value="ALL",
            total_count=2,
            batch_type="data",
        )
        repository.bulk_create_batch_items(
            [
                {
                    "batch_id": bid_data,
                    "symbol": "600216.SH",
                    "symbol_name": "浙江医药",
                    "sector_code": None,
                    "status": "done",
                    "run_id": None,
                    "is_reused": False,
                },
                {
                    "batch_id": bid_data,
                    "symbol": "000001.SZ",
                    "symbol_name": "平安银行",
                    "sector_code": None,
                    "status": "done",
                    "run_id": None,
                    "is_reused": False,
                },
            ]
        )

        deleted = repository.clear_strategy_runs(sname, ph, src)
        assert deleted == 2, f"应删除 2 个 backtest_runs，实际 {deleted}"

        with db.SessionLocal() as s:
            assert s.query(BacktestRun).count() == 0, "backtest_runs 应被清空"
            assert s.query(Trade).count() == 0, "trades 应被级联清空"
            assert s.query(Summary).count() == 0, "summary 应被级联清空"
            # data 批次保留
            data_batches = s.query(Batch).filter(Batch.batch_type == "data").all()
            assert len(data_batches) == 1, "data 批次不应被删"
            data_items = (
                s.query(BatchItem).filter(BatchItem.batch_id == bid_data).all()
            )
            assert len(data_items) == 2 and all(
                it.run_id is None for it in data_items
            ), "data item 不受影响"
            # backtest 批次中命中 run 的 item 被删（双保护生效）
            bt_items = s.query(BatchItem).filter(BatchItem.batch_id == bid_bt).all()
            assert len(bt_items) == 0, "backtest 关联 item 应被清空"


def test_submit_batch_clears_backtest_but_keeps_data_batch(monkeypatch):
    """submit_batch 前置清空同维度 backtest run，但保留既有的 data 批次。"""
    import strategylab.engine.config as config

    monkeypatch.setattr(batch_runner, "run_symbol", _fake_run_symbol)
    monkeypatch.setattr(batch_runner, "WORKERS", 2)
    with temp_db():
        cfg = config.load_strategy_by_arg("kdj_macd_dual_entry")
        ph = repository.compute_params_hash(cfg)
        sname = cfg["name"]
        from strategylab.engine.datasource.config import DataSourceConfig
        from strategylab.engine.datasource.factory import DataSourceFactory

        ds_cfg = DataSourceConfig.from_env()
        eff = DataSourceFactory(ds_cfg).get_effective_source("600216.SH")

        # 预建 data 批次（run_id NULL）
        bid_data = repository.create_batch_id()
        repository.create_batch(
            batch_id=bid_data,
            strategy_name="行情更新",
            strategy_type="data",
            params_hash="__data__",
            scope_type="pool",
            scope_value="ALL",
            total_count=1,
            batch_type="data",
        )
        repository.bulk_create_batch_items(
            [
                {
                    "batch_id": bid_data,
                    "symbol": "600216.SH",
                    "symbol_name": "浙江医药",
                    "sector_code": None,
                    "status": "done",
                    "run_id": None,
                    "is_reused": False,
                }
            ]
        )

        # 预存同维度 backtest_run（提交时会被清空）
        old = repository.save_run(
            run_meta(
                {
                    "strategy_name": sname,
                    "params_hash": ph,
                    "symbol": "600216.SH",
                    "symbol_name": "浙江医药",
                    "data_source": eff,
                }
            ),
            equity_curve(),
            trades(),
            summary(),
            [],
        )

        items = [{"symbol": "600216.SH", "symbol_name": "浙江医药", "sector_code": None}]
        batch_id, _ = batch_runner.submit_batch(
            cfg, sname, ph, "pool", "600216.SH", str(Path(mkdtemp())), items
        )
        _wait_batch_done(batch_id)

        # 旧 backtest_run 被清空
        assert repository.get_run(old) is None, "submit_batch 应清空同维度历史 run"
        # data 批次仍在
        with db.SessionLocal() as s:
            assert (
                s.query(Batch)
                .filter(Batch.batch_id == bid_data, Batch.batch_type == "data")
                .count()
                == 1
            ), "data 批次不应被 submit_batch 清空"


# ===========================================================================
# T-37 双按钮：/api/data/update 路由 + batch_type 列 + 空库迁移
# ===========================================================================
def test_api_data_update_route_uses_data_batch(monkeypatch):
    """POST /api/data/update 路由存在且走 data 批次（mock submit_data_batch 避免联网）。"""
    monkeypatch.setattr(
        batch_runner, "submit_data_batch", lambda *a, **k: ("data-batch-fake-id", 0)
    )
    prev_cwd = os.getcwd()
    os.chdir(PROJECT_ROOT)
    try:
        with temp_db():
            srv = ThreadingHTTPServer(("127.0.0.1", 0), __import__("strategylab.web", fromlist=["Handler"]).Handler)
            port = srv.server_address[1]
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            try:
                base = f"http://127.0.0.1:{port}"
                body = urllib.parse.urlencode(
                    {"scope_type": "pool", "symbols": "600216.SH,000001.SZ"}
                ).encode()
                req = urllib.request.Request(
                    base + "/api/data/update", data=body, method="POST"
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                assert resp.status == 200
                assert data["batch_type"] == "data"
                assert "batch_id" in data and data["batch_id"]
                assert data["total_count"] == 2
            finally:
                srv.shutdown()
                srv.server_close()
                t.join(timeout=5)
    finally:
        os.chdir(prev_cwd)


def test_batch_model_has_batch_type_column():
    """Batch 模型有 batch_type 列（FR-37 数据/回测批次隔离）。"""
    with temp_db():
        engine = db.get_engine()
        cols = [c["name"] for c in inspect(engine).get_columns("batches")]
        assert "batch_type" in cols, "batches 表应有 batch_type 列"


def test_init_db_on_empty_db_creates_batch_type_and_single_version():
    """空库 init_db 可跑通：batches.batch_type 存在，且 schema_version 仅 1 行（当前版本 3）。"""
    with temp_db():
        engine = db.get_engine()
        cols = [c["name"] for c in inspect(engine).get_columns("batches")]
        assert "batch_type" in cols
        with db.SessionLocal() as s:
            vs = s.query(SchemaVersion).all()
            assert len(vs) == 1, f"空库 init_db 应仅 1 行 schema_version，实际 {len(vs)}"
            assert int(vs[0].version) == 3


# ===========================================================================
# T-43 last_buy_date
# ===========================================================================
def test_rank_runs_last_buy_date_is_max_trade_entry():
    """rank_runs：last_buy_date = 该 run trades.entry_date 的 MAX；无交易 run 为 None。"""
    with temp_db():
        sname = "测试策略A"
        ph = "b" * 40
        repository.save_run(
            run_meta(
                {
                    "strategy_name": sname,
                    "params_hash": ph,
                    "symbol": "600216.SH",
                    "symbol_name": "浙江医药",
                }
            ),
            equity_curve(),
            _trades_with(["2023-01-01", "2023-06-01"]),
            summary(),
            [],
        )
        repository.save_run(
            run_meta(
                {
                    "strategy_name": sname,
                    "params_hash": ph,
                    "symbol": "000001.SZ",
                    "symbol_name": "平安银行",
                }
            ),
            equity_curve(),
            _trades_with(["2024-01-01"]),
            summary(),
            [],
        )
        repository.save_run(
            run_meta(
                {
                    "strategy_name": sname,
                    "params_hash": ph,
                    "symbol": "300765.SZ",
                    "symbol_name": "新宙邦",
                }
            ),
            equity_curve(),
            [],
            summary(),
            [],
        )

        res = repository.rank_runs(sname, ph)
        by_sym = {it["symbol"]: it for it in res["items"]}
        assert by_sym["600216.SH"]["last_buy_date"] == "2023-06-01"
        assert by_sym["000001.SZ"]["last_buy_date"] == "2024-01-01"
        assert by_sym["300765.SZ"]["last_buy_date"] is None


def test_rank_last_buy_date_sort_and_none_last():
    """rank_runs sort_by='last_buy_date'：按该列排序，None 统一排末尾；非法字段回退默认。"""
    with temp_db():
        sname = "测试策略A"
        ph = "c" * 40
        repository.save_run(
            run_meta(
                {
                    "strategy_name": sname,
                    "params_hash": ph,
                    "symbol": "600216.SH",
                    "symbol_name": "浙江医药",
                }
            ),
            equity_curve(),
            _trades_with(["2023-06-01"]),
            summary(),
            [],
        )
        repository.save_run(
            run_meta(
                {
                    "strategy_name": sname,
                    "params_hash": ph,
                    "symbol": "000001.SZ",
                    "symbol_name": "平安银行",
                }
            ),
            equity_curve(),
            _trades_with(["2024-01-01"]),
            summary(),
            [],
        )
        repository.save_run(
            run_meta(
                {
                    "strategy_name": sname,
                    "params_hash": ph,
                    "symbol": "300765.SZ",
                    "symbol_name": "新宙邦",
                }
            ),
            equity_curve(),
            [],
            summary(),
            [],
        )  # 无交易 → None

        # desc：2024-01-01, 2023-06-01, None(last)
        res = repository.rank_runs(sname, ph, sort_by="last_buy_date", order="desc")
        dates = [it["last_buy_date"] for it in res["items"]]
        assert dates[0] == "2024-01-01" and dates[1] == "2023-06-01" and dates[2] is None, dates

        # asc：2023-06-01, 2024-01-01, None(last)
        res = repository.rank_runs(sname, ph, sort_by="last_buy_date", order="asc")
        dates = [it["last_buy_date"] for it in res["items"]]
        assert dates[0] == "2023-06-01" and dates[1] == "2024-01-01" and dates[2] is None, dates

        # 非法 sort_by → 回退默认（不崩，返回全部 item）
        res = repository.rank_runs(sname, ph, sort_by="__not_allowed__")
        assert "items" in res and len(res["items"]) == 3


def test_api_rank_last_buy_date_via_web(monkeypatch):
    """/api/rank?sort_by=last_buy_date：返回含 last_buy_date 字段且按该列排序、None 排末尾。"""
    monkeypatch.setattr(
        __import__("strategylab.web", fromlist=["run_backtest"]),
        "run_backtest",
        lambda *a, **k: "<html>ok</html>",
    )
    prev_cwd = os.getcwd()
    os.chdir(PROJECT_ROOT)
    try:
            with temp_db():
                import strategylab.engine.config as config

                cfg = config.load_strategy_by_arg("kdj_macd_dual_entry")
                sname = cfg["name"]  # 必须与 handler 内部据 strategy arg 解析出的 strategy_name 一致
                ph = "d" * 40
                repository.save_run(
                    run_meta(
                        {
                            "strategy_name": sname,
                            "params_hash": ph,
                            "symbol": "600216.SH",
                            "symbol_name": "浙江医药",
                        }
                    ),
                    equity_curve(),
                    _trades_with(["2023-06-01"]),
                    summary(),
                    [],
                )
                repository.save_run(
                    run_meta(
                        {
                            "strategy_name": sname,
                            "params_hash": ph,
                            "symbol": "000001.SZ",
                            "symbol_name": "平安银行",
                        }
                    ),
                    equity_curve(),
                    _trades_with(["2024-01-01"]),
                    summary(),
                    [],
                )
                repository.save_run(
                    run_meta(
                        {
                            "strategy_name": sname,
                            "params_hash": ph,
                            "symbol": "300765.SZ",
                            "symbol_name": "新宙邦",
                        }
                    ),
                    equity_curve(),
                    [],
                    summary(),
                    [],
                )  # None

                web_mod = __import__("strategylab.web", fromlist=["Handler"])
                srv = ThreadingHTTPServer(("127.0.0.1", 0), web_mod.Handler)
                port = srv.server_address[1]
                t = threading.Thread(target=srv.serve_forever, daemon=True)
                t.start()
                try:
                    base = f"http://127.0.0.1:{port}"
                    # /api/rank 的 strategy 参数为策略 arg（如 kdj_macd_dual_entry），
                    # handler 内部据 cfg["name"] 得到 strategy_name 与落库一致。
                    url = (
                        base
                        + "/api/rank?strategy=kdj_macd_dual_entry"
                        + f"&params_hash={ph}&sort_by=last_buy_date&order=desc"
                    )
                    with urllib.request.urlopen(url, timeout=15) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                    items = data["items"]
                    assert len(items) == 3
                    assert all("last_buy_date" in it for it in items), (
                        "排名项应含 last_buy_date 字段"
                    )
                    dates = [it["last_buy_date"] for it in items]
                    assert dates[0] == "2024-01-01" and dates[-1] is None, dates
                finally:
                    srv.shutdown()
                    srv.server_close()
                    t.join(timeout=5)
    finally:
        os.chdir(prev_cwd)
