# -*- coding: utf-8 -*-
"""A 股交易日历工具（从 OSkhQuant khQTTools 抽离，去除 PyQt5 / xtquant 依赖）。

提供：
  - ``is_trade_time()``          ：当前是否处于连续交易时段
  - ``is_trade_day(date_str)``   ：某日是否为 A 股交易日（排除周末 + 法定节假日）
  - ``get_trade_days_count(...)``：区间内交易日天数

节假日判断策略（零额外依赖为默认）：
  1. 若环境已安装 ``holidays`` 库，则使用 ``holidays.China()``（最准确、随库更新）；
  2. 否则退化为「仅排除周末」，并在首次调用时打印一次提示。
两种模式下周末永远排除，因此回测取到的日线（本就是交易日序列）不受影响；
差异仅体现在「区间交易日数 / 年化分母」等统计口径上。
"""
from __future__ import annotations

import datetime
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

# A 股连续交易时段：上午 09:30:00-11:30:00，下午 13:00:00-15:00:00
_TRADING_PERIODS = [("093000", "113000"), ("130000", "150000")]

# 懒加载的节假日集合（None 表示尚未初始化）
_HOLIDAYS: Optional[set] = None
_HOLIDAYS_SOURCE: Optional[str] = None  # "holidays" / "weekend-only"


def _ensure_holidays() -> tuple[set, str]:
    """返回 (法定节假日 set, 来源标识)，并缓存。"""
    global _HOLIDAYS, _HOLIDAYS_SOURCE
    if _HOLIDAYS is not None:
        return _HOLIDAYS, _HOLIDAYS_SOURCE  # type: ignore[return-value]

    try:  # pragma: no cover - 依赖环境
        import holidays  # type: ignore

        # 中国法定节假日（含调休补班日会被 holidays 库正确处理为交易日）
        cn = holidays.China()
        # holidays 库把「补班工作日」也标记为 holiday=False，但 is_trade_day 已先排除周末，
        # 这里收集「真正的法定休市日」用于排除。holidays.China() 的 keys() 即休市日。
        _HOLIDAYS = set(cn.keys())
        _HOLIDAYS_SOURCE = "holidays"
        logger.info("交易日历：使用 holidays 库 (holidays.China)，共 %d 个休市日", len(_HOLIDAYS))
    except Exception:  # 未安装 holidays → 仅排除周末
        _HOLIDAYS = set()
        _HOLIDAYS_SOURCE = "weekend-only"
        logger.warning(
            "交易日历：未检测到 holidays 库，节假日将「仅排除周末」（非交易日统计可能有偏差）。"
            "如需精确法定节假日，请 `pip install holidays`。"
        )
    return _HOLIDAYS, _HOLIDAYS_SOURCE


def _parse_date(date_str: str) -> Optional[datetime.date]:
    """容错解析日期，支持 YYYY-MM-DD / YYYYMMDD / YYYY/MM/DD。失败返回 None。"""
    date_str = (date_str or "").strip()
    if not date_str:
        return None
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
        try:
            return datetime.datetime.strptime(date_str, fmt).date()
        except ValueError:
            continue
    return None


def is_trade_time() -> bool:
    """判断当前系统时间是否处于 A 股连续交易时段。"""
    now = time.strftime("%H%M%S")
    return any(start <= now <= end for start, end in _TRADING_PERIODS)


def is_trade_day(date_str: Optional[str] = None) -> bool:
    """判断某日是否为 A 股交易日。

    Args:
        date_str: ``"YYYY-MM-DD"`` / ``"YYYYMMDD"`` / ``None``(默认今天)。
    Returns:
        工作日且非法定节假日 → True。
    """
    if date_str is None:
        d = datetime.date.today()
    else:
        d = _parse_date(date_str)
        if d is None:
            logger.warning("无法解析日期: %s，默认按交易日处理", date_str)
            return True

    # 周末（5=周六, 6=周日）直接排除
    if d.weekday() >= 5:
        return False

    holidays, _ = _ensure_holidays()
    if d in holidays:
        return False
    return True


def get_trade_days_count(start_date: str, end_date: str) -> int:
    """统计 ``[start_date, end_date]``（含端点）内的 A 股交易日天数。

    Args:
        start_date / end_date: ``"YYYY-MM-DD"`` 格式。
    """
    try:
        start = datetime.datetime.strptime(start_date, "%Y-%m-%d").date()
        end = datetime.datetime.strptime(end_date, "%Y-%m-%d").date()
    except ValueError:
        logger.error("交易日计数：日期格式应为 YYYY-MM-DD，收到 %s / %s", start_date, end_date)
        return 0
    if start > end:
        logger.error("交易日计数：起始日 %s 晚于结束日 %s", start_date, end_date)
        return 0

    count = 0
    cur = start
    one_day = datetime.timedelta(days=1)
    while cur <= end:
        if is_trade_day(cur.strftime("%Y-%m-%d")):
            count += 1
        cur += one_day
    return count
