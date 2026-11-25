"""Payment Integration — Razorpay order creation, webhook handling, and verification.

Endpoints:
- POST /payments/create-order       — create Razorpay order + DB record
- POST /payments/webhook            — Razorpay webhook (signature-verified)
- POST /payments/verify             — manual client-side verification
- GET  /payments/{booking_id}/link  — generate payment link for WhatsApp
"""
from __future__ import annotations

import hashlib
import hmac
import urllib.parse
import uuid
from datetime import datetime, timezone

import razorpay
import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.dashboard.auth import get_current_tenant
from app.database import get_db

logger = structlog.get_logger()

router = APIRouter(prefix="/payments", tags=["payments"])

# ---------------------------------------------------------------------------
# Razorpay client (lazy singleton)
# ---------------------------------------------------------------------------
_razorpay_client: razorpay.Client | None = None


def _get_razorpay_client() -> razorpay.Client:
    global _razorpay_client
    if _razorpay_client is None:
        _razorpay_client = razorpay.Client(
            auth=(settings.razorpay_key_id, settings.razorpay_key_secret)
        )
    return _razorpay_client


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class CreateOrderRequest(BaseModel):
    booking_id: str
    amount_paise: int
    tenant_id: str | None = None


class CreateOrderResponse(BaseModel):
    payment_id: str
    razorpay_order_id: str
    amount_paise: int
    razorpay_key_id: str


class VerifyRequest(BaseModel):
    razorpay_payment_id: str
    razorpay_order_id: str
    razorpay_signature: str
    tenant_id: str | None = None


class VerifyResponse(BaseModel):
    verified: bool
    payment_status: str


class PaymentLinkResponse(BaseModel):
    url: str
    amount_paise: int


# ---------------------------------------------------------------------------
# Core helpers (also usable outside the router)
# ---------------------------------------------------------------------------

async def create_razorpay_order(
    db: AsyncSession,
    tenant_id: str,
    booking_id: str,
    amount_paise: int,
) -> dict:
    """Create a Razorpay order via API and persist a Payment row.

    Returns dict with payment_id, razorpay_order_id, amount_paise, razorpay_key_id.
    """
    client = _get_razorpay_client()
    order_data = client.order.create(
        {
            "amount": amount_paise,
            "currency": "INR",
            "receipt": f"bk_{booking_id[:8]}",
            "notes": {"tenant_id": tenant_id, "booking_id": booking_id},
        }
    )

    razorpay_order_id = order_data["id"]
    payment_id = uuid.uuid4()

    await db.execute(
        text("""
            INSERT INTO payments (id, tenant_id, booking_id, razorpay_order_id, amount_paise, status, created_at)
            VALUES (:id, :tid, :bid, :roid, :amt, 'created', :now)
        """),
        {
            "id": payment_id,
            "tid": tenant_id,
            "bid": booking_id,
            "roid": razorpay_order_id,
            "amt": amount_paise,
            "now": datetime.now(timezone.utc),
        },
    )
    await db.commit()

    logger.info(
        "razorpay_order_created",
        tenant_id=tenant_id,
        booking_id=booking_id,
        razorpay_order_id=razorpay_order_id,
        amount_paise=amount_paise,
    )

    return {
        "payment_id": str(payment_id),
        "razorpay_order_id": razorpay_order_id,
        "amount_paise": amount_paise,
        "razorpay_key_id": settings.razorpay_key_id,
    }


async def verify_payment(
    db: AsyncSession,
    tenant_id: str,
    razorpay_payment_id: str,
    razorpay_order_id: str,
    razorpay_signature: str,
) -> dict:
    """Verify Razorpay payment signature and update payment + booking status.

    Returns dict with verified (bool) and payment_status.
    """
    # Verify the signature
    message = f"{razorpay_order_id}|{razorpay_payment_id}"
    expected = hmac.new(
        settings.razorpay_key_secret.encode(),
        message.encode(),
        hashlib.sha256,
    ).hexdigest()

    verified = hmac.compare_digest(expected, razorpay_signature)

    if verified:
        new_status = "captured"
    else:
        new_status = "failed"
        logger.warning(
            "payment_signature_mismatch",
            tenant_id=tenant_id,
            razorpay_order_id=razorpay_order_id,
        )

    # Update payment record
    await db.execute(
        text("""
            UPDATE payments
            SET razorpay_payment_id = :rpid, status = :status
            WHERE razorpay_order_id = :roid AND tenant_id = :tid
        """),
        {
            "rpid": razorpay_payment_id,
            "status": new_status,
            "roid": razorpay_order_id,
            "tid": tenant_id,
        },
    )

    # If captured, confirm the booking
    if verified:
        await _confirm_booking_if_deposit_paid(db, tenant_id, razorpay_order_id)

    await db.commit()

    logger.info(
        "payment_verified",
        tenant_id=tenant_id,
        razorpay_order_id=razorpay_order_id,
        verified=verified,
        status=new_status,
    )

    return {"verified": verified, "payment_status": new_status}


def generate_payment_link(
    razorpay_order_id: str,
    amount_paise: int,
    business_name: str,
) -> str:
    """Generate a Razorpay hosted checkout URL suitable for WhatsApp."""
    params = urllib.parse.urlencode(
        {
            "order_id": razorpay_order_id,
            "key_id": settings.razorpay_key_id,
            "name": business_name,
            "amount": amount_paise,
        }
    )
    return f"https://rzp.io/rzp/checkout?{params}"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _confirm_booking_if_deposit_paid(
    db: AsyncSession,
    tenant_id: str,
    razorpay_order_id: str,
) -> None:
    """If the payment is for a booking deposit, confirm the booking."""
    result = await db.execute(
        text("""
            SELECT p.booking_id, p.id as payment_id
            FROM payments p
            WHERE p.razorpay_order_id = :roid AND p.tenant_id = :tid
            LIMIT 1
        """),
        {"roid": razorpay_order_id, "tid": tenant_id},
    )
    row = result.first()
    if not row or not row.booking_id:
        return

    # Only confirm if currently pending deposit
    await db.execute(
        text("""
            UPDATE bookings
            SET status = 'confirmed', deposit_payment_id = :pid, updated_at = :now
            WHERE id = :bid AND tenant_id = :tid AND status = 'pending_deposit'
        """),
        {
            "pid": row.payment_id,
            "bid": row.booking_id,
            "tid": tenant_id,
            "now": datetime.now(timezone.utc),
        },
    )

    logger.info(
        "booking_confirmed_via_payment",
        booking_id=str(row.booking_id),
        tenant_id=tenant_id,
    )


def _verify_webhook_signature(body: bytes, signature: str) -> bool:
    """Verify Razorpay webhook signature using the webhook secret."""
    if not settings.razorpay_webhook_secret:
        if settings.environment == "development":
            return True
        return False
    expected = hmac.new(
        settings.razorpay_webhook_secret.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/create-order", response_model=CreateOrderResponse)
async def create_order_endpoint(
    req: CreateOrderRequest,
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Create a Razorpay order for a booking deposit (scoped to authenticated tenant)."""
    # Verify booking belongs to tenant
    check = await db.execute(
        text("SELECT id FROM bookings WHERE id = :bid AND tenant_id = :tid"),
        {"bid": req.booking_id, "tid": tenant_id},
    )
    if not check.first():
        raise HTTPException(status_code=404, detail="Booking not found")

    try:
        result = await create_razorpay_order(
            db,
            tenant_id=str(tenant_id),
            booking_id=req.booking_id,
            amount_paise=req.amount_paise,
        )
        return CreateOrderResponse(**result)
    except Exception as e:
        logger.error("create_order_failed", error=str(e), tenant_id=str(tenant_id))
        raise HTTPException(status_code=502, detail="Failed to create Razorpay order") from e


@router.post("/webhook")
async def razorpay_webhook(
    request: Request,
    x_razorpay_signature: str = Header(..., alias="X-Razorpay-Signature"),
    db: AsyncSession = Depends(get_db),
):
    """Razorpay webhook handler.

    Verifies the X-Razorpay-Signature header, then processes the event.
    Handles: payment.captured, payment.failed.
    """
    body = await request.body()

    if not _verify_webhook_signature(body, x_razorpay_signature):
        logger.warning("webhook_signature_invalid")
        raise HTTPException(status_code=400, detail="Invalid signature")

    payload = await request.json()
    event = payload.get("event", "")
    payment_entity = payload.get("payload", {}).get("payment", {}).get("entity", {})

    razorpay_order_id = payment_entity.get("order_id", "")
    razorpay_payment_id = payment_entity.get("id", "")

    logger.info("webhook_received", event=event, razorpay_order_id=razorpay_order_id)

    if event == "payment.captured":
        # Update payment status to captured
        result = await db.execute(
            text("""
                UPDATE payments
                SET razorpay_payment_id = :rpid, status = 'captured'
                WHERE razorpay_order_id = :roid
                RETURNING tenant_id
            """),
            {"rpid": razorpay_payment_id, "roid": razorpay_order_id},
        )
        row = result.first()
        if row:
            await _confirm_booking_if_deposit_paid(db, str(row.tenant_id), razorpay_order_id)
        await db.commit()

    elif event == "payment.failed":
        await db.execute(
            text("""
                UPDATE payments
                SET razorpay_payment_id = :rpid, status = 'failed'
                WHERE razorpay_order_id = :roid
            """),
            {"rpid": razorpay_payment_id, "roid": razorpay_order_id},
        )
        await db.commit()

    else:
        logger.debug("webhook_event_ignored", event=event)

    return {"status": "ok"}


@router.post("/verify", response_model=VerifyResponse)
async def verify_payment_endpoint(
    req: VerifyRequest,
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Client-side payment verification after Razorpay checkout."""
    result = await verify_payment(
        db,
        tenant_id=str(tenant_id),
        razorpay_payment_id=req.razorpay_payment_id,
        razorpay_order_id=req.razorpay_order_id,
        razorpay_signature=req.razorpay_signature,
    )
    return VerifyResponse(**result)


@router.get("/{booking_id}/link", response_model=PaymentLinkResponse)
async def get_payment_link(
    booking_id: str,
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Get a tappable payment link for a booking (scoped to authenticated tenant)."""
    result = await db.execute(
        text("""
            SELECT p.razorpay_order_id, p.amount_paise, t.business_name
            FROM payments p
            JOIN bookings b ON p.booking_id = b.id
            JOIN tenants t ON b.tenant_id = t.id
            WHERE p.booking_id = :bid AND b.tenant_id = :tid AND p.status = 'created'
            ORDER BY p.created_at DESC
            LIMIT 1
        """),
        {"bid": booking_id, "tid": tenant_id},
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="No pending payment found for this booking")

    url = generate_payment_link(
        razorpay_order_id=row.razorpay_order_id,
        amount_paise=row.amount_paise,
        business_name=row.business_name,
    )
    return PaymentLinkResponse(url=url, amount_paise=row.amount_paise)
