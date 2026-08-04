# -*- coding: utf-8 -*-
"""FR-19/27/39 测试：data_feed.ensure_data 缓存区间校验 + 原子写 + 进程内 symbol 锁（3.0 增量语义）。

重要变更：3.0 把 ``data_feed.py`` 重构为 shim，原模块级 ``fetch_kline`` 已下沉到
``datasource.provider.KlineProvider._try_fetch``（经适配器 ``fetch_kline`` 取数）。
故本测试把 mock 位点从 ``data_feed.fetch_kline`` 改为 ``KlineProvider._try_fetch``，
并把断言语义从「区间覆盖复用」改为 FR-39 的「增量」语义：

  - 增量幂等：缓存已是最新（last >= today）→ 不取数（复用）；
  - 缓存落后（last < today）→ 重取（取数窗口 = [last+1, today]）；
  - meta.json 用临时文件 + os.replace 原子替换（无 .tmp 残留）；
  - FR-27：进程内 symbol 锁，同 secid 并发取数仅取数一次（日+周）。
"""
from __future__ import annotations

import json
import threading
from datetime import date as _real_date
from pathlib import Path
from tempfile import mkdtemp

import pandas as pd
from unittest.mock import MagicMock

from strategylab.engine import data_feed
from strategylab.engine.datasource import provider as provider_mod


# data_feed.ensure_data 接收 symbol_cfg 的「dict」形态（兼容层 normalize_symbol 返回 dict）；
# 注意：datasource.base.normalize_symbol 返回的是 SymbolSpec（不可下标），不能传给 ensure_data。
SYM = data_feed.normalize_symbol("600216.SH")
PREFIX = SYM["prefix"]
SYMBOL = SYM["symbol"]


def _fake_klines_df() -> pd.DataFrame:
    """返回可被 cache.merge 消费的 DataFrame（含 date/open/high/low/close/vol）。

    注意：必须含 vol 列且有值 —— provider.ensure_data 的 update 模式会对缓存做
    vol 健康检查（has_volume_data），若 vol 全空/缺列会判定"存量缓存缺 vol"并
    强制全量重拉，破坏"已最新应复用"等增量断言。
    """
    return pd.DataFrame(
        {
            # 两个要求缺一不可：
            # 1) 首日早于 ensure_data 的 default_beg（20200101），否则 _incremental_window
            #    判定"往前补"（FR-39 双向补）恒全量重拉，破坏"已最新应复用"断言；
            # 2) 末日 = today（测试 _FakeDate 冻结为 2023-01-01），否则缓存 last < today
            #    判定"落后"恒增量拉取，同样破坏"已最新应复用"。
            "date": ["2019-01-01", "2023-01-01"],
            "open": [10.0, 11.0],
            "high": [12.0, 13.0],
            "low": [9.0, 10.0],
            "close": [11.0, 12.0],
            "vol": [100000.0, 120000.0],
        }
    )


def _make_fetch_mock(captured: list | None = None) -> MagicMock:
    """mock KlineProvider._try_fetch：返回固定 DataFrame，可选记录 (period, beg, end)。

    注意：monkeypatch 在类上替换 _try_fetch 为 MagicMock 后，MagicMock 不是函数描述符，
    访问 self._try_fetch 不会绑定 self，故 side_effect 的 fake 不应包含 self 形参。
    """

    def fake(symbol, spec, period, beg, end, lmt):
        if captured is not None:
            captured.append((period, beg, end))
        # 2026-08-04 起 _try_fetch 返回 (df, source_name) 二元组（容灾切换感知）
        return _fake_klines_df(), "akshare"

    return MagicMock(side_effect=fake)


class _FakeDate:
    """冻结 today 的 date 替身（仅用于测试增量窗口）。"""

    @staticmethod
    def today() -> _real_date:
        return _real_date(2023, 1, 1)


def test_cache_reuse_when_up_to_date_and_refetch_when_stale(monkeypatch):
    out = Path(mkdtemp())
    captured: list = []
    fetch = _make_fetch_mock(captured)
    monkeypatch.setattr(provider_mod.KlineProvider, "_try_fetch", fetch)
    # 冻结 today = 2023-01-01，使首次取数后缓存 last==today 即「最新」
    monkeypatch.setattr(provider_mod, "date", _FakeDate)

    # 1) 首次：无缓存 → 日+周各取一次（共 2 次）
    data_feed.ensure_data(SYM, out, mode="update")
    assert fetch.call_count == 2, f"首次应取日+周共 2 次，实际 {fetch.call_count}"

    # 2) 缓存已是最新（last==today）→ 复用，不重复取数
    data_feed.ensure_data(SYM, out, mode="update")
    assert fetch.call_count == 2, f"已最新应复用，fetch 次数不应增加，实际 {fetch.call_count}"

    # 3) 把 today 推进到 2023-06-01（缓存落后）→ 日线+周线均需重取（+2）
    class _LaterDate:
        @staticmethod
        def today() -> _real_date:
            return _real_date(2023, 6, 1)

    monkeypatch.setattr(provider_mod, "date", _LaterDate)
    data_feed.ensure_data(SYM, out, mode="update")
    assert fetch.call_count == 4, f"缓存落后应重取日+周(+2)，实际 {fetch.call_count}"

    # csv + meta 均已落盘
    prov = provider_mod.get_provider()
    eff = prov._factory.get_effective_source(SYMBOL)
    assert (out / f"{PREFIX}_{eff}_daily.csv").exists()
    assert (out / f"{PREFIX}_{eff}_weekly.csv").exists()
    assert (out / f"{PREFIX}_{eff}_daily_meta.json").exists()
    assert (out / f"{PREFIX}_{eff}_weekly_meta.json").exists()


def test_atomic_write_no_tmp_leftover(monkeypatch):
    out = Path(mkdtemp())
    fetch = _make_fetch_mock()
    monkeypatch.setattr(provider_mod.KlineProvider, "_try_fetch", fetch)
    monkeypatch.setattr(provider_mod, "date", _FakeDate)  # today=2023-01-01

    data_feed.ensure_data(SYM, out, mode="update")
    # 原子替换后不应残留 .tmp 半写文件
    tmp_leftovers = list(out.glob("*.tmp"))
    assert not tmp_leftovers, f"不应残留临时文件：{tmp_leftovers}"

    # meta.json 内容正确（记录缓存区间 / 版本 / 来源）
    prov = provider_mod.get_provider()
    eff = prov._factory.get_effective_source(SYMBOL)
    meta = json.loads(
        (out / f"{PREFIX}_{eff}_daily_meta.json").read_text(encoding="utf-8")
    )
    assert meta["beg"] is not None
    assert meta["end"] is not None
    # cache 版本 2→3：K线功能新增 vol 列时升级（cache.py _atomic_write_meta），
    # 该断言随版本升级同步更新
    assert meta["version"] == 3
    assert meta.get("source") == eff


def test_symbol_lock_prevents_duplicate_fetch(monkeypatch):
    """FR-27：同 secid 并发取数，进程内 symbol 锁保证只取数一次（日+周）。"""
    out = Path(mkdtemp())
    fetch = _make_fetch_mock()
    monkeypatch.setattr(provider_mod.KlineProvider, "_try_fetch", fetch)
    monkeypatch.setattr(provider_mod, "date", _FakeDate)

    def worker():
        data_feed.ensure_data(SYM, out, mode="update")

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # symbol 锁保证只有一个线程真正取数（该线程取日+周共 2 次），
    # 另一个线程被双检跳过，不重复取数（若锁失效则为 4 次）。
    assert fetch.call_count == 2, (
        f"同 secid 并发取数应只由单线程取数(日+周=2次)，实际 {fetch.call_count}"
    )
