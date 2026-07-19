# -*- coding: utf-8 -*-
"""akshare 适配器：懒加载 ``akshare``，调新浪接口 ``stock_zh_a_daily``（避开东财）。

东财 IP 已封，改用新浪源。周线由日线聚合生成（pandas resample）。
缺依赖时抛出 ``MissingDependencyError``，含明确安装提示。
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from .base import DataSource, SymbolSpec
from .exceptions import MissingDependencyError, DataSourceError


class AkshareDataSource(DataSource):
    """akshare 新浪数据源适配器。

    懒加载 akshare；未安装时抛出 ``MissingDependencyError``。
    symbol 需加交易所前缀（如 ``600216.SH`` → ``sh600216``）。
    """

    # A 股市场前缀映射
    _EXCH_PREFIX = {"SH": "sh", "SZ": "sz", "BJ": "bj"}

    def __init__(self, config: "DataSourceConfig") -> None:  # noqa: F821
        super().__init__(name="akshare", config=config)

    # ------------------------------------------------------------------
    # 懒加载 akshare
    # ------------------------------------------------------------------
    @staticmethod
    def _import_akshare() -> Any:
        """懒加载 akshare 模块。"""
        try:
            import akshare as ak  # type: ignore[import-untyped]
        except ImportError:
            raise MissingDependencyError(
                "akshare",
                hint='pip install "strategylab[akshare]"',
            ) from None
        return ak

    # ------------------------------------------------------------------
    # 真正取数
    # ------------------------------------------------------------------
    def _raw_fetch_kline(
        self,
        symbol: str,
        period: str,
        start: str,
        end: str,
        **params: Any,
    ) -> pd.DataFrame:
        """通过 akshare 新浪接口拉取 A 股历史 K 线。

        Args:
            symbol: 标准化代码（如 ``600216.SH``）。
            period: ``"101"`` 日线 / ``"102"`` 周线（周线由日线聚合）。
            start: YYYYMMDD 起始。
            end: YYYYMMDD 结束。
            **params: 额外参数（``adjust`` 复权方式，默认 ``"qfq"``）。

        Returns:
            pandas DataFrame（列：date/open/high/low/close）。
        """
        ak = self._import_akshare()
        spec: SymbolSpec = self.normalize(symbol)

        # symbol 加交易所前缀：600216.SH → sh600216
        sym_parts = spec.symbol.split(".")
        code = sym_parts[0]
        exch = sym_parts[1] if len(sym_parts) > 1 else "SH"
        sina_symbol = f"{self._EXCH_PREFIX.get(exch.upper(), 'sh')}{code}"

        adjust: str = params.get("adjust", "qfq")

        try:
            df_raw = ak.stock_zh_a_daily(
                symbol=sina_symbol,
                start_date=start,
                end_date=end,
                adjust=adjust,
            )
        except Exception as e:
            raise DataSourceError(self.name, f"akshare 新浪源取数失败: {type(e).__name__}: {e}") from e

        if df_raw is None or df_raw.empty:
            return pd.DataFrame()

        # 标准化列名
        df = df_raw.copy()
        col_map = {
            "date": "date",
            "开盘": "open",
            "收盘": "close",
            "最高": "high",
            "最低": "low",
        }
        # stock_zh_a_daily 的列名可能是中文或英文
        for eng, std in col_map.items():
            if eng in df.columns:
                df.rename(columns={eng: std}, inplace=True)

        required = ["date", "open", "close", "high", "low"]
        for col in required:
            if col not in df.columns:
                raise DataSourceError(
                    self.name,
                    f"akshare 新浪源返回数据缺少列: {col}，实际列: {list(df.columns)}",
                )

        result = df[required].copy()
        result["date"] = pd.to_datetime(result["date"])
        result[["open", "high", "low", "close"]] = result[
            ["open", "high", "low", "close"]
        ].apply(pd.to_numeric, errors="coerce")

        # 周线：由日线 resample 聚合
        if period == "102":
            result = result.set_index("date").resample("W-FRI").agg({
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
            }).dropna().reset_index()

        return result.sort_values("date").reset_index(drop=True)

    def _to_dataframe(self, raw: Any) -> pd.DataFrame:
        """akshare 已返回 DataFrame，直接透传。"""
        if isinstance(raw, pd.DataFrame):
            return raw
        return super()._to_dataframe(raw)
