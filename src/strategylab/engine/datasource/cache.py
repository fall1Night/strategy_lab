# -*- coding: utf-8 -*-
"""K 线缓存：source 感知的磁盘缓存路径 + 区间覆盖判断 + 原子写。

缓存文件命名规则：
  - 新格式：``<prefix>_<source>_<period>.csv`` + ``<prefix>_<source>_<period>_meta.json``
  - 旧格式兼容：``<prefix>_<period>.csv`` 回退视作 ``eastmoney``（不强制迁移）

meta 字段新增 ``source``，区间覆盖判断逻辑沿用既有 ``_covers``。
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from .base import SymbolSpec


class KlineCache:
    """source 感知的磁盘 K 线缓存。

    提供 ``load`` / ``save``，自动处理路径中 source 标识与旧缓存的兼容回退。

    用法：
        cache = KlineCache()
        df = cache.load(spec, "eastmoney", "daily", "20220706", "20260718")
        cache.save(spec, "eastmoney", "daily", df, "20220706", "20260718")
    """

    # ------------------------------------------------------------------
    # 路径工具
    # ------------------------------------------------------------------
    @staticmethod
    def _csv_path(out_dir: Path, prefix: str, source: str, period: str) -> Path:
        """新格式 CSV：``<prefix>_<source>_<period>.csv``。"""
        return out_dir / f"{prefix}_{source}_{period}.csv"

    @staticmethod
    def _meta_path(out_dir: Path, prefix: str, source: str, period: str) -> Path:
        """新格式 meta：``<prefix>_<source>_<period>_meta.json``。"""
        return out_dir / f"{prefix}_{source}_{period}_meta.json"

    @staticmethod
    def _legacy_csv_path(out_dir: Path, prefix: str, period: str) -> Path:
        """旧格式 CSV：``<prefix>_<period>.csv``（兼容回退）。"""
        return out_dir / f"{prefix}_{period}.csv"

    @staticmethod
    def _legacy_meta_path(out_dir: Path, prefix: str, period: str) -> Path:
        """旧格式 meta：``<prefix>_<period>_meta.json``（兼容回退）。"""
        return out_dir / f"{prefix}_{period}_meta.json"

    # ------------------------------------------------------------------
    # meta 读写
    # ------------------------------------------------------------------
    @staticmethod
    def _read_meta(meta_path: Path) -> dict[str, Any] | None:
        """读取缓存区间 meta；不存在 / 损坏视为 None。"""
        if not meta_path.exists():
            return None
        try:
            return json.loads(meta_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None

    @staticmethod
    def _read_any_meta(
        out_dir: Path, prefix: str, source: str, period: str
    ) -> tuple[dict[str, Any] | None, str, bool]:
        """尝试读取 meta（新格式优先，旧格式兼容回退）。

        Returns:
            ``(meta_dict_or_None, effective_source, is_legacy)``。
        """
        # 1) 新格式优先
        new_meta = KlineCache._meta_path(out_dir, prefix, source, period)
        meta = KlineCache._read_meta(new_meta)
        if meta is not None:
            # 给旧 meta 补 source 字段（历史兼容）
            if "source" not in meta:
                meta["source"] = source
            return meta, source, False

        # 2) 旧格式兼容（仅当 source 为 eastmoney 时尝试）
        if source == "eastmoney":
            legacy_meta = KlineCache._legacy_meta_path(out_dir, prefix, period)
            meta = KlineCache._read_meta(legacy_meta)
            if meta is not None:
                if "source" not in meta:
                    meta["source"] = "eastmoney"
                return meta, "eastmoney", True

        return None, source, False

    # ------------------------------------------------------------------
    # 区间覆盖判断（沿用既有 _covers 逻辑）
    # ------------------------------------------------------------------
    @staticmethod
    def _covers(meta: dict[str, Any] | None, beg: str, end: str,
                end_tolerance_days: int = 0) -> bool:
        """请求区间 [beg, end] 是否 ⊆ 缓存区间 [meta.beg, meta.end]。

        beg/end 为 ``'YYYYMMDD'`` 字符串，同格式下字典序比较即时间序。

        新增 ``end_tolerance_days``（FR-55）：快速回测场景下缓存末日永远
        不可能覆盖"今天"，允许容忍末日落后 ``end`` 最多 N 天。默认 0
        保持其他调用方语义完全不变。
        """
        if not meta:
            return False
        mb = meta.get("beg")
        me = meta.get("end")
        if not mb or not me:
            return False
        if mb > beg:
            return False
        if me >= end:
            return True
        # 末日容差：当数据实际末日略早于请求末日时，允许几天的滞后
        if end_tolerance_days > 0:
            try:
                end_dt = datetime.strptime(end, "%Y%m%d")
                tolerance_date = (end_dt - timedelta(days=end_tolerance_days)).strftime("%Y%m%d")
                if me >= tolerance_date:
                    return True
            except (ValueError, OverflowError):
                pass
        return False

    # ------------------------------------------------------------------
    # 增量更新辅助（FR-39：append 而非整文件覆盖）
    # ------------------------------------------------------------------
    @staticmethod
    def _empty_kline_df() -> "pd.DataFrame":
        """返回空 K 线 DataFrame（列与缓存一致）。"""
        return pd.DataFrame(columns=["date", "open", "high", "low", "close"])

    @staticmethod
    def _parse_yyyymmdd(value: str) -> "datetime":
        """将 ``YYYYMMDD`` 字符串解析为 ``datetime``（用于增量窗口天数计算）。"""
        return datetime.strptime(value, "%Y%m%d")

    @staticmethod
    def _normalize_dates(df: "pd.DataFrame") -> "pd.DataFrame":
        """把 ``date`` 列规整为 ``YYYY-MM-DD`` 字符串，统一新旧数据格式便于去重。"""
        df = df.copy()
        if "date" not in df.columns:
            return df
        df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
        return df

    def _incremental_window(
        self,
        meta: dict[str, Any] | None,
        today_yyyymmdd: str,
        default_beg: str,
    ) -> str | None:
        """返回本次应取数的起点 ``YYYYMMDD``；无需取数返回 ``None``。

        支持**双向补**：

        - 往前补：所需起点 ``default_beg`` 比缓存首日更早（``first > default_beg``）
          时，从 ``default_beg`` 全量拉取，``merge`` 去重会保留已缓存的中间段。
        - 往后补：缓存末日早于今天（``last < today``）时，从 ``last + 1`` 追加。

        Args:
            meta: 缓存 meta（含 ``beg``/``first`` 与 ``last``/``end``）；``None``
                表示无缓存。
            today_yyyymmdd: 今天 ``YYYYMMDD``。
            default_beg: 无缓存时的首拉起点（genesis）；往前补时即回测所需起点。

        Returns:
            取数起点：
              - 无缓存 / 缺边界 → ``default_beg``（全量首拉）；
              - 需往前补 → ``default_beg``（从所需起点拉全量）；
              - 仅往后补 → ``last + 1``（原行为）；
              - 都不缺 → ``None``（已覆盖所需区间 + 已最新，幂等跳过）。
        """
        # 读取缓存区间边界（meta 同时有 beg/end 与 first/last，取其一即可）
        first = str(meta.get("beg") or meta.get("first") or "") if meta else ""
        last = str(meta.get("last") or meta.get("end") or "") if meta else ""

        # 无缓存或缺边界 → 从 genesis 全量首拉（首次首拉行为不变）
        if not meta or not (last or first):
            return default_beg

        # 双向补判定（YYYYMMDD 字典序即时间序）
        need_front = bool(first) and first > default_beg
        need_back = bool(last) and last < today_yyyymmdd

        if not need_front and not need_back:
            return None  # 已覆盖所需区间 + 已最新 → 幂等跳过

        if need_front:
            # 往前补：从所需起点拉全量，merge 去重保留已缓存中间段
            return default_beg

        # 仅往后补：从末日 + 1 起追加（原行为）
        nxt = (self._parse_yyyymmdd(last) + timedelta(days=1)).strftime("%Y%m%d")
        return nxt

    def merge(
        self,
        spec: "SymbolSpec",
        source: str,
        period: str,
        new_df: "pd.DataFrame",
        out_dir: "Path | str | None" = None,
    ) -> Path:
        """读旧 CSV → 与 ``new_df`` 按 ``date`` 合并去重（保留较新）→ 排序 → 原子写回。

        设计要点（FR-39）：
          - 历史 ``beg`` 不丢：合并后首行即全量最早日。
          - ``last`` 推进到实际末日（取回数据的最大 ``date``），而非请求 ``end``。
          - 原子写：临时文件 + ``os.replace``，与 ``save`` 同方案。

        Args:
            spec: ``SymbolSpec`` 实例。
            source: 数据源名称。
            period: 周期标识（``"daily"`` / ``"weekly"``）。
            new_df: 本次取回的 K 线 DataFrame。
            out_dir: 缓存目录。

        Returns:
            写回的 CSV 文件 ``Path``。
        """
        if out_dir is None:
            from ...settings import get_data_dir

            out_dir = get_data_dir()

        out_dir = Path(out_dir)
        prefix = spec.prefix
        csv_path = self._csv_path(out_dir, prefix, source, period)
        meta_path = self._meta_path(out_dir, prefix, source, period)

        # 旧 CSV（不存在则空 DF）；日期统一规整为 ``YYYY-MM-DD`` 字符串以便去重
        if csv_path.exists():
            old = pd.read_csv(csv_path)
        else:
            old = self._empty_kline_df()
        old = self._normalize_dates(old)
        new = self._normalize_dates(new_df)

        combined = (
            pd.concat([old, new], ignore_index=True)
            .drop_duplicates(subset=["date"], keep="last")
            .sort_values("date")
            .reset_index(drop=True)
        )
        rows = self._atomic_write_csv(combined, csv_path)

        # meta：beg 取合并后首行（= 全量最早日，历史不回退），last 取实际末日
        # NOTE: combined["date"] 已规整为 "YYYY-MM-DD" 字符串，需用 replace 而非
        # datetime 的 strftime，避免 AttributeError；输出保持 "YYYYMMDD" 字符串，
        # 与 _covers / _incremental_window 的日期字典序比较约定一致。
        dates = combined["date"].sort_values()
        first_date = dates.iloc[0].replace("-", "") if len(dates) > 0 else None
        last_date = dates.iloc[-1].replace("-", "") if len(dates) > 0 else None
        meta_dict: dict[str, Any] = {
            "beg": first_date,
            "end": last_date,
            "rows": rows,
            "first": first_date,
            "last": last_date,
            "version": 2,
            "source": source,
        }
        self._atomic_write_meta(meta_dict, meta_path)
        return csv_path

    # ------------------------------------------------------------------
    # 公开方法
    # ------------------------------------------------------------------
    def load(
        self,
        spec: SymbolSpec,
        source: str,
        period: str,
        beg: str,
        end: str,
        out_dir: Path | None = None,
    ) -> pd.DataFrame | None:
        """尝试从缓存加载 K 线数据。

        Args:
            spec: ``SymbolSpec`` 实例。
            source: 数据源名称。
            period: 周期标识（``"daily"`` / ``"weekly"``）。
            beg: 请求起始日期 ``YYYYMMDD``。
            end: 请求结束日期 ``YYYYMMDD``。
            out_dir: 缓存目录（若为 None 则使用默认 data 目录）。

        Returns:
            命中且区间覆盖 → DataFrame；否则 → None。
        """
        if out_dir is None:
            from ...settings import get_data_dir

            out_dir = get_data_dir()

        prefix = spec.prefix

        # 读 meta（新格式优先，旧格式兼容）
        meta, effective_source, is_legacy = self._read_any_meta(out_dir, prefix, source, period)

        if not self._covers(meta, beg, end):
            return None

        # 确定 CSV 路径
        if is_legacy:
            csv_path = self._legacy_csv_path(out_dir, prefix, period)
        else:
            csv_path = self._csv_path(out_dir, prefix, effective_source, period)

        if not csv_path.exists():
            return None

        try:
            df = pd.read_csv(csv_path, parse_dates=["date"]).sort_values("date").reset_index(drop=True)
            return df
        except (ValueError, OSError):
            return None

    def save(
        self,
        spec: SymbolSpec,
        source: str,
        period: str,
        df: pd.DataFrame,
        beg: str,
        end: str,
        out_dir: Path | None = None,
    ) -> Path:
        """把 K 线 DataFrame 写入新格式缓存文件。

        Args:
            spec: ``SymbolSpec`` 实例。
            source: 数据源名称。
            period: 周期标识（``"daily"`` / ``"weekly"``）。
            df: K 线 DataFrame（至少含 date/open/high/low/close 列）。
            beg: 请求起始日期。
            end: 请求结束日期。
            out_dir: 缓存目录。

        Returns:
            写入的 CSV 文件 ``Path``。
        """
        if out_dir is None:
            from ...settings import get_data_dir

            out_dir = get_data_dir()

        prefix = spec.prefix
        csv_path = self._csv_path(out_dir, prefix, source, period)
        meta_path = self._meta_path(out_dir, prefix, source, period)

        # 原子写 CSV
        rows = self._atomic_write_csv(df, csv_path)

        # 元信息
        dates = df["date"].sort_values()
        first_date = dates.iloc[0].strftime("%Y%m%d") if len(dates) > 0 else None
        last_date = dates.iloc[-1].strftime("%Y%m%d") if len(dates) > 0 else None

        meta: dict[str, Any] = {
            "beg": beg,
            "end": end,
            "rows": rows,
            "first": first_date,
            "last": last_date,
            "version": 2,
            "source": source,
        }
        self._atomic_write_meta(meta, meta_path)

        return csv_path

    # ------------------------------------------------------------------
    # 原子写工具（沿用既有临时文件 + os.replace 方案）
    # ------------------------------------------------------------------
    @staticmethod
    def _atomic_write_csv(df: pd.DataFrame, csv_path: Path) -> int:
        """把 DataFrame 原子写入 CSV。"""
        lines: list[str] = ["date,open,high,low,close"]
        for _, row in df.iterrows():
            d = str(row["date"])[:10]  # YYYY-MM-DD
            lines.append(f"{d},{row['open']},{row['high']},{row['low']},{row['close']}")
        content = "\n".join(lines) + "\n"

        tmp = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=str(csv_path.parent), suffix=".tmp", delete=False
        )
        try:
            tmp.write(content)
            tmp.close()
            os.replace(tmp.name, csv_path)
        finally:
            if os.path.exists(tmp.name):
                try:
                    os.unlink(tmp.name)
                except OSError:
                    pass
        return len(lines) - 1

    @staticmethod
    def _atomic_write_meta(meta: dict[str, Any], meta_path: Path) -> None:
        """原子写 meta.json。"""
        tmp = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=str(meta_path.parent), suffix=".tmp", delete=False
        )
        try:
            tmp.write(json.dumps(meta, ensure_ascii=False, indent=2))
            tmp.close()
            os.replace(tmp.name, meta_path)
        finally:
            if os.path.exists(tmp.name):
                try:
                    os.unlink(tmp.name)
                except OSError:
                    pass
