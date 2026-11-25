"""Subdomain-based tenant routing middleware.

Extracts tenant slug from Host header (slug.bookbot.com) and adds it to request state.
Dashboard and API endpoints can then scope to this tenant automatically.
"""
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings


class SubdomainMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        host = request.headers.get("host", "").split(":")[0]  # strip port
        base = settings.base_domain.split(":")[0]

        # Extract subdomain: "stylestudio.bookbot.com" -> "stylestudio"
        if host.endswith(f".{base}") and host != base:
            slug = host.replace(f".{base}", "")
            request.state.tenant_slug = slug
        else:
            request.state.tenant_slug = None

        return await call_next(request)
