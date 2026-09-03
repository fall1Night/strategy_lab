# -*- coding: utf-8 -*-
"""假突破鉴别器测试（批次A，复用源项目 4 场景）。"""
from __future__ import annotations

from strategylab.engine.risk.fake_signal_detector import FakeSignalDetector


class TestFakeSignalDetector:
    def setup_method(self):
        self.det = FakeSignalDetector()

    def test_real_breakout(self):
        r = self.det.assess(break_pct=0.035, vol_ratio=2.2, vol_next_ratio=1.1,
                            upper_shadow_ratio=0.4, sector_sync=5,
                            days_after=5, close_vs_pivot=0.02)
        assert r.verdict == "REAL"
        assert r.score >= 65

    def test_fake_breakout(self):
        r = self.det.assess(break_pct=0.008, vol_ratio=1.6, vol_next_ratio=0.5,
                            upper_shadow_ratio=2.0, sector_sync=1,
                            days_after=4, close_vs_pivot=-0.03)
        assert r.verdict == "FAKE"
        assert r.score <= 40

    def test_suspect(self):
        r = self.det.assess(break_pct=0.025, vol_ratio=1.8, vol_next_ratio=0.8,
                            upper_shadow_ratio=1.8, sector_sync=2, days_after=0)
        assert r.verdict == "SUSPECT"

    def test_micro_break_low_vol(self):
        r = self.det.assess(break_pct=0.005, vol_ratio=1.1, days_after=0)
        assert r.verdict == "FAKE"

    def test_confidence_high(self):
        r = self.det.assess(break_pct=0.035, vol_ratio=2.2, vol_next_ratio=1.1,
                            upper_shadow_ratio=0.4, sector_sync=5,
                            days_after=5, close_vs_pivot=0.02)
        assert r.confidence == "高"

    def test_custom_thresholds(self):
        det = FakeSignalDetector({"effective_break_pct": 0.03})
        r = det.assess(break_pct=0.025, vol_ratio=1.8, days_after=0)
        # 阈值提高后，2.5% 不算有效突破
        assert "有效突破" not in " ".join(r.reasons)
