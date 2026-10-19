"""Shared HTTP client for outbound tool calls."""

import httpx

HTTP = httpx.Client(
    headers={"User-Agent": "metalclaw/0.1 github.com/lepinkainen/metalclaw"},
    timeout=15.0,
)
