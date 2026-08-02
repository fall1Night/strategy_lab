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
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from .base import DataSource, SymbolSpec
from .exceptions import DataSourceError, RetryableDataSourceError

# 东财 push2his K 线 API（与旧 data_feed.API_URL 一致）
# 注意：必须用 https。东财现已主推 https，明文 http 请求会被拒绝/重定向，
# 稳定返回 rc=100；改用 https 后 rc=0 正常返回数据。
API_URL: str = "https://push2his.eastmoney.com/api/qt/stock/kline/get"


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
    # 单次请求窗口上限（天）。东财单请求有隐式条数/区间上限，且超大区间易被判异常/截断；
    # 切成 ~4 年窗口逐段取数，按日期去重合并，既降请求负载又避免早期 warmup 被静默截断。
    _CHUNK_DAYS = 1460

    def _raw_fetch_kline(
        self,
        symbol: str,
        period: str,
        start: str,
        end: str,
        **params: Any,
    ) -> list[str]:
        """拉取东财 push2his K 线原始字符串列表（分页取数 + 按日期去重合并）。

        Args:
            symbol: ``SymbolSpec.symbol`` 或 ``SymbolSpec.secid``。
                    若传入的是标准化 symbol（如 ``600216.SH``），内部转为 secid。
            period: ``"101"`` 日线 / ``"102"`` 周线。
            start: 起始日期 ``YYYYMMDD``。
            end: 结束日期 ``YYYYMMDD``。
            **params: 额外参数（如 ``lmt`` 条数上限，默认 1500）。

        Returns:
            K 线原始字符串列表（按日期升序、去重），每行 ``"date,open,close,high,low,..."`` 格式。
        """
        spec: SymbolSpec = self.normalize(symbol)
        secid: str = spec.secid
        klt: str = period
        lmt: int = int(params.get("lmt", 1500))
        try:
            beg_d = datetime.strptime(start, "%Y%m%d")
            end_d = datetime.strptime(end, "%Y%m%d")
        except ValueError:
            raise DataSourceError(self.name, f"eastmoney 日期格式错误 start={start} end={end}")
        merged: dict[str, str] = {}
        cur = beg_d
        while cur <= end_d:
            chunk_end = min(cur + timedelta(days=self._CHUNK_DAYS - 1), end_d)
            for line in self._fetch_chunk(
                secid, klt, cur.strftime("%Y%m%d"), chunk_end.strftime("%Y%m%d"), lmt
            ):
                merged[line.split(",", 1)[0]] = line
            cur = chunk_end + timedelta(days=1)
        return [merged[k] for k in sorted(merged)]

    def _fetch_chunk(
        self, secid: str, klt: str, beg: str, end: str, lmt: int
    ) -> list[str]:
        """单段取数；网络错误 / rc!=0 / 空数据 一律抛 RetryableDataSourceError。"""
        query = (
            f"?secid={secid}"
            f"&fields1=f1,f2,f3,f4,f5,f6"
            f"&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
            f"&klt={klt}&fqt=1&ut=fa5fd1943c7b386f172d6893dbfba10b"
            f"&beg={beg}&end={end}&lmt={lmt}"
        )
        from .base import random_headers

        url = API_URL + query
        headers = {**random_headers(), "Host": "push2his.eastmoney.com"}
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(
                req, timeout=getattr(self.config, "network_timeout", 30)
            ) as resp:
                raw = resp.read().decode("utf-8")
        except Exception as e:  # 网络/HTTP 错误 → 可重试
            raise RetryableDataSourceError(self.name, f"eastmoney 请求失败(可重试): {e}") from e
        try:
            data = json.loads(raw)
        except Exception as e:
            raise RetryableDataSourceError(self.name, f"eastmoney 响应非 JSON(可重试): {raw[:200]!r}") from e
        if not isinstance(data, dict):
            raise RetryableDataSourceError(self.name, f"eastmoney 响应异常(非dict,可重试): {raw[:200]!r}")
        if data.get("rc", 0) != 0:
            # 东财限流/拒绝（rc=100 等）→ 可重试，让基类退避后重试
            raise RetryableDataSourceError(
                self.name,
                f"eastmoney 返回错误码 rc={data.get('rc')} rt={data.get('rt')} raw={raw[:200]!r}",
            )
        payload = data.get("data")
        if not isinstance(payload, dict):
            raise RetryableDataSourceError(
                self.name,
                f"eastmoney 返回空数据 payload={payload!r}; rc={data.get('rc')} "
                f"rt={data.get('rt')} raw={raw[:200]!r}",
            )
        return list(payload.get("klines") or [])

    def _to_dataframe(self, raw: Any) -> pd.DataFrame:
        """把东财原始 kline 字符串列表转为 DataFrame，额外保留成交量（vol）。

        东财 push2his klines 字段顺序为 f51..f61：
          f51=date, f52=open, f53=close, f54=high, f55=low, f56=vol(股),
          f57=amount(元) ...
        故 ``parts`` 索引：0=date, 1=open, 2=close, 3=high, 4=low, 5=vol。
        """
        if raw is None:
            return pd.DataFrame()
        if isinstance(raw, pd.DataFrame):
            return raw
        if not isinstance(raw, list) or not raw:
            return pd.DataFrame()
        if not isinstance(raw[0], str):
            return super()._to_dataframe(raw)
        rows: list[dict[str, Any]] = []
        for line in raw:
            parts = line.split(",")
            if len(parts) < 5:
                continue
            try:
                rec = {
                    "date": parts[0],
                    "open": float(parts[1]),
                    "close": float(parts[2]),
                    "high": float(parts[3]),
                    "low": float(parts[4]),
                }
                # 第 6 字段 f56 为成交量（原始股数）；缺失则留空（后续容错为 None）
                if len(parts) >= 6 and parts[5] not in ("", None):
                    rec["vol"] = float(parts[5])
                rows.append(rec)
            except (ValueError, TypeError):
                continue
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame(rows)
