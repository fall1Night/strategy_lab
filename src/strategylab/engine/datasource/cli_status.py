# -*- coding: utf-8 -*-
"""CLI ``datasource status`` 命令实现。

输出格式（按 PRD 定义）：
  当前默认数据源 : eastmoney
  按品种覆盖     : 600216.SH -> tushare
  备用源顺序     : [akshare, tushare]

  东财(eastmoney): 健康度 正常 | 最近切换 无
  akshare       : 健康度 正常 | 最近切换 无
  tushare       : 健康度 正常 | 最近切换 无
"""

from __future__ import annotations

from .config import DataSourceConfig
from .factory import DataSourceFactory
from .exceptions import MissingDependencyError
from .switch_log import SwitchLog


def cmd_status() -> str:
    """生成 ``datasource status`` 的完整输出文本。

    Returns:
        格式化的 status 文本（可直接 print）。
    """
    cfg = DataSourceConfig.from_env()
    factory = DataSourceFactory(cfg)

    lines: list[str] = []

    # ---- 基础配置 ----
    lines.append(f"当前默认数据源 : {cfg.default_source}")

    if cfg.symbol_overrides:
        overrides = ", ".join(f"{k} -> {v}" for k, v in cfg.symbol_overrides.items())
        lines.append(f"按品种覆盖     : {overrides}")
    else:
        lines.append("按品种覆盖     : (无)")

    if cfg.fallback_order:
        lines.append(f"备用源顺序     : {cfg.fallback_order}")
        lines.append(f"自动故障转移   : {'启用' if cfg.failover_enabled else '关闭'}")
    else:
        lines.append("备用源顺序     : (无)")

    lines.append(f"熔断总开关     : {'开' if cfg.cb_enabled else '关'}")
    lines.append("")

    # ---- 各源健康度 ----
    from ...settings import get_data_dir

    slog = SwitchLog(file_path=get_data_dir() / "datasource_switch_log.jsonl")

    for name in ("eastmoney", "akshare", "tushare", "broker"):
        try:
            ds = factory.get(name, cfg)
        except (MissingDependencyError, ValueError):
            # 不可用
            display = _source_display(name)
            lines.append(f"{display}: 健康度 不可用 (依赖缺失)")
            continue

        hs = ds.health()
        status_map = {
            "ok": "正常",
            "degraded": "降级",
            "open": "熔断中",
            "unavailable": "不可用",
        }
        status_cn = status_map.get(hs.status, hs.status)

        # 最近切换
        recent_events = slog.recent(n=50)
        switches_for_source = [
            e for e in recent_events
            if e.from_source == name or e.to_source == name
        ]
        if switches_for_source:
            latest = switches_for_source[0]
            import time as _time

            iso = _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime(latest.timestamp))
            switch_info = f"{latest.from_source}→{latest.to_source} ({iso})"
        else:
            switch_info = "无"

        display = _source_display(name)
        lines.append(f"{display}: 健康度 {status_cn} | 最近切换 {switch_info}")

    return "\n".join(lines)


def _source_display(name: str) -> str:
    """数据源展示名。"""
    display_map = {
        "eastmoney": "东财(eastmoney)",
        "akshare": "akshare       ",
        "tushare": "tushare       ",
        "broker": "broker(券商)   ",
    }
    return display_map.get(name, f"{name}")
