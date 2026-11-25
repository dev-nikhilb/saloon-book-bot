"""Event-loop-safe async Redis client management.

Using a single module-level aioredis.Redis instance breaks when multiple event loops
are created and closed (e.g. FastAPI TestClient across tests or Celery asyncio.run calls).
We cache one client per running event loop using WeakKeyDictionary so closed loops are
garbage-collected cleanly.
"""
from __future__ import annotations

import asyncio
import weakref

import redis.asyncio as aioredis

from app.config import settings

_loop_clients: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, aioredis.Redis] = (
    weakref.WeakKeyDictionary()
)


async def get_redis() -> aioredis.Redis:
    """Return an async Redis client bound to the current running event loop."""
    loop = asyncio.get_running_loop()
    client = _loop_clients.get(loop)
    if client is None:
        client = aioredis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
        )
        _loop_clients[loop] = client
    return client
