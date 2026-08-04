# -*- coding: utf-8 -*-
"""akshare 适配器：懒加载 ``akshare``，调新浪接口 ``stock_zh_a_daily``（避开东财）。

东财 IP 已封，改用新浪源。周线由日线聚合生成（pandas resample）。
缺依赖时抛出 ``MissingDependencyError``，含明确安装提示。
"""

from __future__ import annotations

import concurrent.futures as _cf
import threading
from typing import Any

import pandas as pd

from ._http_guard import ensure_requests_timeout
from .base import DataSource, SymbolSpec
from .exceptions import MissingDependencyError, DataSourceError

# 模块级共享取数线程池（限流 + 防线程爆炸，配合 requests 真超时）。
# 旧实现每次取数新建 ThreadPoolExecutor(max_workers=1) 且 shutdown(wait=False)：
# 新浪接口请求无内置超时，15s result 超时后底层工作线程仍挂起在网络请求上、
# 永不回收，1301 只全市场批量更新（日/周 2 周期 ≈ 2600 次取数）后线程数耗尽，
# 报 RuntimeError: cannot schedule new future，批次卡死（2026-08-04 实测）。
# 共享池限流：挂起任务最多占用 max_workers 个槽位；配合 ensure_requests_timeout()
# （底层 requests 注入 connect 8s / read 15s 超时），挂起任务 ≤15s 自行结束释放槽位。
_FETCH_EXECUTOR: "_cf.ThreadPoolExecutor | None" = None
_FETCH_EXECUTOR_LOCK: threading.Lock = threading.Lock()


def _get_fetch_executor() -> "_cf.ThreadPoolExecutor":
    """获取模块级共享取数线程池（懒加载单例）。"""
    global _FETCH_EXECUTOR
    if _FETCH_EXECUTOR is None:
        with _FETCH_EXECUTOR_LOCK:
            if _FETCH_EXECUTOR is None:
                _FETCH_EXECUTOR = _cf.ThreadPoolExecutor(
                    max_workers=4, thread_name_prefix="ak-fetch"
                )
    return _FETCH_EXECUTOR


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
        """懒加载 akshare 模块（并注入底层 requests 默认超时，幂等）。"""
        try:
            import akshare as ak  # type: ignore[import-untyped]
        except ImportError:
            raise MissingDependencyError(
                "akshare",
                hint='pip install "strategylab[akshare]"',
            ) from None
        ensure_requests_timeout()
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
            # akshare 内部网络请求无统一超时入口，用共享线程池包裹并设上限，
            # 避免单个标的取数卡死（无限等待）拖垮整个批次。
            # 共享池（_get_fetch_executor）：线程数有上限，杜绝每次新建线程池
            # + shutdown(wait=False) 造成的线程泄漏（cannot schedule new future）。
            _fut = _get_fetch_executor().submit(
                ak.stock_zh_a_daily,
                symbol=sina_symbol,
                start_date=start,
                end_date=end,
                adjust=adjust,
            )
            df_raw = _fut.result(timeout=self.config.network_timeout)
        except _cf.TimeoutError as _te:
            raise DataSourceError(
                self.name,
                f"akshare 新浪源取数超时(>{self.config.network_timeout}s): {_te}",
            ) from _te
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
            "成交量": "vol",
            "volume": "vol",
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

        # 成交量（vol）：新浪源中文列"成交量"或英文列"volume"，保持原始股数；
        # 缺失则不补列（后续落库/回测容错为 None）。
        if "vol" in df.columns:
            result["vol"] = pd.to_numeric(df["vol"], errors="coerce")

        # 周线：由日线 resample 聚合（成交量按区间求和）
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
