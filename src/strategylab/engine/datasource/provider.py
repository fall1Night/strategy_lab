# -*- coding: utf-8 -*-
"""K 线取数编排器（Provider）：选源 → 查缓存 → 容灾链取数 → 记切换 → 写缓存。

对外暴露 ``ensure_data`` —— 与旧 ``data_feed.ensure_data`` 签名完全一致，
使 ``backtest.py`` 零改动。
"""

from __future__ import annotations

import time
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import threading

from .base import DataSource, SymbolSpec, normalize_symbol
from .cache import KlineCache
from .config import DataSourceConfig
from .exceptions import AllSourcesFailedError, DataMissingError, DataSourceError
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


def _cache_needs_front(
    meta: "dict[str, Any] | None", default_beg: str
) -> bool:
    """缓存首日是否晚于所需起点（需往前补历史）。

    用于「旧格式兼容跳过」分支：当缓存首日比 ``default_beg`` 更晚时，
    即便末日已最新也仍需往前补，此时不能把 ``need_daily/need_weekly``
    误判为 ``False``（否则会漏补前面历史，死循环）。
    """
    if not meta:
        return False
    first = str(meta.get("beg") or meta.get("first") or "")
    return bool(first) and first > default_beg


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
        daily_beg: str = "20200101",
        daily_end: str = "20260718",
        daily_lmt: int = 2500,
        weekly_beg: str = "20200101",
        weekly_end: str = "20260718",
        weekly_lmt: int = 500,
        mode: str = "update",
        required_beg: str | None = None,
        required_end: str | None = None,
    ) -> tuple[Path, Path]:
        """拉取或复用日线/周线 CSV，返回 (daily_csv, weekly_csv) 路径。

        新增 ``mode``（FR-39/40）：
          - ``"update"``（默认，向后兼容旧行为）：增量取数 + ``cache.merge`` 写回。
          - ``"verify"``：仅校验缓存覆盖所需区间，缺失即抛 ``DataMissingError``，
            绝不取数（仅「回测」用；缺数据由上层标记 failed 并提示先更新数据源）。

        Args:
            symbol_cfg: ``normalize_symbol`` 返回的 dict（含 symbol/secid/prefix）。
            out_dir: 缓存输出目录。
            daily_beg/daily_end: 日线默认区间（update 模式作首拉 genesis / verify 兜底）。
            weekly_beg/weekly_end: 周线默认区间。
            daily_lmt/weekly_lmt: 条数上限。
            mode: ``"update"`` 或 ``"verify"``。
            required_beg/required_end: verify 模式日线所需覆盖区间（默认回落 daily_beg/daily_end）。

        Returns:
            ``(daily_csv_path, weekly_csv_path)``。

        Raises:
            DataMissingError: verify 模式下缓存未覆盖所需区间时。
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

        if mode == "verify":
            return self._ensure_verify(
                symbol, prefix, effective_source, out_dir,
                daily_csv, weekly_csv,
                daily_beg, daily_end, weekly_beg, weekly_end,
                required_beg, required_end,
            )

        # ---------- mode == "update"（增量取数 + merge 写回） ----------
        today = date.today().strftime("%Y%m%d")

        daily_meta, _, is_legacy_d = self._cache._read_any_meta(
            out_dir, prefix, effective_source, "daily"
        )
        weekly_meta, _, is_legacy_w = self._cache._read_any_meta(
            out_dir, prefix, effective_source, "weekly"
        )

        # 增量窗口：取数起点 = meta.last + 1；已最新则返回 None（幂等）
        daily_beg_calc = self._cache._incremental_window(daily_meta, today, daily_beg)
        weekly_beg_calc = self._cache._incremental_window(weekly_meta, today, weekly_beg)
        need_daily = daily_beg_calc is not None
        need_weekly = weekly_beg_calc is not None

        # 往前补判定：缓存首日晚于所需起点时，旧格式"已最新"跳过逻辑不可用，
        # 必须保证 need_daily/need_weekly 保持 True，否则会漏补前面历史（死循环）。
        daily_need_front = _cache_needs_front(daily_meta, daily_beg)
        weekly_need_front = _cache_needs_front(weekly_meta, weekly_beg)

        # 旧格式兼容：新格式无 meta 但旧格式已是最新 → 视为最新、用旧路径跳过
        if (
            need_daily
            and effective_source == "eastmoney"
            and is_legacy_d
            and not daily_need_front
        ):
            old_meta = self._cache._read_meta(
                self._cache._legacy_meta_path(out_dir, prefix, "daily")
            )
            if old_meta and old_meta.get("last") and old_meta["last"] >= today:
                need_daily = False
                daily_csv = self._cache._legacy_csv_path(out_dir, prefix, "daily")
        if (
            need_weekly
            and effective_source == "eastmoney"
            and is_legacy_w
            and not weekly_need_front
        ):
            old_meta = self._cache._read_meta(
                self._cache._legacy_meta_path(out_dir, prefix, "weekly")
            )
            if old_meta and old_meta.get("last") and old_meta["last"] >= today:
                need_weekly = False
                weekly_csv = self._cache._legacy_csv_path(out_dir, prefix, "weekly")

        if not need_daily and not need_weekly:
            print(f"  [增量复用] {symbol}: 行情已是最新，跳过取数")
            return daily_csv, weekly_csv

        # 进程内 symbol 锁（防并发重复取数）
        with _SYMBOL_LOCKS[secid]:
            # 二次检查（获取锁后可能已被其他线程填好）
            daily_meta, _, _ = self._cache._read_any_meta(
                out_dir, prefix, effective_source, "daily"
            )
            weekly_meta, _, _ = self._cache._read_any_meta(
                out_dir, prefix, effective_source, "weekly"
            )
            daily_beg_calc = self._cache._incremental_window(daily_meta, today, daily_beg)
            weekly_beg_calc = self._cache._incremental_window(weekly_meta, today, weekly_beg)
            need_daily = daily_beg_calc is not None
            need_weekly = weekly_beg_calc is not None
            daily_need_front = _cache_needs_front(daily_meta, daily_beg)
            weekly_need_front = _cache_needs_front(weekly_meta, weekly_beg)
            if (
                need_daily
                and effective_source == "eastmoney"
                and is_legacy_d
                and not daily_need_front
            ):
                old_meta = self._cache._read_meta(
                    self._cache._legacy_meta_path(out_dir, prefix, "daily")
                )
                if old_meta and old_meta.get("last") and old_meta["last"] >= today:
                    need_daily = False
                    daily_csv = self._cache._legacy_csv_path(out_dir, prefix, "daily")
            if (
                need_weekly
                and effective_source == "eastmoney"
                and is_legacy_w
                and not weekly_need_front
            ):
                old_meta = self._cache._read_meta(
                    self._cache._legacy_meta_path(out_dir, prefix, "weekly")
                )
                if old_meta and old_meta.get("last") and old_meta["last"] >= today:
                    need_weekly = False
                    weekly_csv = self._cache._legacy_csv_path(out_dir, prefix, "weekly")

            n_d: int = 0
            n_w: int = 0

            if need_daily:
                days = (
                    self._cache._parse_yyyymmdd(today)
                    - self._cache._parse_yyyymmdd(daily_beg_calc)
                ).days + 1
                lmt = max(daily_lmt, (days + 10) * 2)
                df_daily = self._try_fetch(
                    symbol, spec, "daily", daily_beg_calc, today, lmt
                )
                daily_csv = self._cache.merge(
                    spec, effective_source, "daily", df_daily, out_dir
                )
                n_d = len(df_daily)
                from .base import random_sleep

                random_sleep(0.5, 1.5)

            if need_weekly:
                days = (
                    self._cache._parse_yyyymmdd(today)
                    - self._cache._parse_yyyymmdd(weekly_beg_calc)
                ).days + 1
                lmt = max(weekly_lmt, (days + 10) * 2)
                df_weekly = self._try_fetch(
                    symbol, spec, "weekly", weekly_beg_calc, today, lmt
                )
                weekly_csv = self._cache.merge(
                    spec, effective_source, "weekly", df_weekly, out_dir
                )
                n_w = len(df_weekly)
                from .base import random_sleep

                random_sleep(0.5, 1.5)

        print(
            f"  [增量取数] {symbol}: 日线 {n_d if need_daily else '复用'} 条 "
            f"/ 周线 {n_w if need_weekly else '复用'} 条"
        )
        return daily_csv, weekly_csv

    def _ensure_verify(
        self,
        symbol: str,
        prefix: str,
        effective_source: str,
        out_dir: Path,
        daily_csv: Path,
        weekly_csv: Path,
        daily_beg: str,
        daily_end: str,
        weekly_beg: str,
        weekly_end: str,
        required_beg: str | None,
        required_end: str | None,
    ) -> tuple[Path, Path]:
        """verify 模式：仅校验缓存覆盖所需区间，不足抛 ``DataMissingError``，绝不取数。

        FR-40：回测只校验、不取数；缺数据即由上层标记 failed 并提示先更新数据源。
        FR-55：快速回测末日容差 —— 缓存末日永远不可能覆盖"今天"（当日数据
        未产生时），允许末日落后最多 ``VERIFY_END_TOLERANCE_DAYS`` 天。
        """
        rb = required_beg if required_beg else daily_beg
        re = required_end if required_end else daily_end

        # 末日容差：快速回测 end 默认填"今天"，缓存末日不可达，允许少许滞后
        VERIFY_END_TOLERANCE_DAYS = 7

        daily_meta, _, is_legacy_d = self._cache._read_any_meta(
            out_dir, prefix, effective_source, "daily"
        )
        weekly_meta, _, is_legacy_w = self._cache._read_any_meta(
            out_dir, prefix, effective_source, "weekly"
        )
        daily_ok = self._cache._covers(daily_meta, rb, re,
                                       end_tolerance_days=VERIFY_END_TOLERANCE_DAYS)
        weekly_ok = self._cache._covers(weekly_meta, weekly_beg, weekly_end,
                                        end_tolerance_days=VERIFY_END_TOLERANCE_DAYS)

        # 旧格式兼容回退
        if not daily_ok and effective_source == "eastmoney" and is_legacy_d:
            old_meta = self._cache._read_meta(
                self._cache._legacy_meta_path(out_dir, prefix, "daily")
            )
            if self._cache._covers(old_meta, rb, re,
                                   end_tolerance_days=VERIFY_END_TOLERANCE_DAYS):
                daily_ok = True
                daily_csv = self._cache._legacy_csv_path(out_dir, prefix, "daily")
        if not weekly_ok and effective_source == "eastmoney" and is_legacy_w:
            old_meta = self._cache._read_meta(
                self._cache._legacy_meta_path(out_dir, prefix, "weekly")
            )
            if self._cache._covers(old_meta, weekly_beg, weekly_end,
                                   end_tolerance_days=VERIFY_END_TOLERANCE_DAYS):
                weekly_ok = True
                weekly_csv = self._cache._legacy_csv_path(out_dir, prefix, "weekly")

        if not (daily_ok and weekly_ok):
            raise DataMissingError(
                symbol,
                f"行情缺失/不足（日线覆盖={daily_ok}，周线覆盖={weekly_ok}），"
                f"请先点『更新数据源』刷新后再回测",
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
    daily_beg: str = "20200101",
    daily_end: str = "20260718",
    daily_lmt: int = 2500,
    weekly_beg: str = "20200101",
    weekly_end: str = "20260718",
    weekly_lmt: int = 500,
    mode: str = "update",
    required_beg: str | None = None,
    required_end: str | None = None,
) -> tuple[Path, Path]:
    """模块级便捷函数：委托 ``KlineProvider.ensure_data``。

    透传 ``mode`` / ``required_beg`` / ``required_end``（FR-39/40）。
    """
    return get_provider().ensure_data(
        symbol_cfg, out_dir, daily_beg, daily_end, daily_lmt,
        weekly_beg, weekly_end, weekly_lmt,
        mode, required_beg, required_end,
    )
