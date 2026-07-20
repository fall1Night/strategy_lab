# -*- coding: utf-8 -*-
"""东财适配器 ``_raw_fetch_kline`` 空响应/异常响应的回归测试。

回归目标：修复前 ``{"data": null}`` 会让
``data.get("data", {}).get("klines")`` 在 ``None`` 上调用 ``.get`` 抛
``AttributeError``，或因返回 ``[]`` 而静默写入空数据。修复后所有异常输入
应统一抛 ``DataSourceError``（带可读消息），不再崩溃、也不再静默吞错。

全程用 ``unittest.mock.patch`` 替换 ``urllib.request.urlopen``，不依赖真实网络。

运行（受管 Python）：
    python -m pytest tests/test_eastmoney_null_response.py -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from urllib.error import HTTPError, URLError

# 让 tests/ 能 import 到 src/strategylab（与 conftest 一致，便于独立运行）
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from strategylab.engine.datasource import DataSourceConfig
from strategylab.engine.datasource.eastmoney import EastmoneyDataSource
from strategylab.engine.datasource.exceptions import DataSourceError


# 任意合法 A 股 symbol，用于触发 normalize（无网络）
_SYMBOL = "600216.SH"
_PERIOD = "101"
_START = "20190101"
_END = "20190131"


def _make_ds(network_timeout: float | None = None) -> EastmoneyDataSource:
    """构造一个 EastmoneyDataSource 实例。

    - 传数字 → 使用真实 DataSourceConfig（network_timeout=该值）。
    - 传 None → 使用真实 DataSourceConfig 默认（15.0）。
    """
    cfg = DataSourceConfig(min_fetch_gap=0, cb_enabled=False)
    if network_timeout is not None:
        cfg.network_timeout = network_timeout
    return EastmoneyDataSource(cfg)


def _make_resp(raw_bytes: bytes) -> MagicMock:
    """构造一个 mock 响应对象，其 ``read()`` 返回给定字节。"""
    resp = MagicMock()
    resp.read.return_value = raw_bytes
    return resp


class TestNullResponseRegression:
    """覆盖 A~E 五类异常响应 + F 配置接入。"""

    # ------------------------------------------------------------------
    # 用例 A：接口返回 {"data": null} —— 原崩溃点
    # ------------------------------------------------------------------
    @patch("urllib.request.urlopen")
    def test_A_null_data_raises_datasourceerror_not_attributeerror(
        self, mock_urlopen: MagicMock
    ) -> None:
        """``{"data": null}`` 必须抛 DataSourceError（含 返回空数据 / payload=None），
        且绝不抛 AttributeError（即原 ``None.get('klines')`` 崩溃已被消除）。"""
        raw = json.dumps({"rc": 0, "rt": 0, "data": None}).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = _make_resp(raw)

        ds = _make_ds()
        with pytest.raises(DataSourceError) as excinfo:
            ds._raw_fetch_kline(_SYMBOL, _PERIOD, _START, _END)

        msg = str(excinfo.value)
        # 关键：消息必须给出可诊断信息
        assert "返回空数据" in msg, f"消息缺少 '返回空数据': {msg}"
        assert "payload=None" in msg, f"消息缺少 'payload=None': {msg}"
        # 关键：修复前是 AttributeError，这里类型必须正确
        assert not isinstance(excinfo.value, AttributeError)
        assert isinstance(excinfo.value, DataSourceError)

    # ------------------------------------------------------------------
    # 用例 B：正常响应
    # ------------------------------------------------------------------
    @patch("urllib.request.urlopen")
    def test_B_normal_response_returns_klines_list(
        self, mock_urlopen: MagicMock
    ) -> None:
        """正常响应应原样返回 klines 字符串列表。"""
        kline = "2024-01-02,1.0,2.0,3.0,4.0,5.0,6.0,7.0,8.0,9.0"
        raw = json.dumps({"data": {"klines": [kline]}}).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = _make_resp(raw)

        ds = _make_ds()
        result = ds._raw_fetch_kline(_SYMBOL, _PERIOD, _START, _END)

        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0] == kline

    # ------------------------------------------------------------------
    # 用例 C：非 JSON 响应（如网关 502 HTML）
    # ------------------------------------------------------------------
    @patch("urllib.request.urlopen")
    def test_C_non_json_response_raises_datasourceerror(
        self, mock_urlopen: MagicMock
    ) -> None:
        """非 JSON 文本（502 网关）应抛 DataSourceError 且消息含 '响应非 JSON'。"""
        raw = b"<html>502 Bad Gateway</html>"
        mock_urlopen.return_value.__enter__.return_value = _make_resp(raw)

        ds = _make_ds()
        with pytest.raises(DataSourceError) as excinfo:
            ds._raw_fetch_kline(_SYMBOL, _PERIOD, _START, _END)

        assert "响应非 JSON" in str(excinfo.value)

    # ------------------------------------------------------------------
    # 用例 D：响应非 dict（如 [] 或 JSON 字符串）
    # ------------------------------------------------------------------
    @patch("urllib.request.urlopen")
    @pytest.mark.parametrize(
        "raw",
        [
            b"[]",               # JSON 数组（非 dict）
            b'"abc"',            # JSON 字符串字面量（非 dict）
        ],
        ids=["list", "json_string"],
    )
    def test_D_non_dict_response_raises_datasourceerror(
        self, mock_urlopen: MagicMock, raw: bytes
    ) -> None:
        """顶层响应非 dict 应抛 DataSourceError 且消息含 '非dict'。"""
        mock_urlopen.return_value.__enter__.return_value = _make_resp(raw)

        ds = _make_ds()
        with pytest.raises(DataSourceError) as excinfo:
            ds._raw_fetch_kline(_SYMBOL, _PERIOD, _START, _END)

        assert "非dict" in str(excinfo.value)

    # ------------------------------------------------------------------
    # 用例 E：网络/HTTP 错误
    # ------------------------------------------------------------------
    @patch("urllib.request.urlopen")
    @pytest.mark.parametrize(
        "err",
        [
            HTTPError(
                "http://push2his.eastmoney.com/", 500, "Internal Server Error", {}, None
            ),
            URLError("connection refused"),
        ],
        ids=["HTTPError", "URLError"],
    )
    def test_E_network_error_raises_datasourceerror(
        self, mock_urlopen: MagicMock, err: Exception
    ) -> None:
        """urlopen 抛 HTTPError/URLError 应转译为 DataSourceError，消息含 '请求失败'。"""
        mock_urlopen.side_effect = err

        ds = _make_ds()
        with pytest.raises(DataSourceError) as excinfo:
            ds._raw_fetch_kline(_SYMBOL, _PERIOD, _START, _END)

        assert "请求失败" in str(excinfo.value)

    # ------------------------------------------------------------------
    # 用例 F：配置接入确认（network_timeout → urlopen timeout）
    # ------------------------------------------------------------------
    @patch("urllib.request.urlopen")
    def test_F_timeout_read_from_config(self, mock_urlopen: MagicMock) -> None:
        """urlopen 的 timeout 取自 config.network_timeout；缺省时回退 30。"""
        kline = "2024-01-02,1.0,2.0,3.0,4.0,5.0,6.0,7.0,8.0,9.0"
        raw = json.dumps({"data": {"klines": [kline]}}).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = _make_resp(raw)

        # F1：真实默认 config（network_timeout=15.0）
        ds_default = _make_ds()  # 使用默认 15.0
        ds_default._raw_fetch_kline(_SYMBOL, _PERIOD, _START, _END)
        assert mock_urlopen.call_args.kwargs.get("timeout") == 15.0

        # F2：自定义值应被读取（证明非硬编码）
        ds_custom = _make_ds(network_timeout=7.5)
        ds_custom._raw_fetch_kline(_SYMBOL, _PERIOD, _START, _END)
        assert mock_urlopen.call_args.kwargs.get("timeout") == 7.5

        # F3：哑对象缺失 network_timeout 时回退到 getattr 默认 30
        dummy = SimpleNamespace()  # 无 network_timeout 属性
        ds_dummy = EastmoneyDataSource(dummy)  # type: ignore[arg-type]
        ds_dummy._raw_fetch_kline(_SYMBOL, _PERIOD, _START, _END)
        assert mock_urlopen.call_args.kwargs.get("timeout") == 30
