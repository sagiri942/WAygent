"""HTTP 公共工具：代理选择、带重试的 GET、URL 脱敏。"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
import os
import time
import urllib.request
from typing import Any

import httpx

from app.core.async_resources import provider_slot
from app.core.log_safety import redact_url, redact_log_text


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) TripAgentBackend/0.1"
)
HTTP_PROXY_ENV_KEYS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")


def choose_http_proxy() -> str | None:
    """返回 httpx 可直接使用的 HTTP(S) 代理 URL，没有则返回 None。"""
    for key in HTTP_PROXY_ENV_KEYS:
        proxy = os.getenv(key, "").strip()
        if proxy.startswith(("http://", "https://")):
            return proxy
    return None


@contextmanager
def _request_attempt(attempt: int):
    from app.runtime.tracing import trace_stage

    with trace_stage("runtime.provider_request", {
        "provider.kind": "amap", "provider.attempt": attempt + 1, "provider.call_count": 1,
    }) as span:
        try:
            yield span
        except Exception:
            # Only attempts that will retry emit this fixed event. Cancellation is
            # a BaseException and leaves through the stage without a retry event.
            if span is not None and attempt < 2:
                try:
                    span.add_event("provider.retry")
                except Exception:
                    pass
            raise


def http_get_json(url: str, timeout: int = 15) -> dict[str, Any]:
    """发起 GET 请求并解析 JSON，失败时退避重试三次。"""
    from app.runtime.tracing import record_provider_response
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
        },
    )
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with _request_attempt(attempt) as span:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    charset = response.headers.get_content_charset() or "utf-8"
                    data = json.loads(response.read().decode(charset, errors="replace"))
                    record_provider_response(span, data)
                    return data
        except Exception as exc:
            last_error = exc
            time.sleep(0.8 * (attempt + 1))
    raise RuntimeError(f"请求失败：{redact_url(url)}；原因：{redact_log_text(str(last_error))}")


_async_client: httpx.AsyncClient | None = None
_async_client_lock = asyncio.Lock()


async def get_async_http_client() -> httpx.AsyncClient:
    global _async_client
    if _async_client is None:
        async with _async_client_lock:
            if _async_client is None:
                _async_client = httpx.AsyncClient(
                    proxy=choose_http_proxy(),
                    trust_env=False,
                    timeout=15,
                    headers={
                        "User-Agent": USER_AGENT,
                        "Accept": "application/json,*/*;q=0.8",
                        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
                    },
                )
    return _async_client


async def close_async_http_client() -> None:
    global _async_client
    if _async_client is not None:
        await _async_client.aclose()
        _async_client = None


async def http_get_json_async(url: str, timeout: int = 15, *, before_attempt=None) -> dict[str, Any]:
    from app.runtime.tracing import record_provider_response

    client = await get_async_http_client()
    last_error: Exception | None = None
    async with provider_slot("amap"):
        for attempt in range(3):
            if before_attempt is not None:
                await before_attempt()
            try:
                with _request_attempt(attempt) as span:
                    response = await client.get(url, timeout=timeout)
                    response.raise_for_status()
                    data = response.json()
                    if not isinstance(data, dict):
                        raise ValueError("response is not a JSON object")
                    record_provider_response(span, data)
                    return data
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(0.8 * (attempt + 1))
    raise RuntimeError(f"请求失败：{redact_url(url)}；原因：{redact_log_text(str(last_error))}")
