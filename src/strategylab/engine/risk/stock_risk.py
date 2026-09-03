# -*- coding: utf-8 -*-
"""个股风控层（移植自 deepseek-harness-quant risk/stock_risk.py，MIT）。

★定位：Pitch 前必须过风控——"标的公司有没有雷"。
数据审计（data_audit.py）管"数据本身可不可信"；本模块管"标的公司基本面有没有雷"。

★输入改造（适配 strategy_lab）：原版绑定 finance_quality.db（SQLite），
  本版将核心判定 `_check_row` 保持为**无 IO 纯函数**，数据由调用方注入：
    check(code, fin_data, m_level=None)
  其中 fin_data 为 {cfo_to_np, liability_to_asset, current_ratio, gp_margin, roe} 或 None。
  strategy_lab 暂无财务数据源 → 返回 NO_DATA 降级；数据接入后直接可用。

红旗清单（v1.0，基于可算项）：
  R1 现金流利润剪刀差：cfo_to_np < 0.5 或 > 2.0
  R2 高负债：liability_to_asset > 70%
  R3 流动比率过低：current_ratio < 1.0
  R4 盈利质量差：ROE>0 但 cfo_to_np 为负
  R5 毛利率异常：gp_margin > 95%
  R6 Beneish M-Score（外部输入 m_level: HIGH/WATCH/LOW）
  R8 股权质押/大股东减持（数据接入后启用）

输出：0-100 风控分（越高越危险）+ 红旗明细 → PASS(<40) / WATCH(40-60) / BLOCK(>60)
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

# 红旗阈值（改这里不改代码）
RED_FLAGS = {
    "r1_cfo_np_low":   {"col": "cfo_to_np", "op": "lt", "val": 0.5, "weight": 20,
                        "desc": "现金流/净利 < 0.5：利润缺现金支撑"},
    "r1_cfo_np_high":  {"col": "cfo_to_np", "op": "gt", "val": 2.0, "weight": 10,
                        "desc": "现金流/净利 > 2.0：异常高（可能洗钱/预收堆积）"},
    "r2_high_liab":    {"col": "liability_to_asset", "op": "gt", "val": 0.70, "weight": 15,
                        "desc": "资产负债率 > 70%：偿债压力大"},
    "r3_low_current":  {"col": "current_ratio", "op": "lt", "val": 1.0, "weight": 15,
                        "desc": "流动比率 < 1.0：短期偿付危机"},
    "r4_roe_no_cfo":   {"col": "cfo_to_np", "op": "lt", "val": 0.0, "weight": 25,
                        "desc": "ROE>0 但经营现金流为负：账面利润无现金（★最重红旗）"},
    "r5_high_gp":      {"col": "gp_margin", "op": "gt", "val": 0.95, "weight": 15,
                        "desc": "毛利率 > 95%：造假高发区"},
    "r6_beneish_high":  {"weight": 30, "desc": "Beneish M > -1.78：财务操纵高嫌疑（★最重红旗）"},
    "r6_beneish_watch": {"weight": 15, "desc": "Beneish M ∈ (-2.22, -1.78]：财务操纵中嫌疑"},
}


def check_row(code: str, fin_data: dict | None, m_level: str | None = None,
              period: str | None = None) -> dict:
    """单只标的 → 风控结果（无 IO，批量与单只共用）。

    fin_data: {roe, gp_margin, current_ratio, liability_to_asset, cfo_to_np} 或 None
    m_level: Beneish M-Score 等级（HIGH/WATCH/LOW/None），R6 红旗输入
    """
    if fin_data is None:
        return {"code": code, "score": None, "level": "NO_DATA",
                "flags": [{"id": "no_data", "desc": "财务数据未覆盖", "weight": 0}],
                "period": period}

    roe = fin_data.get("roe")
    gp = fin_data.get("gp_margin")
    cr = fin_data.get("current_ratio")
    liab = fin_data.get("liability_to_asset")
    cfo = fin_data.get("cfo_to_np")

    if (liab is not None and liab > 1.5) or (gp is not None and abs(gp) > 1.0):
        return {"code": code, "score": None, "level": "NO_DATA",
                "flags": [{"id": "dirty_data", "desc": "质量数据口径异常",
                           "weight": 0, "raw": {"liab": liab, "gp": gp}}],
                "period": period}
    flags = []
    score = 0
    for fid, spec in RED_FLAGS.items():
        if fid.startswith("r6_"):
            if m_level == "HIGH" and fid == "r6_beneish_high":
                flags.append({"id": fid, "desc": spec["desc"], "weight": spec["weight"],
                              "value": m_level, "threshold": "-1.78"})
                score += spec["weight"]
            elif m_level == "WATCH" and fid == "r6_beneish_watch":
                flags.append({"id": fid, "desc": spec["desc"], "weight": spec["weight"],
                              "value": m_level, "threshold": "(-2.22, -1.78]"})
                score += spec["weight"]
            continue
        v = {"cfo_to_np": cfo, "liability_to_asset": liab,
             "current_ratio": cr, "gp_margin": gp}.get(spec["col"])
        if v is None:
            continue
        hit = (v < spec["val"]) if spec["op"] == "lt" else (v > spec["val"])
        if hit:
            flags.append({"id": fid, "desc": spec["desc"], "weight": spec["weight"],
                          "value": round(float(v), 4), "threshold": spec["val"]})
            score += spec["weight"]
    if roe is not None and roe <= 0:
        score = max(score - 25, 0)
        flags = [f for f in flags if f["id"] != "r4_roe_no_cfo"]
    score = min(score, 100)
    level = "PASS" if score < 40 else ("WATCH" if score < 60 else "BLOCK")
    return {"code": code, "score": score, "level": level, "flags": flags, "period": period}


def scan(fin_map: dict, m_map: dict | None = None) -> dict:
    """批量风控扫描。

    fin_map: {code: fin_data dict}；m_map: {code: Beneish 等级}（可选）
    返回 {date, stats, results}；results 按风控分降序（越危险越靠前）。
    """
    m_map = m_map or {}
    results = [check_row(code, fd, m_level=m_map.get(code))
               for code, fd in fin_map.items()]
    results.sort(key=lambda x: -(x["score"] or -1))
    stats = {"total": len(results), "PASS": 0, "WATCH": 0, "BLOCK": 0, "NO_DATA": 0}
    for r in results:
        stats[r["level"]] = stats.get(r["level"], 0) + 1
    return {"date": datetime.now().strftime("%Y-%m-%d"), "stats": stats, "results": results}


def risk_level(code: str, fin_data: dict | None, m_level: str | None = None) -> str:
    """调用方入口：返回 PASS/WATCH/BLOCK（NO_DATA 按 WATCH 处理，宁严勿松）"""
    r = check_row(code, fin_data, m_level)
    return "WATCH" if r["level"] == "NO_DATA" else r["level"]


def save_scan_report(out: dict, out_path: str | Path) -> Path:
    """保存扫描结果 JSON（供 Web/审计消费）"""
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


if __name__ == "__main__":
    # 自测：全红旗 vs 干净 vs 无数据
    cases = [
        ("干净", {"roe": 0.15, "gp_margin": 0.45, "current_ratio": 1.8,
                 "liability_to_asset": 0.45, "cfo_to_np": 1.0}),
        ("全红旗", {"roe": 0.18, "gp_margin": 0.97, "current_ratio": 0.7,
                   "liability_to_asset": 0.78, "cfo_to_np": -0.2}),
        ("无数据", None),
    ]
    for name, fd in cases:
        r = check_row("600519.SH", fd)
        print(f"{name}: score={r['score']} level={r['level']} flags={[f['id'] for f in r['flags']]}")
    print("\nBeneish HIGH 场景:")
    r = check_row("600519.SH", {"roe": 0.15, "gp_margin": 0.45, "current_ratio": 1.8,
                                "liability_to_asset": 0.45, "cfo_to_np": 1.0},
                  m_level="HIGH")
    print(f"score={r['score']} level={r['level']} flags={[f['id'] for f in r['flags']]}")
