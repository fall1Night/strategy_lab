# -*- coding: utf-8 -*-
"""回测可视化 + 存档（移植自 deepseek-harness-quant backtest/bt_report.py，MIT）。

★适配 strategy_lab：
  - plotly 改为**可选依赖**：未安装时自动降级为 JSON 存档（save_html=False 语义）
  - 输出目录参数化（默认 data/backtest_archive/），由调用方决定落位

用法：
  from strategylab.engine.bt_report import archive, list_archives, load_archive, compute_metrics
  archive(returns, params={"name": "因子策略", "topn": 5}, benchmark=bench_returns, name="factor_rank")

产出（时间戳命名，防覆盖）：
  bt_{name}_{YYYYMMDD_HHMMSS}.json  → 指标 + 参数 + 日收益序列（可复算/对比）
  bt_{name}_{YYYYMMDD_HHMMSS}.html  → 自包含交互图表（有 plotly 时）
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

DEFAULT_ARCHIVE_DIR = Path("data") / "backtest_archive"


def _annual_factor(index) -> float:
    """按 index 频率推断年化因子：日线 252，周 52，月 12，否则 252"""
    if len(index) < 2:
        return 252.0
    dt = pd.Series(index)
    if not pd.api.types.is_datetime64_any_dtype(dt):
        dt = pd.to_datetime(dt)
    delta_days = (dt.iloc[-1] - dt.iloc[0]).days
    if delta_days <= 0:
        return 252.0
    per_year = len(dt) / (delta_days / 365.25)
    return max(float(per_year), 1.0)


def compute_metrics(returns: pd.Series) -> dict:
    """标准回测指标（日/月收益序列通用）"""
    r = pd.Series(returns).astype(float).dropna()
    if len(r) == 0:
        return {}
    af = _annual_factor(r.index)
    eq = (1 + r).cumprod()
    total = float(eq.iloc[-1] - 1)
    annual = float((1 + total) ** (af / max(len(r), 1)) - 1)
    dd = eq / eq.cummax() - 1
    max_dd = float(dd.min())
    vol = float(r.std() * np.sqrt(af))
    sharpe = float(r.mean() / r.std() * np.sqrt(af)) if r.std() > 0 else 0.0
    downside = r[r < 0].std()
    sortino = float(r.mean() / downside * np.sqrt(af)) if downside and downside > 0 else 0.0
    calmar = float(annual / abs(max_dd)) if max_dd < 0 else 0.0
    win_rate = float((r > 0).mean())
    try:
        mr = r.resample("ME").apply(lambda x: (1 + x).prod() - 1) if af > 100 else r
    except (ValueError, TypeError):
        mr = r
    return {
        "total_return": total, "annual_return": annual, "max_drawdown": max_dd,
        "volatility": vol, "sharpe": sharpe, "sortino": sortino, "calmar": calmar,
        "win_rate": win_rate, "n_days": int(len(r)),
        "best_day": float(r.max()), "worst_day": float(r.min()),
        "final_nav": float(eq.iloc[-1]),
        "monthly_win_rate": float((mr > 0).mean()) if len(mr) else 0.0,
    }


def _series_to_records(returns: pd.Series) -> list:
    r = pd.Series(returns).astype(float).dropna()
    return [{"date": str(i)[:10], "ret": float(v)} for i, v in r.items()]


def render_html(returns: pd.Series, benchmark: pd.Series | None = None,
                metrics: dict | None = None, params: dict | None = None,
                title: str = "回测报告") -> str:
    """自包含交互 HTML（plotly 内联）：净值曲线 + 回撤 + 指标卡。

    未安装 plotly 时抛 ImportError（调用方降级为纯 JSON 存档）。
    """
    try:
        import plotly.graph_objects as go  # noqa: PLC0415
        from plotly.subplots import make_subplots  # noqa: PLC0415
    except ImportError as e:  # pragma: no cover
        raise ImportError("渲染 HTML 需安装 plotly（pip install plotly）；"
                          "未安装时可改用 archive(save_html=False) 纯 JSON 存档") from e

    r = pd.Series(returns).astype(float).dropna()
    metrics = metrics or compute_metrics(r)
    eq = (1 + r).cumprod()
    dd = (eq / eq.cummax() - 1) * 100

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.7, 0.3],
                        vertical_spacing=0.06, subplot_titles=("净值曲线", "回撤 %"))
    fig.add_trace(go.Scatter(x=eq.index, y=eq.values, name="策略净值",
                             line=dict(color="#D4A843", width=2)), row=1, col=1)
    if benchmark is not None:
        beq = (1 + pd.Series(benchmark).astype(float).reindex(r.index).dropna()).cumprod()
        fig.add_trace(go.Scatter(x=beq.index, y=beq.values, name="基准净值",
                                 line=dict(color="#6C8EBF", width=1.5, dash="dot")),
                      row=1, col=1)
    fig.add_trace(go.Scatter(x=dd.index, y=dd.values, name="回撤",
                             fill="tozeroy", line=dict(color="#C0392B", width=1)),
                  row=2, col=1)
    fig.update_layout(template="plotly_dark", height=680, title=title,
                      hovermode="x unified", showlegend=True)
    fig.update_yaxes(title_text="净值", row=1, col=1)
    fig.update_yaxes(title_text="回撤 %", row=2, col=1)

    cards = "".join(
        f'<div style="flex:1;min-width:120px;background:#16213a;border-radius:10px;'
        f'padding:12px 14px;margin:4px"><div style="color:#8a94a6;font-size:12px">{k}</div>'
        f'<div style="font-size:20px;font-weight:700;color:{c}">{v}</div></div>'
        for k, v, c in [
            ("年化收益", f"{metrics.get('annual_return', 0) * 100:.1f}%", "#2ECC71"),
            ("最大回撤", f"{metrics.get('max_drawdown', 0) * 100:.1f}%", "#E74C3C"),
            ("夏普", f"{metrics.get('sharpe', 0):.2f}", "#D4A843"),
            ("索提诺", f"{metrics.get('sortino', 0):.2f}", "#D4A843"),
            ("卡玛", f"{metrics.get('calmar', 0):.2f}", "#D4A843"),
            ("日胜率", f"{metrics.get('win_rate', 0) * 100:.1f}%", "#8a94a6"),
            ("月胜率", f"{metrics.get('monthly_win_rate', 0) * 100:.1f}%", "#8a94a6"),
            ("期末净值", f"{metrics.get('final_nav', 0):.2f}", "#2ECC71"),
        ])
    params_html = ""
    if params:
        items = "".join(f'<span style="margin-right:14px;color:#8a94a6">{k}: <b>{v}</b></span>'
                        for k, v in params.items())
        params_html = f'<div style="color:#8a94a6;font-size:12px;margin-top:6px">{items}</div>'

    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>{title}</title><style>body{{background:#0A1428;color:#e8ecf1;font-family:system-ui,sans-serif;
margin:0;padding:20px}}.wrap{{max-width:1100px;margin:0 auto}}</style></head><body>
<div class="wrap"><h2 style="margin:0 0 4px">{title}</h2>{params_html}
<div style="display:flex;flex-wrap:wrap;margin:12px 0">{cards}</div>
{fig.to_html(full_html=False, include_plotlyjs='inline')}
<div style="color:#6b7280;font-size:12px;margin-top:10px">回测含成本、非实盘；仅供研究，不构成投资建议。
样本 {metrics.get('n_days', 0)} 日 · 生成 {time.strftime('%Y-%m-%d %H:%M:%S')}</div></div></body></html>"""
    return html


def archive(returns: pd.Series, params: dict | None = None, metrics: dict | None = None,
            benchmark: pd.Series | None = None, name: str = "backtest",
            category: str = "策略", factors: list | None = None,
            verdict: str | None = None, save_html: bool = True,
            out_dir: str | Path | None = None) -> dict:
    """存档回测结果：写时间戳 JSON + 自包含 HTML（有 plotly 时），返回路径与指标"""
    out_dir = Path(out_dir) if out_dir else DEFAULT_ARCHIVE_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    r = pd.Series(returns).astype(float).dropna()
    metrics = metrics or compute_metrics(r)
    ts = time.strftime("%Y%m%d_%H%M%S")
    stem = f"{name}_{ts}"
    title = params.get("name", name) if params else name
    verdict = verdict or ("有效" if (metrics.get("annual_return") or 0) >= 0 else "无效")

    payload = {
        "name": name, "title": title, "category": category, "verdict": verdict,
        "factors": factors or [], "strategy": (params or {}).get("strategy", ""),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "params": params or {}, "metrics": metrics,
        "returns": _series_to_records(r),
        "benchmark": _series_to_records(pd.Series(benchmark)) if benchmark is not None else [],
    }
    json_path = out_dir / f"{stem}.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    html_path = out_dir / f"{stem}.html"
    if save_html:
        try:
            html_path.write_text(render_html(r, benchmark=benchmark, metrics=metrics,
                                             params=params, title=title), encoding="utf-8")
        except ImportError:
            save_html = False  # 无 plotly → 降级纯 JSON
    return {"json_path": str(json_path),
            "html_path": str(html_path) if save_html else "",
            "metrics": metrics}


def list_archives(out_dir: str | Path | None = None) -> dict:
    """列出回测存档 → {"history": [...]}（时间戳命名）"""
    out_dir = Path(out_dir) if out_dir else DEFAULT_ARCHIVE_DIR
    if not out_dir.exists():
        return {"history": []}
    history = []
    for j in sorted(out_dir.glob("*.json"), reverse=True):
        try:
            d = json.loads(j.read_text(encoding="utf-8"))
            if "metrics" not in d:
                continue
            history.append({
                "name": d.get("name", ""), "title": d.get("title"),
                "category": d.get("category"), "verdict": d.get("verdict"),
                "factors": d.get("factors") or [], "strategy": d.get("strategy", ""),
                "generated_at": d.get("generated_at"),
                "annual_return": d.get("metrics", {}).get("annual_return"),
                "max_drawdown": d.get("metrics", {}).get("max_drawdown"),
                "sharpe": d.get("metrics", {}).get("sharpe"),
                "json": str(j), "html": str(j.with_suffix(".html")),
                "has_html": j.with_suffix(".html").exists(),
            })
        except (ValueError, OSError):
            continue
    return {"history": history}


def load_archive(path: str | Path) -> dict:
    """读回 JSON 存档（returns 还原为 pd.Series）"""
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    if d.get("returns"):
        idx = pd.to_datetime([x["date"] for x in d["returns"]])
        d["returns_series"] = pd.Series([x["ret"] for x in d["returns"]], index=idx)
    return d


if __name__ == "__main__":
    np.random.seed(1)
    idx = pd.date_range("2020-01-01", periods=1000, freq="B")
    rets = pd.Series(np.random.randn(1000) * 0.01 + 0.0004, index=idx)
    bench = pd.Series(np.random.randn(1000) * 0.008 + 0.0002, index=idx)
    import tempfile
    res = archive(rets, params={"name": "自测策略", "topn": 5}, benchmark=bench,
                  name="selftest", out_dir=Path(tempfile.gettempdir()) / "bt_archive")
    print("存档 JSON:", res["json_path"])
    print("指标:", {k: round(v, 4) if isinstance(v, float) else v for k, v in res["metrics"].items()})
