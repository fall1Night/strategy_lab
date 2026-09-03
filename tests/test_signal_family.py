# -*- coding: utf-8 -*-
"""信号族分类测试（批次A）。"""
from __future__ import annotations

from strategylab.engine.factors.signal_family import (
    FACTOR_ALIAS,
    SIGNAL_FAMILY,
    alias_of,
    category_to_family,
    signal_family_of,
)


class TestSignalFamily:
    def test_known_factors(self):
        assert signal_family_of("sue") == "成长"
        assert signal_family_of("bp") == "价值"
        assert signal_family_of("turnover") == "量价"
        assert signal_family_of("lowvol") == "反转动量"
        assert signal_family_of("f_score") == "质量"

    def test_unknown_falls_back(self):
        assert signal_family_of("unknown_x") == "其他"
        assert signal_family_of("") == "其他"
        assert signal_family_of(None) == "其他"

    def test_rank_suffix_stripped(self):
        assert signal_family_of("sue_rank") == "成长"

    def test_prefix_match(self):
        # sue_x → 前缀命中 sue
        assert signal_family_of("sue_x") == "成长"

    def test_o2c_sum_direct(self):
        # 源表 o2c_sum_20 直接命中"反转动量"
        assert signal_family_of("o2c_sum_20") == "反转动量"

    def test_alias(self):
        assert alias_of("sq_nyoy") == "sue"
        assert alias_of("roe") == "f_score"
        # 无别名原样返回
        assert alias_of("rps_120") == "rps_120"
        assert alias_of("bp_rank") == "bp"

    def test_category_to_family(self):
        assert category_to_family("估值") == "价值"
        assert category_to_family("smart_beta") == "反转动量"
        assert category_to_family("") == ""
        assert category_to_family("不存在的类别") == ""

    def test_registry_consistency(self):
        # 所有别名目标都应有族映射
        for target in FACTOR_ALIAS.values():
            assert signal_family_of(target) != "其他", f"{target} 别名目标无族"
        # 主要族必须都在展示顺序中（允许额外族，如"流动性"）
        from strategylab.engine.factors.signal_family import SIGNAL_FAMILY_ORDER
        for fam in ["价值", "成长", "质量", "量价", "情绪", "反转动量", "资金", "政策", "其他"]:
            assert fam in SIGNAL_FAMILY_ORDER
