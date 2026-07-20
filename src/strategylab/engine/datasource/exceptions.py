# -*- coding: utf-8 -*-
"""数据源抽象层异常层级。

- ``DataSourceError``：所有数据源异常的基类。
- ``MissingDependencyError``：可选依赖（akshare/tushare）未安装时抛出。
- ``DataSourceUnavailableError``：数据源永久不可用（如 P2 桩、token 无效）。
- ``AllSourcesFailedError``：主源 + 所有备用源均失败时抛出。
"""

from __future__ import annotations


class DataSourceError(Exception):
    """数据源异常的基类。"""

    def __init__(self, source: str, message: str = "") -> None:
        self.source: str = source
        super().__init__(f"[{source}] {message}" if message else f"[{source}] 数据源错误")


class MissingDependencyError(DataSourceError):
    """可选依赖未安装时抛出，携带安装提示。

    示例：
        raise MissingDependencyError("akshare", hint='pip install "strategylab[akshare]"')
    """

    def __init__(
        self,
        source: str,
        message: str = "",
        hint: str = "",
    ) -> None:
        full = message or f"缺少可选依赖 '{source}'，请先安装"
        if hint:
            full = f"{full}（{hint}）"
        super().__init__(source, full)


class DataSourceUnavailableError(DataSourceError):
    """数据源永久不可用（如 P2 预留桩、token 无效/过期）。"""

    def __init__(self, source: str, message: str = "数据源不可用") -> None:
        super().__init__(source, message)


class AllSourcesFailedError(DataSourceError):
    """主源 + 所有备用源均失败（容灾耗尽）。"""

    def __init__(self, symbol: str, errors: list[tuple[str, str]]) -> None:
        self.symbol: str = symbol
        self.errors: list[tuple[str, str]] = errors
        parts = "; ".join(f"{src}: {msg[:80]}" for src, msg in errors)
        super().__init__("ALL", f"所有数据源均失败 symbol={symbol}: {parts}")


class DataMissingError(Exception):
    """verify 模式行情不足时抛出（不取数，提示先更新数据源）。

    FR-40：回测 ``ensure_data(mode='verify')`` 仅校验缓存覆盖所需区间，
    缺数据即抛此异常，由上层（``_run_one``）标记 ``batch_item`` 为 failed
    并提示用户先点『更新数据源』刷新行情，绝不静默全量重拉。
    """

    def __init__(self, symbol: str, message: str = "") -> None:
        self.symbol: str = symbol
        self.message: str = message or "行情缺失，请先点『更新数据源』刷新后再回测"
        super().__init__(f"[{symbol}] {self.message}")
