# -*- coding: utf-8 -*-
"""通用技术指标：MACD(12,26,9) / KDJ(9,3,3)。与既有回测口径完全一致。"""
from __future__ import annotations

import numpy as np
import pandas as pd


def compute_macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    """标准 MACD: DIF=EMA(fast)-EMA(slow), DEA=EMA(DIF,signal), hist=DIF-DEA。"""
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False).mean()
    hist = dif - dea
    return dif, dea, hist


def compute_kdj(df: pd.DataFrame, n: int = 9, m1: int = 3, m2: int = 3) -> pd.Series:
    """标准 KDJ, 初始 K=D=50。

    RSV=(C-Ln)/(Hn-Ln)*100; K=(m1-1)/m1*K_prev+1/m1*RSV;
    D=(m2-1)/m2*D_prev+1/m2*K; J=3K-2D。
    RSV 为 NaN 时 K/D 维持上一值(初始 50)。
    """
    length = len(df)
    low_n = df["low"].rolling(n).min()
    high_n = df["high"].rolling(n).max()
    denom = (high_n - low_n).replace(0, np.nan)
    rsv = (df["close"] - low_n) / denom * 100
    rsv_vals = rsv.values

    k = np.full(length, 50.0)
    d = np.full(length, 50.0)
    for i in range(length):
        if pd.notna(rsv_vals[i]):
            if i == 0:
                k[i] = (m1 - 1) / m1 * 50.0 + (1 / m1) * rsv_vals[i]
                d[i] = (m2 - 1) / m2 * 50.0 + (1 / m2) * k[i]
            else:
                k[i] = (m1 - 1) / m1 * k[i - 1] + (1 / m1) * rsv_vals[i]
                d[i] = (m2 - 1) / m2 * d[i - 1] + (1 / m2) * k[i]
    j = 3.0 * k - 2.0 * d
    return pd.Series(j, index=df.index)
