# -*- coding: utf-8 -*-
"""rank_runs 多键组合排序回归测试。

测试路径（明确声明）：**优先路径** —— 通过 ``qa_helpers.temp_db()`` 把
``DATABASE_URL`` 指向临时 SQLite 文件库（零网络、零 MySQL），用 ORM
（``repository.save_run``）播种 BacktestRun + Summary + Trade 行，直接调用
``rank_runs`` 并断言返回 ``items`` 的顺序。秒级完成，不触碰任何慢/脆弱 fixture。

为什么走这条路径就足够：
  - 被测逻辑全部在 ``rank_runs`` 的排序块内（repository.py ~844-877），本文件直接
    调用它，覆盖「去重后 items → 多键稳定排序 → 缺失键粘末尾」的完整真实行为。
  - CSV 导出（web._api_rank 的 ``export=csv`` 分支）与正常查询**共用同一个**
    ``rank_runs``（见 web.py 1233-1242），因此本文件对 rank_runs 的排序断言同时
    覆盖了导出排序（test_export_full_fetch_sorted_consistently 用 size=100000
    模拟导出的整取行为）。

web._api_rank 的多键 order 透传修复为**读代码核对**（不另起 HTTP 服务），结论见
文件末尾 CODE_REVIEW 常量与随测试一起提交的说明。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 让本文件能 import 到 tests/ 同级的 qa_helpers 与 src/strategylab
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from qa_helpers import temp_db  # noqa: E402

from strategylab.engine.storage import repository as repo  # noqa: E402

STRATEGY = "QA多键排序测试"
PARAMS_HASH = "qa-multisort-hash"


# ---------------------------------------------------------------------------
# 测试数据构造
# ---------------------------------------------------------------------------
def _seed(
    symbol: str,
    symbol_name: str,
    total_return_pct,
    win_rate_pct,
    sharpe,
    max_drawdown_pct: float = -1.0,
    last_buy_date: str | None = None,
):
    """播种一条 run；必含 Summary；last_buy_date 通过一条 Trade 的 entry_date 设置。

    ``last_buy_date=None``（不传 Trade）→ 该行该键缺失，应被粘到排序末尾。
    """
    equity = [{"date": "2024-01-01", "value": 1_000_000.0}]
    trades = []
    if last_buy_date is not None:
        trades = [
            {
                "entry_date": last_buy_date,
                "exit_date": last_buy_date,
                "side": "long",
                "role": "底仓",
                "position_id": f"pos-{symbol}",
                "size": 1000,
                "entry_price": 10.0,
                "exit_price": 10.5,
                "pnl": 0.0,
                "pnl_pct": 0.0,
                "holding_bars": 1,
                "symbol": symbol,
                "symbol_name": symbol_name,
                "display_symbol": symbol_name,
                "label": "建仓",
            }
        ]
    repo.save_run(
        {
            "batch_id": "batch-qa-multisort",
            "strategy_type": "kdj_macd_dual_entry",
            "strategy_name": STRATEGY,
            "symbol": symbol,
            "symbol_name": symbol_name,
            "start": "2024-01-01",
            "end": "2024-01-01",
            "initial_cash": 1_000_000.0,
            "params_json": "{}",
            "positions_json": "[]",
            "meta_json": "{}",
            "params_hash": PARAMS_HASH,
        },
        equity,
        trades,
        {
            "total_return_pct": total_return_pct,
            "annual_return_pct": 0.0,
            "max_drawdown_pct": max_drawdown_pct,
            "sharpe": sharpe,
            "win_rate_pct": win_rate_pct,
            "total_trades": 1,
        },
        [],
    )


def _symbols(result: dict) -> list[str]:
    return [it["symbol"] for it in result["items"]]


# 规格级 oracle：复现 rank_runs 的多键稳定排序语义（缺失键粘末尾）。
# 仅作差分测试用的「独立参照实现」，与源码实现互不依赖；下方属性断言才是真正的规格守卫。
_ALLOWED_SORT = {
    "symbol_name",
    "total_return_pct",
    "max_drawdown_pct",
    "sharpe",
    "win_rate_pct",
    "last_buy_date",
}


def _ref_sort(items: list[dict], sort_by, order: str) -> list[dict]:
    keys = [k.strip() for k in (sort_by or "").split(",")][:3] if sort_by else []
    dirs = [d.strip() for d in order.split(",")]
    rules: list[tuple[str, bool]] = []
    for i, k in enumerate(keys):
        if k not in _ALLOWED_SORT:
            continue
        d = dirs[i] if i < len(dirs) else "desc"
        rules.append((k, d == "desc"))
    if not rules:
        rules = [("total_return_pct", True)]
    # 与源码方案 B 对齐：先取出主键缺失行（保留查询序），最后统一附加到末尾；
    # 剩余行按 reversed(rules) 稳定排序——最低优先级先排、最高优先级(主键)最后排，
    # 靠稳定排序保住高优先级键顺序，次级键仅在"高优先级相等"时破平。
    primary_key = rules[0][0]
    null_primary = [x for x in items if x.get(primary_key) is None]
    pool = [x for x in items if x.get(primary_key) is not None]
    head: list[dict] = list(pool)
    tail: list[dict] = []
    for key, rev in reversed(rules):
        body = [x for x in head if x.get(key) is not None]
        nils = [x for x in head if x.get(key) is None]
        body.sort(key=lambda x: x[key], reverse=rev)
        head = body
        tail = nils + tail
    return head + tail + null_primary


# ---------------------------------------------------------------------------
# 1. 单键 total_return_pct desc：降序、None 排末尾、等值保持输入顺序（向后兼容）
# ---------------------------------------------------------------------------
def test_single_key_total_return_desc():
    with temp_db():
        # 输入顺序即插入顺序（rowid 递增）；R2/R4 等值 20.0 用于验证稳定排序
        _seed("R1", "r1", 30.0, 50.0, 1.0)
        _seed("R2", "r2", 20.0, 50.0, 1.0)
        _seed("R4", "r4", 20.0, 50.0, 1.0)
        _seed("R5", "r5", 10.0, 50.0, 1.0)
        _seed("R3", "r3", None, 50.0, 1.0)  # 总收益缺失

        res = repo.rank_runs(STRATEGY, PARAMS_HASH, sort_by="total_return_pct", order="desc")

        assert _symbols(res) == ["R1", "R2", "R4", "R5", "R3"]
        # None 在末尾
        assert res["items"][-1]["symbol"] == "R3"
        assert res["items"][-1]["total_return_pct"] is None
        # 等值 20.0 的两个保持输入顺序（R2 先于 R4，向后兼容的稳定排序）
        assert _symbols(res).index("R2") < _symbols(res).index("R4")


# ---------------------------------------------------------------------------
# 2. 单键 asc：升序正确，None 排末尾
# ---------------------------------------------------------------------------
def test_single_key_asc():
    with temp_db():
        _seed("a", "a", 10.0, 50.0, 1.0)
        _seed("c", "c", 30.0, 50.0, 1.0)
        _seed("b", "b", 20.0, 50.0, 1.0)
        _seed("n", "n", None, 50.0, 1.0)

        res = repo.rank_runs(STRATEGY, PARAMS_HASH, sort_by="total_return_pct", order="asc")

        assert _symbols(res) == ["a", "b", "c", "n"]
        assert res["items"][-1]["total_return_pct"] is None


# ---------------------------------------------------------------------------
# 3. 多键 last_buy_date desc, win_rate_pct asc
#    主键排好后：缺失主键(last_buy_date=None)的行粘到末尾、不参与次级排序；
#    主键非 None 的行内部按 win_rate_pct 升序；同日期内也按 win_rate 升序。
# ---------------------------------------------------------------------------
def test_multi_key_last_buy_date_desc_win_rate_asc():
    with temp_db():
        # 非 None 主键（A/B 同日期 07-20，C 07-10，D 07-05）
        _seed("A", "A", 40.0, 50.0, 1.0, last_buy_date="2026-07-20")
        _seed("B", "B", 30.0, 20.0, 1.0, last_buy_date="2026-07-20")
        _seed("C", "C", 20.0, 90.0, 1.0, last_buy_date="2026-07-10")
        _seed("D", "D", 10.0, 10.0, 1.0, last_buy_date="2026-07-05")
        # 缺失主键（无 Trade → last_buy_date=None）；win_rate 乱序，
        # 用来证明它们不被次级排序重排（保持查询顺序 = total_return desc: E,F,G）
        _seed("E", "E", 100.0, 5.0, 1.0)    # win=5
        _seed("F", "F", 90.0, 99.0, 1.0)    # win=99
        _seed("G", "G", 80.0, 40.0, 1.0)    # win=40

        # 取 rank_runs 在「默认排序」下的输入顺序（去重后、total_return desc），
        # 作为排序变换的同一输入，用规格 oracle 做差分校验。
        base = repo.rank_runs(STRATEGY, PARAMS_HASH, sort_by=None)["items"]
        expected = _ref_sort(base, "last_buy_date,win_rate_pct", "desc,asc")
        actual = repo.rank_runs(
            STRATEGY, PARAMS_HASH, sort_by="last_buy_date,win_rate_pct", order="desc,asc"
        )["items"]

        # 差分：多键稳定排序结果应与规格 oracle 完全一致
        # 正确语义：先按 last_buy_date desc（07-20 组 B/A 在前，C 07-10，D 07-05），
        # 同日期内再按 win_rate asc（B 在 A 前）；主键缺失的 E/F/G 保持查询序不参与次级排序。
        assert [it["symbol"] for it in actual] == [it["symbol"] for it in expected]
        assert [it["symbol"] for it in actual] == ["B", "A", "C", "D", "E", "F", "G"]

        # 属性1：主键非 None 的行全部在 主键为 None 的行之前
        actual_syms = [it["symbol"] for it in actual]
        non_nil = [it["symbol"] for it in actual if it["last_buy_date"] is not None]
        nil = [it["symbol"] for it in actual if it["last_buy_date"] is None]
        assert set(non_nil) == {"A", "B", "C", "D"}
        assert set(nil) == {"E", "F", "G"}
        assert actual_syms.index(nil[0]) > actual_syms.index(non_nil[-1])

        # 属性2：缺失主键的行粘到末尾且「不参与次级排序」
        #        末尾顺序 = 查询顺序(total_return desc: E,F,G)，而非 win_rate 升序(E,G,F)
        assert nil == ["E", "F", "G"], f"末尾顺序应等于 total_return desc，实际 {nil}"
        assert nil != ["E", "G", "F"], "缺失主键的行不应被次级排序重排"

        # 属性3：同一主键值(2026-07-20)内部按 win_rate_pct 升序
        grp = [it for it in actual if it["last_buy_date"] == "2026-07-20"]
        wins = [it["win_rate_pct"] for it in grp]
        assert wins == sorted(wins), f"同日期组内 win 应升序，实际 {wins}"


# ---------------------------------------------------------------------------
# 4. 非法/空 sort_by → 回退默认 total_return_pct desc
# ---------------------------------------------------------------------------
def test_illegal_or_empty_sort_by_falls_back_to_default():
    with temp_db():
        _seed("x", "x", 30.0, 10.0, 1.0)
        _seed("y", "y", 20.0, 20.0, 2.0)
        _seed("z", "z", 10.0, 30.0, 3.0)
        _seed("w", "w", None, 40.0, 4.0)

        candidates = [None, "", "bogus_key", "bogus1,bogus2", "bogus,total_return_pct"]
        for sb in candidates:
            res = repo.rank_runs(STRATEGY, PARAMS_HASH, sort_by=sb, order="desc")
            assert _symbols(res) == ["x", "y", "z", "w"], (
                f"sort_by={sb!r} 应回退默认 total_return desc，实际 {_symbols(res)}"
            )
            assert res["items"][-1]["symbol"] == "w"
            assert res["items"][-1]["total_return_pct"] is None


# ---------------------------------------------------------------------------
# 5a. order dirs 少于 keys → 缺省该 key 默认 desc
#     sort_by="total_return_pct,sharpe" 但 order="asc"（只给 1 个方向）
#     → 第 1 键 total_return asc；第 2 键 sharpe 默认 desc。
# ---------------------------------------------------------------------------
def test_order_dirs_fewer_than_keys_default_desc():
    with temp_db():
        _seed("d", "d", 5.0, 99.0, 3.0)    # total=5
        _seed("b", "b", 10.0, 20.0, 2.0)   # total=10, sharpe=2
        _seed("a", "a", 10.0, 50.0, 1.0)   # total=10, sharpe=1
        _seed("c", "c", 20.0, 10.0, 0.5)   # total=20
        _seed("e", "e", None, 1.0, 9.0)    # 缺失 total

        res = repo.rank_runs(
            STRATEGY, PARAMS_HASH, sort_by="total_return_pct,sharpe", order="asc"
        )
        # total 升序: d(5),b/a(10),c(20); 等值 total 内按 sharpe 降序: b(2) 在 a(1) 前
        assert _symbols(res) == ["d", "b", "a", "c", "e"], f"实际 {_symbols(res)}"
        assert res["items"][-1]["total_return_pct"] is None


# ---------------------------------------------------------------------------
# 5b. 超过 3 个 key → 只取前 3（第 4/5 键被忽略）
#     sort_by 含 5 个键，但只应对前 3 个（total desc, sharpe desc, win asc）生效；
#     若第 4 键 symbol_name 生效，c/d 会被翻转为 d,c（LLL < MMM）。
# ---------------------------------------------------------------------------
def test_more_than_three_keys_truncated():
    with temp_db():
        _seed("c", "MMM", 20.0, 10.0, 5.0)  # symbol_name=MMM
        _seed("d", "LLL", 20.0, 10.0, 5.0)  # symbol_name=LLL（与前 3 键同值）
        _seed("a", "AAA", 10.0, 50.0, 1.0)  # symbol_name=AAA
        _seed("b", "ZZZ", 10.0, 50.0, 1.0)  # symbol_name=ZZZ（与前 3 键同值）

        sort_by = "total_return_pct,sharpe,win_rate_pct,symbol_name,max_drawdown_pct"
        res = repo.rank_runs(
            STRATEGY, PARAMS_HASH, sort_by=sort_by, order="desc,desc,asc,asc,asc"
        )
        # 仅前 3 键：total desc → c,d,a,b；c 在 d 前证明第 4 键(symbol_name)未生效
        assert _symbols(res) == ["c", "d", "a", "b"], (
            f"前 3 键排序应得 c,d,a,b，实际 {_symbols(res)}"
        )


# ---------------------------------------------------------------------------
# 6. CSV 导出走同一 rank_runs：整取（size 很大）排序与分页一致
#    模拟 web._api_rank 的 export=csv 分支（size=100000），验证全量且顺序一致。
# ---------------------------------------------------------------------------
def test_export_full_fetch_sorted_consistently():
    with temp_db():
        _seed("A", "A", 40.0, 50.0, 1.0, last_buy_date="2026-07-20")
        _seed("B", "B", 30.0, 20.0, 1.0, last_buy_date="2026-07-20")
        _seed("C", "C", 20.0, 90.0, 1.0, last_buy_date="2026-07-10")
        _seed("D", "D", 10.0, 10.0, 1.0, last_buy_date="2026-07-05")
        _seed("E", "E", 100.0, 5.0, 1.0)
        _seed("F", "F", 90.0, 99.0, 1.0)
        _seed("G", "G", 80.0, 40.0, 1.0)

        # 模拟导出整取（web._api_rank 的 export=csv 分支 size=100000），
        # 排序应与分页查询走同一个 rank_runs，结果一致。
        base = repo.rank_runs(STRATEGY, PARAMS_HASH, sort_by=None, size=100000)["items"]
        expected = _ref_sort(base, "last_buy_date,win_rate_pct", "desc,asc")
        actual = repo.rank_runs(
            STRATEGY,
            PARAMS_HASH,
            sort_by="last_buy_date,win_rate_pct",
            order="desc,asc",
            size=100000,  # 模拟导出整取
        )
        assert actual["total"] == 7
        assert [it["symbol"] for it in actual["items"]] == [
            it["symbol"] for it in expected
        ]


# ---------------------------------------------------------------------------
# 代码审查结论（web.py _api_rank，约 1192-1242 行，读代码核对，未起 HTTP 服务）
# ---------------------------------------------------------------------------
# CODE_REVIEW = {
#   "location": "src/strategylab/web.py :: Handler._api_rank (≈1192-1242)",
#   "multi_key_order_passthrough": "PASS",
#   "detail": (
#       "order_raw 解析后：若含逗号（多键，如 'desc,asc'），按逗号切分并 strip、去空，"
#       "逐 token 校验均属 {asc,desc} 则原样 ','.join 透传；否则整体回退 'desc'。"
#       "单键则 order = order_raw if order_raw in (asc,desc) else 'desc'，行为不变。"
#       "无论 export='csv' 还是正常查询，均把 sort_by/order 原样传给 rank_runs，"
#       "因此多键 order='desc,asc' 能正确透传，单键行为向后兼容。"
#   ),
#   "shared_function": "PASS - CSV 导出与正常查询共用同一个 rank_runs（1233-1242）",
# }
