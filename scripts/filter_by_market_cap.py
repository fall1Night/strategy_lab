#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
filter_by_market_cap.py
=======================
按总市值区间过滤 data/sector_stocks.json 中的沪深主板+创业板标的。

数据源（自动选择，均非东财系接口）：
  1) 首选 ak.stock_zh_a_spot()（新浪源）。若其返回结果含「总市值」列，
     则一次性拉取全 A 快照，单位通常为「元」。
  2) 兜底 ak.stock_zh_valuation_baidu()（百度源，非东财）。当 stock_zh_a_spot
     不含「总市值」列时（如 akshare>=1.18 该列已被移除），改为逐标的查询总市值，
     单位通常为「亿」。

  之所以做兜底：东财接口（stock_zh_a_spot_em / stock_individual_info_em）当前被封 IP，
  且 akshare 新版新浪快照已无总市值列，因此用百度源补市值，避免依赖东财。

过滤规则：保留总市值在 [100亿, 1000亿]（含边界）之间的标的。
  阈值随数据源单位自动切换：
    - 元 : MIN_CAP=1e10, MAX_CAP=1e11   （= 100亿 ~ 1000亿）
    - 亿 : MIN_CAP=100,  MAX_CAP=1000

用法：
    cd <项目根目录>
    python scripts/filter_by_market_cap.py
可选环境变量：
    SOURCE   'auto'(默认) | 'spot' | 'baidu'   强制指定数据源
    MIN_CAP  手动覆盖下限（使用对应数据源的单位）
    MAX_CAP  手动覆盖上限（使用对应数据源的单位）
    WORKERS  百度逐标的查询的并发数（默认 8）

说明：
  - 不在快照中（停牌/退市/查询失败）或总市值缺失的标的，默认剔除，并打印数量与样例。
  - 百度查询使用并发 + 本地缓存（scripts/.cap_cache.json），被中断后重跑可续传，
    且失败的标的会在后续重跑中重试（仅成功结果写入缓存）。
  - 拉取/查询失败时有限重试（3 次，间隔递增）；若过滤后结果为空则拒绝写空文件并退出。
  - 保留原 JSON 结构（按板块代码分组、每个标的含 code/name 字段）。
  - 市值发生漂移后可重跑本脚本；如需调整区间，设置 MIN_CAP/MAX_CAP 即可。
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import akshare as ak  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent
SECTOR_STOCKS = ROOT / "data" / "sector_stocks.json"
CACHE = Path(__file__).resolve().parent / ".cap_cache.json"

# 提取 6 位纯数字代码（去掉 sh/sz 等交易所前缀），用于跨数据源匹配
CODE_RE = re.compile(r"(\d{6})")

_MIN_ENV = os.environ.get("MIN_CAP")
_MAX_ENV = os.environ.get("MAX_CAP")
MIN_CAP = float(_MIN_ENV) if _MIN_ENV else None
MAX_CAP = float(_MAX_ENV) if _MAX_ENV else None
FORCE_SOURCE = os.environ.get("SOURCE", "auto").lower()
WORKERS = int(os.environ.get("WORKERS", "8"))


def norm_code(code: str) -> str:
    """提取 6 位纯数字代码（去掉 sh/sz 等前缀），用于跨数据源匹配。"""
    m = CODE_RE.search(str(code))
    return m.group(1) if m else ""


def to_float(val) -> "float | None":
    """将市值文本/数值统一转为 float；无法解析或缺失返回 None。"""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().replace(",", "").replace("%", "")
    if s in ("", "-", "--", "None", "nan", "NaN"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def load_cache() -> dict:
    if CACHE.exists():
        try:
            return json.loads(CACHE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}


def save_cache(cache: dict) -> None:
    CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")


def fetch_spot(retries: int = 3, wait: int = 5):
    """拉取全 A 实时快照（新浪源），失败有限重试。"""
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            print(f"[info] 拉取全 A 实时快照 ak.stock_zh_a_spot() 第 {attempt}/{retries} 次...", flush=True)
            return ak.stock_zh_a_spot()
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            print(f"[warn] 第 {attempt} 次拉取失败: {exc}", flush=True)
            if attempt < retries:
                time.sleep(wait)
    print(f"[error] 拉取全 A 快照失败，已重试 {retries} 次。错误: {last_err}", file=sys.stderr, flush=True)
    sys.exit(1)


def baidu_cap_one(code6: str, retries: int = 3):
    """查询单只标的的百度总市值（单位：亿）。失败重试；最终失败返回 None。"""
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            df = ak.stock_zh_valuation_baidu(symbol=code6, indicator="总市值", period="近一周")
            if df is None or len(df) == 0:
                return None
            series = df["value"].dropna()
            if len(series) == 0:
                return None
            return to_float(series.iloc[-1])
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if attempt < retries:
                time.sleep(1.5 * attempt)
    print(f"[warn] 百度市值查询失败 {code6}: {last_err}", file=sys.stderr, flush=True)
    return None


def fetch_caps_concurrent(codes: list, cache: dict) -> dict:
    """并发查询百度总市值；仅把成功结果写入缓存，失败的留待后续重跑重试。"""
    todo = [c for c in codes if c not in cache]
    print(f"[info] 需查询 {len(todo)} 只（缓存命中 {len(codes) - len(todo)}）", flush=True)
    lock = threading.Lock()
    done = [0]

    def worker(code6: str) -> None:
        val = baidu_cap_one(code6)
        if val is not None:
            with lock:
                cache[code6] = val
                done[0] += 1
                if done[0] % 50 == 0:
                    save_cache(cache)
                    print(f"[info] 已获取 {done[0]} 只市值（累计缓存 {len(cache)}）", flush=True)

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        list(ex.map(worker, todo))
    save_cache(cache)
    return cache


def main() -> None:
    data = json.loads(SECTOR_STOCKS.read_text(encoding="utf-8"))
    before_total = sum(len(v) for v in data.values())

    # 收集所有去重后的 6 位代码
    seen: set[str] = set()
    uniq_codes: list[str] = []
    for items in data.values():
        for it in items:
            code6 = norm_code(it.get("code", ""))
            if code6 and code6 not in seen:
                seen.add(code6)
                uniq_codes.append(code6)

    # 选择数据源
    source = FORCE_SOURCE
    cap_map: dict[str, "float | None"] = {}
    if source in ("auto", "spot"):
        df = fetch_spot()
        if "总市值" in df.columns:
            source = "spot"
        elif source == "spot":
            print("[error] 已强制 SOURCE=spot，但快照无『总市值』列。", file=sys.stderr, flush=True)
            sys.exit(1)
        else:
            print("[info] stock_zh_a_spot 不含『总市值』列，切换到百度源。", flush=True)
            source = "baidu"

    if source == "spot":
        min_cap = MIN_CAP if MIN_CAP is not None else 1e10
        max_cap = MAX_CAP if MAX_CAP is not None else 1e11
        unit = "元"
        print(f"[info] 数据源=新浪stock_zh_a_spot，单位=元，阈值=[{min_cap:.0e}, {max_cap:.0e}]", flush=True)
        print("[info] 总市值样例（确认单位）：", flush=True)
        for _, row in df.head(5).iterrows():
            print(f"        代码={row.get('代码')} 名称={row.get('名称')} 总市值={row.get('总市值')}", flush=True)
        for _, row in df.iterrows():
            code6 = norm_code(row.get("代码"))
            if code6:
                cap_map[code6] = to_float(row.get("总市值"))
    else:  # baidu（并发 + 缓存）
        min_cap = MIN_CAP if MIN_CAP is not None else 100.0
        max_cap = MAX_CAP if MAX_CAP is not None else 1000.0
        unit = "亿"
        cache = load_cache()
        cap_map = fetch_caps_concurrent(uniq_codes, cache)
        print(f"[info] 数据源=百度stock_zh_valuation_baidu，单位=亿，阈值=[{min_cap}, {max_cap}]", flush=True)
        shown = 0
        for code6, val in cap_map.items():
            if val is not None:
                print(f"        代码={code6} 总市值={val} 亿", flush=True)
                shown += 1
                if shown >= 5:
                    break

    # 过滤
    after: dict[str, list] = {}
    kept = 0
    dropped_missing: list = []   # 不在快照/市值缺失/查询失败
    dropped_outrange: list = []  # 市值越界
    for sector, items in data.items():
        keep_list = []
        for it in items:
            code6 = norm_code(it.get("code", ""))
            cap = cap_map.get(code6)
            if cap is None:
                dropped_missing.append((it.get("code"), it.get("name", "")))
                continue
            if not (min_cap <= cap <= max_cap):
                dropped_outrange.append((it.get("code"), it.get("name", ""), cap))
                continue
            keep_list.append(it)
        after[sector] = keep_list  # 保留所有板块键（含可能为空者），不破坏结构
        kept += len(keep_list)

    if kept == 0:
        print("[error] 过滤后结果为空，疑似数据异常，拒绝写空文件。", file=sys.stderr, flush=True)
        sys.exit(1)

    SECTOR_STOCKS.write_text(json.dumps(after, ensure_ascii=False, indent=2), encoding="utf-8")

    print("=" * 56)
    print(f"[result] 数据源={source} 单位={unit}")
    print(f"[result] 过滤前 {before_total} → 过滤后 {kept}（阈值 {min_cap}~{max_cap} {unit}）")
    print(f"[result] 剔除-市值缺失/查询失败/无数据: {len(dropped_missing)}")
    if dropped_missing:
        print("         样例:", dropped_missing[:10])
    print(f"[result] 剔除-市值越界: {len(dropped_outrange)}")
    if dropped_outrange:
        print("         样例(前10):", [(c, n, cap) for c, n, cap in dropped_outrange[:10]])
    print(f"[result] 已写回: {SECTOR_STOCKS}")
    print("=" * 56)


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        sys.exit(1)
