# -*- coding: utf-8 -*-
"""回归测试：POST body 解析（multipart/form-data 与 urlencoded 兼容）。

背景 bug：原后端 5 处只调用 ``urllib.parse.parse_qs`` 解析 urlencoded，
前端用 ``FormData``（multipart/form-data）发送 → ``scope_type`` 读不到 →
``{"error":"没有可更新的标的（请检查范围选择）"}``。

修复：``strategylab.web.Handler`` 新增 ``_parse_post_body`` / ``_parse_multipart``，
统一兼容两种编码，返回结构与 ``parse_qs`` 一致（``dict[str, list[str]]``）。

本测试直接对解析逻辑做确定性单元测试（构造真实 Handler 实例并注入最小
headers/rfile 替身，**不依赖起服务**，最快最稳）。
"""
from __future__ import annotations

import io

from strategylab.web import Handler


class _Headers:
    """最小 headers 替身：仅供 .get(name, default) 使用。"""

    def __init__(self, mapping: dict):
        self._m = dict(mapping)

    def get(self, name, default=None):
        return self._m.get(name, default)


class _Rfile:
    """最小 rfile 替身：仅供 .read(n) 使用。"""

    def __init__(self, data: bytes):
        self._bio = io.BytesIO(data)

    def read(self, n=-1):
        return self._bio.read(n)


def _make_handler(body: bytes, content_type: str, content_length: int | None = None):
    """构造一个真实 ``Handler`` 实例（绕过 BaseHTTPRequestHandler.__init__），
    注入最小 headers/rfile，仅用于测试解析方法。"""
    h = Handler.__new__(Handler)
    h.headers = _Headers(
        {
            "Content-Type": content_type,
            "Content-Length": str(
                content_length if content_length is not None else len(body)
            ),
        }
    )
    h.rfile = _Rfile(body)
    return h


def _build_multipart(fields: dict, boundary: str = "----qaBoundaryXYZ", files=None):
    """手工构造 multipart/form-data 原始 body。

    fields: ``{name: value}`` 文本字段。
    files:  可选 ``[{"filename": "x.txt", "content": b"..."}]`` 仅含 filename 的域。
    """
    parts = []
    for name, value in fields.items():
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        )
    for f in files or []:
        fname = f.get("filename")
        content = f.get("content", b"")
        if isinstance(content, str):
            content = content.encode("utf-8")
        disp = "Content-Disposition: form-data"
        if fname:
            disp += f'; filename="{fname}"'
        parts.append(
            f"--{boundary}\r\n"
            f"{disp}\r\n\r\n"
            f"{content.decode('utf-8', 'replace')}\r\n"
        )
    body = "".join(parts) + f"--{boundary}--\r\n"
    return body.encode("utf-8"), f"multipart/form-data; boundary={boundary}"


# ---------------------------------------------------------------------------
# ① multipart 含 scope_type/scope_value/symbols → 解析正确（修复核心）
# ---------------------------------------------------------------------------
def test_multipart_parse_all_fields():
    body, ctype = _build_multipart(
        {
            "scope_type": "all_market",
            "scope_value": "ALL",
            "symbols": "600000.SH,000001.SZ",
        }
    )
    h = _make_handler(body, ctype)
    data = h._parse_post_body()
    assert data == {
        "scope_type": ["all_market"],
        "scope_value": ["ALL"],
        "symbols": ["600000.SH,000001.SZ"],
    }, f"multipart 解析结果不符: {data}"


# ---------------------------------------------------------------------------
# ② urlencoded 同样正确（旧路径未被破坏）
# ---------------------------------------------------------------------------
def test_urlencoded_parse():
    body = b"scope_type=all_market&scope_value=ALL&symbols=600000.SH,000001.SZ"
    h = _make_handler(body, "application/x-www-form-urlencoded")
    data = h._parse_post_body()
    assert data == {
        "scope_type": ["all_market"],
        "scope_value": ["ALL"],
        "symbols": ["600000.SH,000001.SZ"],
    }, f"urlencoded 解析结果不符: {data}"


# ---------------------------------------------------------------------------
# ③ 空 body 返回 {}
# ---------------------------------------------------------------------------
def test_empty_body_returns_empty_dict():
    h = _make_handler(b"", "application/x-www-form-urlencoded", content_length=0)
    assert h._parse_post_body() == {}, "空 urlencoded body 应返回 {}"

    h2 = _make_handler(b"", "multipart/form-data; boundary=abc", content_length=0)
    assert h2._parse_post_body() == {}, "空 multipart body 应返回 {}"


# ---------------------------------------------------------------------------
# ④ 含 filename 但无 name 的域被正确跳过
# ---------------------------------------------------------------------------
def test_multipart_file_part_without_name_is_skipped():
    body, ctype = _build_multipart(
        fields={"scope_type": "all_market"},
        files=[{"filename": "upload.txt", "content": b"should-be-ignored"}],
    )
    h = _make_handler(body, ctype)
    data = h._parse_post_body()
    assert data == {"scope_type": ["all_market"]}, f"文件域未跳过: {data}"
    assert "upload.txt" not in str(data), "文件内容不应出现在结果中"


# ---------------------------------------------------------------------------
# ⑤ 直接调用 _parse_multipart(raw, ctype)（_handle_compare 的 raw/ctype 分支）
# ---------------------------------------------------------------------------
def test_parse_multipart_direct_with_raw_and_ctype():
    body, ctype = _build_multipart(
        {
            "scope_type": "all_market",
            "scope_value": "ALL",
        }
    )
    # 模拟 _handle_compare 中 self._parse_post_body(raw=raw, ctype=ctype) 分支
    h = Handler.__new__(Handler)
    data = h._parse_multipart(body, ctype)
    assert data == {
        "scope_type": ["all_market"],
        "scope_value": ["ALL"],
    }, f"直接调用 _parse_multipart 结果不符: {data}"


if __name__ == "__main__":
    raise SystemExit("请使用 pytest 运行：python -m pytest tests/test_form_parsing.py -q")
