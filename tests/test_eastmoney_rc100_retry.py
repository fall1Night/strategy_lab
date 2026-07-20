# -*- coding: utf-8 -*-
"""东财适配器 rc=100 / https / 分页取数 修复的回归测试。

回归目标（对应 infrastructure 500 崩溃前未完成的那一轮验证）：
  1. ``rc=100``（东财限流/拒绝，明文 http 被拒）必须是``可重试``错误，
     由基类 ``fetch_kline`` 的指数退避重试循环接管，而不是一次性致命抛出。
  2. ``API_URL`` 必须升级为 ``https://push2his.eastmoney.com/...``，
     且 query 带 ``ut=fa5fd1943c7b386f172d6893dbfba10b``、headers 带 ``Host``。
  3. ``_raw_fetch_kline`` 改为分页取数，按行首日期去重合并，返回按日期升序。

全程用 ``unittest.mock.patch`` 替换 ``urllib.request.urlopen``，不依赖真实网络。

运行（受管 Python）：
    python -m pytest tests/test_eastmoney_null_response.py tests/test_eastmoney_rc100_retry.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

# 让 tests/ 能 import 到 src/strategylab（与 conftest / 兄弟测试一致，便于独立运行）
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from strategylab.engine.datasource import DataSourceConfig, _is_retryable_network_error
from strategylab.engine.datasource.eastmoney import EastmoneyDataSource
from strategylab.engine.datasource.exceptions import DataSourceError, RetryableDataSourceError


# ---------------------------------------------------------------------------
# 构造辅助
# ---------------------------------------------------------------------------
def _make_ds() -> EastmoneyDataSource:
    """构造 EastmoneyDataSource（min_fetch_gap=0、熔断关闭，专注取数逻辑）。"""
    cfg = DataSourceConfig(min_fetch_gap=0, cb_enabled=False)
    return EastmoneyDataSource(cfg)


def _ctx_with(payload: dict) -> MagicMock:
    """构造一个 urlopen 的返回值（上下文管理器），其 ``read()`` 返回给定 JSON payload。"""
    ctx = MagicMock()
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode("utf-8")
    ctx.__enter__.return_value = resp
    return ctx


def _ctx_with_raw(raw: bytes) -> MagicMock:
    """同上，但直接给原始字节（用于非 JSON / 异常响应）。"""
    ctx = MagicMock()
    resp = MagicMock()
    resp.read.return_value = raw
    ctx.__enter__.return_value = resp
    return ctx


# ---------------------------------------------------------------------------
# 用例 A：可重试识别（基类重试循环依赖此判定）
# ---------------------------------------------------------------------------
def test_A_retryable_recognition() -> None:
    """``RetryableDataSourceError`` 必须被 ``_is_retryable_network_error`` 判为可重试；
    且它必须是 ``DataSourceError`` 的子类，保证既有 ``pytest.raises(DataSourceError)`` 不破。
    """
    assert _is_retryable_network_error(RetryableDataSourceError("eastmoney", "x")) is True
    assert isinstance(RetryableDataSourceError("e", "x"), DataSourceError) is True


# ---------------------------------------------------------------------------
# 用例 B：分页去重 + 升序
# ---------------------------------------------------------------------------
_PAYLOAD_FIRST = {
    "rc": 0,
    "data": {"klines": ["2019-01-02,1,2,3,4,5", "2021-12-31,9,9,9,9,9"]},
}
_PAYLOAD_REST = {
    "rc": 0,
    "data": {"klines": ["2021-12-31,9,9,9,9,9", "2024-01-03,7,7,7,7,7"]},
}


@patch("time.sleep", lambda *a, **k: None)  # 跳过限流/退避真实 sleep，避免测试慢/抖
@patch("urllib.request.urlopen")
def test_B_paged_dedup_ascending(mock_urlopen: MagicMock) -> None:
    """mock urlopen 按请求 URL 的 ``beg=`` 区分两段：
    - beg=20190101 段 → 含 2019-01-02 与 2021-12-31
    - 后续段（重叠 2021-12-31）→ 含 2021-12-31 与 2024-01-03
    断言：date 列升序、重叠日 2021-12-31 仅出现一次、含首尾两个日期。
    """

    def _se(req, *args, **kwargs):
        url = req.full_url
        payload = _PAYLOAD_FIRST if "beg=20190101" in url else _PAYLOAD_REST
        return _ctx_with(payload)

    mock_urlopen.side_effect = _se

    ds = _make_ds()
    df = ds.fetch_kline("600839.SH", "101", "20190101", "20240103", lmt=1500)

    dates = list(df["date"])
    # 升序（ISO 日期字符串字典序==时间序）
    assert dates == sorted(dates), f"date 列未升序: {dates}"
    # 重叠日去重
    assert dates.count("2021-12-31") == 1, f"重叠日 2021-12-31 未去重: {dates}"
    # 含首尾
    assert "2019-01-02" in dates, f"缺少 2019-01-02: {dates}"
    assert "2024-01-03" in dates, f"缺少 2024-01-03: {dates}"
    # 三段合并去重后应恰好 3 行
    assert len(df) == 3, f"行数应为 3: {dates}"


# ---------------------------------------------------------------------------
# 用例 C：rc=100 被重试后成功
# ---------------------------------------------------------------------------
_PAYLOAD_RC100 = {"rc": 100, "rt": 1, "data": None}
_PAYLOAD_OK = {"rc": 0, "data": {"klines": ["2024-01-02,1,2,3,4,5"]}}


@patch("time.sleep", lambda *a, **k: None)
@patch("urllib.request.urlopen")
def test_C_rc100_retry_then_success(mock_urlopen: MagicMock) -> None:
    """第一次返回 rc=100（明文 http 被拒），第二次返回 rc=0。
    基类指数退避重试应让 rc=100 重试后成功，返回含 1 行（date=2024-01-02）的 DataFrame。
    """
    calls = {"n": 0}

    def _se(req, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _ctx_with(_PAYLOAD_RC100)
        return _ctx_with(_PAYLOAD_OK)

    mock_urlopen.side_effect = _se

    ds = _make_ds()
    df = ds.fetch_kline(
        "600839.SH", "101", "20240101", "20240102", lmt=1500, base_delay=0.0
    )

    assert isinstance(df, pd.DataFrame), "应返回 DataFrame"
    assert len(df) == 1, f"应含 1 行: {df}"
    assert df.iloc[0]["date"] == "2024-01-02", f"日期应为 2024-01-02: {df.iloc[0].to_dict()}"
    assert calls["n"] >= 2, f"rc=100 应触发重试，实际 urlopen 调用次数={calls['n']}"


@patch("time.sleep", lambda *a, **k: None)
@patch("urllib.request.urlopen")
def test_C2_rc100_retryable_even_after_exhaustion(mock_urlopen: MagicMock) -> None:
    """边界：若每次都返回 rc=100（重试耗尽），基类最终抛 ``DataSourceError``（既有测试不被破）。
    说明：耗尽后基类包装错误为非可重试的 DataSourceError（设计如此，停止重试避免死循环）；
    但底层 rc=100 错误本质仍是 ``RetryableDataSourceError``，故一次“全新调用”仍会正常重试——
    这正是本 bug 修复的核心：修复前 rc=100 压根不会进入重试循环。
    """
    mock_urlopen.side_effect = lambda req, *a, **k: _ctx_with(_PAYLOAD_RC100)

    ds = _make_ds()
    with pytest.raises(DataSourceError) as excinfo:
        ds.fetch_kline(
            "600839.SH", "101", "20240101", "20240102", lmt=1500, attempts=4, base_delay=0.0
        )

    # 重试耗尽后基类抛 DataSourceError（保证既有 pytest.raises(DataSourceError) 测试通过）
    assert isinstance(excinfo.value, DataSourceError)
    # 底层 rc=100 性质仍是 RetryableDataSourceError，可被判定为可重试
    assert _is_retryable_network_error(RetryableDataSourceError("eastmoney", "rc=100")) is True


# ---------------------------------------------------------------------------
# 用例 D：https URL 生效（host / ut 参数 / Host 头）
# ---------------------------------------------------------------------------
@patch("urllib.request.urlopen")
def test_D_https_url_and_headers(mock_urlopen: MagicMock) -> None:
    """捕获真实请求 URL 与 headers，断言：
    - URL 以 https://push2his.eastmoney.com/ 开头（修复明文 http 被拒）
    - query 含 ut=fa5fd1943c7b386f172d6893dbfba10b
    - headers 含 Host: push2his.eastmoney.com
    """
    # _ctx_with 返回的是上下文管理器（其 __enter__ 返回 resp），直接赋给 return_value
    mock_urlopen.return_value = _ctx_with({"rc": 0, "data": {"klines": []}})

    ds = _make_ds()
    # 用 _raw_fetch_kline 直接取数（单段），避免重试/限流干扰 URL 捕获
    ds._raw_fetch_kline("600839.SH", "101", "20240101", "20240102")

    req = mock_urlopen.call_args.args[0]
    url = req.full_url

    assert url.startswith("https://push2his.eastmoney.com/"), f"URL 未用 https: {url}"
    assert "ut=fa5fd1943c7b386f172d6893dbfba10b" in url, f"URL 缺 ut 参数: {url}"
    assert req.get_header("Host") == "push2his.eastmoney.com", (
        f"Host 头不正确: {req.get_header('Host')!r}"
    )
