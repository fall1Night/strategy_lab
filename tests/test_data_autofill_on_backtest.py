# -*- coding: utf-8 -*-
"""FR-40 回归测试：回测行情不足时自动双向补（往前补历史 + 往后补到今天）。

覆盖三处改动：
  1. ``KlineCache._incremental_window`` 双向补（用例A）
  2. ``KlineProvider.ensure_data`` 的 update 分支：往前补触发取数、不被误跳过（用例B）
  3. ``backtest.run_symbol`` 的 verify→update 契约：verify 抛 DataMissingError 后
     自动走 update 补足再继续回测（用例C）

所有网络取数均用 monkeypatch 桩替换 ``KlineProvider._try_fetch``，无需联网。
"""
from __future__ import annotations

import json
from datetime import date as _real_date
from pathlib import Path
from tempfile import mkdtemp
from unittest.mock import MagicMock

import pandas as pd

from strategylab.engine import data_feed
from strategylab.engine.datasource import provider as provider_mod
from strategylab.engine.datasource.cache import KlineCache
from strategylab.engine.datasource.exceptions import DataMissingError


# 用 data_feed.normalize_symbol（兼容层，返回 dict）构造 sym_cfg
SYM = data_feed.normalize_symbol("600216.SH")
PREFIX = SYM["prefix"]
SYMBOL = SYM["symbol"]


# ---------------------------------------------------------------------------
# 桩：构造可被 cache.merge 消费的 DataFrame（含 date/open/high/low/close）
# ---------------------------------------------------------------------------
def _stub_df(beg: str, end: str) -> pd.DataFrame:
    """返回覆盖 [beg, end] 起止两行的日/周线桩（date 用 YYYY-MM-DD）。"""
    b = f"{beg[:4]}-{beg[4:6]}-{beg[6:]}"
    e = f"{end[:4]}-{end[4:6]}-{end[6:]}"
    return pd.DataFrame(
        {
            "date": [b, e],
            "open": [10.0, 11.0],
            "high": [12.0, 13.0],
            "low": [9.0, 10.0],
            "close": [11.0, 12.0],
        }
    )


def _make_fetch_mock(captured: list | None = None) -> MagicMock:
    """mock KlineProvider._try_fetch：按调用入参 (period, beg, end) 返桩并可选记录。

    注意：monkeypatch 在类上替换 _try_fetch 为 MagicMock 后，访问 self._try_fetch
    不会绑定 self，故 fake 不应包含 self 形参。
    """

    def fake(symbol, spec, period, beg, end, lmt):
        if captured is not None:
            captured.append((period, beg, end))
        return _stub_df(beg, end)

    return MagicMock(side_effect=fake)


class _FrozenDate:
    """冻结 today = 2026-07-20，使增量窗口判定确定（缓存 last==today 即『最新』）。"""

    @staticmethod
    def today() -> _real_date:
        return _real_date(2026, 7, 20)


# ===========================================================================
# 用例A：_incremental_window 双向补（纯单元，直接调用）
# ===========================================================================
class TestIncrementalWindowBidirectional:
    """KlineCache._incremental_window 双向补逻辑回归。

    today 固定传 '20260720'，避免依赖真实当天日期导致判定漂移。
    """

    def test_no_cache_returns_genesis(self):
        # 无缓存 → 从 genesis 全量首拉
        res = KlineCache()._incremental_window(None, "20260720", "20190101")
        assert res == "20190101"

    def test_front_fill_when_first_after_default_beg(self):
        # 缓存首日 2022-07-06 晚于所需起点 2019-01-01 → 往前补（返回 default_beg）
        meta = {"beg": "20220706", "last": "20260720"}
        res = KlineCache()._incremental_window(meta, "20260720", "20190101")
        assert res == "20190101"

    def test_no_gap_returns_none(self):
        # 缓存首日 == 所需起点，且末日==today（已最新）→ 无需取数，返回 None
        meta = {"beg": "20220706", "last": "20260720"}
        res = KlineCache()._incremental_window(meta, "20260720", "20220706")
        assert res is None

    def test_front_fill_oldest_leftmost(self):
        # first=2022-07-06 > 2019-01-01（且 last=2025-01-01 < today）→ 往前补到最左起点
        meta = {"beg": "20220706", "last": "20250101"}
        res = KlineCache()._incremental_window(meta, "20260720", "20190101")
        assert res == "20190101"


# ===========================================================================
# 用例B：ensure_data(update) 往前补端到端（mock 网络）
# ===========================================================================
class TestEnsureDataFrontFill:
    """模拟『已更新股票 first=2022-07 但回测要 2019 起点』，验证 update 分支往前补。

    关键回归：旧实现仅往后补（last+1）或命中『已最新』跳过 → 永远补不到 warmup
    历史，死循环。修复后应：
      - 不抛异常；
      - 取数起点为回测所需起点 '20190101'（往前补，而非跳过/仅往后补）；
      - 写回的 meta.first 变为 '20190101'（实际补进了更早历史）。
    """

    def test_update_front_fills_daily_history(self, monkeypatch):
        out = Path(mkdtemp())
        captured: list = []
        fetch = _make_fetch_mock(captured)
        monkeypatch.setattr(provider_mod.KlineProvider, "_try_fetch", fetch)
        # 冻结 today = 2026-07-20
        monkeypatch.setattr(provider_mod, "date", _FrozenDate)
        # 避免真实随机 sleep 拖慢测试
        monkeypatch.setattr(
            "strategylab.engine.datasource.base.random_sleep",
            lambda *a, **k: None,
        )

        eff = provider_mod.get_provider()._factory.get_effective_source(SYMBOL)

        # 写日线缓存：已更新股票，first/last = 2022-07-06 ~ 2026-07-20（典型 bug 场景）
        daily_meta = {
            "beg": "20220706",
            "end": "20260720",
            "first": "20220706",
            "last": "20260720",
            "source": eff,
            "version": 2,
        }
        (out / f"{PREFIX}_{eff}_daily_meta.json").write_text(
            json.dumps(daily_meta, ensure_ascii=False), encoding="utf-8"
        )
        (out / f"{PREFIX}_{eff}_daily.csv").write_text(
            "date,open,high,low,close\n2022-07-06,10,12,9,11\n", encoding="utf-8"
        )

        # 写周线缓存：已最新（last==today）→ 应被跳过，聚焦于日线往前补回归
        weekly_meta = {
            "beg": "20171201",
            "end": "20260720",
            "first": "20171201",
            "last": "20260720",
            "source": eff,
            "version": 2,
        }
        (out / f"{PREFIX}_{eff}_weekly_meta.json").write_text(
            json.dumps(weekly_meta, ensure_ascii=False), encoding="utf-8"
        )
        (out / f"{PREFIX}_{eff}_weekly.csv").write_text(
            "date,open,high,low,close\n2017-12-01,10,12,9,11\n", encoding="utf-8"
        )

        # 模拟回测失败后调用的 update：日线要 2019 起点
        daily_csv, weekly_csv = data_feed.ensure_data(
            SYM,
            out,
            daily_beg="20190101",
            daily_end="20260720",
            weekly_beg="20171201",
            weekly_end="20260720",
            mode="update",
        )

        # 1) 不抛异常，返回路径存在
        assert daily_csv.exists(), "日线 CSV 应已落盘（往前补触发了 merge）"

        # 2) 关键回归：取数起点为回测所需起点 '20190101'（往前补，而非跳过/仅往后补）
        daily_calls = [c for c in captured if c[0] == "daily"]
        assert daily_calls, "日线应触发取数（不能被『已最新』误跳过）"
        assert daily_calls[0][1] == "20190101", (
            f"日线取数应往前补到 20190101，实际起点={daily_calls[0][1]}"
        )

        # 3) 写回的 meta.first 变为 '20190101'（真正补进了更早历史）
        merged_meta = json.loads(
            (out / f"{PREFIX}_{eff}_daily_meta.json").read_text(encoding="utf-8")
        )
        assert merged_meta["first"] == "20190101", (
            f"合并后 meta.first 应前移到 20190101，实际={merged_meta.get('first')}"
        )
        assert merged_meta["beg"] == "20190101"

        # 4) CSV 实际包含 2019 年数据（往前补生效）
        df = pd.read_csv(daily_csv)
        assert str(df["date"].min()).startswith("2019"), (
            f"日线 CSV 最早日应为 2019，实际={df['date'].min()}"
        )

        # 5) 周线已最新被跳过（未触发取数）
        weekly_calls = [c for c in captured if c[0] == "weekly"]
        assert not weekly_calls, "周线已最新应被跳过，不应触发取数"
        assert weekly_csv.exists()


# ===========================================================================
# 用例C：backtest.run_symbol 的 verify→update 契约（mock 网络 + 下游）
# ===========================================================================
class TestBacktestVerifyThenUpdate:
    """验证 backtest.run_symbol 的 try/except：verify 抛 DataMissingError 后自动走
    update 补足再继续回测（而非直接失败/死循环）。

    桩：ensure_data 在 mode='verify' 抛 DataMissingError、mode='update' 返回桩路径；
    load_bars / 策略 run / export_results / save_run 全部桩化，避免联网与落库。
    """

    def test_verify_failure_triggers_auto_update(self, monkeypatch):
        calls: list = []

        def fake_ensure_data(sym_cfg, out_dir, **kwargs):
            mode = kwargs.get("mode")
            calls.append(mode)
            if mode == "verify":
                raise DataMissingError(
                    sym_cfg["symbol"], "行情缺失/不足，请先点『更新数据源』"
                )
            # update 成功：返回桩路径
            return (Path(out_dir) / "daily.csv", Path(out_dir) / "weekly.csv")

        monkeypatch.setattr(data_feed, "ensure_data", fake_ensure_data)

        dummy_bars = pd.DataFrame(
            {
                "date": ["2023-01-01", "2023-06-01", "2024-01-01"],
                "open": [10.0, 11.0, 12.0],
                "high": [12.0, 13.0, 14.0],
                "low": [9.0, 10.0, 11.0],
                "close": [11.0, 12.0, 13.0],
            }
        )

        def fake_load_bars(csv_path):
            return dummy_bars

        monkeypatch.setattr(data_feed, "load_bars", fake_load_bars)

        # 桩化策略类
        class FakeStrategy:
            def __init__(self, cfg):
                self.cfg = cfg

            def run(self, daily, weekly, start, end, symbol=None, symbol_name=None):
                return {
                    "equity_curve": [],
                    "trade_history": [],
                    "positions": [],
                }

        monkeypatch.setattr(
            "strategylab.engine.backtest.get_strategy_class",
            lambda type_name: FakeStrategy,
        )

        # 桩化 export_results 与落库（避免真实网络/DB 依赖）
        monkeypatch.setattr(
            "strategylab.engine.vendor.export_results.export_results",
            lambda **kwargs: {"summary_data": {}, "meta": {}},
        )
        monkeypatch.setattr(
            "strategylab.engine.storage.repository.save_run",
            lambda *a, **k: "fake-run-id",
        )
        monkeypatch.setattr(
            "strategylab.engine.storage.repository.compute_params_hash",
            lambda *a, **k: "fake-hash",
        )

        out = Path(mkdtemp())
        strategy_cfg = {
            "type": "kdj_macd_dual_entry",
            "name": "回归测试策略",
            "params": {"initial_cash": 1000000.0},
        }

        # 不应抛异常；verify 失败后应自动走 update 继续回测
        result = None
        try:
            result = __import__(
                "strategylab.engine.backtest", fromlist=["run_symbol"]
            ).run_symbol(
                strategy_cfg,
                "600216.SH",
                None,
                "2023-01-01",
                "2024-01-01",
                out,
            )
        except Exception as exc:  # noqa: BLE001
            raise AssertionError(f"run_symbol 应自动补足后继续，但不应抛异常：{exc}") from exc

        # 调用顺序应为 [verify, update]，证明 except 分支触发了自动 update
        assert calls == ["verify", "update"], (
            f"应确保先 verify 失败再自动 update，实际调用序列={calls}"
        )
        assert result is not None
        assert result.get("run_id") == "fake-run-id"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
