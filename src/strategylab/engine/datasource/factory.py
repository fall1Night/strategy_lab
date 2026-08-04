# -*- coding: utf-8 -*-
"""数据源工厂：按配置构建数据源实例、解析默认源/按品种覆盖、产出容灾链。

- ``DataSourceFactory``：单例模式管理已构建的适配器实例。
- ``get(name, cfg)``：按名称获取或创建数据源实例（含懒加载/缺依赖检测）。
- ``get_effective_source(symbol)``：解析某标的的生效数据源（按品种覆盖优先）。
- ``build_chain(symbol)``：构建容灾链 [主源] + 备用源（去重）。
- ``available_sources()``：列出已成功加载且可用的数据源名称。
"""

from __future__ import annotations

import hashlib

from .base import DataSource
from .config import DataSourceConfig
from .exceptions import MissingDependencyError


class DataSourceFactory:
    """数据源工厂：单例管理适配器实例。

    用法：
        cfg = DataSourceConfig.from_env()
        factory = DataSourceFactory(cfg)
        ds = factory.get("eastmoney", cfg)
        effective = factory.get_effective_source("600216.SH")
        chain = factory.build_chain("600216.SH")
    """

    def __init__(self, config: DataSourceConfig) -> None:
        self._config: DataSourceConfig = config
        # 已创建的实例缓存：{name: DataSource}
        self._instances: dict[str, DataSource] = {}
        # 已尝试加载但失败的源：{name: MissingDependencyError}
        self._failed: dict[str, MissingDependencyError] = {}

    def get(self, name: str, cfg: DataSourceConfig) -> DataSource:
        """按名称获取或创建数据源实例。

        对 akshare / tushare 等可选依赖源，首次加载时懒导入；若依赖缺失
        则抛 ``MissingDependencyError``。

        Args:
            name: 数据源名称（``eastmoney`` / ``akshare`` / ``tencent`` / ``tushare`` / ``broker``）。
            cfg: ``DataSourceConfig`` 实例。

        Returns:
            ``DataSource`` 实例。

        Raises:
            MissingDependencyError: 可选依赖未安装。
            ValueError: 未知的数据源名称。
        """
        name = name.strip().lower()
        if name in self._instances:
            return self._instances[name]
        if name in self._failed:
            raise self._failed[name]

        try:
            instance = self._create(name, cfg)
        except MissingDependencyError as e:
            self._failed[name] = e
            raise
        except Exception:
            raise

        self._instances[name] = instance
        return instance

    def _create(self, name: str, cfg: DataSourceConfig) -> DataSource:
        """内部创建方法，按名称分发到具体适配器构造函数。"""
        if name == "eastmoney":
            from .eastmoney import EastmoneyDataSource

            return EastmoneyDataSource(cfg)
        elif name == "akshare":
            from .akshare_src import AkshareDataSource

            return AkshareDataSource(cfg)
        elif name == "tencent":
            from .tencent_src import TencentDataSource

            return TencentDataSource(cfg)
        elif name == "tushare":
            from .tushare_src import TushareDataSource

            return TushareDataSource(cfg)
        elif name == "broker":
            from .broker import BrokerDataSource

            return BrokerDataSource(cfg)
        else:
            raise ValueError(f"未知数据源: {name}，支持: eastmoney, akshare, tencent, tushare, broker")

    def get_effective_source(self, symbol: str) -> str:
        """解析某标的的生效主源名称。

        优先级：按品种覆盖 > **活跃源稳定轮询** > 全局默认源。

        多源轮询（2026-08-04，防 IP 限制）：配置了 ``active_sources``（如
        ``akshare,tencent,eastmoney``）时，按 ``md5(symbol)`` 取模稳定分配到
        其中一个源——同一标的永远落在同一源（缓存文件前缀一致、可复用），
        不同标的均匀分散到各源，**单源只承担 1/N 的请求量**，显著降低任一源
        被高频触发 IP 限制的概率。用 md5 而非内置 ``hash()``：后者受
        PYTHONHASHSEED 影响，进程重启后同标的会漂移到不同源导致缓存分裂。

        Args:
            symbol: 标准化代码（如 ``600216.SH``）。

        Returns:
            数据源名称（如 ``akshare``）。
        """
        sym_clean = symbol.strip()
        # 按品种覆盖优先
        if sym_clean in self._config.symbol_overrides:
            return self._config.symbol_overrides[sym_clean]
        active = self._config.active_sources or [self._config.default_source]
        if len(active) <= 1:
            return active[0]
        digest = hashlib.md5(sym_clean.encode("utf-8")).hexdigest()
        idx = int(digest, 16) % len(active)
        return active[idx]

    def build_chain(self, symbol: str) -> list[DataSource]:
        """构建某标的的容灾链：主源 + 其他活跃源 + 备用源（去重，排除主源自身）。

        链顺序（2026-08-04 多源轮询）：
          1. 主源 = ``get_effective_source`` 轮询结果；
          2. 其他活跃源（同轮询池内其余源，失败优先切它们，保持分散）；
          3. ``fallback_order`` 备用源（如 tushare 等非活跃源）。

        备用源仅包含已成功加载的源；未安装/加载失败的源静默跳过。

        Args:
            symbol: 标准化代码。

        Returns:
            ``[主源, 活跃源2, 活跃源3, 备用源1, ...]`` 的 ``DataSource`` 实例列表。
        """
        primary_name = self.get_effective_source(symbol)
        chain: list[DataSource] = []
        seen: set[str] = set()

        # 主源放第一位
        try:
            primary = self.get(primary_name, self._config)
        except MissingDependencyError:
            primary = None

        if primary is not None:
            chain.append(primary)
            seen.add(primary.name)

        # 追加其他活跃源（保持多源分散：主源挂了优先切活跃池内其他源）
        for name in self._config.active_sources:
            if name in seen:
                continue
            try:
                ds = self.get(name, self._config)
            except MissingDependencyError:
                continue
            chain.append(ds)
            seen.add(ds.name)

        # 追加 fallback_order 备用源
        for fb_name in self._config.fallback_order:
            if fb_name in seen:
                continue
            try:
                ds = self.get(fb_name, self._config)
            except MissingDependencyError:
                continue
            chain.append(ds)
            seen.add(ds.name)

        return chain

    def available_sources(self) -> list[str]:
        """列出所有已成功加载的数据源名称。

        Returns:
            已加载数据源名称列表（按注册顺序）。
        """
        result: list[str] = []
        for name in ("eastmoney", "akshare", "tencent", "tushare", "broker"):
            try:
                self.get(name, self._config)
                result.append(name)
            except (MissingDependencyError, ValueError):
                continue
        return result
