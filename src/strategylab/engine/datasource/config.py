# -*- coding: utf-8 -*-
"""数据源配置：``DataSourceConfig`` 数据类 + ``from_env()`` 工厂方法。

解析环境变量（``STRATEGALAB_`` 前缀），提供全局默认源、按品种覆盖、
备用源顺序、tushare token、failover 开关、熔断开关等配置。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class DataSourceConfig:
    """数据源运行时配置。

    Attributes:
        default_source: 全局默认数据源名称（默认 ``"eastmoney"``）。
        symbol_overrides: 按品种覆盖映射，``{symbol: source_name}``。
        fallback_order: 备用源切换顺序列表。
        failover_enabled: 是否启用自动故障转移（默认 True）。
        tushare_token: tushare 凭证（可选）。
        min_fetch_gap: 单源最小请求间隔（秒，默认 0.3）。
        cb_enabled: 熔断总开关（默认 True）。
        network_timeout: 取数网络超时（秒，默认 15.0）。用于包裹 akshare/tushare
            等第三方库的内部分网络调用，避免单个标的取数卡死拖垮整个批次。
    """

    default_source: str = "eastmoney"
    symbol_overrides: dict[str, str] = field(default_factory=dict)
    fallback_order: list[str] = field(default_factory=list)
    failover_enabled: bool = True
    tushare_token: str | None = None
    min_fetch_gap: float = 2.0
    cb_enabled: bool = True
    network_timeout: float = 15.0

    @staticmethod
    def from_env() -> "DataSourceConfig":
        """从环境变量（``STRATEGALAB_`` 前缀）解析配置。

        读取以下环境变量：

        - ``STRATEGALAB_DATA_SOURCE`` → ``default_source``（默认 ``"eastmoney"``）
        - ``STRATEGALAB_SYMBOL_SOURCE`` → ``symbol_overrides``
          （格式：``600216.SH:tushare,000001.SZ:akshare``）
        - ``STRATEGALAB_FALLBACK_SOURCES`` → ``fallback_order``
          （格式：``akshare,tushare``）
        - ``STRATEGALAB_TUSHARE_TOKEN`` → ``tushare_token``
        - ``STRATEGALAB_DATASOURCE_FAILOVER`` → ``failover_enabled``
          （``"auto"`` → True / ``"off"`` → False）
        - ``STRATEGALAB_DATASOURCE_CB`` → ``cb_enabled``
          （``"on"`` → True / ``"off"`` → False）
        - ``STRATEGALAB_DATASOURCE_GAP`` → ``min_fetch_gap``
          （秒，默认 ``1.0``；数值越小请求越密、越易触发限流）
        - ``STRATEGALAB_DATASOURCE_TIMEOUT`` → ``network_timeout``
          （取数网络超时秒数，默认 ``15.0``；用于包裹第三方库调用防止单标的卡死）
        """
        default_source = os.environ.get("STRATEGALAB_DATA_SOURCE", "eastmoney").strip().lower()

        # 解析按品种覆盖：600216.SH:tushare,000001.SZ:akshare
        symbol_overrides: dict[str, str] = {}
        raw_overrides = os.environ.get("STRATEGALAB_SYMBOL_SOURCE", "")
        if raw_overrides.strip():
            for pair in raw_overrides.split(","):
                pair = pair.strip()
                if ":" in pair:
                    sym, src = pair.split(":", 1)
                    symbol_overrides[sym.strip()] = src.strip().lower()

        # 解析备用源顺序
        fallback_order: list[str] = []
        raw_fallback = os.environ.get("STRATEGALAB_FALLBACK_SOURCES", "")
        if raw_fallback.strip():
            fallback_order = [s.strip().lower() for s in raw_fallback.split(",") if s.strip()]

        failover_enabled = os.environ.get("STRATEGALAB_DATASOURCE_FAILOVER", "auto").strip().lower() != "off"
        cb_enabled = os.environ.get("STRATEGALAB_DATASOURCE_CB", "on").strip().lower() != "off"
        tushare_token = os.environ.get("STRATEGALAB_TUSHARE_TOKEN") or None
        try:
            gap = float(os.environ.get("STRATEGALAB_DATASOURCE_GAP", "2.0"))
        except (ValueError, TypeError):
            gap = 1.0
        try:
            net_timeout = float(os.environ.get("STRATEGALAB_DATASOURCE_TIMEOUT", "15.0"))
        except (ValueError, TypeError):
            net_timeout = 15.0

        return DataSourceConfig(
            default_source=default_source,
            symbol_overrides=symbol_overrides,
            fallback_order=fallback_order,
            failover_enabled=failover_enabled,
            tushare_token=tushare_token,
            min_fetch_gap=gap,
            cb_enabled=cb_enabled,
            network_timeout=net_timeout,
        )
