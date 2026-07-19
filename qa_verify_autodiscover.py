# -*- coding: utf-8 -*-
"""验证 M2 自动发现：不依赖任何手写 dict，扫描 engine/strategies/*.py 收集 BaseStrategy 子类。

- list_strategies() 仍返回含 turtle / kdj_macd_dual_entry；
- get_strategy_class("turtle") 能取到类；
- 手工 dict 已删除（STRATEGY_REGISTRY 由 discover_strategies() 构建）。
"""
import os
import sys

os.chdir(r"E:\量化交易\strategy_lab")
sys.path.insert(0, r"E:\量化交易\strategy_lab\src")
os.environ.setdefault(
    "DATABASE_URL",
    "mysql+pymysql://root:root@localhost:3306/strategylab?charset=utf8mb4",
)

from strategylab.engine.strategies import (  # noqa: E402
    discover_strategies,
    get_strategy_class,
    list_strategies,
)

reg = discover_strategies()
print("discovered types:", sorted(reg))
assert "turtle" in reg, "自动发现缺失 turtle"
assert "kdj_macd_dual_entry" in reg, "自动发现缺失 kdj_macd_dual_entry"

ls = list_strategies()
print("list_strategies():", ls)
assert ls == sorted(reg), "list_strategies 与自动发现结果不一致"

turtle_cls = get_strategy_class("turtle")
print("get_strategy_class('turtle') ->", turtle_cls.__name__)
assert turtle_cls.__name__ == "TurtleStrategy", "turtle 类型映射错误"

kdj_cls = get_strategy_class("kdj_macd_dual_entry")
print("get_strategy_class('kdj_macd_dual_entry') ->", kdj_cls.__name__)
assert kdj_cls.__name__ == "KdjMacdDualEntry", "kdj 类型映射错误"

print("AUTODISCOVER_PASS")
