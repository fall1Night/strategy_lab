# -*- coding: utf-8 -*-
"""数据审计闸门测试（批次B）。"""
from __future__ import annotations

import numpy as np
import pandas as pd

from strategylab.engine.risk.data_audit import DataAuditor


def _bars(n_days: int = 300, seed: int = 1) -> dict:
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2023-01-01", periods=n_days, freq="B")
    close = 10 + np.cumsum(rng.normal(0, 0.1, n_days))
    df = pd.DataFrame({
        "date": dates,
        "open": close * 0.99,
        "high": close * 1.02,
        "low": close * 0.98,
        "close": close,
        "vol": rng.integers(100000, 1000000, n_days),
    })
    return {"000001.SZ": df}


class TestDataAuditor:
    def test_clean_data_pass(self):
        r = DataAuditor().run(_bars())
        assert r["gate"] is True
        assert r["summary"]["FAIL"] == 0

    def test_empty_bars_fail(self):
        r = DataAuditor().run({})
        assert r["gate"] is False
        ids = [i["id"] for i in r["items"] if i["status"] == "FAIL"]
        assert "A1" in ids

    def test_ohlc_violation(self):
        bars = _bars()
        bars["000001.SZ"].loc[0, "low"] = -1  # 负价格
        r = DataAuditor({"thresholds": {"ohlc_violate_warn_max": 0}}).run(bars)
        assert r["gate"] is False
        assert any(i["id"] == "B1" and i["status"] == "FAIL" for i in r["items"])

    def test_price_limit_warn(self):
        bars = _bars()
        bars["000001.SZ"].loc[10, "close"] = bars["000001.SZ"].loc[9, "close"] * 1.5  # +50%
        r = DataAuditor().run(bars)
        assert any(i["id"] == "C1" and i["status"] == "WARN" for i in r["items"])
        # 涨跌幅超限只 WARN 不 FAIL
        assert r["gate"] is True

    def test_missing_volume_warn(self):
        bars = _bars()
        bars["000001.SZ"] = bars["000001.SZ"].drop(columns=["vol"])
        r = DataAuditor().run(bars)
        assert any(i["id"] == "D1" and i["status"] == "WARN" for i in r["items"])

    def test_short_history_warn(self):
        bars = {"000001.SZ": _bars(n_days=30)["000001.SZ"]}
        r = DataAuditor().run(bars)
        assert any(i["id"] == "A1" and i["status"] == "WARN" for i in r["items"])

    def test_duplicate_dates(self):
        bars = _bars()
        dup = bars["000001.SZ"].iloc[[0, 0]]
        bars["000001.SZ"] = pd.concat([dup, bars["000001.SZ"]])
        r = DataAuditor({"thresholds": {"dup_date_warn_max": 0}}).run(bars)
        assert any(i["id"] == "F1" and i["status"] == "FAIL" for i in r["items"])

    def test_null_fields(self):
        bars = _bars()
        bars["000001.SZ"].loc[1:10, "close"] = np.nan
        r = DataAuditor({"thresholds": {"null_pct_max": 1.0}}).run(bars)
        assert any(i["id"] == "E1" and i["status"] == "FAIL" for i in r["items"])

    def test_report_written(self, tmp_path):
        r = DataAuditor().run(_bars(), out_dir=tmp_path)
        assert (tmp_path / "data_audit_report.md").exists()
        assert (tmp_path / "data_audit_report.json").exists()
        assert r["gate"] is True

    def test_string_date_input(self):
        bars = _bars()
        bars["000001.SZ"]["date"] = bars["000001.SZ"]["date"].astype(str)
        r = DataAuditor().run(bars)
        assert r["gate"] is True
