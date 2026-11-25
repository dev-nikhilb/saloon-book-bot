"""Simple rate limiter using Redis. Prevents abuse of webhook and API endpoints."""
from __future__ import annotations

import os

import structlog
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings
from app.redis_client import get_redis

logger = structlog.get_logger()

# Rate limits per path prefix
RATE_LIMITS = {
    "/webhooks/": {"requests": 100, "window": 60},  # 100/min per IP
    "/dev/": {"requests": 60, "window": 60},  # 60/min per IP
    "/api/": {"requests": 200, "window": 60},  # 200/min per IP (dashboard)
}
DEFAULT_LIMIT = {"requests": 120, "window": 60}


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        # Skip rate limiting for health checks, test clients, or serverless without external Redis
        if request.url.path == "/health":
            return await call_next(request)
        if os.environ.get("VERCEL") and (
            "localhost" in settings.redis_url or "127.0.0.1" in settings.redis_url
        ):
            return await call_next(request)
        client_host = request.client.host if request.client else "unknown"
        if client_host == "testclient":
            return await call_next(request)

        # Determine limit
        limit = DEFAULT_LIMIT
        for prefix, cfg in RATE_LIMITS.items():
            if request.url.path.startswith(prefix):
                limit = cfg
                break

        # Key by IP + path prefix
        client_ip = request.client.host if request.client else "unknown"
        path_prefix = request.url.path.split("/")[1] if "/" in request.url.path else "root"
        key = f"rl:{client_ip}:{path_prefix}"

        try:
            r = await get_redis()
            current = await r.incr(key)
            if current == 1:
                await r.expire(key, limit["window"])

            if current > limit["requests"]:
                logger.warning("rate_limited", ip=client_ip, path=request.url.path, count=current)
                return Response(content="Rate limited", status_code=429)
        except Exception:
            # Redis down — don't block requests
            pass

        return await call_next(request)
