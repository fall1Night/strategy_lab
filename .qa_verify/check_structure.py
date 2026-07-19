#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QA 只读结构校验：data/sector_stocks.json
不修改任何文件，仅读取并统计。
"""
import json
from pathlib import Path

p = Path("E:/量化交易/strategy_lab/data/sector_stocks.json")
data = json.loads(p.read_text(encoding="utf-8"))

sectors = list(data.keys())
n_sectors = len(sectors)
total = 0
bad_fields = []
dup_within = {}
all_codes = []
code_seen = {}
name_map = {}

for sec, items in data.items():
    codes_in_sec = []
    for it in items:
        total += 1
        # 字段校验：必须恰为 {code, name}
        keys = set(it.keys())
        if keys != {"code", "name"}:
            bad_fields.append((sec, it, sorted(keys)))
        code = it.get("code")
        name = it.get("name")
        if not code or not isinstance(code, str):
            bad_fields.append((sec, it, "missing/code"))
        if not name or not isinstance(name, str):
            bad_fields.append((sec, it, "missing/name"))
        codes_in_sec.append(code)
        all_codes.append(code)
        code_seen.setdefault(code, []).append(sec)
    # 板块内重复
    seen = set()
    dups = []
    for c in codes_in_sec:
        if c in seen:
            dups.append(c)
        seen.add(c)
    if dups:
        dup_within[sec] = dups

# 跨板块重复（同一 code 出现在多个板块）
cross = {c: secs for c, secs in code_seen.items() if len(secs) > 1}

print("=== sector_stocks.json 结构校验 ===")
print(f"板块数: {n_sectors}")
print(f"总条目数: {total}")
print(f"字段异常条目: {len(bad_fields)}")
for b in bad_fields[:20]:
    print("  BAD:", b)
print(f"板块内重复 code: {sum(len(v) for v in dup_within.values())} (涉及板块 {len(dup_within)})")
for sec, d in list(dup_within.items())[:10]:
    print("  DUP_WITHIN:", sec, d[:10])
print(f"跨板块重复 code: {len(cross)}")
for c, secs in list(cross.items())[:20]:
    print("  CROSS:", c, secs)
print(f"去重后唯一 code 数: {len(set(all_codes))}")
print("校验结论:", "PASS" if (n_sectors == 31 and total == 1301 and not bad_fields and not dup_within) else "CHECK")
