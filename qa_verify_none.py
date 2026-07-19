# -*- coding: utf-8 -*-
"""补充验证：sharpe 有 5 个 NULL 时的 None-at-end；win_rate_pct 全 item 存在且为 float；
显式 sort_by='total_return_pct' 与默认一致性（数据无 total_return_pct NULL，实践中一致）。"""
import os, sys
os.environ.setdefault("DATABASE_URL",
    "mysql+pymysql://root:root@localhost:3306/strategylab?charset=utf8mb4")
sys.path.insert(0, r"E:\量化交易\strategy_lab\src")
from strategylab.engine.storage import repository as repo

KW = dict(strategy_name="周线MACD + 日线KDJ 双入口做T",
          params_hash="9fecc9859fa3c12d30e8305bebf1be0f3452d704")

# 1) win_rate_pct 在全部 items 均存在且为 float
d = repo.rank_runs(**KW, page=1, size=100000)
items = d["items"]
missing = [i["run_id"] for i in items if "win_rate_pct" not in i]
badtype = [i["run_id"] for i in items if i.get("win_rate_pct") is not None and not isinstance(i["win_rate_pct"], float)]
print("[win_rate_pct] total items =", len(items))
print("  缺失 win_rate_pct 的 item 数:", len(missing))
print("  win_rate_pct 非 None 且非 float 的 item 数:", len(badtype))

# 2) sharpe 有 5 个 NULL -> 排序时 None 必须排末尾
def nils_last(items, f):
    seen_nil = False
    for x in items:
        if x.get(f) is None:
            seen_nil = True
        elif seen_nil:
            return False
    return True

for od in ("asc", "desc"):
    ds = repo.rank_runs(**KW, sort_by="sharpe", order=od)
    its = ds["items"]
    n_nil = sum(1 for x in its if x.get("sharpe") is None)
    print(f"[sharpe {od}] 末位是否全为 None 守卫: nils_last={nils_last(its,'sharpe')}, 实际None数={n_nil}")
    # 末位若干应为 None
    tail = [x.get("sharpe") for x in its[-3:]]
    print(f"   末3位 sharpe = {tail}")
    head = [x.get("sharpe") for x in its[:3]]
    print(f"   首3位 sharpe = {head}")

# 3) 显式 sort_by='total_return_pct', order='desc' 应与默认一致（数据无 NULL total_return_pct）
dd = repo.rank_runs(**KW, sort_by="total_return_pct", order="desc")
print("[一致性] 默认 vs 显式 total_return_pct desc 顺序一致:",
      [x["run_id"] for x in dd["items"]] == [x["run_id"] for x in repo.rank_runs(**KW)["items"]])
