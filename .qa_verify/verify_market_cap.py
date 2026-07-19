#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QA 独立市值合规校验（只读，绝不写 sector_stocks.json）。
数据源：ak.stock_zh_valuation_baidu（百度，单位 亿），与工程师脚本一致但独立实现。
对全部 1301 个唯一 code 取最新「总市值」，核对是否全部落在 [100, 1000] 亿（含边界）。
结果缓存到 .qa_verify/.cap_cache.json，可重跑续传。
"""
import json
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import akshare as ak

ROOT = Path("E:/量化交易/strategy_lab")
SECTOR_STOCKS = ROOT / "data" / "sector_stocks.json"
CACHE = ROOT / ".qa_verify" / ".cap_cache.json"

MIN_CAP = 100.0
MAX_CAP = 1000.0
WORKERS = 16
RETRIES = 3


def norm_code(code: str) -> str:
    import re
    m = re.search(r"(\d{6})", str(code))
    return m.group(1) if m else ""


def fetch_one(code6: str):
    last = None
    for attempt in range(1, RETRIES + 1):
        try:
            df = ak.stock_zh_valuation_baidu(symbol=code6, indicator="总市值", period="近一周")
            if df is None or len(df) == 0:
                return None
            series = df["value"].dropna()
            if len(series) == 0:
                return None
            v = float(series.iloc[-1])
            return None if v != v else v  # drop NaN
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < RETRIES:
                time.sleep(1.0 * attempt)
    return None


def main():
    data = json.loads(SECTOR_STOCKS.read_text(encoding="utf-8"))
    # 唯一 code + 名称
    uniq = {}
    for items in data.values():
        for it in items:
            c6 = norm_code(it.get("code", ""))
            if c6:
                uniq.setdefault(c6, it.get("name", ""))
    codes = list(uniq.keys())
    print(f"[info] 唯一 code 数: {len(codes)}", flush=True)

    cache = {}
    if CACHE.exists():
        try:
            cache = json.loads(CACHE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            cache = {}
    print(f"[info] 缓存命中: {sum(1 for c in codes if c in cache)}", flush=True)

    todo = [c for c in codes if c not in cache]
    lock = threading.Lock()
    done = [0]

    def worker(c6):
        v = fetch_one(c6)
        if v is not None:
            with lock:
                cache[c6] = v
                done[0] += 1
                if done[0] % 100 == 0:
                    CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
                    print(f"[info] 已获取 {done[0]} 只（累计缓存 {len(cache)}）", flush=True)

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        list(ex.map(worker, todo))
    CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    print(f"[info] 抓取完成，耗时 {time.time()-t0:.1f}s", flush=True)

    # 评估
    caps = {}
    missing = []
    for c6 in codes:
        v = cache.get(c6)
        if v is None:
            missing.append(c6)
        else:
            caps[c6] = v

    vals = list(caps.values())
    vals.sort()
    n = len(vals)
    below = [(c6, caps[c6]) for c6 in codes if c6 in caps and caps[c6] < MIN_CAP]
    above = [(c6, caps[c6]) for c6 in codes if c6 in caps and caps[c6] > MAX_CAP]
    inside = n - len(below) - len(above)

    print("=" * 60)
    print(f"[result] 成功获取市值: {n}/{len(codes)}")
    print(f"[result] 缺失/失败: {len(missing)}")
    if vals:
        print(f"[result] min={vals[0]:.2f}  max={vals[-1]:.2f}  median={statistics.median(vals):.2f} 亿")
        print(f"[result] 落在 [100,1000] 亿: {inside}")
        print(f"[result] <100亿: {len(below)}")
        print(f"[result] >1000亿: {len(above)}")
    print("--- 顶点（前10小）---")
    for c6 in vals[:10]:
        print(f"  {[k for k,v in caps.items() if v==c6][0]} = {c6:.2f}亿")
    print("--- 高点（前10大）---")
    for c6 in vals[-10:]:
        print(f"  {[k for k,v in caps.items() if v==c6][0]} = {c6:.2f}亿")
    if below:
        print("--- <100亿 明细 ---")
        for c6, v in sorted(below, key=lambda x: x[1])[:30]:
            print(f"  {c6} {uniq.get(c6,'')} = {v:.2f}亿")
    if above:
        print("--- >1000亿 明细 ---")
        for c6, v in sorted(above, key=lambda x: -x[1])[:30]:
            print(f"  {c6} {uniq.get(c6,'')} = {v:.2f}亿")
    print("=" * 60)
    all_inside = (len(below) == 0 and len(above) == 0 and len(missing) == 0)
    print("COMPLIANCE:", "PASS(全部在[100,1000]亿)" if all_inside else "FAIL(存在越界或缺失)")
    # 缺失的 code 也列出来（最多 30）
    if missing:
        print("MISSING_CODES:", missing[:30])


if __name__ == "__main__":
    main()
