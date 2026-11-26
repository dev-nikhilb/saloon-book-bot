"""Payment Integration — Booking Deposits (Razorpay Payment Links + Direct UPI/UTR) & SaaS Webhooks.

Endpoints:
- POST /payments/create-order                — create Razorpay order + DB record (deposits_enabled gated)
- POST /payments/{booking_id}/whatsapp-link  — create WhatsApp Razorpay Payment Link or UPI link
- POST /payments/{booking_id}/check-status   — live Razorpay Payment Link poll / DB status check
- POST /payments/{booking_id}/confirm-manual — owner confirms manual UPI/UTR deposit -> confirms booking
- POST /payments/{booking_id}/reject-manual  — owner rejects manual deposit -> releases slot
- GET  /payments/pending                     — list pending & UTR-submitted deposits for dashboard
- POST /payments/webhook                     — Razorpay webhook (booking deposits + SaaS subscriptions)
- POST /payments/verify                      — client-side Razorpay signature verification
- GET  /payments/{booking_id}/link           — retrieve payment link for a pending booking
"""
from __future__ import annotations

import hashlib
import hmac
import re
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone

import razorpay
import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.service import (
    activate_subscription,
    mark_subscription_past_due,
    require_feature,
)
from app.config import settings
from app.dashboard.auth import get_current_tenant
from app.database import get_db
from app.exceptions import FeatureNotAvailableError, SubscriptionExpiredError

logger = structlog.get_logger()

router = APIRouter(prefix="/payments", tags=["payments"])

_razorpay_client: razorpay.Client | None = None
UTR_RE = re.compile(r"^[A-Za-z0-9]{6,24}$")


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
    payment_method: str = "razorpay"


class ManualActionRequest(BaseModel):
    reason: str | None = None


# ---------------------------------------------------------------------------
# WhatsApp Notification Helper (best-effort)
# ---------------------------------------------------------------------------

async def _send_whatsapp_notice(
    db: AsyncSession,
    tenant_id,
    phone: str | None,
    message: str,
    buttons: list[dict[str, str]] | None = None,
) -> bool:
    if not phone:
        return False
    try:
        t_row = (
            await db.execute(
                text("SELECT waba_phone_number_id FROM tenants WHERE id = :tid"),
                {"tid": tenant_id},
            )
        ).first()
        if not t_row or not t_row.waba_phone_number_id:
            return False
        from app.messaging.whatsapp.client import ReplyButton, send_reply_buttons, send_text_message

        to_num = phone.lstrip("+")
        if buttons:
            res = await send_reply_buttons(
                phone_number_id=t_row.waba_phone_number_id,
                to=to_num,
                body=message,
                buttons=[ReplyButton(id=b["id"], title=b["title"][:20]) for b in buttons[:3]],
                tenant_id=str(tenant_id),
            )
        else:
            res = await send_text_message(
                phone_number_id=t_row.waba_phone_number_id,
                to=to_num,
                text_body=message,
                tenant_id=str(tenant_id),
            )
        return res is not None
    except Exception as exc:
        logger.warning("payment_whatsapp_notice_failed", error=str(exc))
        return False


# ---------------------------------------------------------------------------
# Core helpers (used by WhatsApp bot, public booking, owner console & router)
# ---------------------------------------------------------------------------

def build_upi_deep_link(
    upi_id: str,
    business_name: str,
    amount_paise: int,
    booking_ref: str,
) -> str:
    """Build a standard NPCI UPI deep link (works with GPay, PhonePe, Paytm, BHIM)."""
    rupees = f"{amount_paise / 100:.2f}"
    params = urllib.parse.urlencode(
        {
            "pa": upi_id.strip(),
            "pn": business_name.strip()[:40],
            "am": rupees,
            "cu": "INR",
            "tn": f"Booking {booking_ref.upper()}",
        }
    )
    return f"upi://pay?{params}"


async def create_razorpay_order(
    db: AsyncSession,
    tenant_id: str,
    booking_id: str,
    amount_paise: int,
    hold_minutes: int = 15,
) -> dict:
    """Create a Razorpay order via API and persist a Payment row."""
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
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=max(5, hold_minutes))

    await db.execute(
        text("""
            INSERT INTO payments (
                id, tenant_id, booking_id, payment_method, razorpay_order_id,
                amount_paise, status, expires_at, created_at
            ) VALUES (
                :id, :tid, :bid, 'razorpay', :roid, :amt, 'created', :exp, :now
            )
        """),
        {
            "id": payment_id,
            "tid": tenant_id,
            "bid": booking_id,
            "roid": razorpay_order_id,
            "amt": amount_paise,
            "exp": expires_at,
            "now": now,
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


async def create_whatsapp_deposit_payment(
    db: AsyncSession,
    tenant_id: str,
    booking_id: str,
    amount_paise: int,
    business_name: str,
    customer_name: str = "",
    customer_phone: str = "",
    deposit_mode: str = "auto",
    upi_id: str | None = None,
    hold_minutes: int = 15,
) -> dict | None:
    """Create the best available deposit payment record + tappable URL for WhatsApp/Web.

    Supports two real-world Indian payment modes:
      1. Razorpay Payment Link API (`client.payment_link.create`) -> zero-touch webhook verification
      2. Direct UPI deep link (`upi://pay?...`) -> 0% MDR + customer UTR / owner 1-tap verification
    """
    if deposit_mode == "disabled" or amount_paise <= 0:
        return None

    has_rzp = bool(settings.razorpay_key_id and settings.razorpay_key_secret)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=max(5, hold_minutes))
    ref = booking_id[:8].upper()

    # Mode 1: Automated Razorpay Payment Link
    if deposit_mode == "razorpay" or (deposit_mode == "auto" and has_rzp):
        if has_rzp:
            client = _get_razorpay_client()
            try:
                plink = client.payment_link.create(
                    {
                        "amount": amount_paise,
                        "currency": "INR",
                        "accept_partial": False,
                        "expire_by": int(expires_at.timestamp()),
                        "reference_id": f"bk_{booking_id[:8]}",
                        "description": f"Deposit for {business_name} (#{ref})",
                        "customer": {
                            "name": customer_name or "Customer",
                            "contact": customer_phone or "",
                        },
                        "notify": {"sms": False, "email": False},
                        "notes": {
                            "tenant_id": str(tenant_id),
                            "booking_id": str(booking_id),
                            "purpose": "booking_deposit",
                        },
                    }
                )
                payment_id = uuid.uuid4()
                short_url = plink.get("short_url") or plink.get("url")
                plink_id = plink.get("id")
                await db.execute(
                    text("""
                        INSERT INTO payments (
                            id, tenant_id, booking_id, payment_method,
                            razorpay_payment_link_id, payment_url,
                            amount_paise, status, expires_at, created_at
                        ) VALUES (
                            :id, :tid, :bid, 'razorpay_link',
                            :plid, :url, :amt, 'created', :exp, :now
                        )
                    """),
                    {
                        "id": payment_id,
                        "tid": tenant_id,
                        "bid": booking_id,
                        "plid": plink_id,
                        "url": short_url,
                        "amt": amount_paise,
                        "exp": expires_at,
                        "now": now,
                    },
                )
                await db.commit()
                return {
                    "payment_id": str(payment_id),
                    "payment_method": "razorpay_link",
                    "razorpay_payment_link_id": plink_id,
                    "url": short_url,
                    "amount_paise": amount_paise,
                    "expires_at": expires_at.isoformat(),
                }
            except Exception as exc:
                logger.warning("razorpay_payment_link_fallback_to_order", error=str(exc))
                order = await create_razorpay_order(
                    db, str(tenant_id), str(booking_id), amount_paise, hold_minutes=hold_minutes
                )
                url = generate_payment_link(order["razorpay_order_id"], amount_paise, business_name)
                await db.execute(
                    text("UPDATE payments SET payment_url = :url WHERE id = :pid"),
                    {"url": url, "pid": order["payment_id"]},
                )
                await db.commit()
                return {
                    "payment_id": order["payment_id"],
                    "payment_method": "razorpay",
                    "razorpay_order_id": order["razorpay_order_id"],
                    "url": url,
                    "amount_paise": amount_paise,
                    "expires_at": expires_at.isoformat(),
                }

    # Mode 2: Zero-MDR Direct UPI Deep Link + Customer UTR / Owner 1-Tap Verification
    if upi_id and deposit_mode != "disabled":
        upi_url = build_upi_deep_link(upi_id, business_name, amount_paise, ref)
        payment_id = uuid.uuid4()
        await db.execute(
            text("""
                INSERT INTO payments (
                    id, tenant_id, booking_id, payment_method, payment_url,
                    amount_paise, status, expires_at, created_at
                ) VALUES (
                    :id, :tid, :bid, 'upi_manual', :url, :amt, 'created', :exp, :now
                )
            """),
            {
                "id": payment_id,
                "tid": tenant_id,
                "bid": booking_id,
                "url": upi_url,
                "amt": amount_paise,
                "exp": expires_at,
                "now": now,
            },
        )
        await db.commit()
        return {
            "payment_id": str(payment_id),
            "payment_method": "upi_manual",
            "upi_id": upi_id.strip(),
            "url": upi_url,
            "amount_paise": amount_paise,
            "expires_at": expires_at.isoformat(),
        }

    return None


async def submit_utr_reference(
    db: AsyncSession,
    tenant_id: str,
    booking_ref: str,
    utr_reference: str,
) -> dict:
    """Record a customer's UPI UTR/reference number and ping the owner for 1-tap verification."""
    cleaned_utr = re.sub(r"[^A-Za-z0-9]", "", utr_reference.strip())
    if not UTR_RE.match(cleaned_utr):
        raise ValueError("Please send a valid UPI Reference / UTR number (6–24 digits/letters).")

    row = (
        await db.execute(
            text("""
                SELECT p.id AS payment_id, p.amount_paise, b.id AS booking_id, b.status AS booking_status,
                       s.name AS service_name, c.name AS customer_name, c.phone AS customer_phone,
                       t.owner_phone, t.business_name
                FROM bookings b
                JOIN services s ON s.id = b.service_id
                JOIN customers c ON c.id = b.customer_id
                JOIN tenants t ON t.id = b.tenant_id
                LEFT JOIN payments p ON p.booking_id = b.id
                WHERE b.tenant_id = :tid
                  AND b.id::text LIKE :ref
                  AND b.status = 'pending_deposit'
                ORDER BY p.created_at DESC NULLS LAST
                LIMIT 1
            """),
            {"tid": tenant_id, "ref": f"{booking_ref.lower().strip()}%"},
        )
    ).first()
    if not row:
        raise ValueError(f"No pending-deposit booking found matching '{booking_ref}'.")

    now = datetime.now(timezone.utc)
    hold_ext = now + timedelta(hours=24)

    if row.payment_id:
        await db.execute(
            text("""
                UPDATE payments
                SET utr_reference = :utr, status = 'submitted_utr', expires_at = :exp
                WHERE id = :pid
            """),
            {"utr": cleaned_utr, "exp": hold_ext, "pid": row.payment_id},
        )
        payment_id = row.payment_id
        amount_paise = row.amount_paise
    else:
        # Create payment row if service required deposit without pre-created payment row
        svc_amt = (
            await db.execute(
                text("SELECT deposit_amount_paise FROM services s JOIN bookings b ON b.service_id = s.id WHERE b.id = :bid"),
                {"bid": row.booking_id},
            )
        ).scalar() or 0
        payment_id = uuid.uuid4()
        amount_paise = svc_amt
        await db.execute(
            text("""
                INSERT INTO payments (
                    id, tenant_id, booking_id, payment_method, utr_reference,
                    amount_paise, status, expires_at, created_at
                ) VALUES (
                    :id, :tid, :bid, 'upi_manual', :utr, :amt, 'submitted_utr', :exp, :now
                )
            """),
            {
                "id": payment_id,
                "tid": tenant_id,
                "bid": row.booking_id,
                "utr": cleaned_utr,
                "amt": amount_paise,
                "exp": hold_ext,
                "now": now,
            },
        )

    await db.commit()

    bid_short = str(row.booking_id)[:8]
    amt_str = f"₹{amount_paise // 100}" if amount_paise else "deposit"
    owner_msg = (
        f"💳 *Payment Verification Needed ({row.business_name})*\n\n"
        f"• Customer: {row.customer_name or row.customer_phone}\n"
        f"• Service: {row.service_name} ({amt_str})\n"
        f"• Booking ID: `{bid_short}`\n"
        f"• UPI UTR: `{cleaned_utr}`\n\n"
        f"Tap below to approve or reply *confirm {bid_short}* / *reject {bid_short}*."
    )
    owner_notified = await _send_whatsapp_notice(
        db,
        tenant_id,
        row.owner_phone,
        owner_msg,
        buttons=[
            {"id": f"owner_confirm_{bid_short}", "title": "✅ Confirm Payment"},
            {"id": f"owner_reject_{bid_short}", "title": "❌ Reject"},
        ],
    )

    return {
        "payment_id": str(payment_id),
        "booking_id": str(row.booking_id),
        "booking_ref": bid_short,
        "utr_reference": cleaned_utr,
        "status": "submitted_utr",
        "owner_notified": owner_notified,
    }


async def confirm_manual_payment(
    db: AsyncSession,
    tenant_id: str,
    booking_ref: str,
    verified_by: str = "owner_dashboard",
) -> dict:
    """Confirm a pending_deposit booking after owner verifies UPI/cash/bank payment."""
    row = (
        await db.execute(
            text("""
                SELECT b.id AS booking_id, b.status, b.starts_at,
                       s.name AS service_name, c.phone AS customer_phone, c.name AS customer_name,
                       t.business_name, p.id AS payment_id, p.amount_paise
                FROM bookings b
                JOIN services s ON s.id = b.service_id
                JOIN customers c ON c.id = b.customer_id
                JOIN tenants t ON t.id = b.tenant_id
                LEFT JOIN payments p ON p.booking_id = b.id
                WHERE b.tenant_id = :tid
                  AND b.id::text LIKE :ref
                  AND b.status = 'pending_deposit'
                ORDER BY p.created_at DESC NULLS LAST
                LIMIT 1
            """),
            {"tid": tenant_id, "ref": f"{booking_ref.lower().strip()}%"},
        )
    ).first()
    if not row:
        raise ValueError(f"No pending-deposit booking found matching '{booking_ref}'.")

    now = datetime.now(timezone.utc)
    payment_id = row.payment_id
    if payment_id:
        await db.execute(
            text("""
                UPDATE payments
                SET status = 'captured', verified_by = :vby, verified_at = :now
                WHERE id = :pid
            """),
            {"vby": verified_by, "now": now, "pid": payment_id},
        )
    else:
        payment_id = uuid.uuid4()
        await db.execute(
            text("""
                INSERT INTO payments (
                    id, tenant_id, booking_id, payment_method, amount_paise,
                    status, verified_by, verified_at, created_at
                ) VALUES (
                    :id, :tid, :bid, 'upi_manual', 0, 'captured', :vby, :now, :now
                )
            """),
            {"id": payment_id, "tid": tenant_id, "bid": row.booking_id, "vby": verified_by, "now": now},
        )

    await db.execute(
        text("""
            UPDATE bookings
            SET status = 'confirmed', deposit_payment_id = :pid, updated_at = :now
            WHERE id = :bid AND tenant_id = :tid AND status = 'pending_deposit'
        """),
        {"pid": payment_id, "bid": row.booking_id, "tid": tenant_id, "now": now},
    )
    await db.commit()

    bid_short = str(row.booking_id)[:8]
    ts_row = (
        await db.execute(
            text(
                "SELECT booking_confirmed_message, google_maps_url, google_review_url "
                "FROM tenant_settings WHERE tenant_id = :tid LIMIT 1"
            ),
            {"tid": tenant_id},
        )
    ).first()
    extras: list[str] = []
    maps_url = (ts_row.google_maps_url or "").strip() if ts_row else ""
    review_url = (ts_row.google_review_url or "").strip() if ts_row else ""
    custom_note = (ts_row.booking_confirmed_message or "").strip() if ts_row else ""
    if custom_note:
        extras.append(
            custom_note.replace("{business_name}", row.business_name or "")
            .replace("{service_name}", row.service_name or "")
            .replace("{customer_name}", row.customer_name or "")
            .replace("{maps_url}", maps_url)
            .replace("{review_url}", review_url)
        )
    if maps_url and maps_url not in "\n".join(extras):
        extras.append(f"📍 *Location:* {maps_url}")
    extra_block = ("\n\n" + "\n\n".join(extras)) if extras else ""

    await _send_whatsapp_notice(
        db,
        tenant_id,
        row.customer_phone,
        f"✅ *Payment Verified & Booking Confirmed!*\n\n"
        f"• {row.service_name} at *{row.business_name}*\n"
        f"• Ref: `{bid_short}`{extra_block}\n\nSee you soon! 🙏",
        buttons=[
            {"id": "action_done", "title": "👍 Thanks, All Set"},
            {"id": "action_book", "title": "📅 Book Another"},
            {"id": f"cancel_{bid_short}", "title": "❌ Cancel This"},
        ],
    )

    return {
        "booking_id": str(row.booking_id),
        "booking_ref": bid_short,
        "status": "confirmed",
        "payment_status": "captured",
        "verified_by": verified_by,
    }


async def reject_manual_payment(
    db: AsyncSession,
    tenant_id: str,
    booking_ref: str,
    reason: str | None = None,
) -> dict:
    """Reject an unverified deposit claim and cancel the pending_deposit hold to free the slot."""
    row = (
        await db.execute(
            text("""
                SELECT b.id AS booking_id, s.name AS service_name,
                       c.phone AS customer_phone, t.business_name, p.id AS payment_id
                FROM bookings b
                JOIN services s ON s.id = b.service_id
                JOIN customers c ON c.id = b.customer_id
                JOIN tenants t ON t.id = b.tenant_id
                LEFT JOIN payments p ON p.booking_id = b.id
                WHERE b.tenant_id = :tid
                  AND b.id::text LIKE :ref
                  AND b.status = 'pending_deposit'
                ORDER BY p.created_at DESC NULLS LAST
                LIMIT 1
            """),
            {"tid": tenant_id, "ref": f"{booking_ref.lower().strip()}%"},
        )
    ).first()
    if not row:
        raise ValueError(f"No pending-deposit booking found matching '{booking_ref}'.")

    now = datetime.now(timezone.utc)
    if row.payment_id:
        await db.execute(
            text("UPDATE payments SET status = 'failed' WHERE id = :pid"),
            {"pid": row.payment_id},
        )
    await db.execute(
        text("""
            UPDATE bookings
            SET status = 'cancelled',
                cancellation_reason = :reason,
                updated_at = :now
            WHERE id = :bid AND tenant_id = :tid AND status = 'pending_deposit'
        """),
        {"reason": reason or "Deposit payment not verified", "now": now, "bid": row.booking_id, "tid": tenant_id},
    )
    await db.commit()

    bid_short = str(row.booking_id)[:8]
    await _send_whatsapp_notice(
        db,
        tenant_id,
        row.customer_phone,
        f"❌ We could not verify the deposit for *{row.service_name}* (`{bid_short}`) at *{row.business_name}*, "
        "so the slot hold was released. Reply *Hi* to rebook or *human* to speak with the business.",
    )
    return {
        "booking_id": str(row.booking_id),
        "booking_ref": bid_short,
        "status": "cancelled",
        "payment_status": "failed",
    }


async def check_payment_status_live(
    db: AsyncSession,
    tenant_id: str,
    booking_ref: str,
) -> dict:
    """Check current deposit status; if it's a Razorpay Payment Link, poll Razorpay live."""
    row = (
        await db.execute(
            text("""
                SELECT p.id AS payment_id, p.status AS pay_status, p.payment_method,
                       p.razorpay_payment_link_id, p.utr_reference, p.amount_paise,
                       b.id AS booking_id, b.status AS booking_status
                FROM bookings b
                LEFT JOIN payments p ON p.booking_id = b.id
                WHERE b.tenant_id = :tid AND b.id::text LIKE :ref
                ORDER BY p.created_at DESC NULLS LAST
                LIMIT 1
            """),
            {"tid": tenant_id, "ref": f"{booking_ref.lower().strip()}%"},
        )
    ).first()
    if not row:
        raise ValueError(f"Booking '{booking_ref}' not found.")

    if row.booking_status == "confirmed" or row.pay_status == "captured":
        return {
            "booking_id": str(row.booking_id),
            "booking_status": row.booking_status,
            "payment_status": "captured",
            "verified": True,
        }

    # Active poll against Razorpay Payment Link API
    if (
        row.razorpay_payment_link_id
        and settings.razorpay_key_id
        and settings.razorpay_key_secret
    ):
        try:
            client = _get_razorpay_client()
            plink = client.payment_link.fetch(row.razorpay_payment_link_id)
            if plink.get("status") == "paid":
                now = datetime.now(timezone.utc)
                rzp_pid = None
                pays = plink.get("payments") or []
                if pays and isinstance(pays[0], dict):
                    rzp_pid = pays[0].get("payment_id")
                await db.execute(
                    text("""
                        UPDATE payments
                        SET status = 'captured', razorpay_payment_id = COALESCE(:rpid, razorpay_payment_id),
                            verified_by = 'api_poll', verified_at = :now
                        WHERE id = :pid
                    """),
                    {"rpid": rzp_pid, "now": now, "pid": row.payment_id},
                )
                await db.execute(
                    text("""
                        UPDATE bookings
                        SET status = 'confirmed', deposit_payment_id = :pid, updated_at = :now
                        WHERE id = :bid AND tenant_id = :tid AND status = 'pending_deposit'
                    """),
                    {"pid": row.payment_id, "bid": row.booking_id, "tid": tenant_id, "now": now},
                )
                await db.commit()
                return {
                    "booking_id": str(row.booking_id),
                    "booking_status": "confirmed",
                    "payment_status": "captured",
                    "verified": True,
                    "verified_by": "api_poll",
                }
        except Exception as exc:
            logger.warning("razorpay_live_poll_failed", error=str(exc))

    return {
        "booking_id": str(row.booking_id),
        "booking_status": row.booking_status,
        "payment_status": row.pay_status or "pending",
        "utr_reference": row.utr_reference,
        "verified": False,
    }


async def verify_payment(
    db: AsyncSession,
    tenant_id: str,
    razorpay_payment_id: str,
    razorpay_order_id: str,
    razorpay_signature: str,
) -> dict:
    """Verify Razorpay payment signature and update payment + booking status."""
    message = f"{razorpay_order_id}|{razorpay_payment_id}"
    expected = hmac.new(
        settings.razorpay_key_secret.encode(),
        message.encode(),
        hashlib.sha256,
    ).hexdigest()

    verified = hmac.compare_digest(expected, razorpay_signature)
    now = datetime.now(timezone.utc)

    if verified:
        new_status = "captured"
    else:
        new_status = "failed"
        logger.warning(
            "payment_signature_mismatch",
            tenant_id=tenant_id,
            razorpay_order_id=razorpay_order_id,
        )

    await db.execute(
        text("""
            UPDATE payments
            SET razorpay_payment_id = :rpid, status = :status,
                verified_by = CASE WHEN :verified THEN 'signature' ELSE verified_by END,
                verified_at = CASE WHEN :verified THEN :now ELSE verified_at END
            WHERE razorpay_order_id = :roid AND tenant_id = :tid
        """),
        {
            "rpid": razorpay_payment_id,
            "status": new_status,
            "verified": verified,
            "now": now,
            "roid": razorpay_order_id,
            "tid": tenant_id,
        },
    )

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
    razorpay_order_id: str | None = None,
    razorpay_payment_link_id: str | None = None,
) -> None:
    """Confirm pending_deposit booking and notify customer on WhatsApp."""
    if razorpay_payment_link_id:
        where_clause = "p.razorpay_payment_link_id = :key"
        key_val = razorpay_payment_link_id
    elif razorpay_order_id:
        where_clause = "p.razorpay_order_id = :key"
        key_val = razorpay_order_id
    else:
        return

    result = await db.execute(
        text(f"""
            SELECT p.booking_id, p.id AS payment_id,
                   c.phone AS customer_phone, s.name AS service_name, t.business_name
            FROM payments p
            JOIN bookings b ON b.id = p.booking_id
            JOIN customers c ON c.id = b.customer_id
            JOIN services s ON s.id = b.service_id
            JOIN tenants t ON t.id = p.tenant_id
            WHERE {where_clause} AND p.tenant_id = :tid
            LIMIT 1
        """),
        {"key": key_val, "tid": tenant_id},
    )
    row = result.first()
    if not row or not row.booking_id:
        return

    now = datetime.now(timezone.utc)
    updated = await db.execute(
        text("""
            UPDATE bookings
            SET status = 'confirmed', deposit_payment_id = :pid, updated_at = :now
            WHERE id = :bid AND tenant_id = :tid AND status = 'pending_deposit'
            RETURNING id
        """),
        {"pid": row.payment_id, "bid": row.booking_id, "tid": tenant_id, "now": now},
    )
    if updated.first():
        logger.info("booking_confirmed_via_payment", booking_id=str(row.booking_id), tenant_id=tenant_id)
        ts_row = (
            await db.execute(
                text(
                    "SELECT booking_confirmed_message, google_maps_url, google_review_url "
                    "FROM tenant_settings WHERE tenant_id = :tid LIMIT 1"
                ),
                {"tid": tenant_id},
            )
        ).first()
        extras: list[str] = []
        maps_url = (ts_row.google_maps_url or "").strip() if ts_row else ""
        review_url = (ts_row.google_review_url or "").strip() if ts_row else ""
        custom_note = (ts_row.booking_confirmed_message or "").strip() if ts_row else ""
        if custom_note:
            extras.append(
                custom_note.replace("{business_name}", row.business_name or "")
                .replace("{service_name}", row.service_name or "")
                .replace("{maps_url}", maps_url)
                .replace("{review_url}", review_url)
            )
        if maps_url and maps_url not in "\n".join(extras):
            extras.append(f"📍 *Location:* {maps_url}")
        extra_block = ("\n\n" + "\n\n".join(extras)) if extras else ""
        bid_short = str(row.booking_id)[:8]
        await _send_whatsapp_notice(
            db,
            tenant_id,
            row.customer_phone,
            f"✅ *Deposit Received & Booking Confirmed!*\n\n"
            f"• {row.service_name} at *{row.business_name}*\n"
            f"• Ref: `{bid_short}`{extra_block}\n\nSee you soon! 🙏",
            buttons=[
                {"id": "action_done", "title": "👍 Thanks, All Set"},
                {"id": "action_book", "title": "📅 Book Another"},
                {"id": f"cancel_{bid_short}", "title": "❌ Cancel This"},
            ],
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
    """Create a Razorpay order for a booking deposit (gated by deposits_enabled entitlement)."""
    try:
        await require_feature(db, tenant_id, "deposits_enabled")
    except SubscriptionExpiredError as exc:
        raise HTTPException(status_code=402, detail=str(exc))
    except FeatureNotAvailableError as exc:
        raise HTTPException(status_code=403, detail=str(exc))

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


@router.get("/pending")
async def list_pending_deposits(
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """List pending and UTR-submitted booking deposits for owner verification in Dashboard."""
    res = await db.execute(
        text("""
            SELECT p.id AS payment_id, p.booking_id, p.payment_method, p.amount_paise,
                   p.status AS payment_status, p.utr_reference, p.payment_url,
                   p.expires_at, p.created_at,
                   b.starts_at, b.status AS booking_status,
                   s.name AS service_name, c.name AS customer_name, c.phone AS customer_phone
            FROM bookings b
            JOIN services s ON s.id = b.service_id
            JOIN customers c ON c.id = b.customer_id
            LEFT JOIN payments p ON p.booking_id = b.id
            WHERE b.tenant_id = :tid AND b.status = 'pending_deposit'
            ORDER BY CASE WHEN p.status = 'submitted_utr' THEN 0 ELSE 1 END, b.created_at DESC
            LIMIT 50
        """),
        {"tid": tenant_id},
    )
    return [
        {
            "payment_id": str(r.payment_id) if r.payment_id else None,
            "booking_id": str(r.booking_id),
            "booking_ref": str(r.booking_id)[:8],
            "payment_method": r.payment_method or "upi_manual",
            "amount_paise": r.amount_paise or 0,
            "payment_status": r.payment_status or "pending",
            "utr_reference": r.utr_reference,
            "payment_url": r.payment_url,
            "expires_at": r.expires_at.isoformat() if r.expires_at else None,
            "starts_at": r.starts_at.isoformat() if r.starts_at else None,
            "service_name": r.service_name,
            "customer_name": r.customer_name,
            "customer_phone": r.customer_phone,
        }
        for r in res.fetchall()
    ]


@router.post("/{booking_id}/whatsapp-link")
async def create_booking_whatsapp_link(
    booking_id: str,
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Generate a fresh Razorpay Payment Link or Direct UPI link for a pending booking."""
    try:
        await require_feature(db, tenant_id, "deposits_enabled")
    except SubscriptionExpiredError as exc:
        raise HTTPException(status_code=402, detail=str(exc))
    except FeatureNotAvailableError as exc:
        raise HTTPException(status_code=403, detail=str(exc))

    row = (
        await db.execute(
            text("""
                SELECT b.id, s.deposit_amount_paise, s.price_paise, t.business_name,
                       c.name AS customer_name, c.phone AS customer_phone,
                       ts.deposit_mode, ts.upi_id, ts.deposit_hold_minutes
                FROM bookings b
                JOIN services s ON s.id = b.service_id
                JOIN tenants t ON t.id = b.tenant_id
                JOIN customers c ON c.id = b.customer_id
                LEFT JOIN tenant_settings ts ON ts.tenant_id = b.tenant_id
                WHERE b.id = :bid AND b.tenant_id = :tid
                LIMIT 1
            """),
            {"bid": booking_id, "tid": tenant_id},
        )
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Booking not found")

    amt = row.deposit_amount_paise or row.price_paise or 0
    if amt <= 0:
        raise HTTPException(status_code=400, detail="Service has no deposit or price configured")

    result = await create_whatsapp_deposit_payment(
        db=db,
        tenant_id=str(tenant_id),
        booking_id=str(row.id),
        amount_paise=amt,
        business_name=row.business_name,
        customer_name=row.customer_name or "",
        customer_phone=row.customer_phone or "",
        deposit_mode=row.deposit_mode or "auto",
        upi_id=row.upi_id,
        hold_minutes=row.deposit_hold_minutes or 15,
    )
    if not result:
        raise HTTPException(
            status_code=400,
            detail="Configure Razorpay API keys or set a Merchant UPI ID in Settings → Payments first.",
        )
    return result


@router.post("/{booking_id}/check-status")
async def check_booking_payment_status(
    booking_id: str,
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Check (and live-poll Razorpay if applicable) the deposit status for a booking."""
    try:
        return await check_payment_status_live(db, str(tenant_id), booking_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/{booking_id}/confirm-manual")
async def confirm_booking_payment_manual(
    booking_id: str,
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Owner confirms a UPI/manual deposit from the dashboard -> confirms booking + notifies customer."""
    try:
        return await confirm_manual_payment(db, str(tenant_id), booking_id, verified_by="owner_dashboard")
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/{booking_id}/reject-manual")
async def reject_booking_payment_manual(
    booking_id: str,
    body: ManualActionRequest | None = None,
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Owner rejects an unverified deposit claim -> cancels hold & releases slot."""
    try:
        return await reject_manual_payment(
            db, str(tenant_id), booking_id, reason=(body.reason if body else None)
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/webhook")
async def razorpay_webhook(
    request: Request,
    x_razorpay_signature: str = Header(..., alias="X-Razorpay-Signature"),
    db: AsyncSession = Depends(get_db),
):
    """Unified Razorpay webhook handler for BOTH booking deposits AND SaaS subscriptions.

    Handles:
      - payment_link.paid, payment.captured, payment.failed (booking deposits)
      - subscription.authenticated, subscription.activated, subscription.charged
      - subscription.pending, subscription.halted, subscription.cancelled, subscription.completed
    """
    body = await request.body()
    if not _verify_webhook_signature(body, x_razorpay_signature):
        logger.warning("webhook_signature_invalid")
        raise HTTPException(status_code=400, detail="Invalid signature")

    payload = await request.json()
    event = payload.get("event", "")
    pl = payload.get("payload", {})
    now = datetime.now(timezone.utc)

    logger.info("razorpay_webhook_received", webhook_event=event)

    # --- 1. Booking Deposit: Payment Link Paid ---
    if event == "payment_link.paid":
        plink_entity = pl.get("payment_link", {}).get("entity", {})
        pay_entity = pl.get("payment", {}).get("entity", {})
        plink_id = plink_entity.get("id", "")
        rpid = pay_entity.get("id", "")
        res = await db.execute(
            text("""
                UPDATE payments
                SET razorpay_payment_id = COALESCE(:rpid, razorpay_payment_id),
                    status = 'captured',
                    verified_by = 'webhook',
                    verified_at = :now
                WHERE razorpay_payment_link_id = :plid
                RETURNING tenant_id
            """),
            {"rpid": rpid, "now": now, "plid": plink_id},
        )
        row = res.first()
        if row:
            await _confirm_booking_if_deposit_paid(
                db, str(row.tenant_id), razorpay_payment_link_id=plink_id
            )
        await db.commit()

    # --- 2. Booking Deposit OR One-Time SaaS Order Captured ---
    elif event == "payment.captured":
        payment_entity = pl.get("payment", {}).get("entity", {})
        razorpay_order_id = payment_entity.get("order_id", "")
        razorpay_payment_id = payment_entity.get("id", "")
        notes = payment_entity.get("notes") or {}

        if notes.get("purpose") == "saas_subscription" and notes.get("tenant_id"):
            tid = uuid.UUID(notes["tenant_id"])
            tier = notes.get("plan_tier", "solo")
            cycle = notes.get("billing_cycle", "monthly")
            await activate_subscription(
                db,
                tenant_id=tid,
                plan_tier=tier,
                billing_cycle=cycle,
                razorpay_payment_id=razorpay_payment_id,
                razorpay_order_id=razorpay_order_id,
                payment_provider="razorpay",
                record_invoice=True,
            )
        else:
            result = await db.execute(
                text("""
                    UPDATE payments
                    SET razorpay_payment_id = :rpid, status = 'captured',
                        verified_by = 'webhook', verified_at = :now
                    WHERE razorpay_order_id = :roid
                    RETURNING tenant_id
                """),
                {"rpid": razorpay_payment_id, "now": now, "roid": razorpay_order_id},
            )
            row = result.first()
            if row:
                await _confirm_booking_if_deposit_paid(db, str(row.tenant_id), razorpay_order_id)
            await db.commit()

    elif event == "payment.failed":
        payment_entity = pl.get("payment", {}).get("entity", {})
        await db.execute(
            text("""
                UPDATE payments
                SET razorpay_payment_id = :rpid, status = 'failed'
                WHERE razorpay_order_id = :roid
            """),
            {
                "rpid": payment_entity.get("id", ""),
                "roid": payment_entity.get("order_id", ""),
            },
        )
        await db.commit()

    # --- 3. SaaS Subscriptions Lifecycle ---
    elif event in ("subscription.authenticated", "subscription.activated", "subscription.charged"):
        sub_entity = pl.get("subscription", {}).get("entity", {})
        pay_entity = pl.get("payment", {}).get("entity", {})
        rzp_sub_id = sub_entity.get("id", "")
        notes = sub_entity.get("notes") or {}

        tid_str = notes.get("tenant_id")
        if not tid_str and rzp_sub_id:
            lookup = (
                await db.execute(
                    text("SELECT tenant_id, plan_tier, billing_cycle FROM subscriptions WHERE razorpay_subscription_id = :rsub LIMIT 1"),
                    {"rsub": rzp_sub_id},
                )
            ).first()
            if lookup:
                tid_str = str(lookup.tenant_id)
                notes.setdefault("plan_tier", lookup.plan_tier)
                notes.setdefault("billing_cycle", lookup.billing_cycle)

        if tid_str:
            tid = uuid.UUID(tid_str)
            tier = notes.get("plan_tier", "solo")
            cycle = notes.get("billing_cycle", "monthly")
            await activate_subscription(
                db,
                tenant_id=tid,
                plan_tier=tier,
                billing_cycle=cycle,
                razorpay_subscription_id=rzp_sub_id or None,
                razorpay_plan_id=sub_entity.get("plan_id"),
                razorpay_payment_id=pay_entity.get("id"),
                razorpay_invoice_id=pay_entity.get("invoice_id"),
                payment_provider="razorpay",
                record_invoice=(event == "subscription.charged"),
            )
            t_row = (
                await db.execute(
                    text("SELECT owner_phone, business_name FROM tenants WHERE id = :tid"),
                    {"tid": tid},
                )
            ).first()
            if t_row:
                await _send_whatsapp_notice(
                    db,
                    tid,
                    t_row.owner_phone,
                    f"✅ *BookBot {tier.capitalize()} Plan Active ({t_row.business_name})*\n"
                    "Thank you! Your subscription payment succeeded and all features are unlocked.",
                )

    elif event in ("subscription.pending", "subscription.halted"):
        sub_entity = pl.get("subscription", {}).get("entity", {})
        rzp_sub_id = sub_entity.get("id", "")
        notes = sub_entity.get("notes") or {}
        tid_str = notes.get("tenant_id")
        if not tid_str and rzp_sub_id:
            lookup = (
                await db.execute(
                    text("SELECT tenant_id FROM subscriptions WHERE razorpay_subscription_id = :rsub LIMIT 1"),
                    {"rsub": rzp_sub_id},
                )
            ).first()
            if lookup:
                tid_str = str(lookup.tenant_id)
        if tid_str:
            tid = uuid.UUID(tid_str)
            await mark_subscription_past_due(db, tid)
            t_row = (
                await db.execute(
                    text("SELECT owner_phone, business_name FROM tenants WHERE id = :tid"),
                    {"tid": tid},
                )
            ).first()
            if t_row:
                await _send_whatsapp_notice(
                    db,
                    tid,
                    t_row.owner_phone,
                    f"⚠️ *Subscription Payment Failed ({t_row.business_name})*\n\n"
                    f"You have {settings.subscription_grace_days} days of grace period before new bookings pause.\n"
                    f"Update your payment method here: {settings.dashboard_url}",
                )

    elif event in ("subscription.cancelled", "subscription.completed"):
        sub_entity = pl.get("subscription", {}).get("entity", {})
        rzp_sub_id = sub_entity.get("id", "")
        if rzp_sub_id:
            await db.execute(
                text("""
                    UPDATE subscriptions
                    SET status = 'cancelled', cancelled_at = :now
                    WHERE razorpay_subscription_id = :rsub
                """),
                {"now": now, "rsub": rzp_sub_id},
            )
            await db.commit()

    else:
        logger.debug("webhook_event_ignored", webhook_event=event)

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
            SELECT p.razorpay_order_id, p.payment_url, p.payment_method, p.amount_paise, t.business_name
            FROM payments p
            JOIN bookings b ON p.booking_id = b.id
            JOIN tenants t ON b.tenant_id = t.id
            WHERE p.booking_id = :bid AND b.tenant_id = :tid AND p.status IN ('created', 'submitted_utr')
            ORDER BY p.created_at DESC
            LIMIT 1
        """),
        {"bid": booking_id, "tid": tenant_id},
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="No pending payment found for this booking")

    url = row.payment_url or generate_payment_link(
        razorpay_order_id=row.razorpay_order_id or "",
        amount_paise=row.amount_paise,
        business_name=row.business_name,
    )
    return PaymentLinkResponse(
        url=url,
        amount_paise=row.amount_paise,
        payment_method=row.payment_method or "razorpay",
    )
