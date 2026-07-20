# -*- coding: utf-8 -*-
"""板块缓存失效回归测试（验证 mtime 失效缓存 bug 修复）。

被测变更（repository.py）：
  ``_load_sectors`` / ``_load_sector_stocks`` 原「首次调用永久缓存、永不过期」，
  文件不存在时缓存成 ``[]`` / ``{}`` 后永远返回空；现改为按文件 mtime 失效，
  缓存形状 ``tuple[float | None, data]``，全局变量
  ``_SECTORS_CACHE`` / ``_SECTOR_STOCKS_CACHE``。

覆盖三种回归场景：
  1) 陈旧缓存自愈（核心）：内存中曾缓存空值，修复后应重新读取真实文件。
  2) 文件更新后重读：mtime 变化触发重读，而非停留在旧缓存。
  3) 全市场展开不受影响（端到端）：poison 缓存后 ``_expand_all_market`` 仍非空
     且等于真实成分股总数（复现「全市场没有可更新的标的」不再出现）。

隔离方式：每个用例用 ``STRATEGALAB_DATA_DIR`` 指向独立目录（或真实 data/），
并用 autouse fixture 重置两个全局缓存，finally 还原环境变量与临时目录。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from strategylab.engine.storage import repository as repository
from strategylab.web import _expand_all_market

# 项目根下的真实数据包目录（场景 1 / 3 以此为真相源）
REAL_DATA_DIR = Path(__file__).resolve().parents[1] / "data"
REAL_SECTOR_STOCKS = REAL_DATA_DIR / "sector_stocks.json"


@pytest.fixture(autouse=True)
def _reset_repo_caches():
    """每个用例前后重置板块缓存全局变量，保证相互隔离（沿用 qa_helpers 约定）。"""
    repository._SECTORS_CACHE = None
    repository._SECTOR_STOCKS_CACHE = None
    yield
    repository._SECTORS_CACHE = None
    repository._SECTOR_STOCKS_CACHE = None


def _set_data_dir(tmp: str) -> str | None:
    """设置 STRATEGALAB_DATA_DIR，返回旧的 env 值以便 finally 还原。"""
    prev = os.environ.get("STRATEGALAB_DATA_DIR")
    os.environ["STRATEGALAB_DATA_DIR"] = tmp
    return prev


def _restore_data_dir(prev: str | None) -> None:
    if prev is None:
        os.environ.pop("STRATEGALAB_DATA_DIR", None)
    else:
        os.environ["STRATEGALAB_DATA_DIR"] = prev


def _write_sector_stocks(path: Path, data: dict, mtime: float) -> None:
    """写入 sector_stocks.json 并强制设置 mtime，避免快速写盘的时钟分辨率问题。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.utime(path, (mtime, mtime))


def _count_stocks(data: dict) -> int:
    """成分股总数（按板块累加长度，不去重）。对应 get_sector_stocks 的返回值形态。"""
    return sum(len(v) for v in data.values())


def _count_unique_codes(data: dict) -> int:
    """去重后的成分股数（_expand_all_market 按 code 去重）。"""
    return len({st["code"] for v in data.values() for st in v})


def test_stale_empty_cache_self_heals_from_real_file():
    """核心回归：服务早期把空结果焊死在内存里，修复后应重新读取真实文件。

    模拟：直接给 repository._SECTOR_STOCKS_CACHE 赋一个伪造的「旧 mtime + 空 dict」。
    调用 repository.get_sector_stocks() 应返回**非空** dict，且成分股总数等于
    真实 data/sector_stocks.json 里的数量。
    """
    prev_env = _set_data_dir(str(REAL_DATA_DIR))
    try:
        assert REAL_SECTOR_STOCKS.exists(), f"真实数据文件缺失: {REAL_SECTOR_STOCKS}"

        # 动态计算真实文件里的成分股总数（避免硬编码 1301 失去敏感性）
        real_data = json.loads(REAL_SECTOR_STOCKS.read_text(encoding="utf-8"))
        expected = _count_stocks(real_data)

        # 模拟「服务在文件生成前启动并首次触达缓存 → 内存被焊死为 {}」
        stale_mtime = 0.0  # 陈旧 mtime，必然与真实文件 mtime 不一致
        poisoned = {}
        repository._SECTOR_STOCKS_CACHE = (stale_mtime, poisoned)

        result = repository.get_sector_stocks()

        # 1) 返回非空，且不是被缓存的那个空 dict（证明发生了真实重读）
        assert result is not poisoned, "修复后应重新读取文件，而非返回被缓存的空 dict"
        assert result != {}, "修复后不应再返回被焊死的空 dict"
        assert isinstance(result, dict) and len(result) > 0

        # 2) 成分股总数等于真实文件
        assert _count_stocks(result) == expected

        # 3) 缓存已被真实文件 mtime 刷新（而非停留在 0.0）
        assert repository._SECTOR_STOCKS_CACHE is not None
        real_mtime = REAL_SECTOR_STOCKS.stat().st_mtime
        assert repository._SECTOR_STOCKS_CACHE[0] == real_mtime
    finally:
        _restore_data_dir(prev_env)


def test_file_update_triggers_reread_via_mtime():
    """文件更新（mtime 变化）后再次调用应重读新内容，而非停留在旧缓存。"""
    tmp = tempfile.mkdtemp(prefix="sector_cache_")
    prev_env = _set_data_dir(tmp)
    try:
        path = Path(tmp) / "sector_stocks.json"

        # 第一次内容：1 个板块、3 只标的
        content1 = {
            "secA": [
                {"code": "sz000001", "name": "平安银行"},
                {"code": "sz000002", "name": "万科A"},
                {"code": "sz000003", "name": "PT金田"},
            ],
        }
        mtime1 = 1_700_000_000.0
        _write_sector_stocks(path, content1, mtime1)

        # 首次调用 → 命中并缓存（mtime1）
        r1 = repository.get_sector_stocks()
        assert _count_stocks(r1) == 3
        assert repository._SECTOR_STOCKS_CACHE is not None
        assert repository._SECTOR_STOCKS_CACHE[0] == mtime1, "首次调用应按文件 mtime 缓存"

        # 用更多标的覆盖写该文件，并刷新 mtime（强制 +100s，确定性）
        content2 = {
            "secA": [
                {"code": "sz000001", "name": "平安银行"},
                {"code": "sz000002", "name": "万科A"},
                {"code": "sz000004", "name": "国华网安"},
                {"code": "sz000005", "name": "ST星源"},
                {"code": "sz000006", "name": "深振业A"},
            ],
            "secB": [
                {"code": "sh600000", "name": "浦发银行"},
                {"code": "sh600001", "name": "邯郸钢铁"},
            ],
        }
        mtime2 = mtime1 + 100.0
        _write_sector_stocks(path, content2, mtime2)

        # 第二次调用：mtime 变化应触发重读，得到新内容（7 只）
        r2 = repository.get_sector_stocks()
        assert _count_stocks(r2) == 7, "mtime 变化后应重读新文件（7 只标的）"
        assert r2 != r1, "第二次结果应反映更新后的文件，而非旧缓存"
        assert repository._SECTOR_STOCKS_CACHE[0] == mtime2
    finally:
        _restore_data_dir(prev_env)
        shutil.rmtree(tmp, ignore_errors=True)


def test_expand_all_market_not_affected_by_stale_cache():
    """端到端：即便内存里曾缓存空值，全市场展开也应正常返回真实成分股。

    直接复现用户看到的『全市场没有可更新的标的』：poison 缓存后调用
    strategylab.web._expand_all_market()，断言返回列表长度 > 0 且等于
    临时文件中成分股总数。
    """
    tmp = tempfile.mkdtemp(prefix="sector_expand_")
    prev_env = _set_data_dir(tmp)
    try:
        # 临时数据目录放入与真实结构一致的 sector_stocks.json
        tmp_file = Path(tmp) / "sector_stocks.json"
        shutil.copyfile(REAL_SECTOR_STOCKS, tmp_file)

        real_data = json.loads(REAL_SECTOR_STOCKS.read_text(encoding="utf-8"))
        expected = _count_unique_codes(real_data)  # _expand_all_market 会去重
        assert expected > 0

        # 复现 bug：服务早期把空结果缓存住
        repository._SECTOR_STOCKS_CACHE = (0.0, {})

        items = _expand_all_market()

        # 1) 全市场展开非空（不再误报『没有可更新的标的』）
        assert isinstance(items, list)
        assert len(items) > 0, "修复后全市场展开不应为空"

        # 2) 展开数量等于临时文件中的成分股总数（去重后）
        assert len(items) == expected, (
            f"全市场展开数量应等于成分股总数 {expected}，实际 {len(items)}"
        )

        # 3) 每项结构正确
        for it in items:
            assert {"symbol", "symbol_name", "sector_code"} <= set(it.keys())
    finally:
        _restore_data_dir(prev_env)
        shutil.rmtree(tmp, ignore_errors=True)
