# -*- coding: utf-8 -*-
"""回归护栏：批次完成态 UI 修复（前端 JS，位于 /production 路由）。

覆盖工程师落地的三处修复（详见 web.py 的 beginBatch / poll 函数体）：
- A. 终态（done/cancelled/interrupted）隐藏「取消批次」按钮：
      cbtn.style.display='none'
- B. 新批次恢复按钮可见可用：
      cbtn.style.display=''
- C. 完成态视觉强化：进度条置绿 #2e7d32：
      background='#2e7d32'（prog-bar 元素）

本测试为「最便宜的静态护栏」：直接读取 web.py 源码文本，断言关键字符串存在，
不依赖数据库 / 板块缓存 / 运行中的服务。若未来有人误删这些分支，本测试会立即报警。
"""
from __future__ import annotations

from pathlib import Path

# conftest.py 已把 src/ 加入 sys.path；这里用绝对路径读取 web.py 源码做静态校验
_SRC = Path(__file__).resolve().parents[1] / "src" / "strategylab" / "web.py"


def _web_source() -> str:
    assert _SRC.exists(), f"找不到 web.py: {_SRC}"
    return _SRC.read_text(encoding="utf-8")


def test_cancel_button_hidden_on_terminal_state():
    """A 修复：poll() 终态分支应隐藏取消按钮。"""
    src = _web_source()
    assert "cbtn.style.display='none'" in src, (
        "A 修复缺失：缺少终态隐藏「取消批次」按钮的逻辑 "
        "(期望出现 cbtn.style.display='none')"
    )


def test_cancel_button_restored_on_new_batch():
    """B 修复：beginBatch() 新批次应恢复取消按钮。"""
    src = _web_source()
    assert "cbtn.style.display=''" in src, (
        "B 修复缺失：缺少新批次恢复「取消批次」按钮可见性的逻辑 "
        "(期望出现 cbtn.style.display='')"
    )


def test_progress_bar_turns_green_on_complete():
    """C 修复：完成态进度条应置绿 #2e7d32。"""
    src = _web_source()
    assert "prog-bar" in src, "缺少 prog-bar 进度条元素"
    assert "#2e7d32" in src, (
        "C 修复缺失：完成态进度条未设置绿色 #2e7d32 "
        "(期望出现 background='#2e7d32' 相关逻辑)"
    )


def test_production_route_carries_cancel_button_label():
    """基础健全性：/production 模板应携带「取消批次」按钮文案。"""
    src = _web_source()
    assert "取消批次" in src, "/production 模板应含「取消批次」按钮"
