"""Rate limiter using Redis with an automatic in-memory sliding-window fallback (serverless-safe).

Prevents brute-force OTP attacks, signup abuse, account deletion abuse, and webhook floods.
"""
from __future__ import annotations

import os
import time
from collections import deque

import structlog
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings
from app.redis_client import get_redis

logger = structlog.get_logger()

# Ordered most-specific prefix first
RATE_LIMITS: list[tuple[str, dict[str, int]]] = [
    ("/api/tenant/delete/request-otp", {"requests": 5, "window": 60}),
    ("/api/tenant/delete", {"requests": 5, "window": 60}),
    ("/api/auth/request-otp", {"requests": 10, "window": 60}),
    ("/api/auth/request-signup-otp", {"requests": 10, "window": 60}),
    ("/api/auth/login", {"requests": 15, "window": 60}),
    ("/api/auth/signup", {"requests": 10, "window": 60}),
    ("/api/auth/check-slug", {"requests": 45, "window": 60}),
    ("/api/public/", {"requests": 60, "window": 60}),
    ("/webhooks/", {"requests": 120, "window": 60}),
    ("/dev/", {"requests": 60, "window": 60}),
    ("/api/", {"requests": 200, "window": 60}),
]
DEFAULT_LIMIT = {"requests": 120, "window": 60}

# Bounded in-memory fallback for Vercel / environments without Redis
_MEM_BUCKETS: dict[str, deque[float]] = {}
_MEM_MAX_KEYS = 4096


def _extract_client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for") or request.headers.get("x-real-ip")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _check_memory_limit(key: str, max_requests: int, window_sec: int) -> bool:
    now = time.monotonic()
    cutoff = now - window_sec
    if len(_MEM_BUCKETS) > _MEM_MAX_KEYS:
        # Evict oldest quarter to prevent unbounded memory growth
        for k in list(_MEM_BUCKETS.keys())[: _MEM_MAX_KEYS // 4]:
            _MEM_BUCKETS.pop(k, None)
    q = _MEM_BUCKETS.setdefault(key, deque())
    while q and q[0] <= cutoff:
        q.popleft()
    if len(q) >= max_requests:
        return False
    q.append(now)
    return True


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        # Skip rate limiting for health checks and static assets
        if path == "/health" or not (
            path.startswith("/api/") or path.startswith("/webhooks/") or path.startswith("/dev/")
        ):
            return await call_next(request)

        # Skip pytest TestClient unless explicitly testing rate limiting
        client_host = request.client.host if request.client else "unknown"
        if client_host == "testclient" and not request.headers.get("x-test-rate-limit"):
            return await call_next(request)

        # Match most-specific limit rule
        matched_prefix = "default"
        limit = DEFAULT_LIMIT
        for prefix, cfg in RATE_LIMITS:
            if path.startswith(prefix):
                matched_prefix = prefix
                limit = cfg
                break

        client_ip = _extract_client_ip(request)
        key = f"rl:{client_ip}:{matched_prefix}"

        use_redis = not (
            os.environ.get("VERCEL")
            and ("localhost" in settings.redis_url or "127.0.0.1" in settings.redis_url)
        )
        limited = False
        if use_redis:
            try:
                r = await get_redis()
                current = await r.incr(key)
                if current == 1:
                    await r.expire(key, limit["window"])
                if current > limit["requests"]:
                    limited = True
            except Exception:
                limited = not _check_memory_limit(key, limit["requests"], limit["window"])
        else:
            limited = not _check_memory_limit(key, limit["requests"], limit["window"])

        if limited:
            logger.warning("rate_limited", ip=client_ip, path=path, rule=matched_prefix)
            return Response(
                content='{"detail":"Too many requests. Please wait a moment and try again."}',
                status_code=429,
                media_type="application/json",
                headers={"Retry-After": str(limit["window"])},
            )

        return await call_next(request)
