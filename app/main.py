import logging
from pathlib import Path

import structlog
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.middleware import RequestIDMiddleware, SecurityHeadersMiddleware

LOG_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer() if settings.environment == "development"
        else structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(
        LOG_LEVELS.get(settings.log_level.upper(), logging.INFO)
    ),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger()

_is_prod = settings.environment == "production"

app = FastAPI(
    title="Tivoria",
    description="WhatsApp-first appointment booking OS for service businesses",
    version="1.0.0",
    docs_url=None if _is_prod else "/docs",
    redoc_url=None if _is_prod else "/redoc",
    openapi_url=None if _is_prod else "/openapi.json",
)

app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RequestIDMiddleware)

from app.subdomain import SubdomainMiddleware

app.add_middleware(SubdomainMiddleware)

from fastapi.middleware.cors import CORSMiddleware

_allowed_origins = [
    "https://tivoria.site",
    "https://www.tivoria.site",
    "https://saloon-book-bot.vercel.app",
]
if not _is_prod:
    _allowed_origins.extend([
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://localhost:3000",
    ])

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_origin_regex=r"^https://([a-z0-9-]+\.)?tivoria\.site$",
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
)

from app.rate_limit import RateLimitMiddleware

app.add_middleware(RateLimitMiddleware)

if settings.sentry_dsn:
    import sentry_sdk
    sentry_sdk.init(dsn=settings.sentry_dsn, environment=settings.environment)

# WhatsApp webhook (always active)
from app.messaging.whatsapp.webhook_router import router as webhook_router

app.include_router(webhook_router)

# Dashboard API (always active — auth is handled per-endpoint)
from app.dashboard.router import router as dashboard_router

app.include_router(dashboard_router)

# Public Shareable Booking Page API
from app.public_booking import router as public_booking_router

app.include_router(public_booking_router)

# Payment endpoints
from app.payments.router import router as payments_router

app.include_router(payments_router)

# Serve dashboard static files
_static_dir = Path(__file__).parent / "dashboard" / "static"
if _static_dir.exists():
    @app.get("/book/{slug}")
    async def public_booking_page(slug: str):
        book_html = _static_dir / "book.html"
        if book_html.exists():
            return FileResponse(str(book_html))
        return {"error": "Booking page unavailable"}

    app.mount("/dashboard", StaticFiles(directory=str(_static_dir), html=True), name="dashboard")

# Dev-only simulate endpoint (only in development)
if settings.environment == "development":
    from app.dev_simulate import router as dev_router
    from app.onboarding.router import router as onboard_router
    app.include_router(dev_router)
    app.include_router(onboard_router)


@app.get("/health")
async def health():
    return {"status": "ok", "environment": settings.environment}


# Landing page + docs + Meta compliance pages (MUST be last — catches all unmatched paths)
_site_dir = Path(__file__).parent.parent / "site"
if _site_dir.exists():
    @app.get("/privacy")
    async def privacy_policy_page():
        return FileResponse(str(_site_dir / "privacy.html"))

    @app.get("/terms")
    async def terms_page():
        return FileResponse(str(_site_dir / "terms.html"))

    if _is_prod:
        from fastapi import HTTPException

        @app.get("/admin.html")
        async def block_admin_in_prod():
            raise HTTPException(status_code=404, detail="Not found")

    app.mount("/", StaticFiles(directory=str(_site_dir), html=True), name="site")
