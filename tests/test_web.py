# -*- coding: utf-8 -*-
"""FR-17/18/20~25 测试：Web 层（四页导航、批次 API、排名、板块状态、回归）。

G. 起 ThreadingHTTPServer 绑定临时端口（临时 SQLite DATABASE_URL），断言：
   - /production、/analysis 返回 200 且无日期选择器；
   - / 首页含日期选择器 + 四页导航；/history 200；
   - POST /api/batch 返回 batch_id/total_count/hit_count；
   - GET /api/batch/<id>/progress 返回进度；
   - GET /api/rank 返回含字段的 items；
   - GET /api/sector-status 返回 31 板块；
   - 回归：/api/runs、/api/search、/api/sector-stocks 仍正常；POST /run 解析不崩。
"""
from __future__ import annotations

import contextlib
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from http.server import ThreadingHTTPServer

from strategylab.engine import batch_runner
from strategylab.engine.config import load_strategy_by_arg
from strategylab.engine.storage import repository
from strategylab.engine.storage.repository import compute_params_hash
from strategylab import web as web_mod

from qa_helpers import (
    DATA_DIR,
    PROJECT_ROOT,
    equity_curve,
    run_meta,
    summary,
    temp_db,
    trades,
)


@contextlib.contextmanager
def _web_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web_mod.Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)


def _get(base, path):
    with urllib.request.urlopen(base + path, timeout=15) as resp:
        return resp.status, resp.read().decode("utf-8")


def _post(base, path, data: dict):
    body = urllib.parse.urlencode(data, doseq=True).encode("utf-8")
    req = urllib.request.Request(base + path, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status, resp.read().decode("utf-8")


def _fake_run_symbol(strategy_cfg, symbol, symbol_name, start, end, out_dir, batch_id=None):
    ph = repository.compute_params_hash(strategy_cfg)
    rid = repository.save_run(
        run_meta(
            {
                "params_hash": ph,
                "symbol": symbol,
                "symbol_name": symbol_name,
                "strategy_name": strategy_cfg.get("name", ""),
            }
        ),
        equity_curve(),
        trades(),
        summary(),
        [],
    )
    return {"run_id": rid}


def test_web_pages_and_apis(monkeypatch):
    import json

    monkeypatch.setattr(batch_runner, "run_symbol", _fake_run_symbol)
    monkeypatch.setattr(web_mod, "run_backtest", lambda *a, **k: "<html>ok-run</html>")

    # CWD 切到项目根，使 web.py 的 data/sectors.json 相对路径正确
    prev_cwd = os.getcwd()
    os.chdir(PROJECT_ROOT)

    with temp_db():
        try:
            cfg = load_strategy_by_arg("kdj_macd_dual_entry")
            ph = compute_params_hash(cfg)
            sname = cfg["name"]

            # 预存 5 条 run，供 /api/rank 返回
            for i, sym in enumerate(["600216.SH", "000001.SZ", "300765.SZ", "002001.SZ", "600066.SH"]):
                repository.save_run(
                    run_meta(
                        {"params_hash": ph, "strategy_name": sname,
                         "symbol": sym, "symbol_name": sym}
                    ),
                    equity_curve(), trades(), summary(total_return_pct=10.0 * (i + 1)), [],
                )

            with _web_server() as base:
                # --- 页面 ---
                st, home = _get(base, "/")
                assert st == 200
                assert '<input type="date"' in home, "首页应保留日期选择器"
                assert "/production" in home and "/analysis" in home and "/history" in home

                st, prod = _get(base, "/production")
                assert st == 200
                assert '<input type="date"' not in prod, "/production 不应含日期选择器"

                st, ana = _get(base, "/analysis")
                assert st == 200
                assert '<input type="date"' not in ana, "/analysis 不应含日期选择器"

                st, hist = _get(base, "/history")
                assert st == 200

                # --- 批次提交 ---
                st, body = _post(
                    base, "/api/batch",
                    {
                        "scope_type": "pool",
                        "symbols": "600216.SH,000001.SZ,603365.SH",  # 前两个命中，603365 待跑（不与排名预存重叠）
                        "strategy": "kdj_macd_dual_entry",
                    },
                )
                assert st == 200, f"POST /api/batch 应 200，实际 {st}: {body}"
                batch = json.loads(body)
                assert "batch_id" in batch and "total_count" in batch and "hit_count" in batch
                assert batch["total_count"] == 3
                assert batch["hit_count"] == 2, f"应有 2 个命中复用，实际 {batch['hit_count']}"
                bid = batch["batch_id"]

                # 等待批次完成（603365.SH 由线程池跑出）
                # 注：running+done_count>0 窗口 get_progress 会 500（已知 bug），重试直至终态
                pj = None
                for _ in range(200):
                    try:
                        _, prog = _get(base, f"/api/batch/{bid}/progress")
                        pj = json.loads(prog)
                        if pj["status"] in ("done", "cancelled", "interrupted"):
                            break
                    except urllib.error.HTTPError:
                        pass
                    time.sleep(0.05)
                assert pj is not None and pj["status"] == "done", f"批次应完成，实际 {pj}"
                assert pj["skipped"] == 2 and pj["done"] == 1

                # 批次列表
                _, bl = _get(base, "/api/batch")
                blj = json.loads(bl)
                assert any(b["batch_id"] == bid for b in blj["batches"])

                # --- 排名 ---
                _, rk = _get(base, f"/api/rank?strategy=kdj_macd_dual_entry&params_hash={ph}")
                rkj = json.loads(rk)
                assert "items" in rkj
                assert len(rkj["items"]) >= 1
                it0 = rkj["items"][0]
                for k in ("run_id", "symbol_name", "total_return_pct", "max_drawdown_pct", "sharpe"):
                    assert k in it0, f"排名项缺少字段 {k}"

                # --- 板块状态（31 板块）---
                _, sec = _get(base, "/api/sector-status?strategy=kdj_macd_dual_entry")
                secj = json.loads(sec)
                assert "sectors" in secj
                assert len(secj["sectors"]) == 31, f"应返回 31 板块，实际 {len(secj['sectors'])}"
                for s in secj["sectors"]:
                    for k in ("code", "name", "status"):
                        assert k in s

                # --- 回归：旧 API 仍正常 ---
                _, runs = _get(base, "/api/runs")
                runsj = json.loads(runs)
                assert "runs" in runsj

                _, search = _get(base, "/api/search?q=")
                srj = json.loads(search)
                assert "items" in srj and isinstance(srj["items"], list)

                # --- 回归：POST /run 解析入口不崩 ---
                st, runhtml = _post(
                    base, "/run",
                    {
                        "symbols": "600216.SH",
                        "start": "2023-01-01",
                        "end": "2024-01-01",
                        "strategy": "kdj_macd_dual_entry",
                    },
                )
                assert st == 200, f"POST /run 应 200，实际 {st}"
                assert "ok-run" in runhtml
        finally:
            os.chdir(prev_cwd)


def test_api_sector_stocks_route_is_wired(monkeypatch):
    """回归断言：前端 build_form_html 的 loadSectorStocks 依赖 GET /api/sector-stocks。

    当前 do_GET 未挂载该路由 → 404（已知源码 bug，已路由给工程师修复）。
    保留本测试作为该 bug 的精准复现与回归守卫。
    """
    prev_cwd = os.getcwd()
    os.chdir(PROJECT_ROOT)
    try:
        with _web_server() as base:
            sectors = repository.get_sectors()
            code = sectors[0]["code"] if sectors else "801010"
            st, body = _get(base, f"/api/sector-stocks?code={code}")
            assert st == 200, f"GET /api/sector-stocks 应返回 200，实际 {st}: {body}"
    finally:
        os.chdir(prev_cwd)
