"""Dashboard API — all endpoints for the salon owner web dashboard.

Mounts under ``/api`` via ``APIRouter(prefix="/api")``.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.dashboard.auth import create_access_token, get_current_tenant
from app.database import get_db

log = structlog.get_logger()

router = APIRouter(prefix="/api", tags=["dashboard"])


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

# -- Auth ------------------------------------------------------------------

class LoginRequest(BaseModel):
    phone: str = Field(..., description="Owner phone number (E.164)")
    otp: str = Field(..., min_length=6, max_length=6, description="6-digit OTP code")


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    tenant_id: str
    business_name: str


# -- Generic ---------------------------------------------------------------

class PaginatedResponse(BaseModel):
    items: list[dict[str, Any]]
    total: int
    page: int
    per_page: int


class MessageResponse(BaseModel):
    message: str


# -- Tenant ----------------------------------------------------------------

class TenantProfile(BaseModel):
    id: str
    business_name: str
    business_type: str
    owner_phone: str
    owner_name: str | None = None
    owner_email: str | None = None
    slug: str | None = None
    primary_language: str
    timezone: str
    waba_phone_number_id: str | None = None
    plan_tier: str
    is_active: bool
    onboarded_at: str | None = None
    created_at: str
    updated_at: str


class TenantUpdate(BaseModel):
    business_name: str | None = None
    business_type: str | None = None
    owner_name: str | None = None
    owner_email: str | None = None
    primary_language: str | None = None
    timezone: str | None = None
    onboarded_at: str | None = None


class TenantSettingsUpdate(BaseModel):
    """All configurable tenant settings. All fields optional — only provided
    fields are persisted."""
    advance_booking_days: int | None = None
    min_cancel_hours: int | None = None
    slot_increment_minutes: int | None = None
    max_bookings_per_customer: int | None = None
    send_reminders: bool | None = None
    reminder_hours_before: int | None = None
    send_feedback_request: bool | None = None
    feedback_delay_hours: int | None = None
    welcome_message: str | None = None
    booking_confirmed_message: str | None = None
    thank_you_message: str | None = None
    google_review_url: str | None = None
    google_calendar_enabled: bool | None = None
    razorpay_enabled: bool | None = None
    upi_id: str | None = None
    deposit_mode: str | None = None  # auto, razorpay, upi_manual, disabled
    deposit_hold_minutes: int | None = Field(None, ge=5, le=1440)


# -- Service ---------------------------------------------------------------

class ServiceCreate(BaseModel):
    name: str
    description: str | None = None
    image_url: str | None = None
    duration_minutes: int = Field(0, ge=0)
    buffer_minutes: int = Field(0, ge=0)
    slot_increment_minutes: int | None = Field(None, ge=5, le=240)
    price_paise: int | None = None
    requires_deposit: bool = False
    deposit_amount_paise: int | None = None
    parent_service_id: str | None = None
    sort_order: int = 0


class ServiceUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    image_url: str | None = None
    parent_service_id: str | None = None
    duration_minutes: int | None = Field(None, ge=0)
    buffer_minutes: int | None = Field(None, ge=0)
    slot_increment_minutes: int | None = Field(None, ge=5, le=240)
    price_paise: int | None = None
    requires_deposit: bool | None = None
    deposit_amount_paise: int | None = None
    is_active: bool | None = None
    sort_order: int | None = None


class ServiceReorderItem(BaseModel):
    id: str
    sort_order: int


class ServiceReorderRequest(BaseModel):
    items: list[ServiceReorderItem]


# -- Staff -----------------------------------------------------------------

class StaffCreate(BaseModel):
    name: str
    branch_id: str | None = None
    role: str | None = None
    bio: str | None = None
    specialty: str | None = None
    skills: list | dict | None = None
    working_hours: dict | None = None


class StaffUpdate(BaseModel):
    name: str | None = None
    branch_id: str | None = None
    role: str | None = None
    bio: str | None = None
    specialty: str | None = None
    skills: list | dict | None = None
    working_hours: dict | None = None
    is_active: bool | None = None


# -- Booking ---------------------------------------------------------------

class BookingCreate(BaseModel):
    service_id: str
    staff_id: str | None = None
    branch_id: str | None = None
    starts_at: str = Field(..., description="ISO-8601 datetime with timezone")
    customer_name: str = Field(..., min_length=1, max_length=100)
    customer_phone: str = Field(..., min_length=8, max_length=20)
    reason: str | None = None
    notes: str | None = None
    requires_deposit: bool = False


class BookingNotesUpdate(BaseModel):
    reason: str | None = None
    notes: str | None = None


class BookingStatusUpdate(BaseModel):
    status: str = Field(..., description="New status: cancelled, completed, no_show")
    cancellation_reason: str | None = None


# -- Branch ----------------------------------------------------------------

class BranchCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=100)
    address: str | None = None
    operating_hours: dict | None = None


class BranchUpdate(BaseModel):
    name: str | None = None
    address: str | None = None
    operating_hours: dict | None = None


class OtpRequest(BaseModel):
    phone: str | None = Field(None, description="Owner phone number (E.164)")
    identifier: str | None = Field(None, description="Owner email or phone number")


class SignupOtpRequest(BaseModel):
    email: str = Field(..., min_length=5, max_length=160)
    business_name: str | None = "BookBot"


class PlanUpdateRequest(BaseModel):
    plan_tier: str = Field(..., description="solo, growth, or pro")
    billing_cycle: str = Field("monthly", description="monthly or annual")
    skip_trial: bool = Field(False, description="Skip remaining free trial and activate paid billing immediately")


class ApplyTemplateRequest(BaseModel):
    business_type: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row_to_dict(row) -> dict[str, Any]:
    """Convert a SQLAlchemy Row/RowMapping to a JSON-safe dict."""
    d = dict(row._mapping) if hasattr(row, "_mapping") else dict(row)
    for k, v in d.items():
        if isinstance(v, (uuid.UUID,)):
            d[k] = str(v)
        elif isinstance(v, datetime):
            d[k] = v.isoformat()
        elif isinstance(v, date):
            d[k] = v.isoformat()
        elif isinstance(v, bytes):
            d[k] = None  # never expose encrypted blobs
    return d


def _parse_uuid(value: str, label: str = "id") -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {label}: {value}")


def _json_or_none(val) -> str | None:
    """Serialize a dict/list to a JSON string for JSONB casts, or return None."""
    if val is None:
        return None
    import json
    return json.dumps(val)


# ---------------------------------------------------------------------------
# AUTH
# ---------------------------------------------------------------------------

# -- Signup schemas --------------------------------------------------------

class SignupRequest(BaseModel):
    business_name: str = Field(..., min_length=2, max_length=100)
    owner_name: str = Field(..., min_length=2, max_length=100)
    owner_phone: str = Field(..., description="Phone in E.164 format")
    owner_email: str | None = Field(None, max_length=160, description="Owner email for OTP & billing receipts")
    business_type: str = Field("salon")
    slug: str = Field(..., min_length=2, max_length=30, pattern="^[a-z0-9][a-z0-9-]*[a-z0-9]$")
    plan_tier: str = Field("solo", description="Initial selected plan for 14-day trial: solo, growth, or pro")
    billing_cycle: str = Field("monthly", description="monthly or annual")
    otp: str | None = Field(None, description="6-digit signup OTP (required when Email OTP is configured)")


class SignupResponse(BaseModel):
    access_token: str
    tenant_id: str
    slug: str
    plan_tier: str
    dashboard_url: str
    message: str


@router.post("/auth/request-signup-otp")
async def request_signup_verification_otp(body: SignupOtpRequest, db: AsyncSession = Depends(get_db)):
    """Send a free 6-digit email verification code before workspace signup."""
    from app.auth.otp import request_signup_otp

    email = body.email.strip().lower()
    if "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(status_code=400, detail="Please enter a valid email address")

    existing_email = await db.execute(
        text("SELECT id FROM tenants WHERE LOWER(owner_email) = LOWER(:email) LIMIT 1"),
        {"email": email},
    )
    if existing_email.first():
        raise HTTPException(status_code=409, detail="An account with this email already exists. Please sign in.")

    res = await request_signup_otp(db, email, business_name=body.business_name or "BookBot")
    if res.get("otp_required") and not res.get("sent") and res.get("delivery_error"):
        raise HTTPException(status_code=502, detail=res["delivery_error"])
    return res


@router.post("/auth/signup", response_model=SignupResponse, status_code=201)
async def signup(body: SignupRequest, db: AsyncSession = Depends(get_db)):
    """Self-service tenant registration. Creates tenant + branch + settings + owner staff + 14-day trial."""
    from app.auth.otp import is_email_otp_configured, verify_otp
    from app.billing.service import PLANS, ensure_trial_subscription
    from app.config import settings as app_settings

    plan_tier = body.plan_tier if body.plan_tier in PLANS else "solo"
    billing_cycle = body.billing_cycle if body.billing_cycle in ("monthly", "annual") else "monthly"
    clean_email = body.owner_email.strip().lower() if body.owner_email and body.owner_email.strip() else None

    if clean_email and ("@" not in clean_email or "." not in clean_email.split("@")[-1]):
        raise HTTPException(status_code=400, detail="Please enter a valid email address")

    # Enforce signup email OTP automatically when Resend/SMTP is configured in production
    if clean_email and is_email_otp_configured() and app_settings.environment == "production":
        if not body.otp:
            raise HTTPException(status_code=400, detail="Please verify the 6-digit OTP sent to your email first.")
        ok, err = await verify_otp(clean_email, body.otp, db=db, purpose="signup")
        if not ok:
            raise HTTPException(status_code=401, detail=err)

    # Check slug uniqueness
    existing = await db.execute(text("SELECT id FROM tenants WHERE slug = :slug"), {"slug": body.slug})
    if existing.first():
        raise HTTPException(409, "This URL is already taken. Try another.")

    # Check phone uniqueness
    existing_phone = await db.execute(text("SELECT id FROM tenants WHERE owner_phone = :phone"), {"phone": body.owner_phone})
    if existing_phone.first():
        raise HTTPException(409, "An account with this phone number already exists.")

    # Check email uniqueness
    if clean_email:
        existing_email = await db.execute(
            text("SELECT id FROM tenants WHERE LOWER(owner_email) = LOWER(:email)"),
            {"email": clean_email},
        )
        if existing_email.first():
            raise HTTPException(409, "An account with this email already exists.")

    import json
    import uuid as uuid_mod
    tenant_id = uuid_mod.uuid4()
    branch_id = uuid_mod.uuid4()
    staff_id = uuid_mod.uuid4()
    settings_id = uuid_mod.uuid4()
    now = datetime.now(timezone.utc)

    hours = json.dumps({d: [{"open": "09:00", "close": "20:00"}] for d in ["mon", "tue", "wed", "thu", "fri", "sat"]})

    # Create tenant
    await db.execute(text("""
        INSERT INTO tenants (id, business_name, business_type, owner_phone, owner_name, owner_email, slug,
            primary_language, timezone, plan_tier, is_active, created_at, updated_at)
        VALUES (:id, :name, :type, :phone, :owner, :email, :slug, 'en', 'Asia/Kolkata', :tier, true, :now, :now)
    """), {"id": tenant_id, "name": body.business_name, "type": body.business_type,
           "phone": body.owner_phone, "owner": body.owner_name, "email": clean_email,
           "slug": body.slug, "tier": plan_tier, "now": now})

    # Create branch
    await db.execute(text("""
        INSERT INTO branches (id, tenant_id, name, is_primary, operating_hours, created_at)
        VALUES (:id, :tid, 'Main', true, :hours, :now)
    """), {"id": branch_id, "tid": tenant_id, "hours": hours, "now": now})

    # Create owner as staff
    await db.execute(text("""
        INSERT INTO staff (id, tenant_id, branch_id, name, role, is_active, created_at)
        VALUES (:id, :tid, :bid, :name, 'owner', true, :now)
    """), {"id": staff_id, "tid": tenant_id, "bid": branch_id, "name": body.owner_name, "now": now})

    # Create default settings
    await db.execute(text("""
        INSERT INTO tenant_settings (id, tenant_id, created_at, updated_at)
        VALUES (:id, :tid, :now, :now)
    """), {"id": settings_id, "tid": tenant_id, "now": now})

    # Provision 14-day free trial on the chosen plan & cycle
    await ensure_trial_subscription(db, tenant_id, plan_tier=plan_tier, billing_cycle=billing_cycle)

    await db.commit()

    from app.dashboard.auth import create_access_token
    token = create_access_token(tenant_id, body.owner_phone)

    dashboard_url = f"https://{body.slug}.{app_settings.base_domain}/dashboard/"

    return SignupResponse(
        access_token=token,
        tenant_id=str(tenant_id),
        slug=body.slug,
        plan_tier=plan_tier,
        dashboard_url=dashboard_url,
        message=f"Welcome! {body.business_name} is ready on your 14-day free trial.",
    )


@router.post("/auth/request-otp")
async def request_login_otp(body: OtpRequest, db: AsyncSession = Depends(get_db)):
    """Generate and deliver a 6-digit login OTP by phone OR email."""
    from app.auth.otp import request_otp

    ident = (body.identifier or body.phone or "").strip()
    if not ident:
        raise HTTPException(status_code=400, detail="Enter your phone number or email address")

    result = await request_otp(db, ident)
    if not result["found"]:
        raise HTTPException(status_code=404, detail="No account found for this phone or email")
    if not result["active"]:
        raise HTTPException(status_code=403, detail="Account is deactivated")

    if result.get("delivered_via") == "log" and result.get("delivery_error"):
        raise HTTPException(status_code=502, detail=result["delivery_error"])

    resp = {
        "sent": True,
        "expires_in": result["expires_in"],
        "delivered_via": result["delivered_via"],
        "email_configured": result.get("email_configured", False),
    }
    if "dev_otp" in result:
        resp["dev_otp"] = result["dev_otp"]
    return resp


@router.post("/auth/login", response_model=LoginResponse)
async def login(body: LoginRequest, db: AsyncSession = Depends(get_db)):
    """Authenticate business owner by phone or email + 6-digit OTP."""
    from app.auth.otp import verify_otp

    if not body.otp.isdigit() or len(body.otp) != 6:
        raise HTTPException(status_code=400, detail="OTP must be exactly 6 digits")

    ident = body.phone.strip()
    if "@" in ident:
        result = await db.execute(
            text("SELECT id, business_name, owner_phone, is_active FROM tenants WHERE LOWER(owner_email) = LOWER(:ident)"),
            {"ident": ident},
        )
    else:
        result = await db.execute(
            text("SELECT id, business_name, owner_phone, is_active FROM tenants WHERE owner_phone = :phone"),
            {"phone": ident},
        )
    tenant = result.first()

    if not tenant:
        raise HTTPException(status_code=404, detail="No account found for this phone number or email")

    row = tenant._mapping
    if not row["is_active"]:
        raise HTTPException(status_code=403, detail="Account is deactivated")

    ok, err = await verify_otp(ident, body.otp, db=db, purpose="login")
    if not ok:
        raise HTTPException(status_code=401, detail=err)

    token = create_access_token(row["id"], row["owner_phone"])
    log.info("dashboard.login", tenant_id=str(row["id"]), identifier=ident)

    return LoginResponse(
        access_token=token,
        tenant_id=str(row["id"]),
        business_name=row["business_name"],
    )


# ---------------------------------------------------------------------------
# TENANT PROFILE
# ---------------------------------------------------------------------------

@router.get("/tenant", response_model=TenantProfile)
async def get_tenant(
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        text("SELECT * FROM tenants WHERE id = :id"), {"id": tenant_id}
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return _row_to_dict(row)


@router.put("/tenant", response_model=TenantProfile)
async def update_tenant(
    body: TenantUpdate,
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    set_clauses = ", ".join(f"{k} = :{k}" for k in updates)
    updates["id"] = tenant_id
    updates["now"] = datetime.now(timezone.utc)
    await db.execute(
        text(f"UPDATE tenants SET {set_clauses}, updated_at = :now WHERE id = :id"),
        updates,
    )
    await db.commit()

    result = await db.execute(
        text("SELECT * FROM tenants WHERE id = :id"), {"id": tenant_id}
    )
    return _row_to_dict(result.first())


class WhatsAppConfig(BaseModel):
    waba_phone_number_id: str
    waba_business_account_id: str | None = None
    waba_access_token: str | None = None


class EmbeddedSignupCompleteRequest(BaseModel):
    code: str = Field(..., min_length=4)
    waba_id: str = Field(..., min_length=5)
    phone_number_id: str = Field(..., min_length=5)


async def _subscribe_waba_webhook(waba_id: str, access_token: str) -> bool:
    """Subscribe a WhatsApp Business Account (WABA) to our Meta App's webhook automatically."""
    if not waba_id or not access_token:
        return False
    import httpx

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"https://graph.facebook.com/v25.0/{waba_id.strip()}/subscribed_apps",
                headers={"Authorization": f"Bearer {access_token.strip()}"},
            )
            if resp.status_code == 200 and resp.json().get("success"):
                log.info("waba.webhook_subscribed", waba_id=waba_id)
                return True
            log.warning("waba.webhook_subscribe_failed", waba_id=waba_id, status=resp.status_code, body=resp.text[:200])
    except Exception as exc:
        log.warning("waba.webhook_subscribe_error", waba_id=waba_id, error=str(exc))
    return False


@router.put("/tenant/whatsapp")
async def update_whatsapp_config(
    body: WhatsAppConfig,
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Update WhatsApp Business API credentials for tenant (auto-subscribes WABA webhook if WABA ID given)."""
    params: dict[str, Any] = {
        "tid": tenant_id,
        "pid": body.waba_phone_number_id.strip(),
        "waba_id": body.waba_business_account_id.strip() if body.waba_business_account_id else None,
        "now": datetime.now(timezone.utc),
    }
    if body.waba_access_token:
        from app.crypto import encrypt_secret

        params["token"] = encrypt_secret(body.waba_access_token.strip())
        await db.execute(
            text(
                "UPDATE tenants SET waba_phone_number_id = :pid, "
                "waba_business_account_id = COALESCE(:waba_id, waba_business_account_id), "
                "waba_access_token_encrypted = :token, updated_at = :now WHERE id = :tid"
            ),
            params,
        )
    else:
        await db.execute(
            text(
                "UPDATE tenants SET waba_phone_number_id = :pid, "
                "waba_business_account_id = COALESCE(:waba_id, waba_business_account_id), "
                "updated_at = :now WHERE id = :tid"
            ),
            params,
        )
    await db.commit()

    webhook_subscribed = False
    if params["waba_id"] and body.waba_access_token:
        webhook_subscribed = await _subscribe_waba_webhook(params["waba_id"], body.waba_access_token)

    return {
        "status": "ok",
        "waba_phone_number_id": params["pid"],
        "waba_business_account_id": params["waba_id"],
        "webhook_subscribed": webhook_subscribed,
    }


@router.post("/tenant/whatsapp/embedded-signup")
async def complete_whatsapp_embedded_signup(
    body: EmbeddedSignupCompleteRequest,
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Meta Tech Provider Embedded Signup (supports WhatsApp Business App Coexistence).

    Exchanges the one-time Facebook Login authorization code for a Business Integration token,
    auto-subscribes the tenant's WABA to our webhook, and saves Fernet-encrypted credentials.
    """
    import httpx

    from app.config import settings as app_settings
    from app.crypto import encrypt_secret

    if not app_settings.meta_app_id or not app_settings.meta_app_secret:
        raise HTTPException(
            status_code=503,
            detail="Meta Tech Provider Embedded Signup is not configured yet (META_APP_ID / META_APP_SECRET missing).",
        )

    async with httpx.AsyncClient(timeout=15.0) as client:
        tok_resp = await client.get(
            "https://graph.facebook.com/v25.0/oauth/access_token",
            params={
                "client_id": app_settings.meta_app_id,
                "client_secret": app_settings.meta_app_secret,
                "code": body.code,
            },
        )
        if tok_resp.status_code != 200:
            err_msg = tok_resp.text[:250]
            log.error("waba.embedded_signup_token_exchange_failed", status=tok_resp.status_code, body=err_msg)
            raise HTTPException(status_code=502, detail=f"Meta token exchange failed: {err_msg}")
        access_token = tok_resp.json().get("access_token")
        if not access_token:
            raise HTTPException(status_code=502, detail="Meta did not return an access_token")

    webhook_subscribed = await _subscribe_waba_webhook(body.waba_id, access_token)

    await db.execute(
        text(
            "UPDATE tenants SET waba_phone_number_id = :pid, "
            "waba_business_account_id = :waba_id, "
            "waba_access_token_encrypted = :token, updated_at = :now WHERE id = :tid"
        ),
        {
            "tid": tenant_id,
            "pid": body.phone_number_id.strip(),
            "waba_id": body.waba_id.strip(),
            "token": encrypt_secret(access_token),
            "now": datetime.now(timezone.utc),
        },
    )
    await db.commit()
    return {
        "status": "connected",
        "waba_phone_number_id": body.phone_number_id.strip(),
        "waba_business_account_id": body.waba_id.strip(),
        "webhook_subscribed": webhook_subscribed,
    }


@router.get("/tenant/settings")
async def get_tenant_settings(
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        text("SELECT * FROM tenant_settings WHERE tenant_id = :tid"),
        {"tid": tenant_id},
    )
    row = result.first()
    if not row:
        return {}
    return _row_to_dict(row)


@router.put("/tenant/settings")
async def update_tenant_settings(
    body: TenantSettingsUpdate,
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    from app.billing.service import require_active_subscription, require_feature
    from app.exceptions import FeatureNotAvailableError, SubscriptionExpiredError

    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    if "deposit_mode" in updates and updates["deposit_mode"] not in ("auto", "razorpay", "upi_manual", "disabled"):
        raise HTTPException(status_code=400, detail="deposit_mode must be auto, razorpay, upi_manual, or disabled")

    try:
        await require_active_subscription(db, tenant_id)
        if updates.get("google_calendar_enabled") is True:
            await require_feature(db, tenant_id, "google_calendar_enabled")
        if (
            updates.get("razorpay_enabled") is True
            or updates.get("upi_id")
            or updates.get("deposit_mode") in ("razorpay", "upi_manual")
        ):
            await require_feature(db, tenant_id, "deposits_enabled")
    except SubscriptionExpiredError as exc:
        raise HTTPException(status_code=402, detail=str(exc))
    except FeatureNotAvailableError as exc:
        raise HTTPException(status_code=403, detail=str(exc))

    # Check if settings row exists
    result = await db.execute(
        text("SELECT id FROM tenant_settings WHERE tenant_id = :tid"),
        {"tid": tenant_id},
    )
    existing = result.first()

    if existing:
        set_clauses = ", ".join(f"{k} = :{k}" for k in updates)
        updates["tid"] = tenant_id
        await db.execute(
            text(f"UPDATE tenant_settings SET {set_clauses} WHERE tenant_id = :tid"),
            updates,
        )
    else:
        updates["id"] = uuid.uuid4()
        updates["tenant_id"] = tenant_id
        cols = ", ".join(updates.keys())
        vals = ", ".join(f":{k}" for k in updates.keys())
        await db.execute(
            text(f"INSERT INTO tenant_settings ({cols}) VALUES ({vals})"),
            updates,
        )

    await db.commit()

    result = await db.execute(
        text("SELECT * FROM tenant_settings WHERE tenant_id = :tid"),
        {"tid": tenant_id},
    )
    return _row_to_dict(result.first())


# ---------------------------------------------------------------------------
# SERVICES
# ---------------------------------------------------------------------------

@router.get("/services")
async def list_services(
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Return all active services in a tree structure (parents with nested children)."""
    result = await db.execute(
        text(
            "SELECT * FROM services WHERE tenant_id = :tid AND is_active = true "
            "ORDER BY sort_order, name"
        ),
        {"tid": tenant_id},
    )
    rows = [_row_to_dict(r) for r in result.fetchall()]

    # Build tree: top-level parents, then nest children
    by_id: dict[str, dict] = {}
    parents: list[dict] = []

    for row in rows:
        row["children"] = []
        by_id[row["id"]] = row

    for row in rows:
        pid = row.get("parent_service_id")
        if pid and pid in by_id:
            by_id[pid]["children"].append(row)
        else:
            parents.append(row)

    return parents


@router.post("/services", status_code=201)
async def create_service(
    body: ServiceCreate,
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    from app.billing.service import require_active_subscription, require_feature
    from app.exceptions import FeatureNotAvailableError, SubscriptionExpiredError

    try:
        await require_active_subscription(db, tenant_id)
        if body.requires_deposit:
            await require_feature(db, tenant_id, "deposits_enabled")
    except SubscriptionExpiredError as exc:
        raise HTTPException(status_code=402, detail=str(exc))
    except FeatureNotAvailableError as exc:
        raise HTTPException(status_code=403, detail=str(exc))

    service_id = uuid.uuid4()
    parent_id = _parse_uuid(body.parent_service_id, "parent_service_id") if body.parent_service_id else None

    # If parent specified, verify it belongs to this tenant
    if parent_id:
        check = await db.execute(
            text("SELECT id FROM services WHERE id = :id AND tenant_id = :tid AND is_active = true"),
            {"id": parent_id, "tid": tenant_id},
        )
        if not check.first():
            raise HTTPException(status_code=404, detail="Parent service not found")

    await db.execute(
        text(
            "INSERT INTO services "
            "(id, tenant_id, parent_service_id, name, description, image_url, "
            "duration_minutes, buffer_minutes, slot_increment_minutes, price_paise, requires_deposit, "
            "deposit_amount_paise, sort_order) "
            "VALUES (:id, :tid, :parent_id, :name, :description, :image_url, "
            ":duration, :buffer, :slot_inc, :price, :deposit_req, :deposit_amt, :sort_order)"
        ),
        {
            "id": service_id,
            "tid": tenant_id,
            "parent_id": parent_id,
            "name": body.name,
            "description": body.description,
            "image_url": body.image_url,
            "duration": body.duration_minutes,
            "buffer": body.buffer_minutes,
            "slot_inc": body.slot_increment_minutes,
            "price": body.price_paise,
            "deposit_req": body.requires_deposit,
            "deposit_amt": body.deposit_amount_paise,
            "sort_order": body.sort_order,
        },
    )
    await db.commit()

    result = await db.execute(
        text("SELECT * FROM services WHERE id = :id"), {"id": service_id}
    )
    return _row_to_dict(result.first())


@router.put("/services/reorder", response_model=MessageResponse)
async def reorder_services(
    body: ServiceReorderRequest,
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    for item in body.items:
        sid = _parse_uuid(item.id, "service id")
        await db.execute(
            text(
                "UPDATE services SET sort_order = :order "
                "WHERE id = :id AND tenant_id = :tid"
            ),
            {"order": item.sort_order, "id": sid, "tid": tenant_id},
        )
    await db.commit()
    return MessageResponse(message="Services reordered")


@router.get("/services/{service_id}")
async def get_service(
    service_id: str,
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    sid = _parse_uuid(service_id, "service_id")
    result = await db.execute(
        text("SELECT * FROM services WHERE id = :id AND tenant_id = :tid"),
        {"id": sid, "tid": tenant_id},
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Service not found")
    return _row_to_dict(row)


@router.put("/services/{service_id}")
async def update_service(
    service_id: str,
    body: ServiceUpdate,
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    from app.billing.service import require_active_subscription, require_feature
    from app.exceptions import FeatureNotAvailableError, SubscriptionExpiredError

    sid = _parse_uuid(service_id, "service_id")
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    try:
        await require_active_subscription(db, tenant_id)
        if updates.get("requires_deposit") is True:
            await require_feature(db, tenant_id, "deposits_enabled")
    except SubscriptionExpiredError as exc:
        raise HTTPException(status_code=402, detail=str(exc))
    except FeatureNotAvailableError as exc:
        raise HTTPException(status_code=403, detail=str(exc))

    if "parent_service_id" in updates:
        raw_pid = updates["parent_service_id"]
        if raw_pid:
            parsed_pid = _parse_uuid(raw_pid, "parent_service_id")
            if parsed_pid == sid:
                raise HTTPException(status_code=400, detail="Service cannot be its own parent")
            updates["parent_service_id"] = parsed_pid
        else:
            updates["parent_service_id"] = None

    set_clauses = ", ".join(f"{k} = :{k}" for k in updates)
    updates["id"] = sid
    updates["tid"] = tenant_id

    result = await db.execute(
        text(f"UPDATE services SET {set_clauses} WHERE id = :id AND tenant_id = :tid RETURNING id"),
        updates,
    )
    if not result.first():
        raise HTTPException(status_code=404, detail="Service not found")
    await db.commit()

    result = await db.execute(
        text("SELECT * FROM services WHERE id = :id"), {"id": sid}
    )
    return _row_to_dict(result.first())


@router.delete("/services/{service_id}", response_model=MessageResponse)
async def delete_service(
    service_id: str,
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    sid = _parse_uuid(service_id, "service_id")
    result = await db.execute(
        text(
            "UPDATE services SET is_active = false "
            "WHERE id = :id AND tenant_id = :tid RETURNING id"
        ),
        {"id": sid, "tid": tenant_id},
    )
    if not result.first():
        raise HTTPException(status_code=404, detail="Service not found")

    # Also soft-delete children
    await db.execute(
        text(
            "UPDATE services SET is_active = false "
            "WHERE parent_service_id = :pid AND tenant_id = :tid"
        ),
        {"pid": sid, "tid": tenant_id},
    )
    await db.commit()
    return MessageResponse(message="Service deleted")


# ---------------------------------------------------------------------------
# STAFF
# ---------------------------------------------------------------------------

@router.get("/staff")
async def list_staff(
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        text(
            "SELECT id, tenant_id, branch_id, name, role, bio, specialty, skills, "
            "working_hours, is_active, created_at "
            "FROM staff WHERE tenant_id = :tid AND is_active = true ORDER BY name"
        ),
        {"tid": tenant_id},
    )
    return [_row_to_dict(r) for r in result.fetchall()]


@router.post("/staff", status_code=201)
async def create_staff(
    body: StaffCreate,
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    from app.billing.service import enforce_plan_limit
    from app.exceptions import PlanLimitExceededError, SubscriptionExpiredError

    try:
        await enforce_plan_limit(db, tenant_id, "staff")
    except SubscriptionExpiredError as exc:
        raise HTTPException(status_code=402, detail=str(exc))
    except PlanLimitExceededError as exc:
        raise HTTPException(status_code=402, detail=f"Plan limit reached ({exc}). Upgrade your plan to add more team members.")

    staff_id = uuid.uuid4()
    branch_id = _parse_uuid(body.branch_id, "branch_id") if body.branch_id else None

    # If no branch specified, use primary branch
    if not branch_id:
        br = await db.execute(
            text("SELECT id FROM branches WHERE tenant_id = :tid AND is_primary = true LIMIT 1"),
            {"tid": tenant_id},
        )
        br_row = br.first()
        if br_row:
            branch_id = br_row.id

    # Normalize skills: accept list or dict
    skills_val = body.skills
    if isinstance(skills_val, list):
        skills_val = skills_val  # keep as list for JSON
    elif isinstance(skills_val, dict):
        skills_val = skills_val
    else:
        skills_val = None

    await db.execute(
        text(
            "INSERT INTO staff (id, tenant_id, branch_id, name, role, bio, specialty, skills, working_hours, is_active, created_at) "
            "VALUES (:id, :tid, :branch_id, :name, :role, :bio, :specialty, CAST(:skills AS jsonb), CAST(:hours AS jsonb), true, now())"
        ),
        {
            "id": staff_id,
            "tid": tenant_id,
            "branch_id": branch_id,
            "name": body.name,
            "role": body.role,
            "bio": body.bio,
            "specialty": body.specialty,
            "skills": _json_or_none(skills_val),
            "hours": _json_or_none(body.working_hours),
        },
    )
    await db.commit()

    result = await db.execute(
        text(
            "SELECT id, tenant_id, branch_id, name, role, bio, specialty, skills, "
            "working_hours, is_active, created_at FROM staff WHERE id = :id"
        ),
        {"id": staff_id},
    )
    return _row_to_dict(result.first())


@router.put("/staff/{staff_id}")
async def update_staff(
    staff_id: str,
    body: StaffUpdate,
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    sid = _parse_uuid(staff_id, "staff_id")
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    # Handle JSONB fields with explicit cast
    set_parts = []
    for k in updates:
        if k in ("skills", "working_hours"):
            set_parts.append(f"{k} = CAST(:{k} AS jsonb)")
            updates[k] = _json_or_none(updates[k])
        elif k == "branch_id":
            updates[k] = _parse_uuid(updates[k], "branch_id") if updates[k] else None
            set_parts.append(f"{k} = :{k}")
        else:
            set_parts.append(f"{k} = :{k}")

    set_clause = ", ".join(set_parts)
    updates["id"] = sid
    updates["tid"] = tenant_id

    result = await db.execute(
        text(f"UPDATE staff SET {set_clause} WHERE id = :id AND tenant_id = :tid RETURNING id"),
        updates,
    )
    if not result.first():
        raise HTTPException(status_code=404, detail="Staff member not found")
    await db.commit()

    result = await db.execute(
        text(
            "SELECT id, tenant_id, branch_id, name, role, bio, specialty, skills, "
            "working_hours, is_active, created_at FROM staff WHERE id = :id"
        ),
        {"id": sid},
    )
    return _row_to_dict(result.first())


@router.delete("/staff/{staff_id}", response_model=MessageResponse)
async def delete_staff(
    staff_id: str,
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    sid = _parse_uuid(staff_id, "staff_id")
    result = await db.execute(
        text(
            "UPDATE staff SET is_active = false "
            "WHERE id = :id AND tenant_id = :tid RETURNING id"
        ),
        {"id": sid, "tid": tenant_id},
    )
    if not result.first():
        raise HTTPException(status_code=404, detail="Staff member not found")
    await db.commit()
    return MessageResponse(message="Staff member deleted")


# ---------------------------------------------------------------------------
# BOOKINGS
# ---------------------------------------------------------------------------

@router.get("/bookings")
async def list_bookings(
    from_date: date | None = Query(None),
    to_date: date | None = Query(None),
    status: str | None = Query(None),
    staff_id: str | None = Query(None),
    customer_id: str | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    conditions = ["b.tenant_id = :tid"]
    params: dict[str, Any] = {"tid": tenant_id}

    if from_date:
        conditions.append("b.starts_at >= :from_date")
        params["from_date"] = datetime.combine(from_date, datetime.min.time(), tzinfo=timezone.utc)
    if to_date:
        conditions.append("b.starts_at::date <= :to_date")
        params["to_date"] = to_date
    if status:
        statuses = [s.strip() for s in status.split(",") if s.strip()]
        if len(statuses) == 1:
            conditions.append("b.status = :status")
            params["status"] = statuses[0]
        elif statuses:
            placeholders = ", ".join(f":st_{i}" for i in range(len(statuses)))
            conditions.append(f"b.status IN ({placeholders})")
            for i, s in enumerate(statuses):
                params[f"st_{i}"] = s
    if staff_id:
        conditions.append("b.staff_id = :staff_id")
        params["staff_id"] = _parse_uuid(staff_id, "staff_id")
    if customer_id:
        conditions.append("b.customer_id = :customer_id")
        params["customer_id"] = _parse_uuid(customer_id, "customer_id")

    where = " AND ".join(conditions)

    # Count
    count_result = await db.execute(
        text(f"SELECT count(*) FROM bookings b WHERE {where}"), params
    )
    total = count_result.scalar()

    # Paginated results with joined names
    offset = (page - 1) * per_page
    params["limit"] = per_page
    params["offset"] = offset

    result = await db.execute(
        text(
            f"SELECT b.*, "
            f"c.name AS customer_name, c.phone AS customer_phone, "
            f"s.name AS service_name, st.name AS staff_name "
            f"FROM bookings b "
            f"LEFT JOIN customers c ON c.id = b.customer_id "
            f"LEFT JOIN services s ON s.id = b.service_id "
            f"LEFT JOIN staff st ON st.id = b.staff_id "
            f"WHERE {where} "
            f"ORDER BY b.starts_at DESC "
            f"LIMIT :limit OFFSET :offset"
        ),
        params,
    )
    items = [_row_to_dict(r) for r in result.fetchall()]

    return PaginatedResponse(items=items, total=total, page=page, per_page=per_page)


@router.get("/bookings/{booking_id}")
async def get_booking(
    booking_id: str,
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    bid = _parse_uuid(booking_id, "booking_id")
    result = await db.execute(
        text(
            "SELECT b.*, "
            "c.name AS customer_name, c.phone AS customer_phone, "
            "s.name AS service_name, s.duration_minutes, s.price_paise AS service_price_paise, "
            "st.name AS staff_name "
            "FROM bookings b "
            "LEFT JOIN customers c ON c.id = b.customer_id "
            "LEFT JOIN services s ON s.id = b.service_id "
            "LEFT JOIN staff st ON st.id = b.staff_id "
            "WHERE b.id = :id AND b.tenant_id = :tid"
        ),
        {"id": bid, "tid": tenant_id},
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Booking not found")
    return _row_to_dict(row)


@router.post("/bookings", status_code=201)
async def create_manual_booking(
    body: BookingCreate,
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Create a walk-in or phone booking from the owner dashboard."""
    from datetime import timedelta

    from app.billing.service import enforce_plan_limit
    from app.bookings.repository import create_booking
    from app.exceptions import PlanLimitExceededError, SlotConflictError, SubscriptionExpiredError

    try:
        await enforce_plan_limit(db, tenant_id, "bookings")
    except SubscriptionExpiredError as exc:
        raise HTTPException(status_code=402, detail=str(exc))
    except PlanLimitExceededError as exc:
        raise HTTPException(status_code=402, detail=f"Monthly booking limit reached ({exc})")

    svc_id = _parse_uuid(body.service_id, "service_id")
    staff_id = _parse_uuid(body.staff_id, "staff_id") if body.staff_id else None
    branch_id = _parse_uuid(body.branch_id, "branch_id") if body.branch_id else None

    try:
        starts_at = datetime.fromisoformat(body.starts_at)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid starts_at ISO datetime")

    svc_res = await db.execute(
        text("SELECT id, duration_minutes, requires_deposit FROM services WHERE id = :sid AND tenant_id = :tid AND is_active = true"),
        {"sid": svc_id, "tid": tenant_id},
    )
    svc = svc_res.first()
    if not svc or svc.duration_minutes <= 0:
        raise HTTPException(status_code=404, detail="Service not found")

    ends_at = starts_at + timedelta(minutes=svc.duration_minutes)

    if not staff_id:
        st_res = await db.execute(
            text("SELECT id, branch_id FROM staff WHERE tenant_id = :tid AND is_active = true ORDER BY created_at LIMIT 1"),
            {"tid": tenant_id},
        )
        st = st_res.first()
        if not st:
            raise HTTPException(status_code=400, detail="No active staff available")
        staff_id = st.id
        if not branch_id:
            branch_id = st.branch_id

    if not branch_id:
        br_res = await db.execute(
            text("SELECT id FROM branches WHERE tenant_id = :tid AND is_primary = true LIMIT 1"),
            {"tid": tenant_id},
        )
        br = br_res.first()
        if not br:
            raise HTTPException(status_code=400, detail="No branch configured")
        branch_id = br.id

    # Normalize & upsert customer
    phone = body.customer_phone.strip().replace(" ", "").replace("-", "")
    if not phone.startswith("+"):
        phone = f"+91{phone}" if len(phone) == 10 and phone.isdigit() else f"+{phone}"

    cust_res = await db.execute(
        text("SELECT id FROM customers WHERE tenant_id = :tid AND phone = :phone"),
        {"tid": tenant_id, "phone": phone},
    )
    cust = cust_res.first()
    if not cust:
        cust_id = uuid.uuid4()
        await db.execute(
            text("INSERT INTO customers (id, tenant_id, phone, name, first_seen_at) VALUES (:id, :tid, :phone, :name, :now)"),
            {"id": cust_id, "tid": tenant_id, "phone": phone, "name": body.customer_name.strip(), "now": datetime.now(timezone.utc)},
        )
        await db.commit()
    else:
        cust_id = cust.id

    try:
        created = await create_booking(
            db=db,
            tenant_id=tenant_id,
            branch_id=branch_id,
            staff_id=staff_id,
            customer_id=cust_id,
            service_id=svc_id,
            starts_at=starts_at,
            ends_at=ends_at,
            requires_deposit=body.requires_deposit,
            reason=body.reason,
            notes=body.notes,
        )
    except SlotConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    return created


@router.put("/bookings/{booking_id}")
async def update_booking_details(
    booking_id: str,
    body: BookingNotesUpdate,
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Update visit reason and/or post-visit notes (doctor notes, stylist comments)."""
    bid = _parse_uuid(booking_id, "booking_id")
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    set_clauses = ", ".join(f"{k} = :{k}" for k in updates)
    updates["id"] = bid
    updates["tid"] = tenant_id
    updates["now"] = datetime.now(timezone.utc)

    res = await db.execute(
        text(f"UPDATE bookings SET {set_clauses}, updated_at = :now WHERE id = :id AND tenant_id = :tid RETURNING id"),
        updates,
    )
    if not res.first():
        raise HTTPException(status_code=404, detail="Booking not found")
    await db.commit()

    row = (await db.execute(text("SELECT * FROM bookings WHERE id = :id"), {"id": bid})).first()
    return _row_to_dict(row)


@router.post("/bookings/{booking_id}/confirm")
async def confirm_pending_booking(
    booking_id: str,
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Manually confirm a pending_deposit booking (e.g. cash/UPI deposit received)."""
    from app.bookings.models import BookingStatus
    from app.bookings.repository import update_booking_status as repo_update_status
    from app.exceptions import BookingNotFoundError, InvalidStateTransitionError

    bid = _parse_uuid(booking_id, "booking_id")
    try:
        await repo_update_status(db, tenant_id, bid, BookingStatus.CONFIRMED)
    except BookingNotFoundError:
        raise HTTPException(status_code=404, detail="Booking not found")
    except InvalidStateTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    row = (await db.execute(text("SELECT * FROM bookings WHERE id = :id"), {"id": bid})).first()
    return _row_to_dict(row)


async def _send_free_24h_thank_you(
    db: AsyncSession, tenant_id: uuid.UUID, booking_id: uuid.UUID
) -> dict[str, Any]:
    """Send a customized Thank-You + Google Review WhatsApp message ONLY if within Meta's free 24-hour
    customer-initiated service window (₹0 cost — never triggers paid templates).
    """
    row = (
        await db.execute(
            text("""
                SELECT b.id AS booking_id, b.customer_id,
                       c.name AS customer_name, c.phone AS customer_phone,
                       s.name AS service_name,
                       t.business_name, t.waba_phone_number_id,
                       COALESCE(ts.send_feedback_request, true) AS send_feedback_request,
                       ts.thank_you_message,
                       ts.google_review_url
                FROM bookings b
                JOIN customers c ON c.id = b.customer_id
                JOIN services s ON s.id = b.service_id
                JOIN tenants t ON t.id = b.tenant_id
                LEFT JOIN tenant_settings ts ON ts.tenant_id = t.id
                WHERE b.id = :bid AND b.tenant_id = :tid
                LIMIT 1
            """),
            {"bid": booking_id, "tid": tenant_id},
        )
    ).first()
    if not row:
        return {"sent": False, "status": "booking_not_found"}
    if not row.send_feedback_request:
        return {"sent": False, "status": "disabled"}
    if not row.waba_phone_number_id or not row.customer_phone:
        return {"sent": False, "status": "whatsapp_not_connected"}

    # Verify customer initiated a WhatsApp message within the last 23h55m (Meta free 24h window)
    win_row = (
        await db.execute(
            text("""
                SELECT MAX(created_at) AS last_in
                FROM messages
                WHERE tenant_id = :tid AND customer_id = :cid AND direction = 'inbound'
            """),
            {"tid": tenant_id, "cid": row.customer_id},
        )
    ).first()
    now = datetime.now(timezone.utc)
    last_in = win_row.last_in if win_row else None
    if not last_in or (now - last_in) > timedelta(hours=23, minutes=55):
        return {"sent": False, "status": "outside_24h_window"}

    cust_name = row.customer_name or "there"
    svc_name = row.service_name or "appointment"
    biz_name = row.business_name or "us"

    template = (row.thank_you_message or "").strip()
    if not template:
        template = (
            "Thank you for visiting *{business_name}*, {customer_name}! 🙏\n"
            "We hope you loved your *{service_name}*."
        )
    msg_text = (
        template.replace("{customer_name}", cust_name)
        .replace("{service_name}", svc_name)
        .replace("{business_name}", biz_name)
    )
    review_url = (row.google_review_url or "").strip()
    if review_url and review_url not in msg_text:
        msg_text += f"\n\n⭐ Loved your experience? Leave us a quick review:\n{review_url}"

    try:
        from app.messaging.whatsapp.client import send_text_message

        send_res = await send_text_message(
            phone_number_id=row.waba_phone_number_id,
            to=row.customer_phone.lstrip("+"),
            text_body=msg_text,
            tenant_id=str(tenant_id),
        )
        if send_res is None:
            return {"sent": False, "status": "whatsapp_send_failed", "preview": msg_text}
        return {"sent": True, "status": "sent_free_24h", "preview": msg_text}
    except Exception as exc:
        log.warning("booking.thank_you_send_failed", booking_id=str(booking_id), error=str(exc))
        return {"sent": False, "status": "whatsapp_send_failed", "preview": msg_text}


@router.put("/bookings/{booking_id}/status")
async def update_booking_status(
    booking_id: str,
    body: BookingStatusUpdate,
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    from app.bookings.repository import update_booking_status as repo_update_status
    from app.exceptions import BookingNotFoundError, InvalidStateTransitionError

    bid = _parse_uuid(booking_id, "booking_id")
    allowed = {"cancelled", "completed", "no_show"}
    if body.status not in allowed:
        raise HTTPException(
            status_code=400, detail=f"Status must be one of: {', '.join(sorted(allowed))}"
        )

    try:
        await repo_update_status(
            db, tenant_id, bid, body.status, cancellation_reason=body.cancellation_reason
        )
    except BookingNotFoundError:
        raise HTTPException(status_code=404, detail="Booking not found")
    except InvalidStateTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    log.info("booking.status_updated", booking_id=str(bid), new_status=body.status)

    thank_you_res = None
    if body.status == "completed":
        thank_you_res = await _send_free_24h_thank_you(db, tenant_id, bid)

    result = await db.execute(
        text("SELECT * FROM bookings WHERE id = :id"), {"id": bid}
    )
    out = _row_to_dict(result.first())
    if thank_you_res is not None:
        out["thank_you_sent"] = bool(thank_you_res.get("sent"))
        out["thank_you_status"] = thank_you_res.get("status")
    return out


# ---------------------------------------------------------------------------
# CUSTOMERS
# ---------------------------------------------------------------------------

@router.get("/customers")
async def list_customers(
    search: str | None = Query(None, description="Search by name or phone"),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    conditions = ["tenant_id = :tid"]
    params: dict[str, Any] = {"tid": tenant_id}

    if search:
        conditions.append("(name ILIKE :search OR phone ILIKE :search)")
        params["search"] = f"%{search}%"

    where = " AND ".join(conditions)

    count_result = await db.execute(
        text(f"SELECT count(*) FROM customers WHERE {where}"), params
    )
    total = count_result.scalar()

    offset = (page - 1) * per_page
    params["limit"] = per_page
    params["offset"] = offset

    result = await db.execute(
        text(
            f"SELECT * FROM customers WHERE {where} "
            f"ORDER BY last_visit_at DESC NULLS LAST, first_seen_at DESC "
            f"LIMIT :limit OFFSET :offset"
        ),
        params,
    )
    items = [_row_to_dict(r) for r in result.fetchall()]
    return PaginatedResponse(items=items, total=total, page=page, per_page=per_page)


@router.get("/customers/{customer_id}")
async def get_customer(
    customer_id: str,
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    cid = _parse_uuid(customer_id, "customer_id")

    # Customer details
    result = await db.execute(
        text("SELECT * FROM customers WHERE id = :id AND tenant_id = :tid"),
        {"id": cid, "tid": tenant_id},
    )
    customer = result.first()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")

    # Booking history
    bookings_result = await db.execute(
        text(
            "SELECT b.*, s.name AS service_name, st.name AS staff_name "
            "FROM bookings b "
            "LEFT JOIN services s ON s.id = b.service_id "
            "LEFT JOIN staff st ON st.id = b.staff_id "
            "WHERE b.customer_id = :cid AND b.tenant_id = :tid "
            "ORDER BY b.starts_at DESC LIMIT 50"
        ),
        {"cid": cid, "tid": tenant_id},
    )

    # Feedback
    feedback_result = await db.execute(
        text(
            "SELECT f.* FROM feedback f "
            "JOIN bookings b ON b.id = f.booking_id "
            "WHERE b.customer_id = :cid AND f.tenant_id = :tid "
            "ORDER BY f.created_at DESC LIMIT 20"
        ),
        {"cid": cid, "tid": tenant_id},
    )

    return {
        "customer": _row_to_dict(customer),
        "bookings": [_row_to_dict(r) for r in bookings_result.fetchall()],
        "feedback": [_row_to_dict(r) for r in feedback_result.fetchall()],
    }


# ---------------------------------------------------------------------------
# ANALYTICS
# ---------------------------------------------------------------------------

@router.get("/analytics/overview")
async def analytics_overview(
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Summary dashboard: today's bookings, this week's revenue, customer count,
    popular services, average rating."""
    params = {"tid": tenant_id}

    # Today's bookings
    today_result = await db.execute(
        text(
            "SELECT count(*) AS count, "
            "count(*) FILTER (WHERE status = 'confirmed') AS confirmed, "
            "count(*) FILTER (WHERE status = 'completed') AS completed, "
            "count(*) FILTER (WHERE status = 'cancelled') AS cancelled "
            "FROM bookings "
            "WHERE tenant_id = :tid AND starts_at::date = CURRENT_DATE"
        ),
        params,
    )
    today = _row_to_dict(today_result.first())

    # This week's revenue (captured payments)
    revenue_result = await db.execute(
        text(
            "SELECT COALESCE(SUM(amount_paise), 0) AS total_paise, count(*) AS payment_count "
            "FROM payments "
            "WHERE tenant_id = :tid AND status = 'captured' "
            "AND created_at >= date_trunc('week', CURRENT_DATE)"
        ),
        params,
    )
    week_revenue = _row_to_dict(revenue_result.first())

    # Total customers
    customer_count_result = await db.execute(
        text("SELECT count(*) AS total FROM customers WHERE tenant_id = :tid"),
        params,
    )
    customer_count = customer_count_result.scalar()

    # Top 5 services (by booking count, last 30 days)
    popular_result = await db.execute(
        text(
            "SELECT s.id, s.name, count(b.id) AS booking_count "
            "FROM bookings b "
            "JOIN services s ON s.id = b.service_id "
            "WHERE b.tenant_id = :tid AND b.starts_at >= CURRENT_DATE - interval '30 days' "
            "GROUP BY s.id, s.name ORDER BY booking_count DESC LIMIT 5"
        ),
        params,
    )
    popular_services = [_row_to_dict(r) for r in popular_result.fetchall()]

    # Average rating
    rating_result = await db.execute(
        text(
            "SELECT ROUND(AVG(rating)::numeric, 2) AS avg_rating, count(*) AS review_count "
            "FROM feedback WHERE tenant_id = :tid AND rating IS NOT NULL"
        ),
        params,
    )
    ratings = _row_to_dict(rating_result.first())

    return {
        "today_bookings": today,
        "week_revenue": week_revenue,
        "total_customers": customer_count,
        "popular_services": popular_services,
        "ratings": ratings,
    }


@router.get("/analytics/bookings")
async def analytics_bookings(
    from_date: date = Query(...),
    to_date: date = Query(...),
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Booking statistics by date range: daily counts and status breakdown."""
    params: dict[str, Any] = {"tid": tenant_id, "from": from_date, "to": to_date}

    # Daily counts
    daily_result = await db.execute(
        text(
            "SELECT starts_at::date AS day, count(*) AS total, "
            "count(*) FILTER (WHERE status = 'confirmed') AS confirmed, "
            "count(*) FILTER (WHERE status = 'completed') AS completed, "
            "count(*) FILTER (WHERE status = 'cancelled') AS cancelled, "
            "count(*) FILTER (WHERE status = 'no_show') AS no_show "
            "FROM bookings "
            "WHERE tenant_id = :tid AND starts_at::date >= :from AND starts_at::date <= :to "
            "GROUP BY starts_at::date ORDER BY day"
        ),
        params,
    )
    daily = [_row_to_dict(r) for r in daily_result.fetchall()]

    # Overall status breakdown
    status_result = await db.execute(
        text(
            "SELECT status, count(*) AS count "
            "FROM bookings "
            "WHERE tenant_id = :tid AND starts_at::date >= :from AND starts_at::date <= :to "
            "GROUP BY status"
        ),
        params,
    )
    status_breakdown = {r._mapping["status"]: r._mapping["count"] for r in status_result.fetchall()}

    # Total
    total = sum(status_breakdown.values())

    return {
        "from_date": from_date.isoformat(),
        "to_date": to_date.isoformat(),
        "total": total,
        "status_breakdown": status_breakdown,
        "daily": daily,
    }


@router.get("/analytics/revenue")
async def analytics_revenue(
    from_date: date = Query(...),
    to_date: date = Query(...),
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Revenue analytics: daily totals and per-service breakdown."""
    params: dict[str, Any] = {"tid": tenant_id, "from": from_date, "to": to_date}

    # Daily revenue
    daily_result = await db.execute(
        text(
            "SELECT p.created_at::date AS day, "
            "SUM(p.amount_paise) AS total_paise, count(*) AS payment_count "
            "FROM payments p "
            "WHERE p.tenant_id = :tid AND p.status = 'captured' "
            "AND p.created_at::date >= :from AND p.created_at::date <= :to "
            "GROUP BY p.created_at::date ORDER BY day"
        ),
        params,
    )
    daily = [_row_to_dict(r) for r in daily_result.fetchall()]

    # Revenue by service
    service_result = await db.execute(
        text(
            "SELECT s.id, s.name, SUM(p.amount_paise) AS total_paise, count(p.id) AS payment_count "
            "FROM payments p "
            "JOIN bookings b ON b.id = p.booking_id "
            "JOIN services s ON s.id = b.service_id "
            "WHERE p.tenant_id = :tid AND p.status = 'captured' "
            "AND p.created_at::date >= :from AND p.created_at::date <= :to "
            "GROUP BY s.id, s.name ORDER BY total_paise DESC"
        ),
        params,
    )
    by_service = [_row_to_dict(r) for r in service_result.fetchall()]

    # Period total
    total_paise = sum(d.get("total_paise", 0) or 0 for d in daily)

    return {
        "from_date": from_date.isoformat(),
        "to_date": to_date.isoformat(),
        "total_paise": total_paise,
        "daily": daily,
        "by_service": by_service,
    }


@router.get("/analytics/staff")
async def analytics_staff(
    from_date: date = Query(...),
    to_date: date = Query(...),
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Staff utilization: bookings per staff, completion rate, revenue generated."""
    params: dict[str, Any] = {"tid": tenant_id, "from": from_date, "to": to_date}

    result = await db.execute(
        text(
            "SELECT st.id, st.name, "
            "count(b.id) AS total_bookings, "
            "count(b.id) FILTER (WHERE b.status = 'completed') AS completed, "
            "count(b.id) FILTER (WHERE b.status = 'cancelled') AS cancelled, "
            "count(b.id) FILTER (WHERE b.status = 'no_show') AS no_show, "
            "COALESCE(SUM(p.amount_paise) FILTER (WHERE p.status = 'captured'), 0) AS revenue_paise "
            "FROM staff st "
            "LEFT JOIN bookings b ON b.staff_id = st.id "
            "  AND b.starts_at::date >= :from AND b.starts_at::date <= :to "
            "LEFT JOIN payments p ON p.booking_id = b.id "
            "WHERE st.tenant_id = :tid AND st.is_active = true "
            "GROUP BY st.id, st.name ORDER BY total_bookings DESC"
        ),
        params,
    )
    staff_metrics = []
    for r in result.fetchall():
        d = _row_to_dict(r)
        total = d.get("total_bookings", 0) or 0
        completed = d.get("completed", 0) or 0
        d["completion_rate"] = round(completed / total * 100, 1) if total > 0 else 0.0
        staff_metrics.append(d)

    return {
        "from_date": from_date.isoformat(),
        "to_date": to_date.isoformat(),
        "staff": staff_metrics,
    }


# ---------------------------------------------------------------------------
# BRANCHES
# ---------------------------------------------------------------------------

@router.get("/branches")
async def list_branches(
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        text("SELECT * FROM branches WHERE tenant_id = :tid ORDER BY is_primary DESC, name"),
        {"tid": tenant_id},
    )
    return [_row_to_dict(r) for r in result.fetchall()]


@router.post("/branches", status_code=201)
async def create_branch(
    body: BranchCreate,
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Add a new branch/location (enforces plan tier branch limits)."""
    from app.billing.service import enforce_plan_limit
    from app.exceptions import PlanLimitExceededError, SubscriptionExpiredError

    try:
        await enforce_plan_limit(db, tenant_id, "branches")
    except SubscriptionExpiredError as exc:
        raise HTTPException(status_code=402, detail=str(exc))
    except PlanLimitExceededError as exc:
        raise HTTPException(status_code=402, detail=f"Plan limit reached ({exc}). Upgrade to add more branches.")

    default_hours = {d: [{"open": "09:00", "close": "20:00"}] for d in ["mon", "tue", "wed", "thu", "fri", "sat"]}
    bid = uuid.uuid4()
    await db.execute(
        text("""
            INSERT INTO branches (id, tenant_id, name, address, is_primary, operating_hours, created_at)
            VALUES (:id, :tid, :name, :address, false, CAST(:hours AS jsonb), :now)
        """),
        {
            "id": bid,
            "tid": tenant_id,
            "name": body.name,
            "address": body.address,
            "hours": _json_or_none(body.operating_hours or default_hours),
            "now": datetime.now(timezone.utc),
        },
    )
    await db.commit()
    row = (await db.execute(text("SELECT * FROM branches WHERE id = :id"), {"id": bid})).first()
    return _row_to_dict(row)


@router.delete("/branches/{branch_id}", response_model=MessageResponse)
async def delete_branch(
    branch_id: str,
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    bid = _parse_uuid(branch_id, "branch_id")
    res = await db.execute(
        text("SELECT id, is_primary FROM branches WHERE id = :id AND tenant_id = :tid"),
        {"id": bid, "tid": tenant_id},
    )
    row = res.first()
    if not row:
        raise HTTPException(status_code=404, detail="Branch not found")
    if row.is_primary:
        raise HTTPException(status_code=400, detail="Cannot delete primary branch")

    await db.execute(text("DELETE FROM branches WHERE id = :id AND tenant_id = :tid"), {"id": bid, "tid": tenant_id})
    await db.commit()
    return MessageResponse(message="Branch deleted")


@router.put("/branches/{branch_id}")
async def update_branch(
    branch_id: str,
    body: BranchUpdate,
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    bid = _parse_uuid(branch_id, "branch_id")
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    set_parts = []
    for k in updates:
        if k == "operating_hours":
            set_parts.append(f"{k} = CAST(:{k} AS jsonb)")
            updates[k] = _json_or_none(updates[k])
        else:
            set_parts.append(f"{k} = :{k}")

    set_clause = ", ".join(set_parts)
    updates["id"] = bid
    updates["tid"] = tenant_id

    result = await db.execute(
        text(f"UPDATE branches SET {set_clause} WHERE id = :id AND tenant_id = :tid RETURNING id"),
        updates,
    )
    if not result.first():
        raise HTTPException(status_code=404, detail="Branch not found")
    await db.commit()

    result = await db.execute(
        text("SELECT * FROM branches WHERE id = :id"), {"id": bid}
    )
    return _row_to_dict(result.first())


# ---------------------------------------------------------------------------
# VERTICALS & ONBOARDING TEMPLATES
# ---------------------------------------------------------------------------

@router.get("/verticals")
async def get_verticals():
    """List all supported business verticals (salons, clinics, dental, spas, tutors, etc.)."""
    from app.verticals import list_verticals
    return list_verticals()


@router.post("/onboarding/apply-template", status_code=201)
async def apply_starter_template(
    body: ApplyTemplateRequest | None = None,
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Apply one-click starter services for the tenant's business vertical."""
    from app.verticals import apply_vertical_template

    btype = body.business_type if body and body.business_type else None
    if not btype:
        row = (await db.execute(text("SELECT business_type FROM tenants WHERE id = :id"), {"id": tenant_id})).first()
        btype = row.business_type if row else "salon"
    else:
        await db.execute(
            text("UPDATE tenants SET business_type = :bt, updated_at = :now WHERE id = :id"),
            {"bt": btype, "now": datetime.now(timezone.utc), "id": tenant_id},
        )
        await db.commit()

    created = await apply_vertical_template(db, tenant_id, btype)
    return {"business_type": btype, "created_count": len(created), "services": created}


# ---------------------------------------------------------------------------
# BILLING & PLANS
# ---------------------------------------------------------------------------

class BillingVerifyRequest(BaseModel):
    plan_tier: str
    billing_cycle: str = "monthly"
    razorpay_payment_id: str
    razorpay_signature: str
    razorpay_subscription_id: str | None = None
    razorpay_order_id: str | None = None


class CancelSubscriptionRequest(BaseModel):
    immediate: bool = False


@router.get("/billing/plans")
async def get_billing_plans():
    """List available SaaS subscription tiers."""
    from app.billing.service import list_plans
    return list_plans()


@router.get("/billing/subscription")
async def get_billing_subscription(
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Get tenant's active plan, lifecycle status, warnings, entitlements, and live usage."""
    from app.billing.service import ensure_trial_subscription, get_usage_summary

    await ensure_trial_subscription(db, tenant_id)
    await db.commit()
    return await get_usage_summary(db, tenant_id)


@router.post("/billing/checkout")
async def start_billing_checkout(
    body: PlanUpdateRequest,
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Create a Razorpay Subscription / Order checkout (or activate immediately in sandbox mode)."""
    from app.billing.service import create_subscription_checkout
    from app.exceptions import DowngradeConflictError

    try:
        return await create_subscription_checkout(
            db, tenant_id, body.plan_tier, body.billing_cycle, skip_trial=body.skip_trial
        )
    except DowngradeConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={"message": str(exc), "conflicts": exc.conflicts},
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/billing/verify-payment")
async def verify_billing_payment(
    body: BillingVerifyRequest,
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Verify Razorpay subscription/order checkout signature and activate plan."""
    from app.billing.service import verify_subscription_payment

    try:
        return await verify_subscription_payment(
            db=db,
            tenant_id=tenant_id,
            plan_tier=body.plan_tier,
            billing_cycle=body.billing_cycle,
            razorpay_payment_id=body.razorpay_payment_id,
            razorpay_signature=body.razorpay_signature,
            razorpay_subscription_id=body.razorpay_subscription_id,
            razorpay_order_id=body.razorpay_order_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/billing/cancel")
async def cancel_billing_subscription(
    body: CancelSubscriptionRequest | None = None,
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Cancel tenant subscription (defaults to end of current paid period)."""
    from app.billing.service import cancel_subscription

    try:
        return await cancel_subscription(
            db, tenant_id, immediate=(body.immediate if body else False)
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/billing/invoices")
async def get_billing_invoices(
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Return subscription payment/invoice history for the authenticated tenant."""
    from app.billing.service import list_invoices

    return await list_invoices(db, tenant_id)


@router.put("/billing/plan")
async def update_billing_plan(
    body: PlanUpdateRequest,
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Switch tenant subscription tier (enforces downgrade safety & payment gate in production)."""
    from app.billing.service import (
        PLANS,
        activate_subscription,
        is_razorpay_configured,
        validate_plan_change,
    )
    from app.config import settings as app_settings
    from app.exceptions import DowngradeConflictError

    if body.plan_tier not in PLANS:
        raise HTTPException(status_code=400, detail=f"Plan must be one of: {', '.join(PLANS.keys())}")
    if body.billing_cycle not in ("monthly", "annual"):
        raise HTTPException(status_code=400, detail="billing_cycle must be 'monthly' or 'annual'")

    try:
        await validate_plan_change(db, tenant_id, body.plan_tier)
    except DowngradeConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    # In production with Razorpay configured, require paid checkout via /api/billing/checkout
    if app_settings.environment == "production" and is_razorpay_configured():
        raise HTTPException(
            status_code=402,
            detail="Complete payment via POST /api/billing/checkout to change your production plan.",
        )

    return await activate_subscription(
        db,
        tenant_id=tenant_id,
        plan_tier=body.plan_tier,
        billing_cycle=body.billing_cycle,
        payment_provider="sandbox",
        record_invoice=True,
    )


class SimulatorRequest(BaseModel):
    mode: str = Field("customer", description="customer or owner")
    body: str = Field(..., min_length=1, max_length=1000)
    customer_phone: str = Field("+919876500111")
    customer_name: str = Field("Test Customer")
    conv_state: str | None = Field(None, description="Opaque JSON state for stateless serverless turns")


@router.post("/simulator/message")
async def dashboard_simulator_message(
    req: SimulatorRequest,
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Authenticated live WhatsApp simulator for the logged-in owner's workspace.

    Works in BOTH development and production (even without Redis) by persisting
    real bookings/customers in Postgres and round-tripping conversation state.
    """
    t_row = (
        await db.execute(
            text(
                "SELECT id, business_name, business_type, owner_phone, primary_language, timezone "
                "FROM tenants WHERE id = :tid LIMIT 1"
            ),
            {"tid": tenant_id},
        )
    ).first()
    if not t_row:
        raise HTTPException(status_code=404, detail="Tenant not found")

    tid_str = str(t_row.id)
    tz_name = t_row.timezone or "Asia/Kolkata"
    btype = t_row.business_type or "salon"
    lang = t_row.primary_language or "en"

    if req.mode == "owner":
        from app.owner_console.handler import handle_owner_message

        reply_text = await handle_owner_message(
            db, tid_str, req.body, t_row.business_name, tz_name=tz_name, business_type=btype
        )
        return {
            "mode": "owner",
            "type": "text",
            "body": reply_text,
            "footer": "",
            "buttons": [],
            "button_text": "",
            "sections": [],
            "conv_state": None,
        }

    # Customer simulation mode
    from app.conversation.menu_flow import handle_customer_message
    from app.conversation.state import ConversationState, clear_state, get_state, set_state

    phone = req.customer_phone.strip() or "+919876500111"

    # Seed conversation state passed from client (keeps multi-turn flows working across Vercel lambdas)
    if req.conv_state:
        try:
            restored = ConversationState.from_json(req.conv_state)
            restored.tenant_id = tid_str
            await set_state(tid_str, phone, restored)
        except Exception:
            await clear_state(tid_str, phone)
    else:
        await clear_state(tid_str, phone)

    # Upsert simulated customer
    cust_row = (
        await db.execute(
            text("SELECT id, name, preferred_language FROM customers WHERE tenant_id = :tid AND phone = :phone"),
            {"tid": tenant_id, "phone": phone},
        )
    ).first()
    if not cust_row:
        cust_id = uuid.uuid4()
        await db.execute(
            text(
                "INSERT INTO customers (id, tenant_id, phone, name, first_seen_at) "
                "VALUES (:id, :tid, :phone, :name, :now)"
            ),
            {"id": cust_id, "tid": tenant_id, "phone": phone, "name": req.customer_name, "now": datetime.now(timezone.utc)},
        )
        await db.commit()
        cust_name = req.customer_name
        cust_lang = lang
    else:
        cust_id = cust_row.id
        cust_name = cust_row.name or req.customer_name
        cust_lang = cust_row.preferred_language or lang

    reply = await handle_customer_message(
        db=db,
        tenant_id=tid_str,
        customer_id=str(cust_id),
        customer_phone=phone,
        customer_name=cust_name,
        body=req.body,
        business_name=t_row.business_name,
        tz_name=tz_name,
        business_type=btype,
        lang=cust_lang,
    )

    next_state = await get_state(tid_str, phone)
    next_state_json = next_state.to_json() if next_state.step != "idle" else None

    rtype = getattr(reply, "type", "text")
    if rtype == "buttons":
        return {
            "mode": "customer",
            "type": "buttons",
            "body": reply.body,
            "footer": getattr(reply, "footer", ""),
            "buttons": reply.buttons,
            "button_text": "",
            "sections": [],
            "conv_state": next_state_json,
        }
    if rtype == "list":
        return {
            "mode": "customer",
            "type": "list",
            "body": reply.body,
            "footer": getattr(reply, "footer", ""),
            "buttons": [],
            "button_text": reply.button_text,
            "sections": reply.sections,
            "conv_state": next_state_json,
        }
    return {
        "mode": "customer",
        "type": "text",
        "body": getattr(reply, "text", str(reply)),
        "footer": "",
        "buttons": [],
        "button_text": "",
        "sections": [],
        "conv_state": next_state_json,
    }


class SupportRequestPayload(BaseModel):
    topic: str = Field(..., min_length=2, max_length=120)
    contact_phone: str | None = Field(None, max_length=30)
    preferred_time: str | None = Field(None, max_length=80)
    notes: str | None = Field(None, max_length=1000)


@router.get("/tenant/whatsapp-setup-info")
async def get_whatsapp_setup_info(
    request: Request,
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Return exact Webhook Callback URL, Verify Token, and connection status for self-serve Meta setup."""
    from app.config import settings as app_settings

    row = (
        await db.execute(
            text(
                "SELECT business_name, slug, waba_phone_number_id, waba_business_account_id "
                "FROM tenants WHERE id = :tid"
            ),
            {"tid": tenant_id},
        )
    ).first()
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme or "https"
    host = request.headers.get("host") or app_settings.base_domain
    webhook_url = f"{scheme}://{host}/webhooks/whatsapp"
    return {
        "webhook_url": webhook_url,
        "verify_token": app_settings.meta_webhook_verify_token,
        "is_connected": bool(row and row.waba_phone_number_id),
        "waba_business_account_id": row.waba_business_account_id if row else None,
        "meta_app_id": app_settings.meta_app_id or None,
        "meta_config_id": app_settings.meta_config_id or None,
        "embedded_signup_enabled": bool(app_settings.meta_app_id and app_settings.meta_config_id),
        "business_name": row.business_name if row else "",
        "slug": row.slug if row else "",
    }


@router.post("/support/request", status_code=201)
async def create_support_request(
    body: SupportRequestPayload,
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Submit a Done-For-You setup or customer support request."""
    row = (
        await db.execute(
            text("SELECT business_name, owner_name, owner_phone, owner_email, slug FROM tenants WHERE id = :tid"),
            {"tid": tenant_id},
        )
    ).first()
    ticket_id = f"SUP-{str(uuid.uuid4())[:6].upper()}"
    log.info(
        "support.request_created",
        ticket_id=ticket_id,
        tenant_id=str(tenant_id),
        business_name=row.business_name if row else "",
        owner_phone=body.contact_phone or (row.owner_phone if row else ""),
        owner_email=row.owner_email if row else "",
        topic=body.topic,
        preferred_time=body.preferred_time,
        notes=body.notes,
    )
    return {
        "ticket_id": ticket_id,
        "status": "received",
        "message": f"Support ticket {ticket_id} created! Our team will reach out shortly to assist you.",
    }


# ---------------------------------------------------------------------------
# INTEGRATIONS (GOOGLE CALENDAR)
# ---------------------------------------------------------------------------

@router.get("/integrations/google-calendar/auth-url")
async def get_google_calendar_auth_url(
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Return Google Calendar OAuth consent URL (gated by google_calendar_enabled entitlement)."""
    from app.billing.service import require_feature
    from app.calendar_sync.service import build_oauth_url, is_google_calendar_configured
    from app.exceptions import FeatureNotAvailableError, SubscriptionExpiredError

    try:
        await require_feature(db, tenant_id, "google_calendar_enabled")
    except SubscriptionExpiredError as exc:
        raise HTTPException(status_code=402, detail=str(exc))
    except FeatureNotAvailableError as exc:
        raise HTTPException(status_code=403, detail=str(exc))

    configured = is_google_calendar_configured()
    url = build_oauth_url(state=str(tenant_id)) if configured else None
    return {"configured": configured, "auth_url": url}


