# -*- coding: utf-8 -*-
"""K 线取数编排器（Provider）：选源 → 查缓存 → 容灾链取数 → 记切换 → 写缓存。

对外暴露 ``ensure_data`` —— 与旧 ``data_feed.ensure_data`` 签名完全一致，
使 ``backtest.py`` 零改动。
"""

from __future__ import annotations

import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import pandas as pd
import threading

from .base import DataSource, SymbolSpec, normalize_symbol
from .cache import KlineCache
from .config import DataSourceConfig
from .exceptions import AllSourcesFailedError, DataSourceError
from .factory import DataSourceFactory
from .switch_log import SwitchEvent, SwitchLog


# 进程内 symbol 锁，防止同 secid 并发取数（复用既有机制）
_SYMBOL_LOCKS: dict[str, threading.Lock] = defaultdict(threading.Lock)

# 模块级单例（延迟初始化）
_config: DataSourceConfig | None = None
_factory: DataSourceFactory | None = None
_cache: KlineCache | None = None
_switch_log: SwitchLog | None = None


def _get_config() -> DataSourceConfig:
    """获取模块级配置单例。"""
    global _config
    if _config is None:
        _config = DataSourceConfig.from_env()
    return _config


def _get_factory() -> DataSourceFactory:
    """获取模块级工厂单例。"""
    global _factory
    if _factory is None:
        _factory = DataSourceFactory(_get_config())
    return _factory


def _get_cache() -> KlineCache:
    """获取模块级缓存单例。"""
    global _cache
    if _cache is None:
        _cache = KlineCache()
    return _cache


def _get_switch_log() -> SwitchLog:
    """获取模块级切换日志单例。"""
    global _switch_log
    if _switch_log is None:
        from ...settings import get_data_dir

        data_dir = get_data_dir()
        _switch_log = SwitchLog(file_path=data_dir / "datasource_switch_log.jsonl")
    return _switch_log


class KlineProvider:
    """K 线取数编排器。

    内聚"选源 → 查缓存 → 容灾链取数 → 记切换 → 写缓存"的编排策略。

    对外 ``ensure_data`` 签名与旧版完全一致，保证 ``backtest.py`` 零改动。
    """

    def __init__(
        self,
        config: DataSourceConfig | None = None,
        factory: DataSourceFactory | None = None,
        cache: KlineCache | None = None,
        switch_log: SwitchLog | None = None,
    ) -> None:
        self._config = config or _get_config()
        self._factory = factory or _get_factory()
        self._cache = cache or _get_cache()
        self._switch_log = switch_log or _get_switch_log()

    def ensure_data(
        self,
        symbol_cfg: dict[str, str],
        out_dir: str | Path,
        daily_beg: str = "20220706",
        daily_end: str = "20260718",
        daily_lmt: int = 1500,
        weekly_beg: str = "20211210",
        weekly_end: str = "20260718",
        weekly_lmt: int = 500,
    ) -> tuple[Path, Path]:
        """拉取或复用日线/周线 CSV，返回 (daily_csv, weekly_csv) 路径。

        签名与旧 ``data_feed.ensure_data`` 完全一致。
        缓存文件名带 source 标识：``<prefix>_<source>_<period>.csv``。

        Args:
            symbol_cfg: ``normalize_symbol`` 返回的 dict（含 symbol/secid/prefix）。
            out_dir: 缓存输出目录。
            daily_beg: 日线起始 YYYYMMDD。
            daily_end: 日线结束 YYYYMMDD。
            daily_lmt: 日线条数上限。
            weekly_beg: 周线起始 YYYYMMDD。
            weekly_end: 周线结束 YYYYMMDD。
            weekly_lmt: 周线条数上限。

        Returns:
            ``(daily_csv_path, weekly_csv_path)``。
        """
        out_dir = Path(out_dir)
        prefix: str = symbol_cfg["prefix"]
        secid: str = symbol_cfg.get("secid", "")
        symbol: str = symbol_cfg["symbol"]
        spec = SymbolSpec(symbol=symbol, secid=secid, prefix=prefix)

        # 解析生效数据源
        effective_source = self._factory.get_effective_source(symbol)

        # 新格式 CSV 路径
        daily_csv = out_dir / f"{prefix}_{effective_source}_daily.csv"
        weekly_csv = out_dir / f"{prefix}_{effective_source}_weekly.csv"

        # 区间覆盖判断（meta 缺失视为缓存不存在 → 触发重取）
        need_daily = not self._cache._covers(
            self._cache._read_any_meta(out_dir, prefix, effective_source, "daily")[0],
            daily_beg,
            daily_end,
        )
        need_weekly = not self._cache._covers(
            self._cache._read_any_meta(out_dir, prefix, effective_source, "weekly")[0],
            weekly_beg,
            weekly_end,
        )
        # 也检查旧格式缓存（兼容 eastmoney）
        if need_daily and effective_source == "eastmoney":
            old_meta = self._cache._read_meta(
                self._cache._legacy_meta_path(out_dir, prefix, "daily")
            )
            need_daily = not self._cache._covers(old_meta, daily_beg, daily_end)
            if not need_daily:
                daily_csv = self._cache._legacy_csv_path(out_dir, prefix, "daily")
        if need_weekly and effective_source == "eastmoney":
            old_meta = self._cache._read_meta(
                self._cache._legacy_meta_path(out_dir, prefix, "weekly")
            )
            need_weekly = not self._cache._covers(old_meta, weekly_beg, weekly_end)
            if not need_weekly:
                weekly_csv = self._cache._legacy_csv_path(out_dir, prefix, "weekly")

        if not need_daily and not need_weekly:
            print(f"  [复用缓存] {symbol}: 缓存区间已覆盖，跳过取数")
            return daily_csv, weekly_csv

        # 进程内 symbol 锁
        with _SYMBOL_LOCKS[secid]:
            # 二次检查（获取锁后可能已被其他线程填好）
            need_daily = not self._cache._covers(
                self._cache._read_any_meta(out_dir, prefix, effective_source, "daily")[0],
                daily_beg,
                daily_end,
            )
            need_weekly = not self._cache._covers(
                self._cache._read_any_meta(out_dir, prefix, effective_source, "weekly")[0],
                weekly_beg,
                weekly_end,
            )
            if need_daily and effective_source == "eastmoney":
                old_meta = self._cache._read_meta(
                    self._cache._legacy_meta_path(out_dir, prefix, "daily")
                )
                need_daily = not self._cache._covers(old_meta, daily_beg, daily_end)
            if need_weekly and effective_source == "eastmoney":
                old_meta = self._cache._read_meta(
                    self._cache._legacy_meta_path(out_dir, prefix, "weekly")
                )
                need_weekly = not self._cache._covers(old_meta, weekly_beg, weekly_end)

            n_d: int = 0
            n_w: int = 0

            if need_daily:
                df_daily = self._try_fetch(
                    symbol, spec, "daily", daily_beg, daily_end, daily_lmt
                )
                daily_csv = self._cache.save(
                    spec, effective_source, "daily", df_daily, daily_beg, daily_end, out_dir
                )
                n_d = len(df_daily)
                from .base import random_sleep; random_sleep(0.5, 1.5)

            if need_weekly:
                df_weekly = self._try_fetch(
                    symbol, spec, "weekly", weekly_beg, weekly_end, weekly_lmt
                )
                weekly_csv = self._cache.save(
                    spec, effective_source, "weekly", df_weekly, weekly_beg, weekly_end, out_dir
                )
                n_w = len(df_weekly)
                from .base import random_sleep; random_sleep(0.5, 1.5)

        print(
            f"  [取数] {symbol}: 日线 {n_d if need_daily else '复用'} 条 "
            f"/ 周线 {n_w if need_weekly else '复用'} 条"
        )
        return daily_csv, weekly_csv

    def _try_fetch(
        self,
        symbol: str,
        spec: SymbolSpec,
        period: str,
        beg: str,
        end: str,
        lmt: int,
    ) -> pd.DataFrame:
        """尝试从容灾链取数，失败则按序切换并记录事件。

        Args:
            symbol: 标准化代码（如 ``600216.SH``）。
            spec: ``SymbolSpec`` 实例。
            period: ``"daily"`` / ``"weekly"``。
            beg: 起始 YYYYMMDD。
            end: 结束 YYYYMMDD。
            lmt: 条数上限。

        Returns:
            K 线 DataFrame。

        Raises:
            AllSourcesFailedError: 所有源均失败。
        """
        chain = self._factory.build_chain(symbol)
        if not chain:
            raise AllSourcesFailedError(
                symbol,
                [("NONE", "无可用数据源（请检查配置与依赖安装）")],
            )

        # 映射 period → 适配器需要的 klt
        klt_map = {"daily": "101", "weekly": "102"}
        klt = klt_map.get(period, "101")

        errors: list[tuple[str, str]] = []
        prev_source: str | None = None

        for ds in chain:
            try:
                df = ds.fetch_kline(
                    spec.symbol,
                    klt,
                    beg,
                    end,
                    lmt=lmt,
                )
                return df
            except (DataSourceError, Exception) as e:
                err_msg = f"{type(e).__name__}: {e}"
                errors.append((ds.name, err_msg))
                # 记录切换事件
                if prev_source is not None:
                    event = SwitchEvent(
                        timestamp=time.time(),
                        symbol=symbol,
                        period=period,
                        from_source=prev_source,
                        to_source=ds.name,
                        reason=err_msg[:200],
                    )
                    self._switch_log.record(event)
                prev_source = ds.name
                if not self._config.failover_enabled:
                    # 容灾关闭 → 直接报错不切换
                    raise

        raise AllSourcesFailedError(symbol, errors)


# ---------------------------------------------------------------------------
# 模块级便捷函数（供兼容层 + CLI 使用）
# ---------------------------------------------------------------------------
def get_provider() -> KlineProvider:
    """获取模块级 ``KlineProvider`` 单例。"""
    return KlineProvider()


def ensure_data(
    symbol_cfg: dict[str, str],
    out_dir: str | Path,
    daily_beg: str = "20220706",
    daily_end: str = "20260718",
    daily_lmt: int = 1500,
    weekly_beg: str = "20211210",
    weekly_end: str = "20260718",
    weekly_lmt: int = 500,
) -> tuple[Path, Path]:
    """模块级便捷函数：委托 ``KlineProvider.ensure_data``。"""
    return get_provider().ensure_data(
        symbol_cfg, out_dir, daily_beg, daily_end, daily_lmt,
        weekly_beg, weekly_end, weekly_lmt,
    )
