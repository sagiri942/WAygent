"""Shared bounded provider capacity for async graph nodes."""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager

from app.runtime.observability import metrics

llm_capacity = asyncio.Semaphore(max(1, int(os.getenv("RUNTIME_LLM_CONCURRENCY", "8"))))
amap_capacity = asyncio.Semaphore(max(1, int(os.getenv("RUNTIME_AMAP_CONCURRENCY", "8"))))


@asynccontextmanager
async def provider_slot(provider: str):
    semaphore = llm_capacity if provider == "llm" else amap_capacity
    if semaphore.locked():
        from app.runtime.tracing import trace_stage
        wait_started = time.monotonic()
        with trace_stage("runtime.capacity_wait", {"capacity.kind": provider}) as span:
            await semaphore.acquire()
            if span is not None:
                span.set_attribute("capacity.wait_ms", max(0, int((time.monotonic() - wait_started) * 1000)))
    else:
        await semaphore.acquire()
    started = time.monotonic()
    metrics.provider_acquired(provider)
    try:
        yield
    finally:
        metrics.provider_released(provider, time.monotonic() - started)
        semaphore.release()