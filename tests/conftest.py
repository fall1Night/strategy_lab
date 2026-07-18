# -*- coding: utf-8 -*-
"""pytest 配置：把 src/ 加入 sys.path，便于不装包也能跑测试。"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
