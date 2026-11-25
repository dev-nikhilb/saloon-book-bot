import uuid
from contextvars import ContextVar

import structlog
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

request_id_var: ContextVar[str] = ContextVar("request_id", default="")

logger = structlog.get_logger()


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        rid = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        request_id_var.set(rid)

        structlog.contextvars.bind_contextvars(request_id=rid)

        response = await call_next(request)
        response.headers["X-Request-ID"] = rid

        structlog.contextvars.unbind_contextvars("request_id")
        return response
