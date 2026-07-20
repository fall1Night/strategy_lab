# -*- coding: utf-8 -*-
"""数据源抽象基类：模板方法 + 每源独立限流/熔断 + 符号归一化。

- ``SymbolSpec``：归一化后的标的标识（symbol/secid/prefix）。
- ``HealthStatus``：数据源健康快照。
- ``normalize_symbol``：复用既有归一化逻辑，将多种写法统一为 ``SymbolSpec``。
- ``_is_retryable_network_error``：判断是否为瞬时网络错误（全适配器复用）。
- ``DataSource``：抽象基类，提供 ``fetch_kline`` 模板方法骨架（限流 → 等熔断 → 重试循环 →
  成功复位/失败计数），把"真正取数"下放给抽象方法 ``_raw_fetch_kline``。
"""

from __future__ import annotations

import http.client
import random
import socket
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote

import pandas as pd

from .exceptions import DataSourceError

# ---------------------------------------------------------------------------
# 请求伪装：随机 User-Agent / Referer 池（防 IP 指纹封禁）
# ---------------------------------------------------------------------------
_DEFAULT_USER_AGENTS: list[str] = [
    # Chrome 系列
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    # Edge 系列
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36 Edg/128.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
    # Firefox 系列
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:129.0) Gecko/20100101 Firefox/129.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
]

_DEFAULT_REFERERS: list[str] = [
    "https://quote.eastmoney.com/",
    "https://www.eastmoney.com/",
    "https://finance.eastmoney.com/",
    "https://data.eastmoney.com/",
    "https://guba.eastmoney.com/",
]


def _ascii_safe(value: str) -> str:
    """保证 HTTP 头值能用 latin-1 编码：非 latin-1 字符做百分号编码。

    HTTP 请求头须以 latin-1 编码，含非 ASCII 字符（如中文）的值会让
    ``urllib.request`` 抛 ``UnicodeEncodeError``。这里对无法 latin-1 编码的
    部分做百分号编码（浏览器等效行为），使任意头值都不会让发送方崩溃。
    """
    try:
        value.encode("latin-1")
        return value
    except UnicodeEncodeError:
        # 对 URL 型头，把非 ASCII 部分百分号编码（保留 URL 结构字符）
        return quote(value, safe=":/?=&%#")


def random_headers() -> dict[str, str]:
    """随机生成请求头，每次调用不同 UA/Referer，降低指纹识别风险。"""
    return {
        "User-Agent": _ascii_safe(random.choice(_DEFAULT_USER_AGENTS)),
        "Referer": _ascii_safe(random.choice(_DEFAULT_REFERERS)),
    }


def random_sleep(base: float = 1.0, jitter: float = 2.0) -> None:
    """带随机抖动的 sleep，使请求间隔不规则，更像人类行为。"""
    time.sleep(base + random.uniform(0, jitter))

# ---------------------------------------------------------------------------
# 符号归一化
# ---------------------------------------------------------------------------
# A 股: sh=1, sz/bj=0（东方财富 secid 市场位）
_MARKET_PREFIX: dict[str, str] = {"sh": "1", "sz": "0", "bj": "0"}


@dataclass
class SymbolSpec:
    """归一化后的标的标识。

    Attributes:
        symbol: 展示用标准化代码，如 ``600216.SH``。
        secid: 东方财富格式，如 ``1.600216``（sh→1, sz/bj→0）。
        prefix: 文件前缀，如 ``600216_sh``。
    """

    symbol: str
    secid: str
    prefix: str

    def to_dict(self) -> dict[str, str]:
        """转为兼容旧接口的 dict（供兼容层 re-export）。"""
        return {"symbol": self.symbol, "secid": self.secid, "prefix": self.prefix}


@dataclass
class HealthStatus:
    """数据源健康快照。

    Attributes:
        name: 数据源名称，如 ``eastmoney``。
        status: 健康状态字符串：``ok`` | ``degraded`` | ``open`` | ``unavailable``。
        last_success_ts: 最近一次成功的 Unix 时间戳（0 表示从未成功）。
        last_failure_ts: 最近一次失败的 Unix 时间戳（0 表示从未失败）。
        consecutive_failures: 连续失败次数。
        cb_open_until: 熔断冷却持续到的时间戳（0 表示未熔断）。
    """

    name: str
    status: str = "ok"
    last_success_ts: float = 0.0
    last_failure_ts: float = 0.0
    consecutive_failures: int = 0
    cb_open_until: float = 0.0


def normalize_symbol(symbol: str) -> SymbolSpec:
    """把多种写法归一为 ``SymbolSpec``。

    接受: ``'600216.SH'`` / ``'sh600216'`` / ``'002001.SZ'`` / ``'300765.sz'`` 等。
    港股/美股暂按原样拼接 secid（东方财富 116/105 等需额外映射，本环境以 A 股为主）。

    返回:
        ``SymbolSpec`` 含 ``symbol`` / ``secid`` / ``prefix`` 三个字段。
    """
    s: str = symbol.strip().lower()
    # 形式 1: 600216.sh
    if "." in s:
        code, exch = s.split(".", 1)
        exch = exch.strip()
    else:
        # 形式 2: sh600216 / 600216sh
        for pref in ("sh", "sz", "bj"):
            if s.startswith(pref):
                exch, code = pref, s[len(pref) :]
                break
            if s.endswith(pref):
                code, exch = s[: -len(pref)], pref
                break
        else:
            code, exch = s, "sh"  # 兜底
    code = code.zfill(6)
    exch = exch.lower()

    if exch in _MARKET_PREFIX:
        secid = f"{_MARKET_PREFIX[exch]}.{code}"
    else:
        # 港股/美股：东方财富特殊 secid，超出本环境范围；保留原样尝试
        secid = f"{exch}.{code}"
    display: str = f"{code.upper()}.{exch.upper()}"
    prefix: str = f"{code}_{exch}"
    return SymbolSpec(symbol=display, secid=secid, prefix=prefix)


# ---------------------------------------------------------------------------
# 瞬时网络错误判定（全适配器复用，沿用既有逻辑）
# ---------------------------------------------------------------------------
def _is_retryable_network_error(exc: BaseException) -> bool:
    """判断异常是否属瞬时网络错误，值得重试。

    'Remote end closed connection without response' 是 http.client.RemoteDisconnected，
    在并发拉取行情时上游偶发关闭连接所致，属瞬时错误，重试通常可恢复。
    """
    from .exceptions import RetryableDataSourceError

    if isinstance(exc, RetryableDataSourceError):
        return True
    if isinstance(
        exc,
        (ConnectionError, TimeoutError, socket.timeout, http.client.RemoteDisconnected),
    ):
        return True
    if isinstance(exc, URLError):
        return isinstance(
            exc.reason,
            (ConnectionError, TimeoutError, socket.timeout, http.client.RemoteDisconnected),
        )
    if isinstance(exc, HTTPError):
        return 500 <= getattr(exc, "code", 0) < 600
    return False


# ---------------------------------------------------------------------------
# DataSource 抽象基类（模板方法 + 每源独立限流/熔断）
# ---------------------------------------------------------------------------
class DataSource(ABC):
    """数据源抽象基类：模板方法骨架 + 每源独立限流/熔断。

    子类只需实现 ``_raw_fetch_kline``（真正取数逻辑），通用能力（限流、
    重试、熔断、健康度）由基类统管。

    每源独立的限流/熔断状态以实例属性存储，避免全局共享导致故障源误伤其他源。

    Attributes:
        name: 数据源名称，如 ``eastmoney`` / ``akshare`` / ``tushare``。
        config: ``DataSourceConfig`` 配置实例。
    """

    # ------------------------------------------------------------------
    # 每源独立的限流/熔断状态（实例属性，非类属性）
    # ------------------------------------------------------------------
    _FETCH_LOCK: threading.Lock
    _LAST_FETCH_TS: float
    _CB_LOCK: threading.Lock
    _CONSEC_FAIL: int
    _CB_COOLDOWN: float
    _CB_COOLDOWN_MAX: float
    _CB_UNTIL: float
    _LAST_SUCCESS_TS: float
    _LAST_FAILURE_TS: float

    def __init__(self, name: str, config: "DataSourceConfig") -> None:  # noqa: F821
        self.name: str = name
        self.config: "DataSourceConfig" = config  # noqa: F821

        # 每源独立限流
        self._FETCH_LOCK = threading.Lock()
        self._LAST_FETCH_TS = 0.0

        # 每源独立熔断（阈值 4 次，起始冷却 60s，上限 600s）
        self._CB_LOCK = threading.Lock()
        self._CONSEC_FAIL = 0
        self._CB_COOLDOWN = 60.0
        self._CB_COOLDOWN_MAX = 600.0
        self._CB_UNTIL = 0.0

        # 健康度追踪
        self._LAST_SUCCESS_TS = 0.0
        self._LAST_FAILURE_TS = 0.0

    # ------------------------------------------------------------------
    # 模板方法：fetch_kline
    # ------------------------------------------------------------------
    def fetch_kline(
        self,
        symbol: str,
        period: str,
        start: str,
        end: str,
        **params: Any,
    ) -> pd.DataFrame:
        """模板方法：限流 → 等熔断 → 重试循环 → 成功复位/失败计数。

        调用方只需传入参数，基类自动处理限流、熔断等待、指数退避重试。
        重试仅针对瞬时网络错误；非瞬时错误直接抛出。

        Args:
            symbol: 标准化代码（如 ``600216.SH``）。
            period: 周期标识（适配器自行解释，如东财 ``"101"`` / ``"102"``）。
            start: 起始日期 ``YYYYMMDD``。
            end: 结束日期 ``YYYYMMDD``。
            **params: 适配器特定参数（如东财 ``lmt``）。

        Returns:
            pandas DataFrame，包含 K 线数据。

        Raises:
            DataSourceError: 重试耗尽且非瞬时错误时抛出。
        """
        attempts: int = int(params.pop("attempts", 4))
        base_delay: float = float(params.pop("base_delay", 1.0))
        last_exc: BaseException | None = None

        for attempt in range(1, attempts + 1):
            self._wait_circuit_breaker()
            self._throttle()
            try:
                result = self._raw_fetch_kline(symbol, period, start, end, **params)
                self._on_fetch_success()
                # 把原始数据转为 DataFrame（子类可覆盖 _to_dataframe）
                return self._to_dataframe(result)
            except Exception as e:  # noqa: BLE001
                retryable = _is_retryable_network_error(e)
                if not retryable:
                    self._on_fetch_failure()
                    raise
                self._on_fetch_failure()
                if attempt == attempts:
                    raise DataSourceError(
                        self.name,
                        f"重试 {attempts} 次后仍失败: {type(e).__name__}: {e}",
                    ) from e
                last_exc = e
                time.sleep(base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.5))

        # 保底（逻辑上不会到达）
        raise DataSourceError(self.name, "未知错误")

    # ------------------------------------------------------------------
    # 抽象方法：子类实现真正取数
    # ------------------------------------------------------------------
    @abstractmethod
    def _raw_fetch_kline(
        self,
        symbol: str,
        period: str,
        start: str,
        end: str,
        **params: Any,
    ) -> Any:
        """适配器真正取数逻辑。返回原始数据（list/dict/DataFrame），由基类 ``_to_dataframe`` 转换。

        Args:
            symbol: 标准化代码。
            period: 周期标识（适配器自行解释）。
            start: YYYYMMDD。
            end: YYYYMMDD。
            **params: 适配器特定参数。

        Returns:
            适配器原生返回（list of str / list of dict / DataFrame）。
        """
        ...

    # ------------------------------------------------------------------
    # 数据转换（可覆盖）
    # ------------------------------------------------------------------
    def _to_dataframe(self, raw: Any) -> pd.DataFrame:
        """把 ``_raw_fetch_kline`` 原始结果转为 pandas DataFrame。

        默认处理 list[str]（东财 CSV 行），子类可覆盖实现自己的转换。
        """
        if raw is None:
            return pd.DataFrame()
        if isinstance(raw, pd.DataFrame):
            return raw
        if isinstance(raw, list) and len(raw) == 0:
            return pd.DataFrame()
        if isinstance(raw, list) and isinstance(raw[0], str):
            # 东财格式：每行 "date,open,close,high,low,..."
            rows: list[dict[str, Any]] = []
            for line in raw:
                parts = line.split(",")
                if len(parts) < 5:
                    continue
                rows.append(
                    {
                        "date": parts[0],
                        "open": float(parts[1]),
                        "close": float(parts[2]),
                        "high": float(parts[3]),
                        "low": float(parts[4]),
                    }
                )
            if not rows:
                return pd.DataFrame()
            return pd.DataFrame(rows)
        if isinstance(raw, list) and isinstance(raw[0], dict):
            return pd.DataFrame(raw)
        return pd.DataFrame(raw)

    # ------------------------------------------------------------------
    # 符号归一化
    # ------------------------------------------------------------------
    def normalize(self, symbol: str) -> SymbolSpec:
        """默认复用全局 ``normalize_symbol``，子类可按需覆盖。"""
        return normalize_symbol(symbol)

    # ------------------------------------------------------------------
    # 健康度
    # ------------------------------------------------------------------
    def health(self) -> HealthStatus:
        """返回当前健康快照。

        - ``ok``：未熔断且近期无大量失败。
        - ``degraded``：有失败但未触发熔断。
        - ``open``：熔断中。
        - ``unavailable``：配置上不可用（子类覆盖）。
        """
        with self._CB_LOCK:
            cb_open = self._CB_UNTIL > time.time()
            consec = self._CONSEC_FAIL
        if cb_open:
            return HealthStatus(
                name=self.name,
                status="open",
                last_success_ts=self._LAST_SUCCESS_TS,
                last_failure_ts=self._LAST_FAILURE_TS,
                consecutive_failures=consec,
                cb_open_until=self._CB_UNTIL,
            )
        if consec > 0:
            return HealthStatus(
                name=self.name,
                status="degraded",
                last_success_ts=self._LAST_SUCCESS_TS,
                last_failure_ts=self._LAST_FAILURE_TS,
                consecutive_failures=consec,
                cb_open_until=0.0,
            )
        return HealthStatus(
            name=self.name,
            status="ok",
            last_success_ts=self._LAST_SUCCESS_TS,
            last_failure_ts=self._LAST_FAILURE_TS,
            consecutive_failures=0,
            cb_open_until=0.0,
        )

    # ------------------------------------------------------------------
    # 私有：每源限流
    # ------------------------------------------------------------------
    def _throttle(self) -> None:
        """保证任意两次上游请求之间至少间隔 ``config.min_fetch_gap`` 秒，加随机抖动。

        另有　10% 概率完全跳过本次请求（让请求节奏进一步稀疏化），
        进一步降低被识别为爬虫的风险。
        """
        # 10% 概率跳过本次请求
        if random.random() < 0.10:
            time.sleep(self.config.min_fetch_gap + random.uniform(0, 3.0))
            return

        min_gap = self.config.min_fetch_gap
        with self._FETCH_LOCK:
            now = time.time()
            gap = min_gap - (now - self._LAST_FETCH_TS)
            if gap > 0:
                time.sleep(gap + random.uniform(0, 3.0))
            else:
                if random.random() < 0.33:
                    time.sleep(random.uniform(0.5, 2.0))
            self._LAST_FETCH_TS = time.time()

    def _wait_circuit_breaker(self) -> None:
        """若处于熔断冷却中，则阻塞等待直到冷却结束。"""
        if not self.config.cb_enabled:
            return
        while True:
            with self._CB_LOCK:
                until = self._CB_UNTIL
            sleep_needed = until - time.time()
            if sleep_needed <= 0:
                break
            time.sleep(min(sleep_needed, 5))

    def _on_fetch_failure(self) -> None:
        """记录一次失败；连续失败达阈值（8 次）则打开熔断。"""
        if not self.config.cb_enabled:
            return
        with self._CB_LOCK:
            self._CONSEC_FAIL += 1
            self._LAST_FAILURE_TS = time.time()
            if self._CONSEC_FAIL >= 3:
                self._CB_UNTIL = time.time() + self._CB_COOLDOWN
                self._CB_COOLDOWN = min(self._CB_COOLDOWN * 2, self._CB_COOLDOWN_MAX)
                self._CONSEC_FAIL = 0

    def _on_fetch_success(self) -> None:
        """复位熔断/冷却状态。"""
        with self._CB_LOCK:
            self._CONSEC_FAIL = 0
            self._CB_COOLDOWN = 60.0
            self._CB_UNTIL = 0.0
            self._LAST_SUCCESS_TS = time.time()
