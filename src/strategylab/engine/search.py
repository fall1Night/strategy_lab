# -*- coding: utf-8 -*-
"""A 股全市场标的实时模糊搜索（联网，东方财富 clist 接口）。

- 首次搜索时联网拉取全 A股（沪A/科创板/深A/创业板/北交所）代码+名称列表并缓存 10 分钟，
  之后在本地按「名称/代码包含关键词」过滤，输入即出、无需每次联网；缓存过期后自动刷新。
- 返回规范代码（如 600216.SH），可直接喂给回测引擎的 normalize_symbol。
"""
from __future__ import annotations

import json
import time
import urllib.request
from typing import Dict, List

# 东方财富「数据中心」全量 A股列表接口（本环境 push2.eastmoney.com 被出网拦截，
# 故改用 datacenter-web.eastmoney.com，经验证可达）。
_DATA_API = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_TTL = 600  # 全量列表缓存时长（秒）

_cache: Dict[str, object] = {"ts": 0.0, "items": None}


def _get_json(url: str, headers: dict, retries: int = 2):
    last: Exception | None = None
    for _ in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.5)
    raise last or RuntimeError("search fetch failed")


def _suffix_of(code: str) -> str | None:
    if len(code) != 6:
        return None
    p = code[0]
    if p == "6":
        return "SH"
    if p in ("0", "3"):
        return "SZ"
    if p in ("8", "4"):
        return "BJ"
    return None  # 排除 B股(2/9 开头)等


def _fetch_all() -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Referer": "https://data.eastmoney.com/",
    }
    pn = 1
    while pn <= 30:
        url = (
            f"{_DATA_API}?reportName=RPT_DMSK_TS_STOCKNEW"
            f"&columns=SECURITY_CODE,SECURITY_NAME_ABBR,TRADE_MARKET_CODE"
            f"&pageSize=500&pageNumber={pn}&sortColumns=SECURITY_CODE&sortTypes=1"
            f"&source=HSF10&client=PC"
        )
        d = _get_json(url, headers)
        rows = (d.get("result") or {}).get("data") or []
        for it in rows:
            code = it.get("SECURITY_CODE")
            name = it.get("SECURITY_NAME_ABBR")
            if not code or not name:
                continue
            suf = _suffix_of(str(code))
            if not suf:
                continue
            items.append({"code": str(code), "name": str(name), "symbol": f"{code}.{suf}"})
        total = (d.get("result") or {}).get("count") or 0
        if not rows or (total and len(items) >= total):
            break
        pn += 1
    return items


def _get_universe() -> List[Dict[str, str]]:
    now = time.time()
    if _cache["items"] is not None and now - float(_cache["ts"]) < _TTL:  # type: ignore[operator]
        return _cache["items"]  # type: ignore[return-value]
    items = _fetch_all()
    _cache["items"] = items
    _cache["ts"] = now
    return items


def search_a_stocks(keyword: str, limit: int = 30) -> List[Dict[str, str]]:
    """按名称或代码模糊搜索 A股标的，返回 [{code, name, symbol}, ...]。"""
    kw = (keyword or "").strip().lower()
    if not kw:
        return []
    uni = _get_universe()
    name_hits: List[Dict[str, str]] = []
    code_hits: List[Dict[str, str]] = []
    for it in uni:
        if kw in it["name"].lower():
            name_hits.append(it)
        elif kw in it["code"].lower():
            code_hits.append(it)
    return (name_hits + code_hits)[:limit]
