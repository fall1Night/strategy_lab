# -*- coding: utf-8 -*-
"""因子有效性评估工具（移植自 deepseek-harness-quant validation/factor_evaluator.py，MIT）。

定位：数据层 → 因子层 → 【因子评估层（本模块）】 → 策略层。
职责：对每个因子做**多维度体检**，输出综合评分卡与裁决（强有效/弱有效/无效/反向 + 权重建议）。
任何因子进入选股/权重决策前，必须先过本关。

评估维度（8 项，机构标准：同花顺/东吴/华泰/中信建投 2025 方法论）：
  1. IC 分析    ：RankIC 均值 / ICIR / IC 胜率 / 近 6 期 IC
  2. 分层单调性 ：Q1-Q5 五分组平均收益的单调性（Spearman 相关）
  3. 多空组合   ：Q5-Q1 年化收益 / 夏普 / t 统计量（显著性）
  4. 多头超额   ：Q5（多头组）相对全池的年化超额
  5. 因子换手   ：月度排名变化率（决定真实交易成本）
  6. 衰减曲线   ：持有 5/20/60/120 日的 IC → 半衰期
  7. 分池稳健性 ：市值大/小组 IC 是否一致（需 mv_map）
  8. 时序稳定性 ：近 1 年 IC vs 全期 IC 差异（漂移检测）

★输入改造（适配 strategy_lab）：原版绑定 SQLite 缓存，本版改为**收盘价面板驱动**
  —— closes: DataFrame(index=日期, columns=股票代码)，零外部数据依赖。

用法：
  from strategylab.engine.factor_evaluator import evaluate_panel
  results = evaluate_panel(closes)                 # 评估全部已注册因子
  results = evaluate_panel(closes, factors=["lowvol_60", "rps_120"])
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from .factors.factor_engine import FACTOR_FUNCS


# ---------- 预处理（机构标准：去极值 + 截面标准化）----------
def winsorize_series(s: pd.Series, lo: float = 0.01, hi: float = 0.99) -> pd.Series:
    """去极值（分位数截断）"""
    return s.clip(s.quantile(lo), s.quantile(hi))


# ---------- 未来收益标签（改造：面板直接算，替代 SQLite DailyCache）----------
def build_forward_returns(closes: pd.DataFrame, month_ends: list,
                          horizon: int = 20) -> dict:
    """未来 horizon 日收益（标签）：T 月末收盘 → T+horizon 收盘。

    closes: DataFrame(index=日期, columns=股票代码)
    month_ends: 月末日期列表（字符串 'YYYY-MM-DD' 或日期）
    返回 {code: Series(index=月末, values=未来收益)}
    """
    fwd = closes.astype(float).shift(-horizon) / closes.astype(float) - 1
    me = pd.to_datetime(month_ends)
    if not isinstance(closes.index, pd.DatetimeIndex):
        closes = closes.copy()
        closes.index = pd.to_datetime(closes.index)
    fwd = fwd.reindex(me)
    return {code: fwd[code] for code in closes.columns}


# ---------- IC 分析（原样保留，输入已是面板格式）----------
def ic_analysis(panel: dict, labels: dict, factor_name: str) -> dict | None:
    """单个因子的 IC 分析：RankIC 均值/ICIR/胜率/近 6 期 IC"""
    ics = []
    month_series: dict[str, list] = {}
    for code, fdf in panel.items():
        if factor_name not in fdf.columns:
            continue
        lab = labels.get(code)
        if lab is None:
            continue
        for m, val in fdf[factor_name].items():
            if pd.notna(val):
                month_series.setdefault(m, []).append((val, lab.get(m, np.nan)))
    for m, pairs in sorted(month_series.items()):
        arr = np.array([(a, b) for a, b in pairs if pd.notna(b)])
        if len(arr) < 30:
            continue
        f = pd.Series(arr[:, 0]).rank()
        r = pd.Series(arr[:, 1]).rank()
        # f/r 已是 rank 值：Pearson ≡ Spearman（避免 scipy 依赖）
        ics.append((m, float(f.corr(r))))
    if len(ics) < 6:
        return None
    ic_series = pd.Series([v for _, v in ics], index=[m for m, _ in ics])
    return {
        "factor": factor_name,
        "n_months": len(ic_series),
        "rank_ic_mean": round(float(ic_series.mean()), 4),
        "rank_ic_std": round(float(ic_series.std()), 4),
        "icir": round(float(ic_series.mean() / ic_series.std()), 4) if ic_series.std() > 0 else 0.0,
        "ic_win_rate": round(float((ic_series > 0).mean()), 4),
        "ic_latest_6m": round(float(ic_series.tail(6).mean()), 4),
        "ic_positive_months": int((ic_series > 0).sum()),
    }


# ---------- 1. 分层回测（Q1-Q5）----------
def layered_backtest(panel: dict, factor_name: str, factor_df: pd.DataFrame,
                     labels: dict, n_groups: int = 5) -> dict:
    """五分组：按月末因子值分组，统计各组未来收益均值 → 单调性 + 多空 + 多头超额"""
    monthly: dict[str, list] = {}
    for code, fdf in panel.items():
        if factor_name not in fdf.columns:
            continue
        lab = labels.get(code)
        if lab is None:
            continue
        vals = fdf[factor_name].dropna()
        for m, v in vals.items():
            r = lab.get(m, np.nan)
            if pd.notna(r):
                monthly.setdefault(m, []).append((v, r))
    g_ret = {i: [] for i in range(1, n_groups + 1)}
    for m, pairs in monthly.items():
        if len(pairs) < 50:
            continue
        arr = sorted(pairs, key=lambda x: x[0])  # 按因子值升序
        n = len(arr)
        for i in range(1, n_groups + 1):
            lo = (i - 1) * n // n_groups
            hi = i * n // n_groups
            seg = arr[lo:hi]
            g_ret[i].append(np.mean([r for _, r in seg]))

    def ann(x: list) -> float:
        if len(x) < 6:
            return np.nan
        return float(np.mean(x) * 12)  # 月频近似年化

    g_annual = [ann(g_ret[i]) for i in range(1, n_groups + 1)]
    # rank 后 Pearson ≡ Spearman（避免 scipy 依赖）
    mono = float(pd.Series(range(1, n_groups + 1)).rank().corr(
        pd.Series(g_annual).rank())) if not any(np.isnan(g_annual)) else np.nan
    ls = [a - b for a, b in zip(g_ret[n_groups], g_ret[1])]
    if len(ls) >= 6 and np.std(ls) > 0:
        ls_ann = float(np.mean(ls) * 12)
        ls_sharpe = float(np.mean(ls) / np.std(ls) * np.sqrt(12))
        ls_t = float(np.mean(ls) / (np.std(ls) / np.sqrt(len(ls))))
    else:
        ls_ann = ls_sharpe = ls_t = np.nan
    top_excess = float(np.mean(g_ret[n_groups]) - np.mean(
        [r for grp in g_ret.values() for r in grp])) * 12 if g_ret[n_groups] else np.nan
    return {
        "group_annual": [round(x, 4) if not np.isnan(x) else None for x in g_annual],
        "monotonicity": round(mono, 4) if not np.isnan(mono) else None,
        "ls_annual": round(ls_ann, 4) if not np.isnan(ls_ann) else None,
        "ls_sharpe": round(ls_sharpe, 4) if not np.isnan(ls_sharpe) else None,
        "ls_t": round(ls_t, 3) if not np.isnan(ls_t) else None,
        "top_excess_annual": round(top_excess, 4) if not np.isnan(top_excess) else None,
    }


# ---------- 5. 因子换手 ----------
def factor_turnover(factor_df: pd.DataFrame) -> float:
    """月度排名变化率（0-1，越大换手越高成本越高）"""
    if factor_df is None or factor_df.empty:
        return np.nan
    ym = factor_df.index.astype(str).str[:7]
    month_ends = pd.Series(factor_df.index).groupby(ym).max()
    ranks = []
    for me in month_ends:
        if me in factor_df.index:
            ranks.append(factor_df.loc[me].rank(pct=True))
    if len(ranks) < 2:
        return np.nan
    chg = [(ranks[i] - ranks[i - 1]).abs().dropna() for i in range(1, len(ranks))]
    chg = [float(c.mean()) for c in chg if len(c) > 0]
    return float(np.mean(chg)) if chg else np.nan


# ---------- 6. 衰减曲线（多持有期 IC，改造：面板直接算）----------
def decay_curve(closes: pd.DataFrame, factor_name: str, month_ends: list,
                horizons: tuple = (5, 20, 60, 120)) -> dict:
    """各持有期的 RankIC 均值 → 半衰期近似"""
    ics = {}
    for h in horizons:
        labels = build_forward_returns(closes, month_ends, horizon=h)
        panel_f = _monthly_panel(closes, factor_name, month_ends)
        res = ic_analysis(panel_f, labels, factor_name)
        ics[h] = res["rank_ic_mean"] if res else np.nan
    half = None
    ic0 = ics.get(5, np.nan)
    if not np.isnan(ic0) and abs(ic0) > 0.001:
        for h in horizons[1:]:
            if not np.isnan(ics.get(h)) and abs(ics[h]) <= abs(ic0) / 2:
                half = h
                break
    return {"ic_by_horizon": {h: round(v, 4) if not np.isnan(v) else None for h, v in ics.items()},
            "half_life_days": half}


def _monthly_panel(closes: pd.DataFrame, factor_name: str, month_ends: list) -> dict:
    """月末截面因子面板 {code: DataFrame(index=月末, columns=[factor])}"""
    raw = closes.apply(lambda c: FACTOR_FUNCS[factor_name](c.astype(float)), axis=0)
    raw = raw.apply(winsorize_series, axis=0)
    me = pd.to_datetime(month_ends)
    raw_m = raw.reindex(me)
    return {code: pd.DataFrame({factor_name: raw_m[code]}) for code in closes.columns}


# ---------- 7. 分池稳健性 ----------
def pool_robustness(panel: dict, factor_name: str, labels: dict,
                    mv_map: dict | None, big_ratio: float = 0.3) -> dict:
    """市值大/小组 IC 一致性（简化：按市值排序前 30% vs 后 70%）"""
    if not mv_map:
        return {"big_ic": None, "small_ic": None, "consistent": None}
    mvs = [(c, mv_map.get(c.split(".")[0], np.nan)) for c in panel]
    mvs = [(c, v) for c, v in mvs if not np.isnan(v)]
    if len(mvs) < 30:
        return {"big_ic": None, "small_ic": None, "consistent": None}
    mvs.sort(key=lambda x: x[1], reverse=True)
    k = max(int(len(mvs) * big_ratio), 5)
    big_codes = [c for c, _ in mvs[:k]]
    small_codes = [c for c, _ in mvs[k:]]

    def ic_of(codes_sub: list) -> float:
        sub = {c: panel[c] for c in codes_sub if c in panel}
        if len(sub) < 10:
            return np.nan
        res = ic_analysis(sub, labels, factor_name)
        return res["rank_ic_mean"] if res else np.nan

    big_ic, small_ic = ic_of(big_codes), ic_of(small_codes)
    consistent = None
    if not np.isnan(big_ic) and not np.isnan(small_ic):
        consistent = (big_ic * small_ic) > 0
    return {"big_ic": round(big_ic, 4) if not np.isnan(big_ic) else None,
            "small_ic": round(small_ic, 4) if not np.isnan(small_ic) else None,
            "consistent": consistent}


# ---------- 8. 时序稳定性（近1年 vs 全期）----------
def temporal_stability(res: dict | None) -> dict:
    """近 6 期 IC vs 全期 IC"""
    if res is None or "ic_latest_6m" not in res or res.get("rank_ic_mean") is None:
        return {"latest_6m": None, "full": None, "drift": None}
    full = res["rank_ic_mean"]
    last = res["ic_latest_6m"]
    drift = None
    if full is not None and abs(full) > 0.001:
        drift = (last - full) / abs(full)
    return {"latest_6m": last, "full": full, "drift": drift}


# ---------- 综合评分卡 ----------
def score_card(ic_res: dict | None, layer: dict | None, turnover: float,
               decay: dict, pool: dict, temporal: dict, direction: int) -> dict:
    """多维度加权 → 0-100 分 + 裁决 + 权重建议"""
    s = 0.0
    n = 0
    # IC 维度（40 分）
    if ic_res:
        ic = abs(ic_res.get("rank_ic_mean") or 0)
        icir = abs(ic_res.get("icir") or 0)
        win = ic_res.get("ic_win_rate") or 0
        s += min(ic / 0.05, 1.0) * 20
        s += min(icir / 0.5, 1.0) * 12
        s += win * 8
        n += 40
    # 分层单调性（20 分）
    if layer and layer.get("monotonicity") is not None:
        s += max(0, abs(layer["monotonicity"])) * 20
        n += 20
    # 多空显著性（20 分）
    if layer and layer.get("ls_t") is not None:
        t = abs(layer["ls_t"])
        s += min(t / 3.0, 1.0) * 14
        if layer.get("ls_annual") and layer["ls_annual"] > 0:
            s += 6
        n += 20
    # 换手（10 分，越低越好）
    if turnover is not None and not np.isnan(turnover):
        s += max(0, 1 - turnover / 0.5) * 10
        n += 10
    # 时序稳定（10 分）
    if temporal and temporal.get("drift") is not None:
        s += max(0, 1 - min(abs(temporal["drift"]), 2)) * 10
        n += 10
    total = s / max(n, 1) * 100 if n else 0

    # 裁决（区分正向/反向信号：A 股反转市大量因子为负 IC 但强单调 → 反用）
    ic_sign = ""
    if ic_res and ic_res.get("rank_ic_mean") is not None:
        ic_sign = "反向" if ic_res["rank_ic_mean"] < 0 else "正向"
    if total >= 70:
        verdict = f"{ic_sign}强有效" if ic_sign else "强有效"
        weight_suggestion = "主权重（60-100%，反用）" if ic_sign == "反向" else "主权重（60-100%）"
    elif total >= 50:
        verdict = f"{ic_sign}弱有效" if ic_sign else "弱有效"
        weight_suggestion = "低权重（10-30%，反用）" if ic_sign == "反向" else "低权重（10-30%）"
    elif total >= 35:
        verdict = "边缘（需分池/条件使用）"
        weight_suggestion = "条件权重（分池启用）"
    else:
        verdict = "无效" if direction > 0 else "反向（反用或剔除）"
        weight_suggestion = "剔除 / 反用验证"
    return {"score": round(total, 1), "verdict": verdict,
            "weight_suggestion": weight_suggestion, "direction": direction}


# ---------- 主流程（面板驱动）----------
def evaluate_panel(closes: pd.DataFrame, factors: list | None = None,
                   month_ends: list | None = None, horizon: int = 20,
                   mv_map: dict | None = None,
                   out_dir: str | Path | None = None) -> dict:
    """评估全部（或指定）因子的 8 维体检 → {factor: {ic, layer, turnover, decay, pool, temporal, scorecard}}

    closes: DataFrame(index=日期(可字符串), columns=股票代码) 收盘价
    factors: 要评估的因子名列表（默认 FACTOR_FUNCS 全部）
    month_ends: 月末截面日期列表（默认自动推导）
    mv_map: {6位代码或代码前缀: 市值}，可选（分池稳健性）
    out_dir: 报告输出目录（默认不写文件，仅返回 results）
    """
    closes = closes.copy()
    closes.index = pd.to_datetime(closes.index)
    if month_ends is None:
        ym = closes.index.astype(str).str[:7]
        month_ends = [str(x)[:10] for x in pd.Series(closes.index).groupby(ym).max().tolist()]

    labels = build_forward_returns(closes, month_ends, horizon=horizon)
    names = factors or list(FACTOR_FUNCS.keys())
    results = {}
    for name in names:
        if name not in FACTOR_FUNCS:
            continue
        panel_f = _monthly_panel(closes, name, month_ends)
        ic_res = ic_analysis(panel_f, labels, name)
        layer = layered_backtest(panel_f, name, panel_f, labels)
        # 换手用原始月末面板
        raw_m = closes.apply(lambda c: FACTOR_FUNCS[name](c.astype(float)), axis=0)
        raw_m = raw_m.apply(winsorize_series, axis=0)
        me = pd.to_datetime(month_ends)
        turnover = factor_turnover(raw_m.reindex(me))
        decay = decay_curve(closes, name, month_ends)
        pool = pool_robustness(panel_f, name, labels, mv_map)
        temporal = temporal_stability(ic_res)
        sc = score_card(ic_res, layer, turnover, decay, pool, temporal, direction=1)
        results[name] = {
            "ic": ic_res, "layer": layer, "turnover": turnover,
            "decay": decay, "pool": pool, "temporal": temporal,
            "scorecard": sc,
        }
    if out_dir is not None:
        _write_report(results, Path(out_dir))
    return results


def _write_report(results: dict, out_dir: Path) -> None:
    """生成 markdown 报告 + JSON 评分卡"""
    out_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "# 因子有效性评估报告（中间层体检）",
        f"\n> 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
        "> 维度：IC分析 / 分层单调性 / 多空检验 / 换手 / 衰减 / 分池稳健 / 时序稳定",
        "",
        "## 综合评分卡",
        "",
        "| 因子 | 评分 | 裁决 | 权重建议 | IC | ICIR | 胜率 | 单调性 | 多空t | 换手 | 半衰期(日) |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name, r in sorted(results.items()):
        sc = r["scorecard"]
        ic = r["ic"]
        layer = r["layer"]
        lines.append(
            f"| {name} | **{sc['score']}** | {sc['verdict']} | {sc['weight_suggestion']} | "
            f"{ic['rank_ic_mean'] if ic else '-'} | {ic['icir'] if ic else '-'} | "
            f"{ic['ic_win_rate'] if ic else '-'} | {layer.get('monotonicity', '-')} | "
            f"{layer.get('ls_t', '-')} | {round(r['turnover'], 3) if not np.isnan(r['turnover']) else '-'} | "
            f"{r['decay'].get('half_life_days', '-')} |")
    (out_dir / "因子评估报告.md").write_text("\n".join(lines), encoding="utf-8")
    (out_dir / "factor_evaluations.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


if __name__ == "__main__":
    rng = np.random.default_rng(42)
    dates = pd.date_range("2022-01-01", periods=600, freq="B")
    closes = pd.DataFrame(
        {f"code{i}": 100 * np.cumprod(1 + rng.normal(0.0003, 0.015, len(dates)))
         for i in range(30)}, index=dates)
    res = evaluate_panel(closes, factors=["lowvol_60", "rps_120"])
    for name, r in res.items():
        sc = r["scorecard"]
        print(f"{name}: 评分 {sc['score']} ｜ {sc['verdict']} ｜ IC {r['ic']['rank_ic_mean'] if r['ic'] else '-'}")
