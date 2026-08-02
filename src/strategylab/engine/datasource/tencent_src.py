# -*- coding: utf-8 -*-
"""akshare 腾讯适配器：懒加载 ``akshare``，调腾讯接口 ``stock_zh_a_hist_tx``。

腾讯源（``web.ifzq.gtimg.cn`` 系）相对新浪更抗 IP 限流，且默认返回前复权(qfq)
数据，是东财被封后「无 tushare token + 依赖前复权」场景下的推荐主源。

与新浪源的差异：
  - symbol 同样需交易所前缀（``600216.SH`` → ``sh600216``）；
  - ``stock_zh_a_hist_tx`` 内部按年份分块拼接多段请求，单次调用即返回完整区间；
  - 返回列名通常为英文 ``date/open/close/high/low/amount``，历史版本也可能返回中文，
    本适配器做双格式列名兼容（与 ``akshare_src`` 同思路）。

周线由日线聚合生成（pandas resample，与 akshare_src 完全一致）。
缺依赖时抛出 ``MissingDependencyError``，含明确安装提示。
"""

from __future__ import annotations

import concurrent.futures as _cf
from typing import Any

import pandas as pd

from .base import DataSource, SymbolSpec
from .exceptions import MissingDependencyError, DataSourceError


class TencentDataSource(DataSource):
    """akshare 腾讯数据源适配器。

    懒加载 akshare；未安装时抛出 ``MissingDependencyError``。
    symbol 需加交易所前缀（如 ``600216.SH`` → ``sh600216``）。
    默认 ``adjust="qfq"``（前复权），与回测口径一致。
    """

    # A 股市场前缀映射（与新浪源一致）
    _EXCH_PREFIX = {"SH": "sh", "SZ": "sz", "BJ": "bj"}

    def __init__(self, config: "DataSourceConfig") -> None:  # noqa: F821
        super().__init__(name="tencent", config=config)

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
        """通过 akshare 腾讯接口拉取 A 股历史 K 线。

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
        tencent_symbol = f"{self._EXCH_PREFIX.get(exch.upper(), 'sh')}{code}"

        adjust: str = params.get("adjust", "qfq")

        try:
            # akshare 内部网络请求无统一超时入口，用单线程包裹并设上限，
            # 避免单个标的取数卡死（无限等待）拖垮整个批次。
            _ex = _cf.ThreadPoolExecutor(max_workers=1)
            try:
                _fut = _ex.submit(
                    ak.stock_zh_a_hist_tx,
                    symbol=tencent_symbol,
                    start_date=start,
                    end_date=end,
                    adjust=adjust,
                )
                df_raw = _fut.result(timeout=self.config.network_timeout)
            except _cf.TimeoutError as _te:
                raise DataSourceError(
                    self.name,
                    f"akshare 腾讯源取数超时(>{self.config.network_timeout}s): {_te}",
                ) from _te
            finally:
                # 非阻塞关闭：即便底层线程仍卡在请求上，也不阻塞当前批次任务。
                _ex.shutdown(wait=False)
        except Exception as e:
            raise DataSourceError(self.name, f"akshare 腾讯源取数失败: {type(e).__name__}: {e}") from e

        if df_raw is None or df_raw.empty:
            return pd.DataFrame()

        # 标准化列名：腾讯接口返回英文（date/open/close/high/low/amount），
        # 历史版本/部分场景可能返回中文，双格式兼容。
        df = df_raw.copy()
        col_map = {
            "date": "date",
            "open": "open",
            "开盘": "open",
            "close": "close",
            "收盘": "close",
            "high": "high",
            "最高": "high",
            "low": "low",
            "最低": "low",
            "成交量": "vol",
            "volume": "vol",
        }
        for eng, std in col_map.items():
            if eng in df.columns:
                df.rename(columns={eng: std}, inplace=True)

        required = ["date", "open", "close", "high", "low"]
        for col in required:
            if col not in df.columns:
                raise DataSourceError(
                    self.name,
                    f"akshare 腾讯源返回数据缺少列: {col}，实际列: {list(df.columns)}",
                )

        result = df[required].copy()
        result["date"] = pd.to_datetime(result["date"])
        result[["open", "high", "low", "close"]] = result[
            ["open", "high", "low", "close"]
        ].apply(pd.to_numeric, errors="coerce")

        # 成交量（vol）：腾讯源中文列"成交量"或英文列"volume"，保持原始股数；
        # 缺失则不补列（后续落库/回测容错为 None）。
        if "vol" in df.columns:
            result["vol"] = pd.to_numeric(df["vol"], errors="coerce")

        # 周线：由日线 resample 聚合（成交量按区间求和，与 akshare_src 一致）
        if period == "102":
            agg = {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
            }
            if "vol" in result.columns:
                agg["vol"] = "sum"
            result = result.set_index("date").resample("W-FRI").agg(agg).dropna().reset_index()

        return result.sort_values("date").reset_index(drop=True)

    def _to_dataframe(self, raw: Any) -> pd.DataFrame:
        """akshare 已返回 DataFrame，直接透传。"""
        if isinstance(raw, pd.DataFrame):
            return raw
        return super()._to_dataframe(raw)
