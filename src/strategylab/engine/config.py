# -*- coding: utf-8 -*-
"""策略配置加载器。

支持 .toml（推荐，tomllib 为 Python 3.11+ 标准库，可写注释）与 .json 两种格式。
一个策略 = 一个配置文件。新增策略 = 复制一份 .toml 改 [params] 即可，无需改代码。
"""
from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import Any

from ..settings import get_strategies_dir


def load_strategy(path: str | Path) -> dict[str, Any]:
    """加载策略配置，返回 dict（含 name/type/market/description/params）。"""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"策略文件不存在: {p}")
    text = p.read_text(encoding="utf-8")
    suffix = p.suffix.lower()
    if suffix == ".toml":
        try:
            import tomllib
        except ModuleNotFoundError:  # pragma: no cover
            raise RuntimeError(
                "当前 Python 缺少 tomllib（需 3.11+），请用 .json 配置或升级 Python。"
            )
        return tomllib.loads(text)
    if suffix == ".json":
        return json.loads(text)
    raise ValueError(f"不支持的策略配置格式: {suffix}（仅支持 .toml / .json）")


def load_strategy_by_arg(arg: str | Path) -> dict[str, Any]:
    """按参数加载策略配置。

    解析顺序：
      1. ``arg`` 是已存在的文件路径 → 直接 ``load_strategy``；
      2. 否则当作别名，先查 ``STRATEGALAB_STRATEGIES_DIR`` 环境变量目录下的
         ``<alias>.toml`` / ``<alias>.json``；
      3. 再查包内置资源 ``strategylab.resources.strategies`` 下的
         ``<alias>.toml`` / ``<alias>.json``（用 ``importlib.resources`` 读取）。
    都找不到 → ``FileNotFoundError``。
    """
    p = Path(arg)
    # 1. 直接文件路径
    if p.exists():
        return load_strategy(p)

    stem = p.stem if p.suffix.lower() in (".toml", ".json") else str(arg)

    # 2. 用户策略目录（STRATEGALAB_STRATEGIES_DIR）
    user_dir = get_strategies_dir()
    if user_dir is not None:
        for ext in (".toml", ".json"):
            cand = user_dir / f"{stem}{ext}"
            if cand.exists():
                return load_strategy(cand)

    # 3. 包内置资源
    for ext in (".toml", ".json"):
        try:
            text = (
                resources.files("strategylab.resources.strategies")
                .joinpath(f"{stem}{ext}")
                .read_text(encoding="utf-8")
            )
        except FileNotFoundError:
            continue
        if ext == ".toml":
            import tomllib

            return tomllib.loads(text)
        return json.loads(text)

    raise FileNotFoundError(
        f"找不到策略配置: {arg}（既不是文件路径，也未在内置策略 / "
        f"STRATEGALAB_STRATEGIES_DIR 下找到 {stem}.toml / {stem}.json）。"
    )


def list_available_strategies() -> list[tuple[str, str]]:
    """返回 [(展示名, 别名), ...]，扫描「内置资源 + 用户策略目录」。"""
    found: dict[str, str] = {}

    import tomllib

    # 内置资源 — 优先 importlib.resources，失败时回退到文件系统路径
    strat_dir = None
    try:
        strat_dir = resources.files("strategylab.resources.strategies")
        list(strat_dir.iterdir())  # 先试一次，确认可访问
    except (ModuleNotFoundError, FileNotFoundError, TypeError):
        strat_dir = None

    if strat_dir is not None:
        for entry in strat_dir.iterdir():
            if entry.name.endswith((".toml", ".json")):
                try:
                    text = entry.read_text(encoding="utf-8")
                    if entry.name.endswith(".toml"):
                        cfg = tomllib.loads(text)
                    else:
                        cfg = json.loads(text)
                    found[cfg.get("name", entry.stem)] = entry.stem
                except Exception:
                    found[entry.stem] = entry.stem
    else:
        # 文件系统回退：直接从 resources/strategies/ 读取
        _builtin = Path(__file__).resolve().parent.parent / "resources" / "strategies"
        if _builtin.is_dir():
            for p in sorted(_builtin.glob("*")):
                if p.suffix in (".toml", ".json"):
                    try:
                        if p.suffix == ".toml":
                            cfg = tomllib.loads(p.read_text(encoding="utf-8"))
                        else:
                            cfg = json.loads(p.read_text(encoding="utf-8"))
                        found[cfg.get("name", p.stem)] = p.stem
                    except Exception:
                        found[p.stem] = p.stem

    # 用户策略目录
    user_dir = get_strategies_dir()
    if user_dir is not None and user_dir.exists():
        for p in sorted(user_dir.glob("*.toml")) + sorted(user_dir.glob("*.json")):
            try:
                cfg = load_strategy(p)
                found[cfg.get("name", p.stem)] = p.stem
            except Exception:
                found[p.stem] = p.stem

    if not found:
        found["周线MACD + 日线KDJ 双入口做T"] = "kdj_macd_dual_entry"

    return sorted(found.items())
