# -*- coding: utf-8 -*-
"""QA 离线验证 A2（隔离环境）：构造「running/done<total」等临时场景 + 出错兜底路径。

使用独立内存 SQLite（StaticPool 保证跨会话可见），不触碰真实 DB 与运行中进程。
验证：
  - running & done<total -> {in_progress:true, status, done, total}
  - done/cancelled/interrupted & done<total -> in_progress:true
  - done==total 或 failed 等情况 -> in_progress:false (warmup=None)
  - 查询抛错 -> 返回 None（防御性兜底）
  - 错误 params_hash -> 返回 None
运行：
  python qa_verify_warmup_mock.py
"""
import os
import sys
import unittest.mock as mock

# 隔离内存库，绝对不会影响真实 DB
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

ROOT = r"E:\量化交易\strategy_lab"
sys.path.insert(0, os.path.join(ROOT, "src"))

from strategylab.engine.storage import repository as repo
from strategylab.engine.storage import db as dbmod
from strategylab.engine.storage.schema import Batch

# 初始化表结构（内存库）
repo.init_db()

ok = True


def check(label, cond, detail=""):
    global ok
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {label} {detail}")
    if not cond:
        ok = False


def insert_batch(name, ph, status, done, total):
    """用 ORM 直接写一条 batches 记录（隔离内存库，不影响真实数据）。"""
    with dbmod.get_session() as s:
        s.add(Batch(
            batch_id=f"test-{name}-{status}-{done}",
            strategy_name=name,
            strategy_type="turtle",
            params_hash=ph,
            scope_type="sector",
            scope_value="ALL",
            total_count=total,
            done_count=done,
            failed_count=0,
            skipped_count=0,
            status=status,
            started_at=None,
            finished_at=None,
        ))


def decide_warmup(latest):
    """复刻 web.py _api_rank L1095-1109 的判定逻辑。"""
    if latest is None:
        return None
    if latest["status"] == "running" or (
        latest["status"] in ("done", "cancelled", "interrupted")
        and latest["done_count"] < latest["total_count"]
    ):
        return {"in_progress": True, "status": latest["status"],
                "done": latest["done_count"], "total": latest["total_count"]}
    return None


WARM_KEYS = {"in_progress", "status", "done", "total"}


def run_scenario(name, status, done, total):
    ph = "ph_" + name
    insert_batch(name, ph, status, done, total)
    latest = repo.latest_batch_for_strategy(name, ph)
    return decide_warmup(latest), latest


print("================ A2. 隔离场景 + 兜底路径 ================")

# --- 场景 1: running & done<total -> in_progress True ---
w, latest = run_scenario("S1_running", "running", 5, 100)
check("running: latest 非 None", latest is not None, repr(latest))
check("running: 透传 status/done/total",
      latest and latest["status"] == "running" and latest["done_count"] == 5 and latest["total_count"] == 100)
check("running: in_progress=True", bool(w and w.get("in_progress") is True))
check("running: warmup 结构 == {in_progress,status,done,total}",
      bool(w) and set(w.keys()) == WARM_KEYS, str(sorted(w.keys())) if w else "None")
check("running: done/total 透传正确", bool(w) and w["done"] == 5 and w["total"] == 100)

# --- 场景 2: done & done<total（刚结束但未跑完） -> in_progress True ---
w, latest = run_scenario("S2_done_partial", "done", 50, 100)
check("done&done<total: in_progress=True", bool(w and w.get("in_progress") is True), repr(w))
check("done&done<total: 结构正确", bool(w) and set(w.keys()) == WARM_KEYS)

# --- 场景 3: done & done==total -> in_progress False ---
w, latest = run_scenario("S3_done_full", "done", 100, 100)
check("done&done==total: in_progress=False (warmup=None)", w is None, repr(w))

# --- 场景 4: cancelled & done<total -> in_progress True ---
w, latest = run_scenario("S4_cancelled", "cancelled", 30, 100)
check("cancelled&done<total: in_progress=True", bool(w and w.get("in_progress") is True), repr(w))

# --- 场景 5: interrupted & done<total -> in_progress True ---
w, latest = run_scenario("S5_interrupted", "interrupted", 70, 100)
check("interrupted&done<total: in_progress=True", bool(w and w.get("in_progress") is True), repr(w))

# --- 场景 6: interrupted & done==total -> in_progress False ---
w, latest = run_scenario("S6_interrupted_full", "interrupted", 100, 100)
check("interrupted&done==total: in_progress=False", w is None, repr(w))

# --- 场景 7: failed -> 不在判定元组内，in_progress False ---
w, latest = run_scenario("S7_failed", "failed", 0, 100)
check("failed: in_progress=False (warmup=None)", w is None, repr(w))

# --- 场景 8: 错误 params_hash -> 无匹配 -> None ---
insert_batch("S8_real", "ph_S8_real", "running", 5, 100)
wrong = repo.latest_batch_for_strategy("S8_real", "wrong_ph_xyz")
check("错误 params_hash -> 无匹配返回 None", wrong is None, repr(wrong))

# --- 场景 9: 出错兜底（查询抛异常 -> 返回 None，不向上抛）---
with mock.patch.object(repo, "get_session", side_effect=RuntimeError("simulated DB failure")):
    res = repo.latest_batch_for_strategy("S1_running", "ph_S1_running")
check("查询抛错 -> 返回 None（防御性兜底，不抛异常）", res is None, repr(res))

print("\n=== 隔离场景总体:", "ALL PASS ===" if ok else "HAS FAIL ===")
sys.exit(0 if ok else 1)
