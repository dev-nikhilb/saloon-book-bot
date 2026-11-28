import uuid
from contextvars import ContextVar

import structlog
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings

request_id_var: ContextVar[str] = ContextVar("request_id", default="")

logger = structlog.get_logger()

_CSP_HEADER = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://connect.facebook.net https://checkout.razorpay.com; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: https:; "
    "font-src 'self' data:; "
    "connect-src 'self' https://graph.facebook.com https://www.facebook.com https://api.razorpay.com https://lumberjack.razorpay.com; "
    "frame-src 'self' https://www.facebook.com https://web.facebook.com https://api.razorpay.com https://checkout.razorpay.com; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "object-src 'none'"
)


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        rid = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        request_id_var.set(rid)

        structlog.contextvars.bind_contextvars(request_id=rid)

        response = await call_next(request)
        response.headers["X-Request-ID"] = rid

        structlog.contextvars.unbind_contextvars("request_id")
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Apply OWASP security headers on every response (clickjacking, MIME sniffing, CSP, HSTS)."""

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        response.headers.setdefault("X-XSS-Protection", "0")
        response.headers.setdefault("Content-Security-Policy", _CSP_HEADER)
        if settings.environment != "development":
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response
