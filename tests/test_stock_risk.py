# -*- coding: utf-8 -*-
"""个股风控评分测试（批次B）。"""
from __future__ import annotations

from strategylab.engine.risk.stock_risk import (
    RED_FLAGS,
    check_row,
    risk_level,
    scan,
)

CLEAN = {"roe": 0.15, "gp_margin": 0.45, "current_ratio": 1.8,
         "liability_to_asset": 0.45, "cfo_to_np": 1.0}
DIRTY = {"roe": 0.18, "gp_margin": 0.97, "current_ratio": 0.7,
         "liability_to_asset": 0.78, "cfo_to_np": -0.2}


class TestStockRisk:
    def test_clean_pass(self):
        r = check_row("600519.SH", CLEAN)
        assert r["level"] == "PASS"
        assert r["score"] < 40
        assert r["flags"] == []

    def test_dirty_block(self):
        r = check_row("600519.SH", DIRTY)
        assert r["level"] == "BLOCK"
        assert r["score"] >= 60
        ids = [f["id"] for f in r["flags"]]
        # 高负债/低流动/毛利异常/现金流为负 多红旗叠加
        assert "r2_high_liab" in ids and "r3_low_current" in ids

    def test_no_data_degrades(self):
        r = check_row("600519.SH", None)
        assert r["level"] == "NO_DATA"
        assert r["score"] is None
        # risk_level 把 NO_DATA 当 WATCH（宁严勿松）
        assert risk_level("600519.SH", None) == "WATCH"

    def test_dirty_data_flag(self):
        r = check_row("600519.SH", {"liability_to_asset": 1.8, "gp_margin": 0.5})
        assert r["level"] == "NO_DATA"
        assert any(f["id"] == "dirty_data" for f in r["flags"])

    def test_beneish_high(self):
        r = check_row("600519.SH", CLEAN, m_level="HIGH")
        assert r["score"] >= 30  # R6 高权重
        assert any(f["id"] == "r6_beneish_high" for f in r["flags"])

    def test_beneish_watch(self):
        r = check_row("600519.SH", CLEAN, m_level="WATCH")
        assert any(f["id"] == "r6_beneish_watch" for f in r["flags"])
        assert not any(f["id"] == "r6_beneish_high" for f in r["flags"])

    def test_roe_negative_removes_r4(self):
        # ROE<=0 时豁免 R4（无利润可支撑）
        r = check_row("600519.SH", {"roe": -0.05, "gp_margin": 0.5,
                                    "current_ratio": 1.5, "liability_to_asset": 0.4,
                                    "cfo_to_np": -0.3})
        assert not any(f["id"] == "r4_roe_no_cfo" for f in r["flags"])

    def test_score_capped_100(self):
        r = check_row("600519.SH", {"roe": 0.2, "gp_margin": 0.99, "current_ratio": 0.5,
                                    "liability_to_asset": 0.85, "cfo_to_np": -1.0},
                      m_level="HIGH")
        assert r["score"] <= 100

    def test_scan_stats(self):
        out = scan({"a": CLEAN, "b": DIRTY, "c": None})
        assert out["stats"]["total"] == 3
        assert out["stats"]["PASS"] == 1
        assert out["stats"]["BLOCK"] == 1
        assert out["stats"]["NO_DATA"] == 1
        # 结果按危险度降序：BLOCK 在最前
        assert out["results"][0]["level"] == "BLOCK"

    def test_red_flags_registry(self):
        # 注册表字段完整
        for fid, spec in RED_FLAGS.items():
            assert "weight" in spec and "desc" in spec
            if not fid.startswith("r6_"):
                assert {"col", "op", "val"} <= set(spec)
