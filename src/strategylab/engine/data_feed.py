# -*- coding: utf-8 -*-
"""行情数据获取兼容层（re-export shim）。

本模块保留 ``normalize_symbol`` / ``ensure_data`` / ``load_bars`` 的
**同名同签名**，内部全量委托给 ``datasource`` 子包，保证 ``backtest.py``
与 ``batch_runner.py`` 的现有调用零改动。

API_URL / _is_retryable_network_error 等既有导出常量/工具也照常可访问，
由 datasource 子包 re-export。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

# ---- 全部委托给 datasource 子包 ----
from .datasource import (
    normalize_symbol as _ds_normalize_symbol,
    ensure_data as _ds_ensure_data,
    load_bars as _ds_load_bars,
    _is_retryable_network_error,
)

# 东财 API 常量（从 eastmoney 模块导出，保持向后兼容）
from .datasource.eastmoney import API_URL  # noqa: F401


def normalize_symbol(symbol: str) -> dict[str, str]:
    """把多种写法归一为 dict（标准化代码, secid, 前缀）。

    签名与返回格式与旧版 100% 一致：返回 ``{symbol, secid, prefix}`` 的 dict。
    内部委托给 ``datasource.base.normalize_symbol``。
    """
    spec = _ds_normalize_symbol(symbol)
    return spec.to_dict()


def ensure_data(
    symbol_cfg: dict[str, str],
    out_dir: str | Path,
    daily_beg: str = "20220706",
    daily_end: str = "20260718",
    daily_lmt: int = 1500,
    weekly_beg: str = "20211210",
    weekly_end: str = "20260718",
    weekly_lmt: int = 500,
    mode: str = "update",
    required_beg: str | None = None,
    required_end: str | None = None,
) -> tuple[Path, Path]:
    """拉取或复用日线/周线 CSV，返回 (daily_csv, weekly_csv) 路径。

    签名与旧版兼容，新增 ``mode`` / ``required_beg`` / ``required_end`` 透传，
    内部委托给 ``datasource.provider.ensure_data``（FR-39/40）。
    """
    return _ds_ensure_data(
        symbol_cfg, out_dir,
        daily_beg=daily_beg, daily_end=daily_end, daily_lmt=daily_lmt,
        weekly_beg=weekly_beg, weekly_end=weekly_end, weekly_lmt=weekly_lmt,
        mode=mode, required_beg=required_beg, required_end=required_end,
    )


def load_bars(csv_path: Path) -> pd.DataFrame:
    """加载缓存 CSV，返回按 date 排序的 DataFrame。签名不变。"""
    return _ds_load_bars(csv_path)
