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
    def _covers(meta: dict[str, Any] | None, beg: str, end: str) -> bool:
        """请求区间 [beg, end] 是否 ⊆ 缓存区间 [meta.beg, meta.end]。

        beg/end 为 ``'YYYYMMDD'`` 字符串，同格式下字典序比较即时间序。
        """
        if not meta:
            return False
        mb = meta.get("beg")
        me = meta.get("end")
        if not mb or not me:
            return False
        return mb <= beg and me >= end

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
