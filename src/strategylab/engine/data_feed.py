# -*- coding: utf-8 -*-
"""行情数据获取：东方财富 push2his K 线 API（前复权 qfq），带本地缓存。

- 支持 A 股（sh/sz/bj）、港股、美股代码归一化为东方财富 secid。
- 缓存落盘为 <prefix>_daily.csv / <prefix>_weekly.csv，重复运行直接复用。
- 取数区间含 warmup（日线早 1 年、周线早约 1.5 年），保证指标不缺数据。
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path
from typing import Any

import pandas as pd

API_URL = "http://push2his.eastmoney.com/api/qt/stock/kline/get"

# A 股: sh=1, sz/bj=0（东方财富 secid 市场位）
_MARKET_PREFIX = {"sh": "1", "sz": "0", "bj": "0"}


def normalize_symbol(symbol: str) -> dict[str, str]:
    """把多种写法归一为 (标准化代码, 东方财富 secid, 前缀)。

    接受: '600216.SH' / 'sh600216' / '002001.SZ' / '300765.sz' 等。
    港股/美股暂按原样拼接 secid（东方财富 116/105 等需额外映射，本环境以 A 股为主）。
    """
    s = symbol.strip().lower()
    # 形式 1: 600216.sh
    if "." in s:
        code, exch = s.split(".", 1)
        exch = exch.strip()
    else:
        # 形式 2: sh600216 / 600216sh
        for pref in ("sh", "sz", "bj"):
            if s.startswith(pref):
                exch, code = pref, s[len(pref):]
                break
            if s.endswith(pref):
                code, exch = s[: -len(pref)], pref
                break
        else:
            code, exch = s, "sh"  # 兜底
    code = code.zfill(6)
    exch = exch.lower()

    if exch in _MARKET_PREFIX:
        secid = f"{_MARKET_PREFIX[exch]}.{code}"
    else:
        # 港股/美股：东方财富特殊 secid，超出本环境范围；保留原样尝试
        secid = f"{exch}.{code}"
    display = f"{code.upper()}.{exch.upper()}"
    prefix = f"{code}_{exch}"
    return {"symbol": display, "secid": secid, "prefix": prefix}


def fetch_kline(secid: str, klt: str, beg: str, end: str, lmt: int) -> list[str]:
    """拉取 K 线原始字符串列表。klt=101 日线 / 102 周线。"""
    params = (
        f"?secid={secid}"
        f"&fields1=f1,f2,f3,f4,f5,f6"
        f"&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
        f"&klt={klt}&fqt=1&beg={beg}&end={end}&lmt={lmt}"
    )
    url = API_URL + params
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Referer": "https://quote.eastmoney.com/",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data.get("data", {}).get("klines", [])


def _write_csv(klines: list[str], csv_path: Path) -> int:
    lines = ["date,open,high,low,close"]
    for line in klines:
        parts = line.split(",")
        if len(parts) < 5:
            continue
        date, o, c, h, l = parts[0], parts[1], parts[2], parts[3], parts[4]
        lines.append(f"{date},{o},{h},{l},{c}")
    csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(lines) - 1


def ensure_data(
    symbol_cfg: dict[str, str],
    out_dir: str | Path,
    daily_beg: str = "20220706",
    daily_end: str = "20260718",
    daily_lmt: int = 1500,
    weekly_beg: str = "20211210",
    weekly_end: str = "20260718",
    weekly_lmt: int = 500,
) -> tuple[Path, Path]:
    """拉取或复用日线/周线 CSV，返回 (daily_csv, weekly_csv) 路径。"""
    out_dir = Path(out_dir)
    prefix = symbol_cfg["prefix"]
    daily_csv = out_dir / f"{prefix}_daily.csv"
    weekly_csv = out_dir / f"{prefix}_weekly.csv"
    secid = symbol_cfg["secid"]

    have_daily = daily_csv.exists() and daily_csv.stat().st_size > 30
    have_weekly = weekly_csv.exists() and weekly_csv.stat().st_size > 30
    if have_daily and have_weekly:
        print(f"  [复用缓存] {symbol_cfg['symbol']}: 日/周线 CSV 已存在, 跳过取数")
        return daily_csv, weekly_csv

    dk = fetch_kline(secid, "101", daily_beg, daily_end, daily_lmt)
    n_d = _write_csv(dk, daily_csv)
    wk = fetch_kline(secid, "102", weekly_beg, weekly_end, weekly_lmt)
    n_w = _write_csv(wk, weekly_csv)
    print(f"  [取数] {symbol_cfg['symbol']}: 日线 {n_d} 条 / 周线 {n_w} 条")
    time.sleep(0.3)
    return daily_csv, weekly_csv


def load_bars(csv_path: Path) -> pd.DataFrame:
    return pd.read_csv(csv_path, parse_dates=["date"]).sort_values("date").reset_index(drop=True)
