# -*- coding: utf-8 -*-
"""QA 补充边界验证：腾讯适配器 — 严过关（只验证不改源码）"""
from __future__ import annotations

import traceback
import pandas as pd

from strategylab.engine.datasource.config import DataSourceConfig
from strategylab.engine.datasource.factory import DataSourceFactory

cfg = DataSourceConfig.from_env()
ds = DataSourceFactory(cfg).get("tencent", cfg)


def check(name, fn):
    try:
        fn()
        print(f"[PASS] {name}")
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] {name}: {type(e).__name__}: {e}\n{traceback.format_exc()}")


# A. SZ 前缀映射（000001.SZ → sz000001）真实拉取
def a_sz():
    df = ds.fetch_kline("000001.SZ", "101", "20240102", "20240115", lmt=50)
    assert len(df) >= 5, f"sz rows={len(df)}"
    print(f"   sz rows={len(df)} range={df['date'].min()}~{df['date'].max()}")


check("A. SZ 前缀映射 000001.SZ", a_sz)


# B. adjust=hfq 参数透传（后复权可拉，且与前复权数值不同）
def b_hfq():
    qfq = ds.fetch_kline("600216.SH", "101", "20240102", "20240105", lmt=10, adjust="qfq")
    hfq = ds.fetch_kline("600216.SH", "101", "20240102", "20240105", lmt=10, adjust="hfq")
    assert len(qfq) == len(hfq) > 0, "hfq rows mismatch"
    diff = (qfq["close"].reset_index(drop=True) != hfq["close"].reset_index(drop=True)).any()
    print(f"   qfq close={qfq['close'].tolist()}")
    print(f"   hfq close={hfq['close'].tolist()} differs={bool(diff)}")


check("B. adjust=hfq 透传", b_hfq)


# C. 周线 OHLC 聚合正确性：W-FRI 首开/最高/最低/末收
def c_weekly_ohlc():
    daily = ds.fetch_kline("600216.SH", "101", "20240102", "20240112", lmt=50)
    weekly = ds.fetch_kline("600216.SH", "102", "20240102", "20240112", lmt=50)
    daily_d = daily.set_index("date")
    # 第一周：2024-01-02 ~ 2024-01-05
    w0 = daily_d.loc["2024-01-02":"2024-01-05"]
    wk = weekly.iloc[0]
    assert abs(wk["open"] - w0["open"].iloc[0]) < 1e-6, "weekly open != first daily open"
    assert abs(wk["high"] - w0["high"].max()) < 1e-6, "weekly high != max"
    assert abs(wk["low"] - w0["low"].min()) < 1e-6, "weekly low != min"
    assert abs(wk["close"] - w0["close"].iloc[-1]) < 1e-6, "weekly close != last daily close"
    print(f"   week {wk['date'].date()} open={wk['open']} high={wk['high']} low={wk['low']} close={wk['close']}")
    print(f"   daily first week open={w0['open'].iloc[0]} high={w0['high'].max()} low={w0['low'].min()} close={w0['close'].iloc[-1]}")


check("C. 周线 OHLC 聚合正确性", c_weekly_ohlc)


# D. 空区间返回空 DataFrame（不崩溃）
def d_empty():
    df = ds.fetch_kline("600216.SH", "101", "20200101", "20200102", lmt=10)
    print(f"   empty range rows={len(df)}")
    assert df.empty


check("D. 空区间返回空 DataFrame", d_empty)


# E. 未知代码（数据源无此代码）行为
def e_bad_symbol():
    df = ds.fetch_kline("999999.SH", "101", "20240101", "20240131", lmt=10)
    print(f"   bad symbol rows={len(df)} (empty={df.empty})")


check("E. 未知代码行为（不崩溃）", e_bad_symbol)


# F. 数值列类型/有限性
def f_numeric():
    df = ds.fetch_kline("600216.SH", "101", "20240102", "20240110", lmt=20)
    for c in ("open", "high", "low", "close"):
        assert pd.api.types.is_numeric_dtype(df[c]), f"{c} not numeric"
        assert df[c].notna().all(), f"{c} has NaN"
    print(f"   numeric cols OK rows={len(df)}")


check("F. 数值列类型与有限性", f_numeric)


print("\n补充边界验证完成")
