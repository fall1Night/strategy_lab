# -*- coding: utf-8 -*-
"""QA 独立验证：腾讯数据源适配器 (tencent_src.py) — 严过关

只做验证与测试，不改任何源码。覆盖 6 项清单：
  1. 默认源切换生效
  2. 腾讯源真实拉取日线
  3. 周线聚合（W-FRI）
  4. 容灾链包含腾讯且主源优先
  5. 端到端增量落盘兼容（ensure_data 全链路，临时目录）
  6. 回归冒烟（其它源可构造）
"""

from __future__ import annotations

import json
import tempfile
import traceback
from pathlib import Path

import pandas as pd

RESULTS: list[dict] = []


def report(item: str, ok: bool, detail: str = "") -> None:
    RESULTS.append({"item": item, "ok": ok, "detail": detail})
    tag = "PASS" if ok else "FAIL"
    print(f"\n===== [{tag}] {item} =====")
    if detail:
        print(detail)


# ---------------------------------------------------------------------------
# 0. 环境确认
# ---------------------------------------------------------------------------
print("Python env: strategylab 已导入（.env 自动加载）")

# ---------------------------------------------------------------------------
# 1. 默认源切换生效
# ---------------------------------------------------------------------------
try:
    from strategylab.engine.datasource.config import DataSourceConfig

    cfg = DataSourceConfig.from_env()
    assert cfg.default_source == "tencent", cfg.default_source
    report(
        "1. 默认源切换生效",
        True,
        f"default_source={cfg.default_source!r}  fallback_order={cfg.fallback_order!r}",
    )
except Exception as e:  # noqa: BLE001
    report("1. 默认源切换生效", False, f"{type(e).__name__}: {e}\n{traceback.format_exc()}")

# ---------------------------------------------------------------------------
# 2. 腾讯源真实拉取日线（600216.SH, 2024-01）
# ---------------------------------------------------------------------------
daily_df = None
try:
    from strategylab.engine.datasource.factory import DataSourceFactory

    ds = DataSourceFactory(cfg).get("tencent", cfg)
    daily_df = ds.fetch_kline("600216.SH", "101", "20240101", "20240131", lmt=100)
    rows = len(daily_df)
    cols = list(daily_df.columns)
    detail_lines = [
        f"rows={rows}  cols={cols}",
        f"date dtype={daily_df['date'].dtype if 'date' in daily_df.columns else 'MISSING'}",
        f"date range: {daily_df['date'].min()} ~ {daily_df['date'].max()}" if rows else "empty",
    ]
    ok = rows >= 20
    for c in ("date", "open", "high", "low", "close"):
        ok = ok and (c in daily_df.columns)
        if c in daily_df.columns and c != "date":
            ok = ok and daily_df[c].notna().all()
    if "date" in daily_df.columns:
        ok = ok and pd.api.types.is_datetime64_any_dtype(daily_df["date"])
    report("2. 腾讯源真实拉取日线", ok, "\n".join(detail_lines))
except Exception as e:  # noqa: BLE001
    report("2. 腾讯源真实拉取日线", False, f"{type(e).__name__}: {e}\n{traceback.format_exc()}")

# ---------------------------------------------------------------------------
# 3. 周线聚合（600216.SH, 2024Q1）
# ---------------------------------------------------------------------------
weekly_df = None
try:
    weekly_df = ds.fetch_kline("600216.SH", "102", "20240101", "20240331", lmt=100)
    w_rows = len(weekly_df)
    dow = sorted(weekly_df["date"].dt.dayofweek.unique().tolist()) if w_rows else []
    detail_lines = [
        f"weekly rows={w_rows}",
        f"dayofweek unique={dow}",
        f"weekly date range: {weekly_df['date'].min()} ~ {weekly_df['date'].max()}" if w_rows else "empty",
    ]
    ok = w_rows >= 10 and all(d == 4 for d in dow)
    report("3. 周线聚合（W-FRI）", ok, "\n".join(detail_lines))
except Exception as e:  # noqa: BLE001
    report("3. 周线聚合（W-FRI）", False, f"{type(e).__name__}: {e}\n{traceback.format_exc()}")

# ---------------------------------------------------------------------------
# 4. 容灾链包含腾讯且主源优先
# ---------------------------------------------------------------------------
try:
    chain = DataSourceFactory(cfg).build_chain("600216.SH")
    names = [d.name for d in chain]
    ok = bool(chain) and chain[0].name == "tencent" and "akshare" in names
    report("4. 容灾链包含腾讯且主源优先", ok, f"chain={names}")
except Exception as e:  # noqa: BLE001
    report("4. 容灾链包含腾讯且主源优先", False, f"{type(e).__name__}: {e}\n{traceback.format_exc()}")

# ---------------------------------------------------------------------------
# 5. 端到端增量落盘兼容（临时目录）
# ---------------------------------------------------------------------------
try:
    from strategylab.engine import data_feed
    from strategylab.engine.datasource.exceptions import DataMissingError

    tmp = Path(tempfile.mkdtemp(prefix="qa_tencent_"))
    scfg = data_feed.normalize_symbol("600216.SH")
    kw = dict(
        daily_beg="20240101", daily_end="20240131",
        weekly_beg="20231201", weekly_end="20240131",
    )

    # 5a. 空缓存 verify 应抛 DataMissingError
    verify_raised = False
    try:
        data_feed.ensure_data(scfg, tmp, mode="verify", required_beg="20240101", required_end="20240131", **kw)
        print("    verify(空缓存) unexpectedly passed")
    except DataMissingError as e:
        verify_raised = True
        print(f"    verify(空缓存) DataMissingError as expected: {str(e)[:80]}")

    # 5b. update 拉取落盘
    d_csv, w_csv = data_feed.ensure_data(scfg, tmp, mode="update", **kw)
    d_csv = Path(d_csv)
    print(f"    daily csv = {d_csv}")
    print(f"    weekly csv = {w_csv}")

    # 5c. 断言 meta / 文件名 / 回读
    meta_path = Path(str(d_csv).replace(".csv", "_meta.json"))
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    bars = data_feed.load_bars(d_csv)
    detail_lines = [
        f"daily csv name: {d_csv.name}",
        f"meta exists: {meta_path.exists()}",
        f"meta: {meta}",
        f"loaded bars: {len(bars)}",
    ]
    ok = verify_raised
    ok = ok and str(d_csv).endswith("_tencent_daily.csv")
    ok = ok and meta_path.exists()
    ok = ok and meta.get("source") == "tencent"
    ok = ok and len(bars) >= 20

    # 5d. 落盘后 verify 应通过
    verify_after = False
    try:
        data_feed.ensure_data(scfg, tmp, mode="verify", required_beg="20240101", required_end="20240131", **kw)
        verify_after = True
        detail_lines.append("verify(落盘后): PASS")
    except DataMissingError as e:
        detail_lines.append(f"verify(落盘后): DataMissingError: {str(e)[:80]}")
    ok = ok and verify_after

    report("5. 端到端增量落盘兼容", ok, "\n".join(detail_lines))
except Exception as e:  # noqa: BLE001
    report("5. 端到端增量落盘兼容", False, f"{type(e).__name__}: {e}\n{traceback.format_exc()}")

# ---------------------------------------------------------------------------
# 6. 回归冒烟：其它源可构造（不真实拉取）
# ---------------------------------------------------------------------------
try:
    f = DataSourceFactory(cfg)
    lines = []
    all_ok = True
    for n in ("akshare", "tushare", "eastmoney", "broker"):
        try:
            f.get(n, cfg)
            lines.append(f"{n}: constructible")
        except Exception as e:  # noqa: BLE001
            all_ok = False
            lines.append(f"{n}: -> {type(e).__name__}: {str(e)[:60]}")
    report("6. 回归冒烟（其它源可构造）", all_ok, "\n".join(lines))
except Exception as e:  # noqa: BLE001
    report("6. 回归冒烟（其它源可构造）", False, f"{type(e).__name__}: {e}\n{traceback.format_exc()}")

# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
print("\n" + "=" * 60)
total = len(RESULTS)
passed = sum(1 for r in RESULTS if r["ok"])
print(f"SUMMARY: {passed}/{total} passed")
for r in RESULTS:
    print(f"  [{'PASS' if r['ok'] else 'FAIL'}] {r['item']}")
if passed == total:
    print("结论: 全部通过")
else:
    print("结论: 存在失败项")
