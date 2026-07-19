# -*- coding: utf-8 -*-
"""老缓存 meta.json 补建脚本（FR-28）。

扫描 ``data/*.csv``（日线/周线），对无对应 ``<prefix>_<period>_meta.json`` 的，
读 CSV 首尾行补建 meta（first/last → 紧凑 ``YYYYMMDD``），使新 ``ensure_data``
能正确判断缓存区间并复用。

用法：``python -m strategylab.migrate_cache``
"""
from __future__ import annotations

import json
from pathlib import Path

from ..settings import get_data_dir


def _build_meta(csv_path: Path) -> dict[str, str | int] | None:
    """从 CSV 首尾行推断缓存区间 meta。"""
    try:
        lines = csv_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    if len(lines) < 2:
        return None
    first_date = lines[1].split(",")[0]
    last_date = lines[-1].split(",")[0]
    if not first_date or not last_date:
        return None
    return {
        "beg": first_date.replace("-", ""),
        "end": last_date.replace("-", ""),
        "rows": len(lines) - 1,
        "first": first_date,
        "last": last_date,
        "version": 1,
    }


def main() -> int:
    data_dir = get_data_dir()
    csvs = sorted(data_dir.glob("*_daily.csv")) + sorted(data_dir.glob("*_weekly.csv"))
    built = 0
    for csv in csvs:
        name = csv.stem  # e.g. 000001_sz_daily
        period = "daily" if name.endswith("_daily") else "weekly"
        prefix = name[: -(len("_" + period))]
        meta_path = data_dir / f"{name}_meta.json"
        if meta_path.exists():
            continue
        meta = _build_meta(csv)
        if meta is None:
            continue
        meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        built += 1
        print(f"  [补建] {meta_path.name}: {meta['beg']}~{meta['end']} ({meta['rows']} 行)")
    print(f"完成：共补建 {built} 个 meta.json")
    return built


if __name__ == "__main__":
    main()
