"""Planning capacity shared by scheduled runs and inline revision tools."""

import asyncio
import os
from collections import defaultdict
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar

_chat_lease = ContextVar("runtime_chat_capacity_lease", default=None)


class ChatCapacityLease:
    """Release a Chat Run's slot while its tools wait or perform planning work."""

    def __init__(self, capacity):
        self.capacity = capacity
        self.held = False
        self.suspensions = 0

    async def __aenter__(self):
        await self.capacity.acquire()
        self.held = True
        return self

    async def __aexit__(self, *args):
        if self.held:
            self.capacity.release()
            self.held = False

    @asynccontextmanager
    async def suspend(self, deadline):
        self.suspensions += 1
        if self.held:
            self.capacity.release()
            self.held = False
        cancelled = False
        try:
            yield
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            self.suspensions -= 1
            # Rejected tools can be handled by Chat and followed by an answer.
            # They need their slot back too; terminal cancellation does not.
            if not cancelled and self.suspensions == 0:
                async with asyncio.timeout_at(deadline):
                    await self.capacity.acquire()
                if self.suspensions:
                    self.capacity.release()
                else:
                    self.held = True


@contextmanager
def chat_capacity_scope(lease):
    token = _chat_lease.set(lease)
    try:
        yield
    finally:
        _chat_lease.reset(token)


@asynccontextmanager
async def suspend_chat_capacity(deadline):
    lease = _chat_lease.get()
    if lease is None:
        yield
    else:
        async with lease.suspend(deadline):
            yield


class PlanningResources:
    def __init__(self, planning_limit=None, planning_per_user=None):
        self.capacity = asyncio.Semaphore(max(1, int(
            planning_limit if planning_limit is not None else os.getenv("RUNTIME_PLANNING_CONCURRENCY", "2"))))
        per_user = max(1, int(planning_per_user if planning_per_user is not None
                              else os.getenv("RUNTIME_PLANNING_PER_USER", "2")))
        self.users = defaultdict(lambda: asyncio.Semaphore(per_user))
        self.keys = defaultdict(asyncio.Lock)

    @asynccontextmanager
    async def revision(self, user_id, itinerary_id):
        # Same acquisition order as scheduled planning runs: global, user, key.
        async with self.capacity, self.users[user_id], self.keys[f"revision:{itinerary_id}"]:
            yield
