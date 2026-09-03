# -*- coding: utf-8 -*-
"""模拟盘测试（批次C）。"""
from __future__ import annotations

import pandas as pd

from strategylab.engine.paper.paper_account import PaperAccount


class TestPaperAccount:
    def test_buy_sell_nav_cycle(self, tmp_path):
        db = tmp_path / "paper.db"
        acc = PaperAccount("test", cash=100_000, db_path=db)
        r1 = acc.buy("600519.SH", 100, "2026-08-06", close=1572.0, reason="突破买入")
        assert r1["ok"] is True
        r2 = acc.buy("000858.SZ", 1000, "2026-08-06", close=157.5, reason="突破买入")
        assert r2["ok"] is True
        # 市值更新
        v1 = acc.mark_to_market({"600519.SH": 1600.0, "000858.SZ": 155.0}, "2026-08-07")
        assert v1 > 100_000
        # 卖出半仓
        r3 = acc.sell("600519.SH", 50, "2026-08-10", close=1610.0, reason="止盈一半")
        assert r3["ok"] is True
        v2 = acc.mark_to_market({"600519.SH": 1610.0, "000858.SZ": 158.0}, "2026-08-10")
        assert v2 > v1
        # 查询
        assert len(acc.positions()) == 2
        assert len(acc.orders()) == 3
        assert len(acc.equity_curve()) == 2
        snap = acc.snapshot()
        assert snap["n_positions"] == 2

    def test_idempotent_buy(self, tmp_path):
        acc = PaperAccount("t", cash=100_000, db_path=tmp_path / "p.db")
        assert acc.buy("600519.SH", 100, "2026-08-06", close=10.0)["ok"] is True
        r = acc.buy("600519.SH", 100, "2026-08-06", close=10.0)  # 同日重复
        assert r["ok"] is False
        assert r["msg"] == "重复下单"

    def test_insufficient_cash_auto_shrink(self, tmp_path):
        # 源逻辑：现金不足时自动缩量（仍可买 1 股以上则成交）
        acc = PaperAccount("t", cash=1000, db_path=tmp_path / "p.db")
        r = acc.buy("600519.SH", 1000, "2026-08-06", close=100.0)  # 全额需 10 万
        assert r["ok"] is True
        assert r["qty"] < 1000  # 缩量成交

    def test_cash_below_one_share_rejects(self, tmp_path):
        # 现金连 1 股都买不起 → 拒绝
        acc = PaperAccount("t", cash=50, db_path=tmp_path / "p.db")
        r = acc.buy("600519.SH", 100, "2026-08-06", close=100.0)
        assert r["ok"] is False
        assert r["msg"] == "现金不足"

    def test_sell_more_than_holding(self, tmp_path):
        acc = PaperAccount("t", cash=100_000, db_path=tmp_path / "p.db")
        acc.buy("600519.SH", 10, "2026-08-06", close=10.0)
        r = acc.sell("600519.SH", 20, "2026-08-10", close=11.0)
        assert r["ok"] is False
        assert r["msg"] == "持仓不足"

    def test_sell_all(self, tmp_path):
        acc = PaperAccount("t", cash=100_000, db_path=tmp_path / "p.db")
        acc.buy("600519.SH", 10, "2026-08-06", close=10.0)
        r = acc.sell_all("600519.SH", "2026-08-10", close=11.0)
        assert r["ok"] is True
        assert acc.positions() == []

    def test_mark_to_market_missing_price(self, tmp_path):
        acc = PaperAccount("t", cash=100_000, db_path=tmp_path / "p.db")
        acc.buy("600519.SH", 10, "2026-08-06", close=10.0)
        v = acc.mark_to_market({}, "2026-08-07")  # 无价 → 用成本近似
        assert v > 0

    def test_db_path_required(self):
        try:
            PaperAccount("t")
            assert False, "应要求显式 db_path"
        except ValueError:
            pass

    def test_fee_charged(self, tmp_path):
        acc = PaperAccount("t", cash=100_000, db_path=tmp_path / "p.db")
        acc.buy("600519.SH", 100, "2026-08-06", close=100.0)
        # 佣金 = 100*100*0.00026 = 2.6
        orders = acc.orders()
        assert abs(orders[0]["fee"] - 2.6) < 1e-9

    def test_restart_persists(self, tmp_path):
        db = tmp_path / "p.db"
        acc1 = PaperAccount("t", cash=100_000, db_path=db)
        acc1.buy("600519.SH", 10, "2026-08-06", close=10.0)
        # 重新打开同一 db：持仓仍在
        acc2 = PaperAccount("t", cash=100_000, db_path=db)
        assert len(acc2.positions()) == 1
        assert acc2.positions()[0]["code"] == "600519.SH"
