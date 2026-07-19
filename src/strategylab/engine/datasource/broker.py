# -*- coding: utf-8 -*-
"""Broker（券商/同花顺）适配器桩（P2 预留）。

仅实现 ``DataSource`` 契约，``_raw_fetch_kline`` 抛 ``DataSourceUnavailableError``。
配置项 ``broker`` 可被 factory 识别但返回桩。
"""

from __future__ import annotations

from typing import Any

from .base import DataSource
from .exceptions import DataSourceUnavailableError


class BrokerDataSource(DataSource):
    """券商/同花顺适配器桩（P2 预留）。

    ``_raw_fetch_kline`` 永远抛 ``DataSourceUnavailableError("P2 预留，未实现")``。
    仅用于接口预留与后续扩展占位。
    """

    def __init__(self, config: "DataSourceConfig") -> None:  # noqa: F821
        super().__init__(name="broker", config=config)

    def _raw_fetch_kline(
        self,
        symbol: str,
        period: str,
        start: str,
        end: str,
        **params: Any,
    ) -> Any:
        """P2 预留：抛 ``DataSourceUnavailableError``。"""
        raise DataSourceUnavailableError(
            self.name,
            "P2 预留，未实现。券商/同花顺接口需后续凭证或逆向评估才能落地。",
        )

    def health(self) -> "HealthStatus":  # noqa: F821
        """Broker 永远不可用。"""
        hs = super().health()
        hs.status = "unavailable"
        return hs
