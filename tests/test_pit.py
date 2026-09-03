# -*- coding: utf-8 -*-
"""PIT 披露延迟测试（批次A）。"""
from __future__ import annotations

from strategylab.engine.factors.pit import DISCLOSE_LAG, available_date


class TestPit:
    def test_q1(self):
        assert available_date("2024-03-31") == "2024-04-30"

    def test_h1(self):
        assert available_date("2024-06-30") == "2024-08-31"

    def test_q3(self):
        assert available_date("2024-09-30") == "2024-10-31"

    def test_annual_crosses_year(self):
        # 年报延迟到次年 4-30
        assert available_date("2024-12-31") == "2025-04-30"

    def test_invalid_returns_unchanged(self):
        assert available_date("bad") == "bad"
        assert available_date("") == ""

    def test_lag_table_complete(self):
        assert DISCLOSE_LAG == {3: "04-30", 6: "08-31", 9: "10-31", 12: "04-30"}
