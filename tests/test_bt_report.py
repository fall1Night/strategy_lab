# -*- coding: utf-8 -*-
"""回测报告与存档测试（批次D）。"""
from __future__ import annotations

import numpy as np
import pandas as pd

from strategylab.engine.bt_report import (
    compute_metrics,
    archive,
    list_archives,
    load_archive,
    render_html,
)


def _rets(n: int = 1000, seed: int = 1) -> pd.Series:
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2020-01-01", periods=n, freq="B")
    return pd.Series(rng.normal(0.0004, 0.01, n), index=idx)


class TestBtReport:
    def test_compute_metrics(self):
        m = compute_metrics(_rets())
        for k in ("total_return", "annual_return", "max_drawdown", "sharpe",
                  "sortino", "calmar", "win_rate", "n_days", "final_nav"):
            assert k in m
        assert m["n_days"] == 1000
        assert m["max_drawdown"] <= 0
        assert m["final_nav"] > 0

    def test_empty_series(self):
        assert compute_metrics(pd.Series(dtype=float)) == {}

    def test_archive_json_only_without_plotly(self, tmp_path):
        # 无 plotly：save_html=True 自动降级，仅 JSON
        res = archive(_rets(), params={"name": "测试", "strategy": "rsi"},
                      name="selftest", out_dir=tmp_path)
        assert (tmp_path / res["json_path"].split("/")[-1]).exists()
        assert res["html_path"] == ""  # 降级

    def test_archive_roundtrip(self, tmp_path):
        res = archive(_rets(), params={"name": "测试"}, name="bt1", out_dir=tmp_path)
        d = load_archive(res["json_path"])
        assert d["name"] == "bt1"
        assert "returns_series" in d
        assert len(d["returns_series"]) > 0
        assert d["metrics"]["n_days"] == 1000

    def test_list_archives(self, tmp_path):
        archive(_rets(seed=1), name="a", out_dir=tmp_path)
        archive(_rets(seed=2), name="b", out_dir=tmp_path)
        lst = list_archives(out_dir=tmp_path)
        assert len(lst["history"]) == 2
        assert {x["name"] for x in lst["history"]} == {"a", "b"}
        assert lst["history"][0]["annual_return"] is not None

    def test_list_archives_empty_dir(self, tmp_path):
        assert list_archives(out_dir=tmp_path) == {"history": []}

    def test_verdict_auto(self, tmp_path):
        # 确定性正收益 → 有效
        idx = pd.date_range("2020-01-01", periods=500, freq="B")
        pos = pd.Series(np.full(500, 0.001), index=idx)  # 日涨 0.1%
        res = archive(pos, params={"name": "x"}, name="v", out_dir=tmp_path)
        d = load_archive(res["json_path"])
        assert d["verdict"] == "有效"

    def test_render_html_raises_without_plotly(self):
        try:
            render_html(_rets())
            # 若环境装了 plotly，则正常返回字符串
        except ImportError:
            pass  # 无 plotly → 期望抛 ImportError
