# -*- coding: utf-8 -*-
"""多数据源抽象层回归测试（FR-29 ~ FR-36）。

覆盖范围：
  A. 基础导入测试          — ``from strategylab.engine.datasource import *``
  B. 兼容层回退测试        — ``normalize_symbol`` 返回 dict（签名 100% 一致）
  C. 缺依赖报错测试        — akshare/tushare 抛 ``MissingDependencyError``
  D. 东财适配器单元测试    — mock 网络，验证限流/熔断/retry
  E. 工厂/配置测试         — ``from_env()`` 解析默认值、``get_effective_source``
  F. 缓存 key 命名测试     — 新格式 ``<prefix>_<source>_<period>.csv`` / 旧兼容
  G. CLI 导入测试          — ``cmd_status`` 零错误
  H. DB 迁移测试           — ``migrate_v2_to_v3`` ALTER + 索引 + 回填
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from strategylab.engine.datasource.exceptions import DataSourceError

# ===================================================================
# A. 基础导入测试
# ===================================================================


class TestImport:
    """确保 datasource 子包及其所有导出符号可正常导入。"""

    def test_star_import(self) -> None:
        """from strategylab.engine.datasource import * 零错误。"""
        from strategylab.engine.datasource import (  # noqa: F401
            AkshareDataSource,
            AllSourcesFailedError,
            BrokerDataSource,
            DataSource,
            DataSourceConfig,
            DataSourceError,
            DataSourceFactory,
            DataSourceUnavailableError,
            EastmoneyDataSource,
            HealthStatus,
            KlineCache,
            KlineProvider,
            MissingDependencyError,
            SwitchEvent,
            SwitchLog,
            SymbolSpec,
            TushareDataSource,
            _is_retryable_network_error,
            cmd_status,
            ensure_data,
            get_provider,
            load_bars,
            normalize_symbol,
        )
        # 验证通过（能导入即成功）
        assert DataSource is not None

    def test_data_feed_shim_import(self) -> None:
        """兼容层 data_feed.py 的三个核心函数可正常导入并使用。"""
        from strategylab.engine.data_feed import ensure_data, load_bars, normalize_symbol

        assert callable(normalize_symbol)
        assert callable(ensure_data)
        assert callable(load_bars)

    def test_cli_import(self) -> None:
        """CLI datasource status 命令可导入。"""
        from strategylab.engine.datasource.cli_status import cmd_status

        assert callable(cmd_status)


# ===================================================================
# B. 兼容层回退测试
# ===================================================================


class TestCompatibilityShim:
    """兼容层 ``data_feed.normalize_symbol`` 返回 dict 且签名与旧版一致。"""

    def test_normalize_symbol_returns_dict(self) -> None:
        """normalize_symbol("600216.SH") 返回含 symbol/secid/prefix 的 dict。"""
        from strategylab.engine.data_feed import normalize_symbol

        result = normalize_symbol("600216.SH")
        assert isinstance(result, dict), f"期望 dict, 实际 {type(result)}"
        assert "symbol" in result
        assert "secid" in result
        assert "prefix" in result
        assert result["symbol"] == "600216.SH"
        assert result["secid"] == "1.600216"
        assert result["prefix"] == "600216_sh"

    def test_normalize_symbol_various_formats(self) -> None:
        """兼容层 normalize_symbol 处理多种写法。"""
        from strategylab.engine.data_feed import normalize_symbol

        # sz 写法
        r1 = normalize_symbol("000001.SZ")
        assert r1["symbol"] == "000001.SZ"
        assert r1["secid"] == "0.000001"

        # sh 前缀写法
        r2 = normalize_symbol("sh600216")
        assert r2["symbol"] == "600216.SH"
        assert r2["prefix"] == "600216_sh"

        # bj 后缀写法
        r3 = normalize_symbol("688981bj")
        assert r3["prefix"] == "688981_bj"

    def test_normalize_symbol_dict_keys_match_old(self) -> None:
        """返回 dict 的 key 与旧版 data_feed 一致。"""
        from strategylab.engine.data_feed import normalize_symbol

        result = normalize_symbol("600216.SH")
        # 旧版返回 dict 包含这三个 key
        expected_keys = {"symbol", "secid", "prefix"}
        assert set(result.keys()) == expected_keys, (
            f"dict keys 不匹配: {set(result.keys())} != {expected_keys}"
        )

    def test_normalize_symbol_direct(self) -> None:
        """直接调 datasource.base.normalize_symbol 返回 SymbolSpec 对象。"""
        from strategylab.engine.datasource import normalize_symbol as ds_normalize

        spec = ds_normalize("600216.SH")
        # datasource 层返回 SymbolSpec，不是 dict
        assert spec.symbol == "600216.SH"
        assert spec.secid == "1.600216"
        assert spec.prefix == "600216_sh"
        # 兼容层通过 to_dict() 转换为 dict
        assert isinstance(spec.to_dict(), dict)


# ===================================================================
# C. 缺依赖报错测试
# ===================================================================


class TestMissingDependency:
    """akshare / tushare 未安装时抛出 MissingDependencyError。"""

    def setup_method(self) -> None:
        from strategylab.engine.datasource import DataSourceConfig, DataSourceFactory

        self.cfg = DataSourceConfig()
        self.factory = DataSourceFactory(self.cfg)

    def test_akshare_missing(self) -> None:
        """未安装 akshare 时 fetch_kline 抛 MissingDependencyError（懒加载在调用时触发）。"""
        from strategylab.engine.datasource import MissingDependencyError

        ds = self.factory.get("akshare", self.cfg)
        with pytest.raises(MissingDependencyError) as excinfo:
            ds.fetch_kline("600216.SH", "101", "20230101", "20240101")
        assert "akshare" in str(excinfo.value)
        assert "pip install" in str(excinfo.value) or "strategylab[akshare]" in str(excinfo.value)

    def test_tushare_missing(self) -> None:
        """未安装 tushare 时 fetch_kline 抛 MissingDependencyError（懒加载在调用时触发）。"""
        from strategylab.engine.datasource import MissingDependencyError

        ds = self.factory.get("tushare", self.cfg)
        with pytest.raises(MissingDependencyError) as excinfo:
            ds.fetch_kline("600216.SH", "101", "20230101", "20240101")
        assert "tushare" in str(excinfo.value)

    def test_broker_no_error(self) -> None:
        """broker 适配器无额外依赖，可正常创建（但取数时抛 DataSourceUnavailableError）。"""
        from strategylab.engine.datasource import DataSourceUnavailableError

        ds = self.factory.get("broker", self.cfg)
        assert ds.name == "broker"
        # 取数时抛 DataSourceUnavailableError（因为 broker 是桩）
        with pytest.raises(DataSourceUnavailableError):
            ds.fetch_kline("600216.SH", "101", "20230101", "20240101")

    def test_unknown_source(self) -> None:
        """未知数据源抛 ValueError。"""
        with pytest.raises(ValueError, match="未知数据源"):
            self.factory.get("nonexistent", self.cfg)

    def test_failed_source_cached(self) -> None:
        """已失败的源再次 fetch_kline 应抛相同异常。"""
        from strategylab.engine.datasource import MissingDependencyError

        ds = self.factory.get("akshare", self.cfg)
        with pytest.raises(MissingDependencyError):
            ds.fetch_kline("600216.SH", "101", "20230101", "20240101")
        # 第二次调用，应仍是 MissingDependencyError
        with pytest.raises(MissingDependencyError):
            ds.fetch_kline("600216.SH", "101", "20230101", "20240101")

    def test_available_sources(self) -> None:
        """available_sources() 列出所有可创建实例的源（不检查懒加载依赖）。"""
        sources = self.factory.available_sources()
        # 所有 4 个源都能创建实例（akshare/tushare 的 MissingDependencyError 在 fetch_kline 时才触发）
        assert "eastmoney" in sources
        assert "akshare" in sources
        assert "tushare" in sources
        assert "broker" in sources


# ===================================================================
# D. 东财适配器单元测试（mock 网络）
# ===================================================================


class TestEastmoneyAdapter:
    """mock 网络，验证限流/熔断/retry 等基类能力。"""

    def setup_method(self) -> None:
        from strategylab.engine.datasource import DataSourceConfig
        from strategylab.engine.datasource.eastmoney import EastmoneyDataSource

        self.cfg = DataSourceConfig(min_fetch_gap=0, cb_enabled=True)
        self.ds = EastmoneyDataSource(self.cfg)

    # ---- 正常取数 ----

    @patch("urllib.request.urlopen")
    def test_fetch_kline_success(self, mock_urlopen: MagicMock) -> None:
        """模拟东财返回有效 K 线数据，验证解析正确。"""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(
            {
                "data": {
                    "klines": [
                        "20230101,10.0,10.5,10.8,9.9,1000",
                        "20230102,10.5,11.0,11.2,10.3,2000",
                    ]
                }
            }
        ).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        df = self.ds.fetch_kline("600216.SH", "101", "20230101", "20230102")

        assert isinstance(df, pd.DataFrame)
        assert len(df) == 2
        assert list(df.columns) == ["date", "open", "close", "high", "low"]
        assert df.iloc[0]["date"] == "20230101"
        assert df.iloc[0]["close"] == 10.5

    @patch("urllib.request.urlopen")
    def test_fetch_kline_empty_response(self, mock_urlopen: MagicMock) -> None:
        """东财返回空 klines 列表。"""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(
            {"data": {"klines": []}}
        ).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        df = self.ds.fetch_kline("600216.SH", "101", "20230101", "20230102")
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 0

    @patch("urllib.request.urlopen")
    def test_fetch_kline_no_data_field(self, mock_urlopen: MagicMock) -> None:
        """东财返回无 data 字段（顶层非预期结构）的响应 → 抛 DataSourceError。"""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"code": 0}).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        with pytest.raises(DataSourceError):
            self.ds.fetch_kline("600216.SH", "101", "20230101", "20230102")

    # ---- 限流测试 ----

    @patch("urllib.request.urlopen")
    def test_throttle_respects_min_gap(self, mock_urlopen: MagicMock) -> None:
        """限流：两次连续请求之间至少间隔 min_fetch_gap 秒。"""
        from strategylab.engine.datasource import DataSourceConfig
        from strategylab.engine.datasource.eastmoney import EastmoneyDataSource

        cfg = DataSourceConfig(min_fetch_gap=0.05, cb_enabled=False)
        ds = EastmoneyDataSource(cfg)

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(
            {"data": {"klines": ["20230101,10,11,12,9,1000"]}}
        ).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        t0 = time.time()
        ds.fetch_kline("600216.SH", "101", "20230101", "20230102")
        ds.fetch_kline("600216.SH", "101", "20230102", "20230103")
        elapsed = time.time() - t0
        # 两次请求之间应有 gap（0.05s * 2 次 ≈ 0.1s 总耗时，但限流只加一次 gap）
        assert elapsed >= 0.04, f"限流未生效: elapsed={elapsed:.3f}s"

    # ---- 熔断测试 ----

    @patch("urllib.request.urlopen")
    def test_circuit_breaker_opens_after_consecutive_failures(
        self, mock_urlopen: MagicMock
    ) -> None:
        """连续 8 次失败后熔断打开。"""
        # mock urlopen 每次都抛 ConnectionError（可重试的瞬时错误）
        mock_urlopen.side_effect = ConnectionError("模拟连接失败")

        for i in range(8):
            with pytest.raises(Exception):
                # attempts=1 避免指数退避等待
                self.ds.fetch_kline("600216.SH", "101", "20230101", "20230102",
                                      attempts=1, base_delay=0.01)

        health = self.ds.health()
        assert health.status == "open", f"预期熔断打开，实际: {health.status}"
        assert health.cb_open_until > 0
        assert health.consecutive_failures == 0  # 熔断后复位

    @patch("urllib.request.urlopen")
    def test_circuit_breaker_resets_on_success(self, mock_urlopen: MagicMock) -> None:
        """一次成功取数后熔断状态复位。"""
        # 先连续失败 4 次（未到 8 次）
        mock_urlopen.side_effect = ConnectionError("模拟连接失败")
        for _ in range(4):
            with pytest.raises(Exception):
                self.ds.fetch_kline("600216.SH", "101", "20230101", "20230102",
                                      attempts=1, base_delay=0.01)

        # 验证已 degraded
        health = self.ds.health()
        assert health.status == "degraded"
        assert health.consecutive_failures == 4

        # 然后成功一次
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(
            {"data": {"klines": ["20230101,10,11,12,9,1000"]}}
        ).encode("utf-8")
        mock_urlopen.side_effect = None
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        df = self.ds.fetch_kline("600216.SH", "101", "20230101", "20230102")
        assert len(df) == 1

        # 验证健康度恢复为 ok
        health = self.ds.health()
        assert health.status == "ok"
        assert health.consecutive_failures == 0

    # ---- 重试测试 ----

    @patch("urllib.request.urlopen")
    def test_retry_on_transient_error(self, mock_urlopen: MagicMock) -> None:
        """瞬时网络错误触发重试，重试成功后返回正常数据。"""
        # 模拟 side_effect: 前 2 次抛 ConnectionError，第 3 次正常返回
        ok_resp = MagicMock()
        ok_resp.read.return_value = json.dumps(
            {"data": {"klines": ["20230101,10,11,12,9,1000"]}}
        ).encode("utf-8")
        # 让 __enter__ 返回自身（使 with 语句正常工作）
        ok_resp.__enter__.return_value = ok_resp

        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise ConnectionError(f"第 {call_count} 次失败")
            return ok_resp

        mock_urlopen.side_effect = side_effect

        df = self.ds.fetch_kline("600216.SH", "101", "20230101", "20230102",
                                  attempts=5, base_delay=0.01)
        assert len(df) == 1
        # urlopen 被调了 3 次（2 次失败 + 1 次成功）
        assert call_count == 3

    @patch("urllib.request.urlopen")
    def test_non_retryable_error_no_retry(self, mock_urlopen: MagicMock) -> None:
        """非瞬时错误（如 HTTP 400）不重试，立即抛出。"""
        from urllib.error import HTTPError

        # 模拟 HTTP 400 错误
        http_error = HTTPError(
            url="http://push2his.eastmoney.com/api/qt/stock/kline/get",
            code=400,
            msg="Bad Request",
            hdrs={},
            fp=None,
        )
        mock_urlopen.side_effect = http_error

        with pytest.raises(Exception):
            self.ds.fetch_kline("600216.SH", "101", "20230101", "20230102")

        # 只调了一次（未重试）
        assert mock_urlopen.call_count == 1

    # ---- _is_retryable_network_error 测试 ----

    def test_is_retryable_network_error(self) -> None:
        """_is_retryable_network_error 判断正确。"""
        import socket
        import http.client

        from strategylab.engine.datasource import _is_retryable_network_error

        # 可重试：ConnectionError / TimeoutError / socket.timeout / RemoteDisconnected
        assert _is_retryable_network_error(ConnectionError("reset"))
        assert _is_retryable_network_error(TimeoutError("timeout"))
        assert _is_retryable_network_error(socket.timeout("timed out"))
        assert _is_retryable_network_error(http.client.RemoteDisconnected())

        # 不可重试
        assert not _is_retryable_network_error(ValueError("bad value"))
        assert not _is_retryable_network_error(RuntimeError("unknown"))

    # ---- 健康度测试 ----

    @patch("urllib.request.urlopen")
    def test_health_status_degraded(self, mock_urlopen: MagicMock) -> None:
        """部分失败但未熔断时健康度为 degraded。"""
        mock_urlopen.side_effect = ConnectionError("失败")
        for _ in range(3):
            with pytest.raises(Exception):
                self.ds.fetch_kline("600216.SH", "101", "20230101", "20230102",
                                      attempts=1, base_delay=0.01)

        health = self.ds.health()
        assert health.status == "degraded"
        assert health.consecutive_failures == 3


# ===================================================================
# E. 工厂/配置测试
# ===================================================================


class TestDataSourceConfig:
    """DataSourceConfig 与 DataSourceFactory 行为验证。"""

    def test_default_config(self) -> None:
        """默认配置：default_source=eastmoney, cb_enabled=True, failover=True。"""
        from strategylab.engine.datasource import DataSourceConfig

        cfg = DataSourceConfig()
        assert cfg.default_source == "eastmoney"
        assert cfg.cb_enabled is True
        assert cfg.failover_enabled is True
        assert cfg.min_fetch_gap == 0.3
        assert cfg.tushare_token is None
        assert cfg.symbol_overrides == {}
        assert cfg.fallback_order == []

    def test_from_env_defaults(self) -> None:
        """无环境变量时 from_env() 返回默认值。"""
        from strategylab.engine.datasource import DataSourceConfig

        # 清除相关环境变量
        for key in [
            "STRATEGALAB_DATA_SOURCE",
            "STRATEGALAB_SYMBOL_SOURCE",
            "STRATEGALAB_FALLBACK_SOURCES",
            "STRATEGALAB_TUSHARE_TOKEN",
            "STRATEGALAB_DATASOURCE_FAILOVER",
            "STRATEGALAB_DATASOURCE_CB",
        ]:
            os.environ.pop(key, None)

        cfg = DataSourceConfig.from_env()
        assert cfg.default_source == "eastmoney"
        assert cfg.cb_enabled is True
        assert cfg.failover_enabled is True
        assert cfg.tushare_token is None

    def test_from_env_custom_values(self) -> None:
        """设置环境变量后 from_env() 正确解析。"""
        from strategylab.engine.datasource import DataSourceConfig

        try:
            os.environ["STRATEGALAB_DATA_SOURCE"] = "akshare"
            os.environ["STRATEGALAB_SYMBOL_SOURCE"] = "600216.SH:tushare,000001.SZ:akshare"
            os.environ["STRATEGALAB_FALLBACK_SOURCES"] = "tushare,broker"
            os.environ["STRATEGALAB_TUSHARE_TOKEN"] = "test_token_123"
            os.environ["STRATEGALAB_DATASOURCE_FAILOVER"] = "off"
            os.environ["STRATEGALAB_DATASOURCE_CB"] = "off"

            cfg = DataSourceConfig.from_env()
            assert cfg.default_source == "akshare"
            assert cfg.symbol_overrides == {
                "600216.SH": "tushare",
                "000001.SZ": "akshare",
            }
            assert cfg.fallback_order == ["tushare", "broker"]
            assert cfg.tushare_token == "test_token_123"
            assert cfg.failover_enabled is False
            assert cfg.cb_enabled is False
        finally:
            # 清理
            for key in [
                "STRATEGALAB_DATA_SOURCE",
                "STRATEGALAB_SYMBOL_SOURCE",
                "STRATEGALAB_FALLBACK_SOURCES",
                "STRATEGALAB_TUSHARE_TOKEN",
                "STRATEGALAB_DATASOURCE_FAILOVER",
                "STRATEGALAB_DATASOURCE_CB",
            ]:
                os.environ.pop(key, None)


class TestDataSourceFactory:
    """DataSourceFactory 行为验证。"""

    def setup_method(self) -> None:
        from strategylab.engine.datasource import DataSourceConfig, DataSourceFactory

        self.cfg = DataSourceConfig()
        self.factory = DataSourceFactory(self.cfg)

    def test_get_eastmoney(self) -> None:
        """获取 eastmoney 实例成功。"""
        from strategylab.engine.datasource.eastmoney import EastmoneyDataSource

        ds = self.factory.get("eastmoney", self.cfg)
        assert ds.name == "eastmoney"
        assert isinstance(ds, EastmoneyDataSource)

    def test_get_broker(self) -> None:
        """获取 broker 实例成功。"""
        from strategylab.engine.datasource.broker import BrokerDataSource

        ds = self.factory.get("broker", self.cfg)
        assert ds.name == "broker"
        assert isinstance(ds, BrokerDataSource)

    def test_get_caches_instance(self) -> None:
        """同名称返回缓存的同一实例。"""
        ds1 = self.factory.get("eastmoney", self.cfg)
        ds2 = self.factory.get("eastmoney", self.cfg)
        assert ds1 is ds2

    def test_get_effective_source_default(self) -> None:
        """无覆盖时返回默认源。"""
        source = self.factory.get_effective_source("600216.SH")
        assert source == "eastmoney"

    def test_get_effective_source_override(self) -> None:
        """有品种覆盖时返回覆盖源。"""
        from strategylab.engine.datasource import DataSourceConfig, DataSourceFactory

        cfg = DataSourceConfig(symbol_overrides={"600216.SH": "tushare"})
        factory = DataSourceFactory(cfg)
        source = factory.get_effective_source("600216.SH")
        assert source == "tushare"
        # 未覆盖的品种仍走默认
        source2 = factory.get_effective_source("000001.SZ")
        assert source2 == "eastmoney"

    def test_build_chain_with_eastmoney_broker(self) -> None:
        """build_chain 返回 [eastmoney, broker] 的可用链。"""
        from strategylab.engine.datasource import DataSourceConfig, DataSourceFactory

        cfg = DataSourceConfig(fallback_order=["broker"])
        factory = DataSourceFactory(cfg)
        chain = factory.build_chain("600216.SH")
        assert len(chain) >= 1
        assert chain[0].name == "eastmoney"
        if len(chain) > 1:
            assert chain[1].name == "broker"

    def test_build_chain_skips_missing_deps(self) -> None:
        """build_chain 包含所有 fallback_order 中的源（akshare/tushare 可创建实例，仅在取数时报错）。"""
        from strategylab.engine.datasource import (
            DataSourceConfig,
            DataSourceFactory,
        )

        cfg = DataSourceConfig(
            fallback_order=["akshare", "tushare", "broker"]
        )
        factory = DataSourceFactory(cfg)
        chain = factory.build_chain("600216.SH")
        # 主源 eastmoney，所有备用源都被包括（akshare/tushare 可创建实例）
        names = [ds.name for ds in chain]
        assert "eastmoney" in names
        assert "akshare" in names
        assert "tushare" in names
        assert "broker" in names


# ===================================================================
# F. 缓存 key 命名测试
# ===================================================================


class TestKlineCacheKey:
    """缓存文件命名规则：新格式含 source 标识，旧格式兼容回退。"""

    def setup_method(self) -> None:
        from strategylab.engine.datasource import KlineCache, SymbolSpec

        self.cache = KlineCache()
        self.spec = SymbolSpec(symbol="600216.SH", secid="1.600216", prefix="600216_sh")
        self.tmpdir = Path(tempfile.mkdtemp())

    def teardown_method(self) -> None:
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_new_format_csv_path(self) -> None:
        """新格式 CSV: <prefix>_<source>_<period>.csv。"""
        csv_path = self.cache._csv_path(self.tmpdir, "600216_sh", "eastmoney", "daily")
        expected = self.tmpdir / "600216_sh_eastmoney_daily.csv"
        assert csv_path == expected

    def test_new_format_meta_path(self) -> None:
        """新格式 meta: <prefix>_<source>_<period>_meta.json。"""
        meta_path = self.cache._meta_path(self.tmpdir, "600216_sh", "eastmoney", "daily")
        expected = self.tmpdir / "600216_sh_eastmoney_daily_meta.json"
        assert meta_path == expected

    def test_legacy_csv_path(self) -> None:
        """旧格式 CSV: <prefix>_<period>.csv。"""
        legacy_path = self.cache._legacy_csv_path(self.tmpdir, "600216_sh", "daily")
        expected = self.tmpdir / "600216_sh_daily.csv"
        assert legacy_path == expected

    def test_save_creates_new_format(self) -> None:
        """save() 写入新格式文件（含 source 标识）。"""
        df = pd.DataFrame({
            "date": pd.to_datetime(["2023-01-01", "2023-01-02"]),
            "open": [10.0, 11.0],
            "high": [12.0, 13.0],
            "low": [9.0, 10.0],
            "close": [11.0, 12.0],
        })
        csv_path = self.cache.save(
            self.spec, "eastmoney", "daily", df, "20230101", "20230102",
            out_dir=self.tmpdir,
        )
        # 验证文件名为新格式
        expected = self.tmpdir / "600216_sh_eastmoney_daily.csv"
        assert csv_path == expected
        assert csv_path.exists()
        # 验证 meta 文件也存在
        meta_path = self.tmpdir / "600216_sh_eastmoney_daily_meta.json"
        assert meta_path.exists()

    def test_save_creates_meta_with_source(self) -> None:
        """save() 写入的 meta 文件包含 source 字段。"""
        df = pd.DataFrame({
            "date": pd.to_datetime(["2023-01-01"]),
            "open": [10.0],
            "high": [12.0],
            "low": [9.0],
            "close": [11.0],
        })
        self.cache.save(
            self.spec, "eastmoney", "daily", df, "20230101", "20230102",
            out_dir=self.tmpdir,
        )
        meta_path = self.tmpdir / "600216_sh_eastmoney_daily_meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["source"] == "eastmoney"
        assert meta["beg"] == "20230101"
        assert meta["end"] == "20230102"
        assert meta["version"] == 2

    def test_load_new_format(self) -> None:
        """load() 可读取新格式缓存。"""
        df = pd.DataFrame({
            "date": pd.to_datetime(["2023-01-01", "2023-01-02"]),
            "open": [10.0, 11.0],
            "high": [12.0, 13.0],
            "low": [9.0, 10.0],
            "close": [11.0, 12.0],
        })
        self.cache.save(
            self.spec, "eastmoney", "daily", df, "20230101", "20230105",
            out_dir=self.tmpdir,
        )
        loaded = self.cache.load(
            self.spec, "eastmoney", "daily", "20230101", "20230103",
            out_dir=self.tmpdir,
        )
        assert loaded is not None
        assert len(loaded) == 2

    def test_load_legacy_fallback(self) -> None:
        """load() 在无新格式时自动回退到旧格式缓存。"""
        # 创建旧格式 CSV 和 meta
        old_csv = self.tmpdir / "600216_sh_daily.csv"
        old_meta = self.tmpdir / "600216_sh_daily_meta.json"
        old_csv.write_text("date,open,high,low,close\n2023-01-01,10,12,9,11\n", encoding="utf-8")
        old_meta.write_text(
            json.dumps({"beg": "20230101", "end": "20230105", "rows": 1}),
            encoding="utf-8",
        )

        loaded = self.cache.load(
            self.spec, "eastmoney", "daily", "20230101", "20230103",
            out_dir=self.tmpdir,
        )
        assert loaded is not None, "旧格式回退失败"
        assert len(loaded) == 1

    def test_load_legacy_fallback_non_eastmoney(self) -> None:
        """非 eastmoney 源的回退应被拒绝（只有 eastmoney 走旧格式兼容）。"""
        old_csv = self.tmpdir / "600216_sh_daily.csv"
        old_meta = self.tmpdir / "600216_sh_daily_meta.json"
        old_csv.write_text("date,open,high,low,close\n2023-01-01,10,12,9,11\n", encoding="utf-8")
        old_meta.write_text(
            json.dumps({"beg": "20230101", "end": "20230105", "rows": 1}),
            encoding="utf-8",
        )

        # 用 akshare 源请求 → 不应回退到旧格式
        loaded = self.cache.load(
            self.spec, "akshare", "daily", "20230101", "20230103",
            out_dir=self.tmpdir,
        )
        assert loaded is None, "非 eastmoney 不应回退到旧格式"

    def test_load_not_covered(self) -> None:
        """请求区间不在缓存覆盖范围内时返回 None。"""
        df = pd.DataFrame({
            "date": pd.to_datetime(["2023-01-01"]),
            "open": [10.0],
            "high": [12.0],
            "low": [9.0],
            "close": [11.0],
        })
        self.cache.save(
            self.spec, "eastmoney", "daily", df, "20230101", "20230101",
            out_dir=self.tmpdir,
        )
        # 请求超出缓存范围
        loaded = self.cache.load(
            self.spec, "eastmoney", "daily", "20230102", "20230105",
            out_dir=self.tmpdir,
        )
        assert loaded is None

    def test_covers_logic(self) -> None:
        """_covers 区间覆盖判断正确。"""
        meta = {"beg": "20230101", "end": "20230201"}
        assert self.cache._covers(meta, "20230101", "20230201") is True
        assert self.cache._covers(meta, "20230115", "20230120") is True
        assert self.cache._covers(meta, "20230101", "20230202") is False  # 超出末尾
        assert self.cache._covers(meta, "20221201", "20230101") is False  # 超出开头
        assert self.cache._covers(None, "20230101", "20230201") is False  # None

    def test_read_any_meta_new_format(self) -> None:
        """_read_any_meta 优先读取新格式。"""
        # 写新格式
        new_meta_path = self.tmpdir / "600216_sh_eastmoney_daily_meta.json"
        new_meta_path.write_text(
            json.dumps({"beg": "20230101", "end": "20230201", "source": "eastmoney"}),
            encoding="utf-8",
        )
        meta, source, is_legacy = self.cache._read_any_meta(
            self.tmpdir, "600216_sh", "eastmoney", "daily"
        )
        assert meta is not None
        assert source == "eastmoney"
        assert is_legacy is False

    def test_read_any_meta_legacy_fallback(self) -> None:
        """_read_any_meta 旧格式回退时 is_legacy=True。"""
        # 只写旧格式
        old_meta_path = self.tmpdir / "600216_sh_daily_meta.json"
        old_meta_path.write_text(
            json.dumps({"beg": "20230101", "end": "20230201"}),
            encoding="utf-8",
        )
        meta, source, is_legacy = self.cache._read_any_meta(
            self.tmpdir, "600216_sh", "eastmoney", "daily"
        )
        assert meta is not None
        assert source == "eastmoney"
        assert is_legacy is True
        # 旧格式 meta 被补了 source 字段
        assert meta.get("source") == "eastmoney"


# ===================================================================
# G. CLI 导入测试
# ===================================================================


class TestCliStatus:
    """CLI datasource status 命令测试。"""

    def test_cmd_status_import(self) -> None:
        """cmd_status 可正常导入。"""
        from strategylab.engine.datasource.cli_status import cmd_status

        assert callable(cmd_status)

    def test_cmd_status_runs(self) -> None:
        """cmd_status 可正常执行并返回字符串。"""
        from strategylab.engine.datasource.cli_status import cmd_status

        result = cmd_status()
        assert isinstance(result, str)
        assert "当前默认数据源" in result
        assert "东财(eastmoney)" in result
        assert "broker" in result
        assert "健康度" in result

    def test_cmd_status_contains_overrides(self) -> None:
        """cmd_status 包含覆盖配置信息。"""
        from strategylab.engine.datasource.cli_status import cmd_status

        result = cmd_status()
        assert "按品种覆盖" in result
        assert "备用源顺序" in result
        assert "熔断总开关" in result


# ===================================================================
# H. DB 迁移测试（v2 → v3）
# ===================================================================


class TestMigrationV2ToV3:
    """migrate_v2_to_v3 迁移测试：ALTER + 索引 + 回填。"""

    def _make_v2_db(self, path: str) -> None:
        """构造一个 v2 数据库（有 params_hash 列但无 data_source 列）。"""
        conn = sqlite3.connect(path)
        conn.execute(
            """
            CREATE TABLE backtest_runs (
                run_id VARCHAR(36) PRIMARY KEY,
                batch_id VARCHAR(36),
                strategy_type VARCHAR(64),
                strategy_name VARCHAR(128),
                symbol VARCHAR(32),
                symbol_name VARCHAR(128),
                start DATE,
                end DATE,
                initial_cash NUMERIC(18,4),
                params_json TEXT,
                params_hash VARCHAR(40),
                positions_json TEXT,
                meta_json TEXT,
                created_at DATETIME
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE schema_version (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                version INTEGER NOT NULL,
                applied_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            INSERT INTO schema_version (version) VALUES (2)
            """
        )
        conn.commit()
        conn.close()

    def _insert_v2_runs(self, path: str, rows: list[dict]) -> None:
        conn = sqlite3.connect(path)
        for r in rows:
            conn.execute(
                """
                INSERT INTO backtest_runs
                  (run_id, strategy_type, strategy_name, symbol, symbol_name,
                   start, end, initial_cash, params_json, params_hash, positions_json, meta_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    r["run_id"],
                    r.get("strategy_type", "kdj_macd_dual_entry"),
                    r["strategy_name"],
                    r["symbol"],
                    r["symbol_name"],
                    r["start"],
                    r["end"],
                    r["initial_cash"],
                    r["params_json"],
                    r.get("params_hash", ""),
                    r.get("positions_json", "[]"),
                    r.get("meta_json", "{}"),
                ),
            )
        conn.commit()
        conn.close()

    V2_ROWS = [
        {
            "run_id": "bbbbbbbb-0000-0000-0000-000000000001",
            "strategy_name": "策略A",
            "symbol": "600216.SH",
            "symbol_name": "浙江医药",
            "start": "2023-01-01",
            "end": "2024-01-01",
            "initial_cash": 1000000.0,
            "params_json": '{"type":"kdj_macd_dual_entry","name":"策略A"}',
            "params_hash": "a" * 40,
        },
        {
            "run_id": "bbbbbbbb-0000-0000-0000-000000000002",
            "strategy_name": "策略B",
            "symbol": "000001.SZ",
            "symbol_name": "平安银行",
            "start": "2023-01-01",
            "end": "2024-01-01",
            "initial_cash": 500000.0,
            "params_json": '{"type":"kdj_macd_dual_entry","name":"策略B"}',
            "params_hash": "b" * 40,
        },
    ]

    def test_migrate_adds_data_source_column(self) -> None:
        """migrate_v2_to_v3 增加 data_source 列。"""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._make_v2_db(path)
        self._insert_v2_runs(path, self.V2_ROWS)

        from strategylab.engine.storage.migrate import migrate_v2_to_v3
        from sqlalchemy import create_engine, inspect

        engine = create_engine(f"sqlite:///{path}")
        migrate_v2_to_v3(engine)

        insp = inspect(engine)
        columns = [c["name"] for c in insp.get_columns("backtest_runs")]
        assert "data_source" in columns, "data_source 列应存在"

    def test_migrate_backfills_eastmoney(self) -> None:
        """回填历史 run 的 data_source 为 'eastmoney'。"""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._make_v2_db(path)
        self._insert_v2_runs(path, self.V2_ROWS)

        from strategylab.engine.storage.migrate import migrate_v2_to_v3
        from sqlalchemy import create_engine, text

        engine = create_engine(f"sqlite:///{path}")
        migrate_v2_to_v3(engine)

        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT run_id, data_source FROM backtest_runs ORDER BY run_id")
            ).fetchall()
        assert len(rows) == 2
        for run_id, ds in rows:
            assert ds == "eastmoney", f"run {run_id} 的 data_source 应为 eastmoney, 实际 {ds}"

    def test_migrate_creates_index(self) -> None:
        """迁移后 data_source 索引存在。"""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._make_v2_db(path)
        self._insert_v2_runs(path, self.V2_ROWS[:1])

        from strategylab.engine.storage.migrate import migrate_v2_to_v3
        from sqlalchemy import create_engine, inspect

        engine = create_engine(f"sqlite:///{path}")
        migrate_v2_to_v3(engine)

        insp = inspect(engine)
        indexes = {idx["name"] for idx in insp.get_indexes("backtest_runs")}
        assert "ix_backtest_runs_data_source" in indexes

    def test_migrate_sets_schema_version(self) -> None:
        """迁移后 schema_version = 3。"""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._make_v2_db(path)
        self._insert_v2_runs(path, self.V2_ROWS[:1])

        from qa_helpers import temp_db

        with temp_db(f"sqlite:///{path}"):
            from strategylab.engine.storage.migrate import migrate_v2_to_v3
            from sqlalchemy import text

            # 此时全局引擎和 SessionLocal 都指向 path
            from strategylab.engine.storage.db import get_engine
            migrate_v2_to_v3(get_engine())

            with get_engine().connect() as conn:
                row = conn.execute(
                    text("SELECT version FROM schema_version ORDER BY id DESC LIMIT 1")
                ).fetchone()
            assert row is not None
            assert int(row[0]) == 3

    def test_migrate_idempotent(self) -> None:
        """幂等：重复迁移不报错。"""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._make_v2_db(path)
        self._insert_v2_runs(path, self.V2_ROWS[:1])

        from qa_helpers import temp_db

        with temp_db(f"sqlite:///{path}"):
            from strategylab.engine.storage.migrate import migrate_v2_to_v3
            from strategylab.engine.storage.db import get_engine

            migrate_v2_to_v3(get_engine())  # 第一次
            migrate_v2_to_v3(get_engine())  # 第二次（幂等）
        # 不抛异常即可

    def test_migrate_v2_to_v3_via_init_db(self) -> None:
        """通过 init_db() 触发 v2→v3 迁移。"""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._make_v2_db(path)
        self._insert_v2_runs(path, self.V2_ROWS[:1])

        from strategylab.engine.storage import migrate as migrate_mod
        from strategylab.engine.storage.db import init_db, _engine, _SessionFactory
        from sqlalchemy import create_engine

        # 先用已有引擎触发
        old_environ = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = f"sqlite:///{path}"
        # 重置引擎单例
        import strategylab.engine.storage.db as db_mod

        db_mod._engine = None
        db_mod._SessionFactory = None

        try:
            init_db()  # 触发迁移
            ver = migrate_mod.get_version()
            assert ver == 3, f"schema_version 应为 3, 实际 {ver}"

            from sqlalchemy import inspect, text

            engine = db_mod.get_engine()
            insp = inspect(engine)
            columns = [c["name"] for c in insp.get_columns("backtest_runs")]
            assert "data_source" in columns, "data_source 列应存在"

            # 验证回填
            with engine.connect() as conn:
                rows = conn.execute(
                    text("SELECT data_source FROM backtest_runs")
                ).fetchall()
            for (ds,) in rows:
                assert ds == "eastmoney"
        finally:
            if old_environ is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = old_environ
            db_mod._engine = None
            db_mod._SessionFactory = None


# ===================================================================
# SymbolSpec 工具类测试
# ===================================================================


class TestSymbolSpec:
    """SymbolSpec 数据类行为验证。"""

    def test_to_dict(self) -> None:
        from strategylab.engine.datasource import SymbolSpec

        spec = SymbolSpec(symbol="600216.SH", secid="1.600216", prefix="600216_sh")
        d = spec.to_dict()
        assert d == {"symbol": "600216.SH", "secid": "1.600216", "prefix": "600216_sh"}

    def test_normalize_sh(self) -> None:
        from strategylab.engine.datasource import normalize_symbol

        spec = normalize_symbol("600216.SH")
        assert spec.symbol == "600216.SH"
        assert spec.secid == "1.600216"
        assert spec.prefix == "600216_sh"

    def test_normalize_sh_prefix(self) -> None:
        from strategylab.engine.datasource import normalize_symbol

        spec = normalize_symbol("sh600216")
        assert spec.symbol == "600216.SH"
        assert spec.secid == "1.600216"

    def test_normalize_sz(self) -> None:
        from strategylab.engine.datasource import normalize_symbol

        spec = normalize_symbol("000001.SZ")
        assert spec.symbol == "000001.SZ"
        assert spec.secid == "0.000001"
        assert spec.prefix == "000001_sz"

    def test_normalize_sz_suffix(self) -> None:
        from strategylab.engine.datasource import normalize_symbol

        spec = normalize_symbol("000001sz")
        assert spec.symbol == "000001.SZ"

    def test_normalize_bj(self) -> None:
        from strategylab.engine.datasource import normalize_symbol

        spec = normalize_symbol("688981.BJ")
        assert spec.symbol == "688981.BJ"
        assert spec.secid == "0.688981"

    def test_normalize_fallback_sh(self) -> None:
        """无后缀时默认兜底 sh。"""
        from strategylab.engine.datasource import normalize_symbol

        spec = normalize_symbol("600216")
        assert spec.symbol == "600216.SH"
        assert spec.secid == "1.600216"

    def test_normalize_zfill(self) -> None:
        """不足 6 位的代码自动补零。"""
        from strategylab.engine.datasource import normalize_symbol

        spec = normalize_symbol("216.SH")
        assert spec.symbol == "000216.SH"
        assert spec.secid == "1.000216"


# ===================================================================
# 异常层级测试
# ===================================================================


class TestExceptions:
    """异常层级行为验证。"""

    def test_datasource_error(self) -> None:
        from strategylab.engine.datasource import DataSourceError

        err = DataSourceError("eastmoney", "取数失败")
        assert err.source == "eastmoney"
        assert "eastmoney" in str(err)
        assert "取数失败" in str(err)

    def test_missing_dependency_error(self) -> None:
        from strategylab.engine.datasource import MissingDependencyError

        err = MissingDependencyError("akshare", hint='pip install "strategylab[akshare]"')
        assert err.source == "akshare"
        assert "pip install" in str(err)

    def test_missing_dependency_is_datasource_error(self) -> None:
        from strategylab.engine.datasource import DataSourceError, MissingDependencyError

        assert issubclass(MissingDependencyError, DataSourceError)

    def test_all_sources_failed_error(self) -> None:
        from strategylab.engine.datasource import AllSourcesFailedError

        err = AllSourcesFailedError(
            "600216.SH",
            [("eastmoney", "HTTPError: 500"), ("akshare", "MissingDependencyError")],
        )
        assert err.symbol == "600216.SH"
        assert "600216.SH" in str(err)
        assert "eastmoney" in str(err)
        assert "akshare" in str(err)


# ===================================================================
# SwitchLog 测试
# ===================================================================


class TestSwitchLog:
    """容灾切换日志行为验证。"""

    def test_record_and_recent(self) -> None:
        from strategylab.engine.datasource import SwitchEvent, SwitchLog
        from pathlib import Path

        tmpfile = Path(tempfile.mkstemp(suffix=".jsonl")[1])
        try:
            slog = SwitchLog(file_path=tmpfile, ring_size=10)
            e1 = SwitchEvent(
                timestamp=1000.0, symbol="600216.SH", period="daily",
                from_source="eastmoney", to_source="akshare", reason="超时",
            )
            e2 = SwitchEvent(
                timestamp=2000.0, symbol="000001.SZ", period="weekly",
                from_source="akshare", to_source="tushare", reason="限流",
            )
            slog.record(e1)
            slog.record(e2)

            recent = slog.recent(5)
            assert len(recent) == 2
            # 时间降序
            assert recent[0].symbol == "000001.SZ"
            assert recent[1].symbol == "600216.SH"
        finally:
            try:
                os.unlink(tmpfile)
            except OSError:
                pass

    def test_ring_buffer_overflow(self) -> None:
        """环形缓冲溢出时保留最近的 N 条。"""
        from strategylab.engine.datasource import SwitchEvent, SwitchLog

        slog = SwitchLog(file_path=None, ring_size=3)
        for i in range(5):
            slog.record(
                SwitchEvent(
                    timestamp=float(i), symbol=f"sym{i}", period="daily",
                    from_source="a", to_source="b", reason="test",
                )
            )
        recent = slog.recent(10)
        assert len(recent) == 3  # 最多 3 条
        symbols = [e.symbol for e in recent]
        assert "sym2" in symbols
        assert "sym3" in symbols
        assert "sym4" in symbols

    def test_jsonl_file_written(self) -> None:
        """record() 同时写入了 JSONL 文件。"""
        from strategylab.engine.datasource import SwitchEvent, SwitchLog
        from pathlib import Path

        tmpfile = Path(tempfile.mkstemp(suffix=".jsonl")[1])
        try:
            slog = SwitchLog(file_path=tmpfile)
            slog.record(
                SwitchEvent(
                    timestamp=3000.0, symbol="600216.SH", period="daily",
                    from_source="eastmoney", to_source="akshare", reason="超时",
                )
            )
            content = tmpfile.read_text(encoding="utf-8")
            assert "600216.SH" in content
            assert "eastmoney" in content
            assert "timestamp_iso" in content
        finally:
            try:
                os.unlink(tmpfile)
            except OSError:
                pass


# ===================================================================
# KlineProvider / ensure_data 模块级函数测试
# ===================================================================


class TestKlineProvider:
    """KlineProvider 编排器基础行为。"""

    def test_get_provider_import(self) -> None:
        """get_provider 可正常导入。"""
        from strategylab.engine.datasource import get_provider

        provider = get_provider()
        assert provider is not None

    def test_ensure_data_import(self) -> None:
        """ensure_data 可正常导入。"""
        from strategylab.engine.datasource import ensure_data

        assert callable(ensure_data)

    def test_ensure_data_from_data_feed(self) -> None:
        """兼容层 data_feed.ensure_data 可导入。"""
        from strategylab.engine.data_feed import ensure_data

        assert callable(ensure_data)

    def test_load_bars_import(self) -> None:
        """load_bars 可正常导入。"""
        from strategylab.engine.datasource import load_bars

        assert callable(load_bars)

    def test_load_bars_from_data_feed(self) -> None:
        """兼容层 data_feed.load_bars 可导入。"""
        from strategylab.engine.data_feed import load_bars

        assert callable(load_bars)
