# -*- coding: utf-8 -*-
"""tushare 适配器：懒加载 ``tushare``，用 ``pro_bar`` + token。

缺依赖/缺 token 时明确报错。
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from .base import DataSource, SymbolSpec
from .exceptions import MissingDependencyError, DataSourceError, DataSourceUnavailableError


class TushareDataSource(DataSource):
    """tushare 数据源适配器。

    懒加载 tushare；需配置 ``STRATEGALAB_TUSHARE_TOKEN``。
    """

    def __init__(self, config: "DataSourceConfig") -> None:  # noqa: F821
        super().__init__(name="tushare", config=config)
        self._pro: Any = None

    # ------------------------------------------------------------------
    # 懒加载 tushare + 初始化 pro 对象
    # ------------------------------------------------------------------
    def _get_pro(self) -> Any:
        """懒加载 tushare 并返回 pro 对象。"""
        if self._pro is not None:
            return self._pro

        try:
            import tushare as ts  # type: ignore[import-untyped]
        except ImportError:
            raise MissingDependencyError(
                "tushare",
                hint='pip install "strategylab[tushare]"',
            ) from None

        token = self.config.tushare_token
        if not token:
            raise DataSourceUnavailableError(
                "tushare",
                "缺少 STRATEGALAB_TUSHARE_TOKEN，请在 .env 中配置 tushare 凭证",
            )

        try:
            ts.set_token(token)
            self._pro = ts.pro_api()
        except Exception as e:
            raise DataSourceUnavailableError(
                "tushare",
                f"tushare 初始化失败（token 可能无效）: {type(e).__name__}: {e}",
            ) from e

        return self._pro

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
        """通过 tushare ``pro_bar`` 拉取 K 线。

        Args:
            symbol: 标准化代码（如 ``600216.SH``）。
            period: ``"101"`` 日线 / ``"102"`` 周线。
            start: YYYYMMDD 起始。
            end: YYYYMMDD 结束。
            **params: 额外参数（如 ``adj`` 复权，默认 ``"qfq"``）。

        Returns:
            pandas DataFrame（标准列：date/open/high/low/close）。
        """
        pro = self._get_pro()
        spec: SymbolSpec = self.normalize(symbol)
        # tushare 用 ts_code 格式（如 600216.SH）
        ts_code = spec.symbol

        adj: str = params.get("adj", "qfq")
        # 频率映射
        freq_map = {"101": "D", "102": "W"}
        freq = freq_map.get(period, "D")

        # 日期转 YYYYMMDD 格式
        start_clean = start.replace("-", "")[:8]
        end_clean = end.replace("-", "")[:8]

        try:
            df_raw = pro.daily(
                ts_code=ts_code,
                start_date=start_clean,
                end_date=end_clean,
                adj=adj,
            )
        except Exception as e:
            raise DataSourceError(self.name, f"tushare 取数失败: {type(e).__name__}: {e}") from e

        if df_raw is None or df_raw.empty:
            return pd.DataFrame()

        # 如果是周线，用 ts.pro_bar 或 daily 聚合
        if freq == "W":
            df_raw = self._daily_to_weekly(df_raw)

        # 标准化列名
        df = df_raw.copy()
        col_map = {
            "trade_date": "date",
            "open": "open",
            "close": "close",
            "high": "high",
            "low": "low",
        }
        df.rename(columns=col_map, inplace=True)

        # 确保必要列存在
        required = ["date", "open", "close", "high", "low"]
        for col in required:
            if col not in df.columns:
                raise DataSourceError(
                    self.name,
                    f"tushare 返回数据缺少列: {col}，实际列: {list(df.columns)}",
                )

        # date 列转字符串 YYYYMMDD
        df["date"] = df["date"].astype(str)

        return df[required].sort_values("date").reset_index(drop=True)

    @staticmethod
    def _daily_to_weekly(df_daily: pd.DataFrame) -> pd.DataFrame:
        """把日线 DataFrame 聚合为周线。

        tushare 的 pro_bar 有时周线接口权限不足，这里用日线聚合兜底。
        """
        if df_daily is None or df_daily.empty:
            return df_daily

        df = df_daily.copy()
        if "trade_date" in df.columns:
            df["trade_date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d")
        else:
            return df

        # 按周聚合
        df["week"] = df["trade_date"].dt.to_period("W")
        weekly = (
            df.groupby("week")
            .agg(
                trade_date=("trade_date", "last"),
                open=("open", "first"),
                high=("high", "max"),
                low=("low", "min"),
                close=("close", "last"),
            )
            .reset_index(drop=True)
        )
        weekly["trade_date"] = weekly["trade_date"].dt.strftime("%Y%m%d")
        return weekly

    def _to_dataframe(self, raw: Any) -> pd.DataFrame:
        """tushare 已返回 DataFrame，直接透传。"""
        if isinstance(raw, pd.DataFrame):
            return raw
        return super()._to_dataframe(raw)

    def health(self) -> "HealthStatus":  # noqa: F821
        """检查 tushare 健康度：token 是否有效。"""
        base = super().health()
        if not self.config.tushare_token:
            base.status = "unavailable"
        elif base.status == "ok":
            # 尝试轻量验证
            try:
                pro = self._get_pro()
                if pro is None:
                    base.status = "unavailable"
            except DataSourceUnavailableError:
                base.status = "unavailable"
            except MissingDependencyError:
                base.status = "unavailable"
        return base
