# -*- coding: utf-8 -*-
"""财报披露延迟（Point-in-Time，防未来函数）。

移植自 deepseek-harness-quant factors/fundamental.py 的 DISCLOSE_LAG 方法论。

背景：财报数据在报告期末后并不立即可用，存在法定披露延迟。直接用"报告期"
作为因子可用日会引入 look-ahead（未来函数）偏差——回测在报表实际披露前就
使用了该报表数据。PIT 近似：报告期 + 标准披露延迟才可用。

披露延迟（保守取月末）：
  一季报 04-30 ｜ 中报 08-31 ｜ 三季报 10-31 ｜ 年报 次年 04-30
"""
from __future__ import annotations

# 报告月 → 可用日（MM-DD）
DISCLOSE_LAG = {
    3: "04-30",
    6: "08-31",
    9: "10-31",
    12: "04-30",
}


def available_date(period: str) -> str:
    """报告期 → 数据可用日（PIT 近似）。

    period: 'YYYY-MM-DD'（报告期末，如 '2024-06-30'）
    返回: 'YYYY-MM-DD' 数据可用日；解析失败原样返回。
    """
    try:
        y, m = int(period[:4]), int(period[5:7])
        if m == 12:
            return f"{y + 1}-{DISCLOSE_LAG[m]}"
        return f"{y}-{DISCLOSE_LAG[m]}"
    except (ValueError, IndexError):
        return period


if __name__ == "__main__":
    for p in ["2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31", "bad"]:
        print(f"{p} → 可用日 {available_date(p)}")
