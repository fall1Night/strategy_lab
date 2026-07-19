# -*- coding: utf-8 -*-
"""东方财富适配器：把既有 push2his 取数逻辑迁入 ``_raw_fetch_kline``。

行为与重构前 100% 一致：
  - klt=101 日线 / 102 周线
  - qfq 前复权（fqt=1）
  - fields1/fields2 参数不变
  - User-Agent / Referer 头不变
  - 超时 30s 不变
"""
from __future__ import annotations

import json
import urllib.request
from typing import Any

from .base import DataSource, SymbolSpec

# 东财 push2his K 线 API（与旧 data_feed.API_URL 一致）
API_URL: str = "http://push2his.eastmoney.com/api/qt/stock/kline/get"


class EastmoneyDataSource(DataSource):
    """东方财富 push2his K 线适配器。

    把既有 ``data_feed.fetch_kline`` 的 push2his 主体迁入 ``_raw_fetch_kline``，
    限流/熔断/重试由基类 ``DataSource.fetch_kline`` 模板方法统一管理。
    """

    def __init__(self, config: "DataSourceConfig") -> None:  # noqa: F821
        super().__init__(name="eastmoney", config=config)

    # ------------------------------------------------------------------
    # 真正取数（只写取数逻辑，限流/熔断/重试由基类模板方法管理）
    # ------------------------------------------------------------------
    def _raw_fetch_kline(
        self,
        symbol: str,
        period: str,
        start: str,
        end: str,
        **params: Any,
    ) -> list[str]:
        """拉取东财 push2his K 线原始字符串列表。

        Args:
            symbol: ``SymbolSpec.symbol`` 或 ``SymbolSpec.secid``。
                    若传入的是标准化 symbol（如 ``600216.SH``），内部转为 secid。
            period: ``"101"`` 日线 / ``"102"`` 周线。
            start: 起始日期 ``YYYYMMDD``。
            end: 结束日期 ``YYYYMMDD``。
            **params: 额外参数（如 ``lmt`` 条数上限，默认 1500）。

        Returns:
            K 线原始字符串列表，每行 ``"date,open,close,high,low,..."`` 格式。
        """
        # 若传入 SymbolSpec.symbol 格式（如 "600216.SH"），转为 secid
        spec: SymbolSpec = self.normalize(symbol)
        secid: str = spec.secid
        klt: str = period  # "101" 日线 / "102" 周线
        beg: str = start
        end_str: str = end
        lmt: int = int(params.get("lmt", 1500))

        query = (
            f"?secid={secid}"
            f"&fields1=f1,f2,f3,f4,f5,f6"
            f"&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
            f"&klt={klt}&fqt=1&beg={beg}&end={end_str}&lmt={lmt}"
        )
        from .base import random_headers

        url = API_URL + query
        req = urllib.request.Request(url, headers=random_headers())
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return list(data.get("data", {}).get("klines", []))
