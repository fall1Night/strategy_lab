# -*- coding: utf-8 -*-
"""数据审计（移植自 deepseek-harness-quant risk/data_audit.py 设计，MIT）。

定位：数据不可信则策略不可信 —— 数据审计是风控第一道防线。
策略/回测的错误若源于数据幻觉（价格错误 / 未来函数 / 缺失断档），风控层无法兜底。

★输入改造（适配 strategy_lab）：原版对 SQLite 聚合检查，本版改为 **CSV/DataFrame 检查**。
  审计对象为日线行情（date/open/high/low/close[/vol]），按策略 lab 的数据形态重写检查项。

设计原则（继承原版）：
1. ★只读审计：绝不修改任何数据文件
2. ★检查项注册制：每项独立方法 + 元数据，新增检查只需加一个方法
3. ★阈值走 config，改配置不改代码
4. ★审计即文档：每次运行输出报告（md/json），可追溯健康度
5. 状态三级：PASS / WARN / FAIL；strict 模式下任一 FAIL → 阻断（gate=False）

用法：
  from strategylab.engine.risk.data_audit import DataAuditor
  auditor = DataAuditor()
  result = auditor.run({"600519.SH": df, ...})     # bars: {code: DataFrame}
  result.gate  # False = 有 FAIL 项需阻断
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

# 检查项分类（A=完整性 B=价格 C=涨跌幅 D=量价 E=字段缺失 F=重复）
_STATUS_ORDER = {"PASS": 0, "WARN": 1, "FAIL": 2}

_DEFAULT_CFG = {
    "strict": True,                    # 任一 FAIL → gate=False（阻断）
    "warn_block": False,               # 可选：WARN 数过多也阻断
    "warn_block_count": 6,
    "report_dir": "report",
    "thresholds": {
        "min_rows_per_stock": 60,          # A1: 少于该行数视为短数据
        "max_gap_days": 5,                 # A2: 交易日缺口（自然日）超过 → WARN
        "price_limit_over": 21.0,          # C1: 涨跌幅超过该值（%）→ WARN（A股上限）
        "ohlc_violate_warn_max": 10,       # B1: OHLC 违规行数 ≤ 该值 → WARN，否则 FAIL
        "dup_date_warn_max": 5,            # F1: 重复日期行数阈值
        "null_pct_max": 5.0,               # E1: 核心字段缺失率上限(%)
    },
}


class AuditItem:
    """单项检查结果"""

    def __init__(self, check_id, category, name, status, detail, suggestion=""):
        self.id = check_id
        self.category = category
        self.name = name
        self.status = status
        self.detail = detail
        self.suggestion = suggestion

    def to_dict(self) -> dict:
        return {"id": self.id, "category": self.category, "name": self.name,
                "status": self.status, "detail": self.detail, "suggestion": self.suggestion}


class DataAuditor:
    """数据审计器：检查项注册制，全部只读"""

    def __init__(self, config: dict | None = None):
        cfg = config or {}
        self.cfg = {**_DEFAULT_CFG, **cfg}
        th = self.cfg.setdefault("thresholds", {})
        self.th = {**_DEFAULT_CFG["thresholds"], **th}
        self.items: list[AuditItem] = []
        self._start_ts = time.time()

    # ---------------- 基础设施 ----------------
    def _add(self, check_id, category, name, status, detail, suggestion=""):
        self.items.append(AuditItem(check_id, category, name, status, detail, suggestion))

    def _bar_df(self, bars: dict, code: str) -> pd.DataFrame | None:
        df = bars.get(code)
        if df is None or df.empty:
            return None
        out = df.copy()
        if not isinstance(out.index, pd.DatetimeIndex):
            if "date" in out.columns:
                out["date"] = pd.to_datetime(out["date"])
                out = out.set_index("date")
            else:
                out.index = pd.to_datetime(out.index)
        return out.sort_index()

    # ---------------- 检查项 ----------------
    # A1 数据覆盖：有效股票数 / 每只行数
    def _check_coverage(self, bars: dict):
        n_stocks = len(bars)
        if n_stocks == 0:
            self._add("A1", "完整性", "数据覆盖", "FAIL", "无任何标的数据")
            return
        short = [code for code, df in bars.items()
                 if df is not None and len(df) < self.th["min_rows_per_stock"]]
        if short:
            self._add("A1", "完整性", "数据覆盖", "WARN",
                      f"{len(short)}/{n_stocks} 只行数 < {self.th['min_rows_per_stock']}：{short[:5]}")
        else:
            self._add("A1", "完整性", "数据覆盖", "PASS", f"{n_stocks} 只标的，行数达标")

    # A2 日期连续性：升序、去重后缺口
    def _check_gaps(self, bars: dict):
        gaps = []
        for code, df in bars.items():
            bdf = self._bar_df(bars, code)
            if bdf is None or len(bdf) < 2:
                continue
            diffs = bdf.index.to_series().diff().dt.days.dropna()
            big = diffs[diffs > self.th["max_gap_days"]]
            if len(big) > 0:
                gaps.append(f"{code}:{len(big)} 处")
        if gaps:
            self._add("A2", "完整性", "日期连续性", "WARN",
                      f"{len(gaps)} 只存在 >{self.th['max_gap_days']} 日缺口：{gaps[:5]}")
        else:
            self._add("A2", "完整性", "日期连续性", "PASS", "无大缺口")

    # B1 OHLC 合法性：价格 > 0、low ≤ high、open/close 在 [low, high]
    def _check_ohlc(self, bars: dict):
        total_violations = 0
        n_with_data = 0
        for code in bars:
            bdf = self._bar_df(bars, code)
            if bdf is None:
                continue
            n_with_data += 1
            o, h, l, c = bdf["open"], bdf["high"], bdf["low"], bdf["close"]
            bad = (o <= 0) | (h <= 0) | (l <= 0) | (c <= 0) | (l > h)
            bad = bad | (o < l * 0.99) | (o > h * 1.01) | (c < l * 0.99) | (c > h * 1.01)
            total_violations += int(bad.sum())
        if total_violations > self.th["ohlc_violate_warn_max"]:
            self._add("B1", "价格", "OHLC 合法性", "FAIL",
                      f"{total_violations} 行 OHLC 违规（> {self.th['ohlc_violate_warn_max']}）",
                      "检查价格除权/数据源异常，修复后重拉")
        elif total_violations > 0:
            self._add("B1", "价格", "OHLC 合法性", "WARN", f"{total_violations} 行 OHLC 违规")
        else:
            self._add("B1", "价格", "OHLC 合法性", "PASS", f"{n_with_data} 只全部合法")

    # C1 涨跌幅超限：close 相对前收超过 A 股上限 → WARN（允许新股/ST 例外，故不 FAIL）
    def _check_price_limit(self, bars: dict):
        hits = []
        for code in bars:
            bdf = self._bar_df(bars, code)
            if bdf is None or len(bdf) < 2:
                continue
            pct = bdf["close"].pct_change() * 100
            over = pct[pct.abs() > self.th["price_limit_over"]]
            if len(over) > 0:
                hits.append(f"{code}:{len(over)} 日")
        if hits:
            self._add("C1", "价格", "涨跌幅超限", "WARN",
                      f"{len(hits)} 只存在 >{self.th['price_limit_over']}% 涨跌：{hits[:5]}",
                      "新股/ST/除权日可能；若为普通股需核查")
        else:
            self._add("C1", "价格", "涨跌幅超限", "PASS", "无超限涨跌")

    # D1 量价关系：成交量非负（有 vol 字段时）
    def _check_volume(self, bars: dict):
        neg = 0
        has_vol = False
        for code in bars:
            bdf = self._bar_df(bars, code)
            if bdf is None or "vol" not in bdf.columns:
                continue
            has_vol = True
            v = bdf["vol"]
            if v.isna().any() or (v < 0).any():
                neg += 1
        if not has_vol:
            self._add("D1", "量价", "成交量", "WARN", "全部标的无 vol 字段（量能因子不可算）")
        elif neg > 0:
            self._add("D1", "量价", "成交量", "WARN", f"{neg} 只存在成交量缺失/负值")
        else:
            self._add("D1", "量价", "成交量", "PASS", "成交量合法")

    # E1 字段缺失率
    def _check_nulls(self, bars: dict):
        worst = 0.0
        for code in bars:
            bdf = self._bar_df(bars, code)
            if bdf is None:
                continue
            for col in ("open", "high", "low", "close"):
                if col in bdf.columns:
                    pct = bdf[col].isna().mean() * 100
                    worst = max(worst, float(pct))
        if worst > self.th["null_pct_max"]:
            self._add("E1", "字段", "缺失率", "FAIL",
                      f"核心字段最高缺失率 {worst:.1f}% > {self.th['null_pct_max']}%")
        elif worst > 0:
            self._add("E1", "字段", "缺失率", "WARN", f"核心字段最高缺失率 {worst:.1f}%")
        else:
            self._add("E1", "字段", "缺失率", "PASS", "核心字段无缺失")

    # F1 重复日期
    def _check_duplicates(self, bars: dict):
        dup = 0
        for code in bars:
            bdf = self._bar_df(bars, code)
            if bdf is None:
                continue
            dup += int(bdf.index.duplicated().sum())
        if dup > self.th["dup_date_warn_max"]:
            self._add("F1", "完整性", "重复日期", "FAIL", f"{dup} 行重复日期（> {self.th['dup_date_warn_max']}）")
        elif dup > 0:
            self._add("F1", "完整性", "重复日期", "WARN", f"{dup} 行重复日期")
        else:
            self._add("F1", "完整性", "重复日期", "PASS", "无重复日期")

    # ---------------- 主流程 ----------------
    def run(self, bars: dict, out_dir: str | Path | None = None) -> dict:
        """执行全部检查项 → {gate, health_score, items[], summary}"""
        self.items = []
        self._check_coverage(bars)
        self._check_gaps(bars)
        self._check_ohlc(bars)
        self._check_price_limit(bars)
        self._check_volume(bars)
        self._check_nulls(bars)
        self._check_duplicates(bars)

        fails = [i for i in self.items if i.status == "FAIL"]
        warns = [i for i in self.items if i.status == "WARN"]
        passes = [i for i in self.items if i.status == "PASS"]
        health = len(passes) / max(len(self.items), 1)
        gate = not fails
        if self.cfg["warn_block"] and len(warns) > self.cfg["warn_block_count"]:
            gate = False
        result = {
            "gate": gate,
            "health_score": round(health, 3),
            "summary": {"total": len(self.items), "PASS": len(passes),
                        "WARN": len(warns), "FAIL": len(fails)},
            "items": [i.to_dict() for i in self.items],
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        if out_dir is not None:
            self._write_report(result, Path(out_dir))
        return result

    def _write_report(self, result: dict, out_dir: Path) -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        lines = [
            "# 数据审计报告",
            f"\n> 生成时间：{result['generated_at']} ｜ 健康度 {result['health_score']:.1%} ｜ "
            f"闸门：{'通过' if result['gate'] else '★FAIL 阻断'}",
            "",
            "| ID | 分类 | 检查项 | 状态 | 详情 | 建议 |",
            "|---|---|---|---|---|---|",
        ]
        for it in result["items"]:
            lines.append(f"| {it['id']} | {it['category']} | {it['name']} | **{it['status']}** | "
                         f"{it['detail']} | {it.get('suggestion', '')} |")
        (out_dir / "data_audit_report.md").write_text("\n".join(lines), encoding="utf-8")
        (out_dir / "data_audit_report.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


if __name__ == "__main__":
    rng = np.random.default_rng(1)
    dates = pd.date_range("2023-01-01", periods=400, freq="B")
    clean = pd.DataFrame({
        "open": 10 + np.cumsum(rng.normal(0, 0.1, 400)),
        "high": 11 + np.cumsum(rng.normal(0, 0.1, 400)),
        "low": 9 + np.cumsum(rng.normal(0, 0.1, 400)),
        "close": 10.5 + np.cumsum(rng.normal(0, 0.1, 400)),
        "vol": rng.integers(100000, 1000000, 400),
    }, index=dates)
    bars = {"000001.SZ": clean}
    auditor = DataAuditor()
    r = auditor.run(bars)
    print(f"健康度 {r['health_score']:.1%} ｜ 闸门 {'通过' if r['gate'] else 'FAIL'}")
    for it in r["items"]:
        print(f"  [{it['status']}] {it['id']} {it['name']}: {it['detail']}")
