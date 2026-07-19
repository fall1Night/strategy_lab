# -*- coding: utf-8 -*-
"""QA 离线只读验证：直接调 repository.rank_runs，验证排序/分页/None安全/向后兼容。
不修改任何业务代码，不与运行中进程交互（独立 MySQL 连接，仅 SELECT）。
"""
import os
import sys

# 使用与项目一致的 MySQL 连接串（独立连接，不触碰运行中进程）
os.environ.setdefault(
    "DATABASE_URL",
    "mysql+pymysql://root:root@localhost:3306/strategylab?charset=utf8mb4",
)

ROOT = r"E:\量化交易\strategy_lab"
sys.path.insert(0, os.path.join(ROOT, "src"))

import pymysql  # noqa: E402

# ---- 1) 纯只读 SQL：找 run 数最多的 (strategy_name, params_hash) 组合 ----
conn = pymysql.connect(host="localhost", port=3306, user="root", password="root",
                       database="strategylab", connect_timeout=8)
try:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT strategy_name, params_hash, COUNT(*) c "
            "FROM backtest_runs GROUP BY strategy_name, params_hash "
            "ORDER BY c DESC LIMIT 5"
        )
        top = cur.fetchall()
        print("=== TOP 5 (strategy_name, params_hash, count) ===")
        for r in top:
            print(repr(r[0]), r[1], "count=", r[2])

        # 仅在最多组合的 run 上检查 NULL 分布（summary 表）
        sname, phash, _ = top[0]
        print("\n=== 选定组合 ===")
        print("strategy_name =", repr(sname))
        print("params_hash    =", phash)

        # 该组合有多少 run 有 summary（rank_runs 要求 join Summary）
        cur.execute(
            "SELECT COUNT(*) FROM backtest_runs r "
            "JOIN `summary` s ON s.run_id = r.run_id "
            "WHERE r.strategy_name=%s AND r.params_hash=%s", (sname, phash)
        )
        has_summary = cur.fetchone()[0]
        print("该组合 有 summary 的 run 数 =", has_summary)

        # NULL 分布（在 summary 关联行上）
        cur.execute(
            "SELECT "
            "SUM(CASE WHEN s.total_return_pct IS NULL THEN 1 ELSE 0 END) AS tr_null, "
            "SUM(CASE WHEN s.sharpe IS NULL THEN 1 ELSE 0 END) AS sh_null, "
            "SUM(CASE WHEN s.win_rate_pct IS NULL THEN 1 ELSE 0 END) AS wr_null, "
            "SUM(CASE WHEN s.max_drawdown_pct IS NULL THEN 1 ELSE 0 END) AS dd_null "
            "FROM backtest_runs r JOIN `summary` s ON s.run_id=r.run_id "
            "WHERE r.strategy_name=%s AND r.params_hash=%s", (sname, phash)
        )
        print("NULL 分布(该组合):", cur.fetchone())
finally:
    conn.close()

# ---- 2) 导入并直接调 rank_runs ----
from strategylab.engine.storage import repository as repo  # noqa: E402

def top5(items, field):
    return [(i.get("symbol_name"), i.get(field)) for i in items[:5]]

print("\n================ 调用 rank_runs ================")
KW = dict(strategy_name=sname, params_hash=phash)

# --- 1. 默认 sort_by=None：应含 win_rate_pct，total_return_pct 降序 ---
d0 = repo.rank_runs(**KW)
items0 = d0["items"]
print("\n[1] 默认 sort_by=None")
print("  返回字段 win_rate_pct 是否出现在 items[0]:",
      "win_rate_pct" in items0[0] if items0 else "NO_ITEMS")
print("  total =", d0["total"], " 本页条数 =", len(items0))
print("  top5 (symbol, total_return_pct):")
for t in top5(items0, "total_return_pct"):
    print("    ", t)
# 校验降序（忽略 None）
tr = [x["total_return_pct"] for x in items0 if x["total_return_pct"] is not None]
print("  total_return_pct 降序校验:", tr == sorted(tr, reverse=True))

# --- 2. win_rate_pct desc / asc ---
d_wr_desc = repo.rank_runs(**KW, sort_by="win_rate_pct", order="desc")
d_wr_asc = repo.rank_runs(**KW, sort_by="win_rate_pct", order="asc")
iw_d, iw_a = d_wr_desc["items"], d_wr_asc["items"]
print("\n[2] sort_by=win_rate_pct")
print("  desc top5:", top5(iw_d, "win_rate_pct"))
print("  asc  top5:", top5(iw_a, "win_rate_pct"))
wr_d = [x["win_rate_pct"] for x in iw_d if x["win_rate_pct"] is not None]
wr_a = [x["win_rate_pct"] for x in iw_a if x["win_rate_pct"] is not None]
print("  desc 校验(非None升序反转):", wr_d == sorted(wr_d, reverse=True))
print("  asc  校验(非None升序):", wr_a == sorted(wr_a))
# None 排末尾校验
def nils_last(items, f):
    seen_nil = False
    for x in items:
        if x.get(f) is None:
            seen_nil = True
        elif seen_nil:
            return False
    return True
print("  desc None在末尾:", nils_last(iw_d, "win_rate_pct"))
print("  asc  None在末尾:", nils_last(iw_a, "win_rate_pct"))
# desc/asc 末位应为 None（若数据存在 None）
print("  desc 末位 win_rate_pct:", iw_d[-1].get("win_rate_pct"))
print("  asc  首位 win_rate_pct:", iw_a[0].get("win_rate_pct"))

# --- 3. symbol_name / max_drawdown_pct / sharpe ---
d_sym = repo.rank_runs(**KW, sort_by="symbol_name", order="asc")
syms = [x["symbol_name"] for x in d_sym["items"]]
print("\n[3] sort_by=symbol_name asc")
print("  前5:", syms[:5])
print("  名称升序校验:", syms == sorted(syms))

d_dd = repo.rank_runs(**KW, sort_by="max_drawdown_pct", order="desc")
dd = [x["max_drawdown_pct"] for x in d_dd["items"] if x["max_drawdown_pct"] is not None]
print("\n[3] sort_by=max_drawdown_pct desc 校验:", dd == sorted(dd, reverse=True), "top5:", dd[:5])

d_sh = repo.rank_runs(**KW, sort_by="sharpe", order="asc")
sh = [x["sharpe"] for x in d_sh["items"] if x["sharpe"] is not None]
print("[3] sort_by=sharpe asc 校验:", sh == sorted(sh), "top5:", sh[:5])

# --- 4. 非法 sort_by ---
try:
    d_bad = repo.rank_runs(**KW, sort_by="xxx", order="desc")
    print("\n[4] 非法 sort_by='xxx' -> 未抛异常, total =", d_bad["total"])
    # 应与默认一致（total_return_pct 降序）
    bad_tr = [x["total_return_pct"] for x in d_bad["items"] if x["total_return_pct"] is not None]
    print("    回退默认(total_return_pct 降序)校验:", bad_tr == sorted(bad_tr, reverse=True))
    print("    与默认结果顺序一致:", [x["run_id"] for x in d_bad["items"]] == [x["run_id"] for x in items0])
except Exception as e:
    print("\n[4] 非法 sort_by 抛异常!!!", type(e).__name__, e)

# --- 5. 分页 page=1/2/3 size=50 ---
total = d0["total"]
print("\n[5] 分页 size=50, total =", total)
for p in (1, 2, 3):
    dp = repo.rank_runs(**KW, page=p, size=50)
    cnt = len(dp["items"])
    expected = min(50, total - (p - 1) * 50) if (p - 1) * 50 < total else 0
    print(f"    page={p}: 本页条数={cnt}, expected={expected}, total字段={dp['total']}, OK={cnt==expected and dp['total']==total}")

# --- 6. None 安全：直接构造含 None 的 items 场景已在真实数据里验证；再确认不抛异常 ---
print("\n[6] None 安全：所有排序字段对真实数据均不抛异常（上面均已执行）")
print("    确认：若存在 win_rate_pct=None 或 sharpe=None 的行，排序 None 在末尾（见[2]）")

print("\n================ 验证脚本结束 ================")
