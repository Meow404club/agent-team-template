"""Web fetch tool for the researcher role.

Uses curl_cffi to impersonate a real browser TLS/JA3/HTTP2 fingerprint, which
passes anti-bot checks (Cloudflare etc.) that plain urllib/requests fail.
HTML is stripped to readable text by default; raw=True returns the original
HTML. No external extraction dependencies — regex-based, good enough for
feeding LLMs.
"""
from __future__ import annotations

import html as _htmlmod
import re

_IMPERSONATE = "chrome"  # 浏览器指纹别名（curl_cffi 映射到其支持的新版 Chrome）
_MAX_CHARS = 30000

_DROP_BLOCKS = re.compile(r"(?is)<(script|style|noscript|svg|head|iframe|form)[^>]*>.*?</\1>")
_COMMENTS = re.compile(r"(?s)<!--.*?-->")
_BLOCK_NEWLINES = re.compile(r"(?i)<(br|/p|/div|/h[1-6]|/li|/tr|/table|/pre)[^>]*>")
_TAGS = re.compile(r"(?s)<[^>]+>")
_SPACES = re.compile(r"[ \t\r\f]+")
_BLANK_LINES = re.compile(r"\n\s*\n+")


def _html_to_text(html: str) -> str:
    text = _COMMENTS.sub(" ", _DROP_BLOCKS.sub(" ", html))
    text = _BLOCK_NEWLINES.sub("\n", text)
    text = _TAGS.sub(" ", text)
    text = _htmlmod.unescape(text)
    text = _SPACES.sub(" ", text)
    text = _BLANK_LINES.sub("\n\n", text)
    return text.strip()


def web_fetch(url: str, timeout: int = 20, max_chars: int = _MAX_CHARS,
              raw: bool = False) -> dict:
    """Fetch a web page with browser-fingerprint impersonation.

    Returns dict with url (final after redirects), status, content_type,
    length, mode, truncated, content. HTTP error statuses (403/503 challenge
    pages) are returned as-is so the caller can see what the anti-bot said.
    """
    from curl_cffi import requests as cffi  # 延迟导入：未装 curl_cffi 时给出可读错误

    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        raise ValueError(f"仅支持 http(s) URL: {url!r}")
    if max_chars <= 0:
        max_chars = _MAX_CHARS

    resp = cffi.get(
        url,
        impersonate=_IMPERSONATE,
        timeout=timeout,
        allow_redirects=True,
        headers={"Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8"},
    )
    body = resp.text
    ctype = (resp.headers.get("content-type") or "").lower()
    if "html" in ctype and not raw:
        content, mode = _html_to_text(body), "html->text"
    else:
        content, mode = body, ctype.split(";")[0] or "text"

    truncated = len(content) > max_chars
    if truncated:
        content = content[:max_chars]
    return {
        "url": str(resp.url),
        "status": resp.status_code,
        "content_type": ctype,
        "length": len(body),
        "mode": mode,
        "truncated": truncated,
        "content": content,
    }
