# -*- coding: utf-8 -*-
"""QA 离线验证 A（真实 DB）：latest_batch_for_strategy 回归验证。

仅做 SELECT，不修改任何数据，不触碰运行中进程（独立连接，指向与运行服务
相同的 MySQL 实例）。运行：
  python qa_verify_warmup_realdb.py
"""
import os
import sys

# 与运行服务一致的 MySQL 连接（独立连接，只读）
os.environ.setdefault(
    "DATABASE_URL",
    "mysql+pymysql://root:root@localhost:3306/strategylab?charset=utf8mb4",
)

ROOT = r"E:\量化交易\strategy_lab"
sys.path.insert(0, os.path.join(ROOT, "src"))

from strategylab.engine import config
from strategylab.engine.storage import repository as repo

ok = True


def check(label, cond, detail=""):
    global ok
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {label} {detail}")
    if not cond:
        ok = False


print("================ A. 真实 DB 验证 latest_batch_for_strategy ================")

# 1) 取得 RSI 策略配置与 params_hash
cfg = config.load_strategy_by_arg("rsi")
strategy_name = cfg.get("name", "")
ph = repo.compute_params_hash(cfg)
print("strategy_name =", repr(strategy_name))
print("params_hash   =", ph)
check("rsi name 与需求一致", strategy_name == "RSI超买超卖", repr(strategy_name))
check("params_hash 非空", bool(ph), ph)

# 2) 真实批次查询（name + ph）：预期 done/total=1301，status=done
latest = repo.latest_batch_for_strategy(strategy_name, ph)
print("latest (name+ph) =", latest)
check("返回非 None", latest is not None)
if latest is not None:
    check("status == 'done'", latest["status"] == "done", latest["status"])
    check("done_count == 1301", latest["done_count"] == 1301, str(latest["done_count"]))
    check("total_count == 1301", latest["total_count"] == 1301, str(latest["total_count"]))
    check("created_at 为字符串且非空", isinstance(latest.get("created_at"), str) and bool(latest["created_at"]))
    check("返回字段集合 == {status,done_count,total_count,created_at}",
          set(latest.keys()) == {"status", "done_count", "total_count", "created_at"},
          str(sorted(latest.keys())))

# 3) 复刻 _api_rank 的 warmup 判定（与 web.py L1095-1109 同逻辑），验证 done==total 时 in_progress=False
def decide_warmup(latest):
    if latest is None:
        return None
    if latest["status"] == "running" or (
        latest["status"] in ("done", "cancelled", "interrupted")
        and latest["done_count"] < latest["total_count"]
    ):
        return {"in_progress": True, "status": latest["status"],
                "done": latest["done_count"], "total": latest["total_count"]}
    return None


warm = decide_warmup(latest)
check("done==total 时应 in_progress=False（warmup=None）", warm is None, repr(warm))

# 4) 不存在的策略名 -> 必须返回 None，且不得抛异常
try:
    none1 = repo.latest_batch_for_strategy("__不存在的策略__", None)
    check("不存在策略 -> None 且不抛错", none1 is None, repr(none1))
except Exception as e:
    check("不存在策略 -> None 且不抛错", False, f"抛异常 {type(e).__name__}: {e}")

# 5) params_hash=None 时仍能按 strategy_name 查到最新批次
latest_no_ph = repo.latest_batch_for_strategy(strategy_name, None)
print("latest (name, ph=None) =", latest_no_ph)
check("ph=None 仍返回非 None（按 strategy_name 命中）", latest_no_ph is not None)
if latest_no_ph is not None:
    check("ph=None 返回结构正确",
          set(latest_no_ph.keys()) == {"status", "done_count", "total_count", "created_at"},
          str(sorted(latest_no_ph.keys())))
    print("  ph=None 命中批次 status/done/total =",
          latest_no_ph["status"], latest_no_ph["done_count"], "/", latest_no_ph["total_count"])

print("\n=== 真实 DB 总体:", "ALL PASS ===" if ok else "HAS FAIL ===")
sys.exit(0 if ok else 1)
