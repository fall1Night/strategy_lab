# -*- coding: utf-8 -*-
"""独立验收测试：单标的回测明细页 K 线走势图（含成交量副图 + 买卖点标记 + 可缩放交互 + 落库）。

测试策略（严过关 / Edward，QA）：
  - 全程使用受管 venv python（C:\\Users\\Administrator\\.workbuddy\\binaries\\python\\envs\\default）。
  - 严禁触发真实网络取数；DB 相关测试一律用 SQLite 内存库（StaticPool）。
  - 临时文件用 pytest tmp_path，测试结束自动清理，不污染生产数据。

5 组测试（对应验收清单）：
  1) 缓存旧/新格式兼容回归（最重要）——含 save(写) / load(读) 往返，暴露 cache 写路径缺陷。
  2) PricePoint 存储回路 + 零迁移验证（price_points 表被建、backtest_runs 无新列）。
  3) run_symbol 构造 price_curve（不联网：monkeypatch ensure_data/load_bars/get_strategy_class/save_run/export_results）。
4) build_dashboard_data 注入 combo_chart 组合图模块（方案 B：K线+权益合并为一张卡；
   含 ohlc/points/markers/date；全 None volume 不崩；无 price_curve 时回退 overview_chart）。
5) 前端静态核验（render_dashboard 产物含 buildComboChart/缩放控件/组合图卡片；node --check 语法）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.pool import StaticPool

# 让 tests/ 能 import 到 src/strategylab（直接 `python -m pytest` 时也保险）
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from strategylab.engine.datasource.base import SymbolSpec  # noqa: E402
from strategylab.engine.datasource.cache import KlineCache  # noqa: E402
from strategylab.engine.storage.schema import Base, PricePoint, BacktestRun  # noqa: E402
from strategylab.engine.storage.serializers import db_row_to_result  # noqa: E402
from strategylab.engine.vendor.render_dashboard import (  # noqa: E402
    build_dashboard_data,
    render_dashboard,
)

import strategylab.engine.backtest as backtest_mod  # noqa: E402
import strategylab.engine.storage.repository as repository_mod  # noqa: E402
import strategylab.engine.vendor.export_results as export_results_mod  # noqa: E402

NODE_EXE = (
    "C:\\Users\\Administrator\\.workbuddy\\binaries\\node\\versions\\22.22.2\\node.exe"
)

# backtest_runs 设计上的原始列集合（不含任何新增列，如 price_json）
EXPECTED_BACKTEST_RUNS_COLS = {
    "run_id", "batch_id", "strategy_type", "strategy_name", "symbol",
    "symbol_name", "start", "end", "initial_cash", "params_json",
    "positions_json", "meta_json", "created_at", "params_hash", "data_source",
}


# ---------------------------------------------------------------------------
# 测试组 1：缓存旧/新格式兼容回归
# ---------------------------------------------------------------------------
def _spec() -> SymbolSpec:
    return SymbolSpec(symbol="600216.SH", secid="1.600216", prefix="600216_sh")


def test_cache_old_format_read_compat(tmp_path):
    """旧格式缓存（无 vol 列）应能被 load 读回，且 DataFrame 无 vol 列（代码容错），不抛错。

    这是回归防护重点：cache.py 改动后，旧缓存回测不能被破坏。
    """
    spec = _spec()
    source, period = "eastmoney", "daily"
    csv_path = tmp_path / f"{spec.prefix}_{source}_{period}.csv"
    csv_path.write_text(
        "date,open,high,low,close\n"
        "2023-01-02,10.0,10.2,9.8,10.1\n"
        "2023-01-03,10.5,10.7,10.3,10.6\n"
        "2023-01-04,11.0,11.2,10.9,11.1\n",
        encoding="utf-8",
    )
    meta = {
        "beg": "20230101", "end": "20231231", "rows": 3,
        "first": "20230101", "last": "20231231",
        "version": 2, "source": source,
    }
    (tmp_path / f"{spec.prefix}_{source}_{period}_meta.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )

    out = KlineCache().load(spec, source, period, "20230101", "20231231", tmp_path)
    assert out is not None, "旧格式缓存应被 load 命中读回"
    # 旧格式无 vol 列：读回的 DataFrame 不应含 vol 列（代码按 'vol' in columns 兜底）
    assert "vol" not in out.columns, "旧格式缓存读回不应出现 vol 列"
    assert list(out["date"]) == [
        pd.Timestamp("2023-01-02"), pd.Timestamp("2023-01-03"), pd.Timestamp("2023-01-04")
    ]
    assert abs(float(out.loc[0, "open"]) - 10.0) < 1e-9
    assert abs(float(out.loc[2, "close"]) - 11.1) < 1e-9


def test_cache_new_format_read_manual(tmp_path):
    """新格式缓存（含 vol，部分 vol 为空串）手动写入后，load 读回应：vol 为 float，空串→NaN，OHLC 正确。"""
    spec = _spec()
    source, period = "eastmoney", "daily"
    csv_path = tmp_path / f"{spec.prefix}_{source}_{period}.csv"
    csv_path.write_text(
        "date,open,high,low,close,vol\n"
        "2023-01-02,10.0,10.2,9.8,10.1,1000.0\n"
        "2023-01-03,10.5,10.7,10.3,10.6,\n"          # 空串 → NaN
        "2023-01-04,11.0,11.2,10.9,11.1,2000.0\n",
        encoding="utf-8",
    )
    meta = {
        "beg": "20230101", "end": "20231231", "rows": 3,
        "first": "20230101", "last": "20231231",
        "version": 3, "source": source,
    }
    (tmp_path / f"{spec.prefix}_{source}_{period}_meta.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )

    out = KlineCache().load(spec, source, period, "20230101", "20231231", tmp_path)
    assert out is not None
    assert "vol" in out.columns, "新格式缓存应含 vol 列"
    # 非空 vol 为 float
    assert abs(float(out.loc[0, "vol"]) - 1000.0) < 1e-6
    assert abs(float(out.loc[2, "vol"]) - 2000.0) < 1e-6
    # 空串读回为 NaN（与 daily 缺 vol 时 NaN 一致，供 run_symbol 容错为 None）
    assert pd.isna(out.loc[1, "vol"])
    # OHLC 正确
    assert abs(float(out.loc[0, "open"]) - 10.0) < 1e-9
    assert abs(float(out.loc[2, "close"]) - 11.1) < 1e-9


def test_cache_save_then_load_roundtrip_new_format(tmp_path):
    """新格式经 save 写入后应能被 load 读回，且 OHLC/vol 数值正确。

    ⚠️ 该用例用于暴露 cache 写路径缺陷：当前 KlineCache.save 在写 meta 阶段对字符串
    化的 date 列调用 .strftime() 会抛 AttributeError（见 QA 报告 Route: Engineer）。
    """
    spec = _spec()
    source, period = "eastmoney", "daily"
    cache = KlineCache()
    df = pd.DataFrame({
        "date": ["2023-01-02", "2023-01-03", "2023-01-04"],
        "open": [10.0, 10.5, 11.0],
        "high": [10.2, 10.7, 11.2],
        "low": [9.8, 10.3, 10.9],
        "close": [10.1, 10.6, 11.1],
        "vol": [1000.0, float("nan"), 2000.0],
    })
    cache.save(spec, source, period, df, "20230101", "20231231", tmp_path)
    out = cache.load(spec, source, period, "20230101", "20231231", tmp_path)
    assert out is not None
    assert "vol" in out.columns
    assert abs(float(out.loc[0, "vol"]) - 1000.0) < 1e-6
    assert abs(float(out.loc[2, "vol"]) - 2000.0) < 1e-6
    assert abs(float(out.loc[0, "open"]) - 10.0) < 1e-9


# ---------------------------------------------------------------------------
# 测试组 2：PricePoint 存储回路 + 零迁移验证
# ---------------------------------------------------------------------------
def test_pricepoint_storage_roundtrip_and_zero_migration():
    """PricePoint 子表应被 create_all 建出（零迁移）；手动 bulk_insert 后读回按 date 升序、volume=None 保持 None；backtest_runs 未被加列。"""
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(eng)
    inspector = inspect(eng)

    # 1) price_points 表被创建（无需任何手写迁移）
    assert "price_points" in inspector.get_table_names(), "PricePoint 子表应被自动建出（零迁移）"

    # 2) 手动写入（含一条 volume=None），顺序故意乱序以验证按 date 排序
    run_id = "qa-run-price-001"
    with Session(eng) as s:
        s.add(BacktestRun(
            run_id=run_id, batch_id="b1", strategy_type="t", strategy_name="n",
            symbol="600216.SH", symbol_name="浙江医药",
            start=date(2023, 1, 1), end=date(2024, 1, 1),
            initial_cash=1_000_000.0, params_json="{}",
        ))
        s.flush()
        s.bulk_insert_mappings(PricePoint, [
            {"run_id": run_id, "date": date(2023, 1, 3), "open": 11.0, "high": 11.2,
             "low": 10.9, "close": 11.1, "volume": 3000.0},
            {"run_id": run_id, "date": date(2023, 1, 1), "open": 10.0, "high": 10.2,
             "low": 9.8, "close": 10.1, "volume": None},  # 旧缓存无成交量
            {"run_id": run_id, "date": date(2023, 1, 2), "open": 10.5, "high": 10.7,
             "low": 10.3, "close": 10.6, "volume": 2000.0},
        ])
        s.commit()

    # 3) 用与 db_row_to_result 等价的 ORM 查询 + 序列化读回
    with Session(eng) as s:
        run = (
            s.query(BacktestRun)
            .options(joinedload(BacktestRun.price))
            .filter(BacktestRun.run_id == run_id)
            .first()
        )
        result = db_row_to_result(run)
        price_curve = result["price_curve"]

    assert len(price_curve) == 3, "应读回 3 条价格点"
    # 按 date 升序
    dates = [p["date"] for p in price_curve]
    assert dates == sorted(dates), f"price_curve 应按 date 升序，实际 {dates}"
    # 字段齐全
    for p in price_curve:
        assert set(p.keys()) >= {"date", "open", "high", "low", "close", "volume"}
    # volume=None 那条保持 None（旧缓存兼容）
    assert price_curve[0]["date"] == "2023-01-01"
    assert price_curve[0]["volume"] is None, "volume=None 应保持 None（旧缓存兼容）"
    assert abs(price_curve[1]["volume"] - 2000.0) < 1e-9
    assert abs(price_curve[2]["volume"] - 3000.0) < 1e-9

    # 4) 零迁移验证：backtest_runs 表未被加任何新列（如 price_json）
    cols = {c["name"] for c in inspector.get_columns("backtest_runs")}
    assert cols == EXPECTED_BACKTEST_RUNS_COLS, (
        f"backtest_runs 列集合应与原设计一致（零迁移），差异：{cols ^ EXPECTED_BACKTEST_RUNS_COLS}"
    )
    assert "price_json" not in cols, "backtest_runs 不应新增 price_json 之类的列"


# ---------------------------------------------------------------------------
# 测试组 3：run_symbol 构造 price_curve（不联网）
# ---------------------------------------------------------------------------
class _FakeStrategy:
    def __init__(self, cfg):
        self.cfg = cfg

    def run(self, daily, weekly, start, end, symbol=None, symbol_name=None):
        return {
            "equity_curve": [
                {"date": "2023-01-03", "value": 1_000_000.0},
                {"date": "2023-01-08", "value": 1_010_000.0},
            ],
            "trade_history": [{
                "entry_date": "2023-01-03", "exit_date": "2023-01-06", "side": "long",
                "size": 100, "entry_price": 10.5, "exit_price": 11.6, "pnl": 110.0,
                "pnl_pct": 1.0, "holding_bars": 3, "symbol": "600216.SH",
                "symbol_name": "浙江医药", "display_symbol": "浙江医药", "label": "建仓",
            }],
            "positions": [],
        }


def test_run_symbol_builds_price_curve_no_network(tmp_path):
    """run_symbol 在 [start,end] 窗口构造 price_curve：仅含窗口内行、字段含 volume、NaN vol→None、按日期升序；且不触发真实网络/落库。"""
    daily = pd.DataFrame({
        "date": pd.to_datetime([
            "2023-01-02", "2023-01-03", "2023-01-04", "2023-01-05",
            "2023-01-06", "2023-01-09", "2023-01-10",
        ]),
        "open": [10.0, 10.5, 11.0, 11.5, 12.0, 12.5, 13.0],
        "high": [10.2, 10.7, 11.2, 11.7, 12.2, 12.7, 13.2],
        "low": [9.8, 10.3, 10.9, 11.3, 11.8, 12.3, 12.8],
        "close": [10.1, 10.6, 11.1, 11.6, 12.1, 12.6, 13.1],
        # 2023-01-04 的 vol 缺失（NaN），应被容错为 None
        "vol": [1000.0, 2000.0, float("nan"), 3000.0, 4000.0, 5000.0, 6000.0],
    })

    strategy_cfg = {
        "type": "fake_kline_strategy",
        "name": "测试K线策略",
        "market": "china_a",
        "params": {"initial_cash": 1_000_000.0},
    }
    captured = {}

    def fake_save_run(run_meta, equity_curve, trade_history, summary,
                      positions=None, price_curve=None):
        captured["price_curve"] = price_curve
        captured["called"] = True
        return "fake-run-id-001"

    def fake_export_results(*a, **k):
        return {
            "summary_data": {"total_return_pct": 1.0},
            "meta": {"strategy_name": "测试K线策略", "market": "china_a"},
        }

    out_dir = tmp_path / "out"
    with patch.object(backtest_mod.data_feed, "ensure_data",
                      return_value=(out_dir / "d.csv", out_dir / "w.csv")), \
         patch.object(backtest_mod.data_feed, "load_bars", return_value=daily), \
         patch.object(backtest_mod, "get_strategy_class", return_value=_FakeStrategy), \
         patch.object(repository_mod, "save_run", side_effect=fake_save_run), \
         patch.object(export_results_mod, "export_results", side_effect=fake_export_results):
        result = backtest_mod.run_symbol(
            strategy_cfg, "600216.SH", "浙江医药",
            "2023-01-03", "2023-01-08", str(out_dir), data_source="eastmoney",
        )

    price_curve = result["price_curve"]
    # 窗口 [2023-01-03, 2023-01-08] 内应只有 01-03/01-04/01-05/01-06（01-02 之前、01-09/01-10 之后均排除）
    assert len(price_curve) == 4, f"price_curve 应仅含窗口内 4 行，实际 {len(price_curve)}"
    dates = [p["date"] for p in price_curve]
    assert dates == sorted(dates), f"price_curve 应按日期升序，实际 {dates}"
    assert dates == ["2023-01-03", "2023-01-04", "2023-01-05", "2023-01-06"]
    for p in price_curve:
        assert "volume" in p, "price_curve 每项应含 volume 字段"
    # 2023-01-04 的 NaN vol → None
    assert price_curve[1]["date"] == "2023-01-04"
    assert price_curve[1]["volume"] is None, "NaN vol 应被容错为 None"
    assert abs(price_curve[0]["volume"] - 2000.0) < 1e-9
    # save_run 收到的 price_curve 与返回值一致（落库透传正确）
    assert captured.get("called") is True
    assert captured.get("price_curve") == price_curve
    assert result["run_id"] == "fake-run-id-001"


# ---------------------------------------------------------------------------
# 测试组 4：build_dashboard_data 注入 combo_chart 组合图模块（方案 B）
# ---------------------------------------------------------------------------
def _sample_equity():
    return [
        {"date": "2023-01-03", "value": 1_000_000.0},
        {"date": "2023-01-08", "value": 1_010_000.0},
    ]


def _sample_trades():
    return [{
        "entry_date": "2023-01-03", "exit_date": "2023-01-06", "side": "long",
        "size": 100, "entry_price": 10.5, "exit_price": 11.6, "pnl": 110.0,
        "pnl_pct": 1.0, "holding_bars": 3, "symbol": "600216.SH",
        "symbol_name": "浙江医药", "display_symbol": "浙江医药", "label": "建仓",
    }]


def _sample_summary():
    return {
        "total_return_pct": 1.0, "annual_return_pct": 1.0, "max_drawdown_pct": -1.0,
        "sharpe": 1.2, "win_rate_pct": 100.0, "total_trades": 1,
    }


def _sample_price_curve():
    return [
        {"date": "2023-01-03", "open": 10.5, "high": 10.7, "low": 10.3, "close": 10.6, "volume": 2000.0},
        {"date": "2023-01-04", "open": 11.0, "high": 11.2, "low": 10.9, "close": 11.1, "volume": 3000.0},
        {"date": "2023-01-05", "open": 11.5, "high": 11.7, "low": 11.3, "close": 11.6, "volume": 4000.0},
    ]


def test_build_dashboard_data_injects_combo_chart():
    """方案 B：price_curve 非空时，modules 首项为 combo_chart（不再下发独立 overview_chart/price_chart）。

    combo_chart 应含 ohlc（date/open/high/low/close/volume）、points（date/equity/drawdown_abs/pnl）、
    markers（同时含 buy 与 sell）、toggles/modes；且整份 modules 不含 overview_chart / price_chart。
    """
    rd = build_dashboard_data(
        equity_curve=_sample_equity(),
        trade_history=_sample_trades(),
        summary=_sample_summary(),
        meta={"strategy_name": "测试策略", "market": "china_a"},
        language="zh", market="china_a",
        price_curve=_sample_price_curve(),
    )
    modules = rd.get("modules", [])
    # 首模块即 combo_chart
    assert modules[0]["type"] == "combo_chart", f"首模块应为 combo_chart，实际 {modules[0].get('type')}"
    cm = modules[0]
    # 不再同时下发独立卡
    types = {m.get("type") for m in modules}
    assert "overview_chart" not in types, "方案 B 后不应再含 overview_chart 独立卡"
    assert "price_chart" not in types, "方案 B 后不应再含 price_chart 独立卡"
    # ohlc 长度与 price_curve 一致，且每项带 date/volume
    assert len(cm["ohlc"]) == len(_sample_price_curve()), "ohlc 长度应与 price_curve 一致"
    assert all({"date", "volume"} <= set(o.keys()) for o in cm["ohlc"]), "ohlc 每项应含 date/volume"
    # points 带 date/equity
    assert cm["points"], "combo_chart 应含 points"
    assert all({"date", "equity"} <= set(p.keys()) for p in cm["points"]), "points 每项应含 date/equity"
    # markers 同时含 buy 与 sell
    actions = {mk.get("action") for mk in cm.get("markers", [])}
    assert "buy" in actions, "markers 应含 buy"
    assert "sell" in actions, "markers 应含 sell"
    # 组合图卡片带控制项
    assert cm.get("toggles"), "combo_chart 应含 toggles"
    assert cm.get("modes"), "combo_chart 应含 modes"


def test_build_dashboard_data_combo_chart_all_none_volume():
    """price_curve 全 None volume（旧缓存兼容）仍产出 combo_chart 模块且不抛错。"""
    pc = [dict(d, volume=None) for d in _sample_price_curve()]
    rd = build_dashboard_data(
        equity_curve=_sample_equity(),
        trade_history=_sample_trades(),
        summary=_sample_summary(),
        meta={"strategy_name": "测试策略", "market": "china_a"},
        language="zh", market="china_a",
        price_curve=pc,
    )
    modules = rd.get("modules", [])
    assert modules[0]["type"] == "combo_chart", "全 None volume 仍应产出 combo_chart 模块"
    assert all(o["volume"] is None for o in modules[0]["ohlc"])


def test_build_dashboard_data_empty_price_curve_falls_back_overview_chart():
    """price_curve 为空时不应出现 combo_chart（兼容路径）：首模块回退为原 overview_chart，行为不变。"""
    rd = build_dashboard_data(
        equity_curve=_sample_equity(),
        trade_history=_sample_trades(),
        summary=_sample_summary(),
        meta={"strategy_name": "测试策略", "market": "china_a"},
        language="zh", market="china_a",
        price_curve=[],
    )
    modules = rd.get("modules", [])
    types = {m.get("type") for m in modules}
    assert "combo_chart" not in types, "空 price_curve 不应注入 combo_chart 模块"
    assert modules[0]["type"] == "overview_chart", "空 price_curve 应回退首模块为 overview_chart"


def test_build_dashboard_data_event_study_no_combo_chart():
    """事件研究路径（event_study）即使传入 price_curve 也不应产生 combo_chart/price_chart：行为不变。

    事件研究由 _build_default_modules 转发到 _build_event_modules，该路径仅产出
    overview_chart（timeline/both 模式）+ metric_table + trades_table；方案 B 的
    combo_chart 只针对普通单标的详情页。
    """
    rd = build_dashboard_data(
        equity_curve=_sample_equity(),
        trade_history=_sample_trades(),
        # summary 含 avg_return_pct 等事件研究指标 → _detect_report_kind 判定 event_study
        summary={
            "total_return_pct": 1.0, "avg_return_pct": 0.5, "median_return_pct": 0.5,
            "best_trade_pct": 1.0, "worst_trade_pct": 0.0, "total_trades": 1,
        },
        meta={"strategy_name": "测试策略", "market": "china_a"},
        language="zh", market="china_a",
        event_overview_mode="timeline",
        price_curve=_sample_price_curve(),
    )
    modules = rd.get("modules", [])
    types = {m.get("type") for m in modules}
    assert "combo_chart" not in types, "事件研究路径不应产生 combo_chart"
    assert "price_chart" not in types, "事件研究路径不应产生 price_chart"
    assert "overview_chart" in types, "事件研究 timeline 模式应保留原 overview_chart"
    assert {"metric_table", "trades_table"} <= types, "事件研究路径应保留 metric_table/trades_table"


# ---------------------------------------------------------------------------
# 测试组 5：前端静态核验
# ---------------------------------------------------------------------------
def _extract_js_blocks(html_text: str) -> list[str]:
    """提取 HTML 中所有内联 <script> 内容（排除 application/json 与外部 src 脚本）。"""
    blocks = []
    pattern = re.compile(r"<script\b([^>]*)>(.*?)</script>", re.IGNORECASE | re.DOTALL)
    for m in pattern.finditer(html_text):
        attrs, body = m.group(1), m.group(2)
        if "application/json" in attrs.lower():
            continue
        if re.search(r"\bsrc\s*=", attrs):
            continue
        if body.strip():
            blocks.append(body)
    return blocks


def _node_check(js_text: str) -> tuple[int, str]:
    fd, path = tempfile.mkstemp(suffix=".js")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(js_text)
        proc = subprocess.run(
            [NODE_EXE, "--check", path], capture_output=True, text=True, timeout=60
        )
        return proc.returncode, proc.stderr
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def test_render_dashboard_contains_combo_chart_and_zoom(tmp_path):
    """render_dashboard 产物应含 buildComboChart / 组合图卡片 / 缩放控件；并 node --check 模板 JS 语法。"""
    rd = build_dashboard_data(
        equity_curve=_sample_equity(),
        trade_history=_sample_trades(),
        summary=_sample_summary(),
        meta={"strategy_name": "测试策略", "market": "china_a"},
        language="zh", market="china_a",
        price_curve=_sample_price_curve(),
    )
    out_html = tmp_path / "dash.html"
    render_dashboard(rd, output_path=out_html)
    html_text = out_html.read_text(encoding="utf-8")

    required_tokens = [
        "buildComboChart", "combo_chart", "price-reset-btn", "range-slider", "MIN_SPAN",
        '"ohlc"', '"points"',
    ]
    for tok in required_tokens:
        assert tok in html_text, f"渲染产物缺失关键 token: {tok}"

    # 提取 JS 做语法检查
    blocks = _extract_js_blocks(html_text)
    assert blocks, "应能提取到内联 <script> 块"
    main_block = next((b for b in blocks if "function buildComboChart" in b), blocks[0])
    rc, err = _node_check(main_block)
    assert rc == 0, f"模板 JS 语法检查失败 (node --check):\n{err}"

    # buildComboChart 函数体内应绑定整图缩放交互监听（wheel / mousedown / input / click）
    m = re.search(r"function buildComboChart\(.*?(?=\n    function )", main_block, re.DOTALL)
    region = m.group(0) if m else main_block
    for tok in ("wheel", "mousedown", "addEventListener", "input"):
        assert tok in region, f"buildComboChart 缩放交互区域缺失监听 token: {tok}"
