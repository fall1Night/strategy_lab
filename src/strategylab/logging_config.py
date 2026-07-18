# -*- coding: utf-8 -*-
"""统一日志配置：包导入即初始化（幂等）。

日志级别由环境变量 ``STRATEGALAB_LOG_LEVEL`` 控制，默认 ``INFO``。
"""
from __future__ import annotations

import logging
import os


def setup_logging() -> None:
    """幂等地配置根日志（仅首次调用生效，避免重复添加 handler）。"""
    if logging.getLogger().handlers:  # 幂等，避免重复添加 handler
        return
    level = os.environ.get("STRATEGALAB_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
