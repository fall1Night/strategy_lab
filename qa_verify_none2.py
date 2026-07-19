# -*- coding: utf-8 -*-
"""真实数据构造 None 场景：找出「最新 run 的 sharpe 为 NULL」的 symbol，
再与若干非 NULL symbol 混合，调用真实 rank_runs(sort_by='sharpe') 验证 None 在末尾。
只读查询，不修改数据。"""
import os, sys
os.environ.setdefault("DATABASE_URL",
    "mysql+pymysql://root:root@localhost:3306/strategylab?charset=utf8mb4")
sys.path.insert(0, r"E:\量化交易\strategy_lab\src")
import pymysql
from strategylab.engine.storage import repository as repo

KW = dict(strategy_name="周线MACD + 日线KDJ 双入口做T",
          params_hash="9fecc9859fa3c12d30e8305bebf1be0f3452d704")

conn = pymysql.connect(host="localhost", port=3306, user="root", password="root",
                       database="strategylab", connect_timeout=8)
try:
    with conn.cursor() as cur:
        # 每个 symbol 的最新 run（按 created_at 最大）；找最新 run sharpe 为 NULL 的
        cur.execute(
            "SELECT r.symbol FROM backtest_runs r "
            "JOIN `summary` s ON s.run_id=r.run_id "
            "WHERE r.strategy_name=%s AND r.params_hash=%s "
            "AND r.created_at = (SELECT MAX(r2.created_at) FROM backtest_runs r2 "
            "   WHERE r2.symbol=r.symbol AND r2.strategy_name=%s AND r2.params_hash=%s) "
            "AND s.sharpe IS NULL LIMIT 8",
            (KW["strategy_name"], KW["params_hash"], KW["strategy_name"], KW["params_hash"])
        )
        null_syms = [row[0] for row in cur.fetchall()]
        # 若干非 NULL 的 symbol（最新 run sharpe 非 NULL）
        cur.execute(
            "SELECT r.symbol FROM backtest_runs r "
            "JOIN `summary` s ON s.run_id=r.run_id "
            "WHERE r.strategy_name=%s AND r.params_hash=%s "
            "AND r.created_at = (SELECT MAX(r2.created_at) FROM backtest_runs r2 "
            "   WHERE r2.symbol=r.symbol AND r2.strategy_name=%s AND r2.params_hash=%s) "
            "AND s.sharpe IS NOT NULL LIMIT 8",
            (KW["strategy_name"], KW["params_hash"], KW["strategy_name"], KW["params_hash"])
        )
        nonnull_syms = [row[0] for row in cur.fetchall()]
finally:
    conn.close()

print("最新 run sharpe=NULL 的 symbol:", null_syms)
print("最新 run sharpe!=NULL 的 symbol:", nonnull_syms)

if null_syms and nonnull_syms:
    mixed = null_syms + nonnull_syms
    for od in ("asc", "desc"):
        d = repo.rank_runs(**KW, symbols=mixed, sort_by="sharpe", order=od)
        its = d["items"]
        seq = [x.get("sharpe") for x in its]
        # None 必须在末尾
        seen_nil = False
        ok = True
        for v in seq:
            if v is None:
                seen_nil = True
            elif seen_nil:
                ok = False
        print(f"[sharpe {od}] 返回条数={len(its)}, 序列={seq}")
        print(f"           None 全在末尾: {ok}, 实际 None 数={seq.count(None)}")
else:
    print("未能构造混合场景（数据中无符合符号），改为逻辑核对：")
    print("  rank_runs 使用 _body=[非None] + _nils=[None] 拼接，保证 None 在末尾。")
