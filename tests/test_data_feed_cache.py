# -*- coding: utf-8 -*-
"""FR-19/27 测试：data_feed.ensure_data 缓存区间校验 + 原子写 + 进程内 symbol 锁。

F. 缓存修复（FR-19）：
   - 区间被已缓存区间覆盖 → 复用（不重复 fetch）；
   - 请求区间超出已缓存 → 重新取数（fetch 次数增加）；
   - meta.json 用临时文件 + os.replace 原子替换（无 .tmp 残留）。
   - FR-27：进程内 symbol 锁，同 secid 并发取数仅 fetch 一次。
"""
from __future__ import annotations

import threading
from pathlib import Path
from tempfile import mkdtemp

from unittest.mock import MagicMock

from strategylab.engine import data_feed


FAKE_KLINES = ["20220101,10.0,11.0,12.0,9.0", "20230101,11.0,12.0,13.0,10.0"]
SYM_CFG = data_feed.normalize_symbol("600216.SH")


def _make_fetch_mock():
    m = MagicMock(return_value=list(FAKE_KLINES))
    return m


def test_cache_reuse_and_refetch(monkeypatch):
    out = Path(mkdtemp())
    fetch = _make_fetch_mock()
    monkeypatch.setattr(data_feed, "fetch_kline", fetch)

    daily_beg, daily_end = "20220101", "20230101"
    weekly_beg, weekly_end = "20220101", "20230101"

    # 1) 首次：日/周线均无缓存 → 各取一次（共 2 次）
    data_feed.ensure_data(
        SYM_CFG, out, daily_beg=daily_beg, daily_end=daily_end,
        weekly_beg=weekly_beg, weekly_end=weekly_end,
    )
    assert fetch.call_count == 2, f"首次应取日+周共 2 次，实际 {fetch.call_count}"

    # 2) 相同区间：应复用（不重复 fetch）
    data_feed.ensure_data(
        SYM_CFG, out, daily_beg=daily_beg, daily_end=daily_end,
        weekly_beg=weekly_beg, weekly_end=weekly_end,
    )
    assert fetch.call_count == 2, f"相同区间应复用，fetch 次数不应增加，实际 {fetch.call_count}"

    # 3) 区间超出已缓存（end 延伸）→ 日线需重取（周线仍覆盖）
    data_feed.ensure_data(
        SYM_CFG, out, daily_beg=daily_beg, daily_end="20231231",
        weekly_beg=weekly_beg, weekly_end=weekly_end,
    )
    assert fetch.call_count == 3, f"超出缓存应仅重取日线(+1)，实际 {fetch.call_count}"

    # csv + meta 均已落盘
    assert (out / f"{SYM_CFG['prefix']}_daily.csv").exists()
    assert (out / f"{SYM_CFG['prefix']}_weekly.csv").exists()
    assert (out / f"{SYM_CFG['prefix']}_daily_meta.json").exists()
    assert (out / f"{SYM_CFG['prefix']}_weekly_meta.json").exists()


def test_atomic_write_no_tmp_leftover(monkeypatch):
    out = Path(mkdtemp())
    fetch = _make_fetch_mock()
    monkeypatch.setattr(data_feed, "fetch_kline", fetch)

    data_feed.ensure_data(
        SYM_CFG, out, daily_beg="20220101", daily_end="20230101",
        weekly_beg="20220101", weekly_end="20230101",
    )
    # 原子替换后不应残留 .tmp 半写文件
    tmp_leftovers = list(out.glob("*.tmp"))
    assert not tmp_leftovers, f"不应残留临时文件：{tmp_leftovers}"

    # meta.json 内容正确（记录缓存区间）
    import json
    meta = json.loads((out / f"{SYM_CFG['prefix']}_daily_meta.json").read_text(encoding="utf-8"))
    assert meta["beg"] == "20220101"
    assert meta["end"] == "20230101"
    assert meta["version"] == 1


def test_symbol_lock_prevents_duplicate_fetch(monkeypatch):
    """FR-27：同 secid 并发取数，进程内 symbol 锁保证只 fetch 一次。"""
    out = Path(mkdtemp())
    fetch = _make_fetch_mock()
    monkeypatch.setattr(data_feed, "fetch_kline", fetch)

    def worker():
        data_feed.ensure_data(
            SYM_CFG, out, daily_beg="20220101", daily_end="20230101",
            weekly_beg="20220101", weekly_end="20230101",
        )

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # symbol 锁保证只有一个线程真正取数（该线程 fetch 日+周共 2 次），
    # 另一个线程被双检跳过，不重复取数（若锁失效则为 4 次）。
    assert fetch.call_count == 2, f"同 secid 并发取数应只由单线程取数(日+周=2次)，实际 {fetch.call_count}"
