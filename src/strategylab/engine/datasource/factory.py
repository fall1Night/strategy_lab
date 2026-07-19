# -*- coding: utf-8 -*-
"""数据源工厂：按配置构建数据源实例、解析默认源/按品种覆盖、产出容灾链。

- ``DataSourceFactory``：单例模式管理已构建的适配器实例。
- ``get(name, cfg)``：按名称获取或创建数据源实例（含懒加载/缺依赖检测）。
- ``get_effective_source(symbol)``：解析某标的的生效数据源（按品种覆盖优先）。
- ``build_chain(symbol)``：构建容灾链 [主源] + 备用源（去重）。
- ``available_sources()``：列出已成功加载且可用的数据源名称。
"""

from __future__ import annotations

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
            name: 数据源名称（``eastmoney`` / ``akshare`` / ``tushare`` / ``broker``）。
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
        elif name == "tushare":
            from .tushare_src import TushareDataSource

            return TushareDataSource(cfg)
        elif name == "broker":
            from .broker import BrokerDataSource

            return BrokerDataSource(cfg)
        else:
            raise ValueError(f"未知数据源: {name}，支持: eastmoney, akshare, tushare, broker")

    def get_effective_source(self, symbol: str) -> str:
        """解析某标的的生效数据源名称。

        优先级：按品种覆盖 > 全局默认源。

        Args:
            symbol: 标准化代码（如 ``600216.SH``）。

        Returns:
            数据源名称（如 ``eastmoney``）。
        """
        sym_clean = symbol.strip()
        # 按品种覆盖优先
        if sym_clean in self._config.symbol_overrides:
            return self._config.symbol_overrides[sym_clean]
        return self._config.default_source

    def build_chain(self, symbol: str) -> list[DataSource]:
        """构建某标的的容灾链：[主源] + 备用源（去重，排除主源自身）。

        备用源仅包含已成功加载的源；未安装/加载失败的源静默跳过。

        Args:
            symbol: 标准化代码。

        Returns:
            ``[主源, 备用源1, 备用源2, ...]`` 的 ``DataSource`` 实例列表。
        """
        primary_name = self.get_effective_source(symbol)
        chain: list[DataSource] = []
        seen: set[str] = set()

        # 主源放第一位
        try:
            primary = self.get(primary_name, self._config)
        except MissingDependencyError:
            # 主源都不可用 → 仅靠备用源
            primary = None

        if primary is not None:
            chain.append(primary)
            seen.add(primary.name)

        # 追加备用源（跳过已在链中的）
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
        for name in ("eastmoney", "akshare", "tushare", "broker"):
            try:
                self.get(name, self._config)
                result.append(name)
            except (MissingDependencyError, ValueError):
                continue
        return result
