# -*- coding: utf-8 -*-
"""策略回测 CLI —— 输入策略 + 标的 + 日期，生成仪表盘。

用法示例（包化后）：
  strategylab --symbols 002001.SZ --start 2020-01-01 --end 2026-07-28
  python -m strategylab --symbols 600216.SH 300765.SZ --start 2020-01-01 --end 2026-07-28
  strategylab --list-strategies
  strategylab --list-runs

产出（默认落在 data/ 目录，由 STRATEGALAB_DATA_DIR 控制）：
  index.html          仪表盘（单标的=1个Tab；多标的=对比Tab+各标的Tab），仅为渲染视图
  <prefix>_daily.csv / <prefix>_weekly.csv   行情缓存（复用，不重复取数）
  回测结果（权益曲线 / 成交 / 指标 / 持仓）统一落库（SQLAlchemy，DB 无关），
  由 DATABASE_URL 控制（默认 sqlite:///./strategy_lab.db）。不再写三件套数据文件。
"""
from __future__ import annotations

import argparse
import logging
import sys
import uuid
from pathlib import Path

from .engine.config import load_strategy, load_strategy_by_arg
from .engine.backtest import run_symbol
from .engine.dashboard import build_compare_dashboard
from .engine.strategies import list_strategies
from .engine.storage import repository, db
from .settings import get_data_dir

logger = logging.getLogger(__name__)


def _print_summary(results: dict) -> None:
    logger.info("=== 回测结果 ===")
    for r in results.values():
        s = r["summary"]
        logger.info(
            "%s (%s): 总收益 %s%% | 年化 %s%% | 回撤 %s%% | Sharpe %s | 胜率 %s%% | 交易 %s 笔 | run_id=%s",
            r["name"], r["symbol"],
            s.get("total_return_pct"), s.get("annual_return_pct"),
            s.get("max_drawdown_pct"), s.get("sharpe"),
            s.get("win_rate_pct"), s.get("total_trades"),
            r.get("run_id"),
        )


def _print_db_runs() -> None:
    """打印数据库中已保存的回测历史（--list-runs）。"""
    try:
        db.init_db()
    except Exception as e:  # noqa: BLE001
        logger.error("数据库错误: 无法连接数据库，请检查 DATABASE_URL 配置。原始错误: %s: %s", type(e).__name__, e)
        return
    runs = repository.list_runs(limit=200)
    if not runs:
        logger.info("数据库中暂无回测历史（先跑一次回测即可落库）。")
        return
    logger.info("=== 回测历史（共 %d 条，按时间倒序）===", len(runs))
    logger.info("%-38s %-14s %-22s %s", "run_id", "标的", "策略", "区间")
    for r in runs:
        logger.info("%-38s %-14s %-22s %s~%s", r["run_id"], r["symbol"], r["strategy_name"], r["start"], r["end"])


def main() -> None:
    ap = argparse.ArgumentParser(description="策略回测 CLI：输入策略+标的+日期 → 仪表盘（结果落库）")
    ap.add_argument("--strategy", help="策略配置文件路径 或 别名（默认「周线MACD + 日线KDJ 双入口做T」）")
    ap.add_argument("--symbols", nargs="+", help="标的代码，如 002001.SZ 600216.SH（可多个）")
    ap.add_argument("--names", nargs="*", help="与 --symbols 对应的展示名称（可选）")
    ap.add_argument("--start", help="评估开始日期 YYYY-MM-DD")
    ap.add_argument("--end", help="评估结束日期 YYYY-MM-DD")
    ap.add_argument("--out", help="输出目录（默认 data/ 目录，由 STRATEGALAB_DATA_DIR 控制）")
    ap.add_argument("--list-strategies", action="store_true", help="列出已注册策略类型并退出")
    ap.add_argument("--list-runs", action="store_true", help="列出数据库中已保存的回测历史并退出")

    if len(sys.argv) == 1:
        ap.print_help()
        return

    # ---- datasource status 子命令（极薄分发，不破坏现有 argparse） ----
    if len(sys.argv) >= 2 and sys.argv[1] == "datasource":
        if len(sys.argv) >= 3 and sys.argv[2] == "status":
            from .engine.datasource.cli_status import cmd_status
            print(cmd_status())
            return
        else:
            logger.info("用法: strategylab datasource status")
            return

    args = ap.parse_args()

    if args.list_strategies:
        logger.info("已注册策略类型:")
        for s in list_strategies():
            logger.info("  - %s", s)
        return

    if args.list_runs:
        _print_db_runs()
        return

    if not args.symbols or not args.start or not args.end:
        ap.error("必须提供 --symbols / --start / --end（或使用 --list-strategies / --list-runs）")

    cfg = load_strategy_by_arg(args.strategy or "kdj_macd_dual_entry")
    logger.info("=== 加载策略: %s (type=%s) ===", cfg.get("name"), cfg.get("type"))

    names = args.names or [None] * len(args.symbols)

    out_dir = Path(args.out) if args.out else get_data_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    # 同一批提交共享一个 batch_id，便于「回看我上一批提交」
    batch_id = str(uuid.uuid4())

    results = {}
    for sym, nm in zip(args.symbols, names):
        logger.info("--- 回测 %s ---", sym)
        try:
            r = run_symbol(cfg, sym, nm, args.start, args.end, out_dir, batch_id=batch_id)
        except Exception as e:  # noqa: BLE001
            raise SystemExit(
                f"\n[数据库错误] 无法将回测结果保存到数据库，已终止。请检查 DATABASE_URL 配置。\n"
                f"原始错误: {type(e).__name__}: {e}"
            )
        results[r["prefix"]] = r

    _print_summary(results)

    out_html = out_dir / "index.html"
    # 统一使用「策略对比」排版：单标的也走对比布局（Tab1 策略对比 + 该标的独立 Tab），
    # 与多标的视图一致，避免「单标的/多标的 两种排版」的差异。
    # 注意：index.html 仅为「渲染视图」，数据已落库，不再写三件套数据文件。
    build_compare_dashboard(results, cfg, out_html, args.start, args.end)

    logger.info("=== 仪表盘已生成（渲染视图）===\n  %s", out_html)
    logger.info("回测结果已保存到数据库，可用 `strategylab --list-runs` 查看，或在网页 /history 跨回测对比。")


if __name__ == "__main__":
    main()
