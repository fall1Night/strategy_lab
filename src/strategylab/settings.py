# -*- coding: utf-8 -*-
"""包级配置：自动加载 .env，提供数据目录 / 策略目录解析。

- 模块级 ``load_dotenv()``：包导入即自动加载 ``.env``（python-dotenv）。
- ``get_data_dir()``：行情缓存 / 渲染视图 HTML 的基目录，读 ``STRATEGALAB_DATA_DIR``，
  默认 ``./data``，自动创建。
- ``get_strategies_dir()``：用户自定义策略目录，读 ``STRATEGALAB_STRATEGIES_DIR``，
  未设置返回 ``None``（仅用包内置策略）。
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# 模块级自动加载 .env（包导入即生效）
load_dotenv()


def get_data_dir() -> Path:
    """返回行情缓存 / 渲染视图目录，默认 ./data（可由 STRATEGALAB_DATA_DIR 覆盖）。"""
    d = Path(os.environ.get("STRATEGALAB_DATA_DIR", "./data"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_strategies_dir() -> Path | None:
    """返回用户自定义策略目录（STRATEGALAB_STRATEGIES_DIR），未设置返回 None。"""
    raw = os.environ.get("STRATEGALAB_STRATEGIES_DIR")
    return Path(raw) if raw else None
