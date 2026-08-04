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
import json
import logging
import threading
from typing import Any

import pandas as pd

from ._http_guard import ensure_requests_timeout
from .base import DataSource, SymbolSpec
from .exceptions import MissingDependencyError, DataSourceError

logger = logging.getLogger(__name__)

# 腾讯新接口（proxy.finance.qq.com newfqkline）单次请求最多返回 640 条（count 上限），
# 且**忽略 start 日期**——总是返回"end 往前推 640 个交易日"的数据。akshare 的
# stock_zh_a_hist_tx 靠"按年分块拼接 + drop_duplicates"弥补了大部分，但为防第三方
# 库分块某段失败/未来行为变化导致历史缺失，本适配器在校验发现数据不足时，用
# _fetch_tx_yearly() 自研按年分块整体重拉（不经过 akshare 的 start 提升逻辑）。
_TX_COUNT_LIMIT = 640
_BACKFILL_MIN_GAP_DAYS = 15  # 首日比请求起点晚超过该天数 → 判定疑似缺失
_TX_KLINE_URL = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"

# 模块级共享取数线程池（与 akshare_src 同方案，2026-08-04）：
# 旧实现每次取数新建 ThreadPoolExecutor(max_workers=1) + shutdown(wait=False)——
# 请求挂起时工作线程不回收，批量更新后线程数耗尽报 cannot schedule new future。
# 共享池限流：线程数有上限；配合 ensure_requests_timeout()（底层 requests 注入
# connect 8s / read 15s 超时），挂起请求自行结束释放池槽，不会死锁。
_FETCH_EXECUTOR: "_cf.ThreadPoolExecutor | None" = None
_FETCH_EXECUTOR_LOCK: threading.Lock = threading.Lock()


def _get_fetch_executor() -> "_cf.ThreadPoolExecutor":
    """获取模块级共享取数线程池（懒加载单例）。"""
    global _FETCH_EXECUTOR
    if _FETCH_EXECUTOR is None:
        with _FETCH_EXECUTOR_LOCK:
            if _FETCH_EXECUTOR is None:
                _FETCH_EXECUTOR = _cf.ThreadPoolExecutor(
                    max_workers=4, thread_name_prefix="tx-fetch"
                )
    return _FETCH_EXECUTOR


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

        df_raw = self._fetch_akshare_df(ak, tencent_symbol, start, end, adjust)

        # 完整性校验 + 自研按年分块补拉（640 条上限防御）：
        # akshare 内部分块异常时可能只返回最近 640 条（首日明显晚于请求起点），
        # 此处检测到即用 _fetch_tx_yearly 整体重拉缺失段，循环拼接至覆盖起点。
        df_raw = self._ensure_tx_complete(df_raw, tencent_symbol, start, end, adjust)

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

    # ------------------------------------------------------------------
    # 640 条上限防御：完整性校验 + 自研按年分块补拉
    # ------------------------------------------------------------------
    def _fetch_akshare_df(
        self, ak: Any, tencent_symbol: str, start: str, end: str, adjust: str
    ) -> "pd.DataFrame | None":
        """经 akshare 拉取（内部按年分块 + 去重），共享线程池包裹超时。"""
        try:
            _fut = _get_fetch_executor().submit(
                ak.stock_zh_a_hist_tx,
                symbol=tencent_symbol,
                start_date=start,
                end_date=end,
                adjust=adjust,
            )
            return _fut.result(timeout=self.config.network_timeout)
        except _cf.TimeoutError as _te:
            raise DataSourceError(
                self.name,
                f"akshare 腾讯源取数超时(>{self.config.network_timeout}s): {_te}",
            ) from _te
        except Exception as e:
            raise DataSourceError(self.name, f"akshare 腾讯源取数失败: {type(e).__name__}: {e}") from e

    def _ensure_tx_complete(
        self,
        df_raw: "pd.DataFrame | None",
        tencent_symbol: str,
        start: str,
        end: str,
        adjust: str,
    ) -> "pd.DataFrame | None":
        """校验 akshare 返回是否覆盖请求起点；疑似缺失时自研按年分块整体重拉。

        腾讯新接口单次最多 640 条且忽略 start（返回 end 往前 640 个交易日）。
        akshare 靠按年分块拼接弥补，但任一分块失败/行为变化会静默丢历史。
        校验规则：首日比请求起点晚超过 ``_BACKFILL_MIN_GAP_DAYS`` 天 → 判定
        疑似缺失 → ``_fetch_tx_yearly`` 从请求起点年份起循环分块重拉拼接。
        """
        if df_raw is None or df_raw.empty or "date" not in df_raw.columns:
            return df_raw
        try:
            first_dt = pd.to_datetime(df_raw["date"]).min()
            start_dt = pd.to_datetime(start)
            if (first_dt - start_dt).days <= _BACKFILL_MIN_GAP_DAYS:
                return df_raw  # 已覆盖请求起点（含老股首日与起点仅差 1~2 交易日）
        except Exception:  # noqa: BLE001 日期解析异常不阻塞主流程
            return df_raw
        # 首日明显晚于起点：可能是"次新股上市晚"（数据本就完整）或"真缺失"。
        # 用腾讯侧真实最早数据日判定，避免对次新股白跑一遍完整分块。
        try:
            tx_first = self._get_tx_start_date(tencent_symbol)
            if tx_first is not None and abs((first_dt - tx_first).days) <= _BACKFILL_MIN_GAP_DAYS:
                return df_raw  # akshare 首日 = 腾讯真实最早日 → 数据完整（次新股）
        except Exception:  # noqa: BLE001
            pass
        # 真缺失 → 自研按年分块整体重拉
        try:
            full = self._fetch_tx_yearly(tencent_symbol, start, end, adjust)
            if full is not None and len(full) > len(df_raw):
                logger.warning(
                    "[tencent] %s 数据疑似不足(akshare %s条 首日%s)，自研按年分块补拉 → %s条(首日%s)",
                    tencent_symbol, len(df_raw), str(first_dt.date()),
                    len(full), str(pd.to_datetime(full["date"]).min().date()),
                )
                return full
        except Exception as e:  # noqa: BLE001 补拉失败静默回退 akshare 结果
            logger.warning("[tencent] %s 补拉失败，回退 akshare 结果: %s", tencent_symbol, e)
        return df_raw

    def _get_tx_start_date(self, tencent_symbol: str) -> "pd.Timestamp | None":
        """查腾讯侧该股真实最早数据日（weekTrends 接口，仿 akshare get_tx_start_year）。

        用于区分"次新股上市晚（数据完整）"与"akshare 分块缺失（需补拉）"。
        失败返回 None（调用方回退到补拉判定）。
        """
        import requests  # 局部导入：仅判定/补拉路径使用

        url = "https://web.ifzq.gtimg.cn/other/klineweb/klineWeb/weekTrends"
        params = {"code": tencent_symbol, "type": "qfq", "_var": "trend_qfq", "r": "0.35"}

        def _req() -> str:
            return requests.get(url, params=params, timeout=(8, 15)).text

        try:
            _fut = _get_fetch_executor().submit(_req)
            txt = _fut.result(timeout=self.config.network_timeout)
        except Exception:  # noqa: BLE001
            return None
        idx = txt.find("={")
        if idx == -1:
            return None
        try:
            data = json.loads(txt[idx + 1:]).get("data")
            if isinstance(data, list) and data and data[0]:
                return pd.to_datetime(data[0][0])
            if isinstance(data, dict):
                sym = data.get(tencent_symbol) or {}
                arr = sym.get("day") or sym.get("qfqday") or []
                if arr and arr[0]:
                    return pd.to_datetime(arr[0][0])
        except Exception:  # noqa: BLE001
            return None
        return None

    def _fetch_tx_yearly(
        self, tencent_symbol: str, start: str, end: str, adjust: str
    ) -> "pd.DataFrame | None":
        """自研按年分块请求腾讯新接口并拼接（绕过 akshare 的 start 提升/分块逻辑）。

        每年段请求 ``{year}-01-01 ~ {year+1}-12-31``（≤640 条上限内），多段
        拼接去重即覆盖完整区间——即"数据不够时循环调用、拼接缺失数据"。
        返回标准化列 date/open/high/low/close（与主流程 col_map 后一致，幂等）。

        Returns:
            拼接后的 DataFrame；全部段失败返回 ``None``。
        """
        try:
            start_year = int(start[:4])
            end_year = int(end[:4])
        except (TypeError, ValueError):
            return None
        if start_year < 1990 or end_year < start_year or end_year > 2100:
            return None

        frames: list[pd.DataFrame] = []
        for year in range(start_year, end_year + 1):
            seg = self._fetch_tx_year_segment(tencent_symbol, year, adjust)
            if seg is not None and not seg.empty:
                frames.append(seg)
        if not frames:
            return None
        full = pd.concat(frames, ignore_index=True)
        full = full.drop_duplicates(subset="date").sort_values("date").reset_index(drop=True)
        # 按请求区间切片（与 akshare 末尾 big_df[start_date:end_date] 对齐）
        full = full[(full["date"] >= pd.to_datetime(start)) & (full["date"] <= pd.to_datetime(end))]
        return full.reset_index(drop=True)

    def _fetch_tx_year_segment(
        self, tencent_symbol: str, year: int, adjust: str
    ) -> "pd.DataFrame | None":
        """请求单一年份段（腾讯新接口），返回标准化 df 或 None。

        接口返回形如 ``kline_dayqfq2020={...}``，data[symbol] 下 day/qfqday/hfqday
        数组，每行 ``[date, open, close, high, low, ...]``（前 5 列有效）。
        """
        params = {
            "_var": f"kline_dayqfq{year}",
            "param": f"{tencent_symbol},day,{year}-01-01,{year + 1}-12-31,"
                     f"{_TX_COUNT_LIMIT},{adjust}",
            "r": "0.1",
        }
        try:
            import requests  # 局部导入：仅补拉路径使用

            def _req() -> str:
                r = requests.get(_TX_KLINE_URL, params=params, timeout=(8, 15))
                return r.text

            _fut = _get_fetch_executor().submit(_req)
            txt = _fut.result(timeout=self.config.network_timeout)
        except Exception as e:  # noqa: BLE001 单段失败跳过，不中断整体补拉
            logger.warning("[tencent] %s %s 年段请求失败: %s", tencent_symbol, year, e)
            return None
        idx = txt.find("={")
        if idx == -1:
            return None
        try:
            data = json.loads(txt[idx + 1:])
            sym_data = data["data"].get(tencent_symbol) or {}
            arr = sym_data.get("day") or sym_data.get("qfqday") or sym_data.get("hfqday")
        except Exception:  # noqa: BLE001
            return None
        if not arr:
            return None
        try:
            rows = [row[:5] for row in arr]
            df = pd.DataFrame(rows, columns=["date", "open", "close", "high", "low"])
            df["date"] = pd.to_datetime(df["date"], errors="coerce")
            for col in ("open", "close", "high", "low"):
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df = df.dropna(subset=["date", "open", "close", "high", "low"])
            return df[["date", "open", "high", "low", "close"]].copy()
        except Exception:  # noqa: BLE001
            return None

    def _to_dataframe(self, raw: Any) -> pd.DataFrame:
        """akshare 已返回 DataFrame，直接透传。"""
        if isinstance(raw, pd.DataFrame):
            return raw
        return super()._to_dataframe(raw)
