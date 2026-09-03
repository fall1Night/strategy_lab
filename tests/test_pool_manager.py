# -*- coding: utf-8 -*-
"""三池状态机测试（批次C）。"""
from __future__ import annotations

import pandas as pd

from strategylab.engine.paper.pool_manager import PoolManager

RANK = pd.DataFrame({
    "code": ["600519.SH", "000858.SZ", "002252.SZ"],
    "综合分": [0.9, 0.85, 0.80],
})


class TestPoolManager:
    def test_candidate_to_holding_watch(self, tmp_path):
        pm = PoolManager(db_path=tmp_path / "pools.db")
        pm.update_pools(RANK, buy_signals={"000858.SZ": "VCP突破"}, date="2026-08-06")
        pools = pm.all()
        assert pools["000858.SZ"]["pool"] == "holding"      # 有买点 → 持有
        assert pools["600519.SH"]["pool"] == "watch"        # 无买点 → 观察
        assert "观察" in pm.report() or "候选" in pm.report()

    def test_sell_moves_to_history(self, tmp_path):
        pm = PoolManager(db_path=tmp_path / "pools.db")
        pm.update_pools(RANK, buy_signals={"000858.SZ": "VCP突破"}, date="2026-08-06")
        pm.update_pools(RANK, holdings_out={"000858.SZ": "硬止损-7%"}, date="2026-08-10")
        assert pm.all()["000858.SZ"]["pool"] == "history"
        assert pm.all()["000858.SZ"]["exit_reason"] == "硬止损-7%"

    def test_watch_timeout_degrades(self, tmp_path):
        pm = PoolManager(db_path=tmp_path / "pools.db")
        pm.update_pools(RANK, date="2026-01-01")  # 全部进观察池
        # 超过 8 周后更新
        pm.update_pools(RANK, date="2026-04-01")
        pools = pm.all()
        for code in RANK["code"]:
            assert pools[code]["pool"] == "history"
            assert pools[code]["exit_reason"] == "观察超时降级"

    def test_sold_today_not_recycled(self, tmp_path):
        pm = PoolManager(db_path=tmp_path / "pools.db")
        pm.update_pools(RANK, buy_signals={c: "买点" for c in RANK["code"]}, date="2026-08-06")
        # 同日卖出 Top 1 → 不应回到候选池
        pm.update_pools(RANK, holdings_out={"600519.SH": "止盈"}, date="2026-08-10")
        assert pm.all()["600519.SH"]["pool"] == "history"

    def test_candidate_capacity(self, tmp_path):
        pm = PoolManager(db_path=tmp_path / "pools.db")
        big = pd.DataFrame({"code": [f"c{i}" for i in range(30)],
                            "综合分": [0.9 - i * 0.01 for i in range(30)]})
        pm.update_pools(big, date="2026-08-06")
        # 只保留 Top 15 在候选/观察
        active = [k for k, v in pm.all().items() if v["pool"] != "history"]
        assert len(active) <= 15

    def test_db_path_required(self):
        try:
            PoolManager()
            assert False, "应要求显式 db_path"
        except ValueError:
            pass

    def test_upsert_preserves_fields(self, tmp_path):
        pm = PoolManager(db_path=tmp_path / "pools.db")
        pm.to_candidate("600519.SH", 0.9, "2026-08-01")
        pm.to_watch("600519.SH", "等放量", "2026-08-02")
        info = pm.all()["600519.SH"]
        assert info["pool"] == "watch"
        assert info["watch_reason"] == "等放量"
