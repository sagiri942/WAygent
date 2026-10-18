"""Optional Redis cache with bounded, request-driven recovery."""
from __future__ import annotations
import hashlib
import json
import logging
import os
import threading
import time
from typing import Any
from redis.exceptions import ConnectionError, TimeoutError

logger = logging.getLogger(__name__)
_redis_client = None
_next_retry = 0.0
_connection_lock = threading.Lock()


def _retry_interval() -> float:
    try:
        value = float(os.getenv("REDIS_RETRY_INTERVAL_SECONDS", "5"))
        return value if 0 < value < float("inf") else 5.0
    except ValueError:
        return 5.0


def _close(client):
    if client is not None:
        try:
            client.close()
        except Exception:
            logger.debug("Redis client close failed", exc_info=True)


def close_cache() -> None:
    """Release pooled connections at shutdown or explicit reset."""
    global _redis_client, _next_retry
    with _connection_lock:
        client, _redis_client = _redis_client, None
        _next_retry = 0.0
        _close(client)


def _connection_failed(client):
    global _redis_client, _next_retry
    if not _connection_lock.acquire(blocking=False):
        return
    try:
        if _redis_client is client:
            _redis_client = None
            _next_retry = time.monotonic() + _retry_interval()
            _close(client)
    finally:
        _connection_lock.release()


def _get_redis():
    """One caller probes; concurrent callers bypass without waiting for ping."""
    global _redis_client, _next_retry
    if _redis_client is not None:
        return _redis_client
    redis_url = os.getenv("REDIS_URL", "").strip()
    if not redis_url or time.monotonic() < _next_retry:
        return None
    if not _connection_lock.acquire(blocking=False):
        return None
    try:
        if _redis_client is not None:
            return _redis_client
        if time.monotonic() < _next_retry:
            return None
        client = None
        try:
            import redis
            client = redis.from_url(
                redis_url, decode_responses=True,
                socket_connect_timeout=2, socket_timeout=1,
                retry_on_timeout=False,
            )
            client.ping()
        except Exception:
            _close(client)
            _next_retry = time.monotonic() + _retry_interval()
            logger.warning("Redis unavailable; cache retry scheduled")
            return None
        _redis_client = client
        return client
    finally:
        _connection_lock.release()


def get_cached(key: str) -> Any | None:
    r = _get_redis()
    if r is None:
        return None
    try:
        raw = r.get(key)
        if raw is not None:
            return json.loads(raw)
    except (ConnectionError, TimeoutError):
        _connection_failed(r)
    except Exception as exc:
        logger.debug("Cache read failed [%s]: %s", key, type(exc).__name__)
    return None


def set_cached(key: str, value: Any, ttl_seconds: int) -> None:
    r = _get_redis()
    if r is None:
        return
    try:
        r.setex(key, ttl_seconds, json.dumps(value, ensure_ascii=False))
    except (ConnectionError, TimeoutError):
        _connection_failed(r)
    except Exception as exc:
        logger.debug("Cache write failed [%s]: %s", key, type(exc).__name__)


def weather_cache_key(city: str) -> str:
    """天气预报缓存键。格式：tripagent:weather:{city}"""
    return f"tripagent:weather:{city}"


def poi_cache_key(
    city: str, keyword: str, *, types: str, offset: int,
    response_kind: str = "amap-text-raw",
) -> str:
    """Versioned first-page identity; legacy keys expire without being read.

    Encode exact provider inputs without trimming/reordering their semantics.
    A structured payload avoids ambiguous separators; response_kind isolates
    raw provider records from the manual editor's parsed result schema.
    """
    payload = json.dumps(
        [response_kind, city, keyword, types, str(offset)],
        ensure_ascii=False, separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"tripagent:poi:v2:{digest}"


# ─── TTL 常量 ──────────────────────────────────────────────────

WEATHER_TTL = 4 * 3600    # 天气缓存 4 小时
POI_TTL = 12 * 3600       # POI 缓存 12 小时
