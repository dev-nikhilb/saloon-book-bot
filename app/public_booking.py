"""Public Shareable Booking Page API (works via /book/{slug} or {slug}.bookbot.com).

Allows end-customers to discover services, view live staff/slot availability,
and book appointments directly from a web link (Instagram bio, Google Maps,
clinic website, QR code) with the exact same zero-double-booking engine as WhatsApp.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.service import enforce_plan_limit
from app.bookings.repository import create_booking
from app.bookings.slots import get_available_slots
from app.config import settings as app_settings
from app.database import get_db
from app.exceptions import PlanLimitExceededError, SlotConflictError
from app.verticals import get_vertical

logger = structlog.get_logger()

router = APIRouter(prefix="/api/public", tags=["public-booking"])


class PublicBookingRequest(BaseModel):
    service_id: str
    staff_id: str | None = None
    branch_id: str | None = None
    starts_at: str  # ISO datetime
    customer_name: str = Field(..., min_length=2, max_length=100)
    customer_phone: str = Field(..., min_length=8, max_length=20)
    reason: str | None = Field(None, max_length=500)


def _normalize_phone(phone: str) -> str:
    cleaned = phone.strip().replace(" ", "").replace("-", "")
    if cleaned.startswith("+"):
        return cleaned
    if len(cleaned) == 10 and cleaned.isdigit():
        return f"+91{cleaned}"
    return f"+{cleaned}"


async def _get_tenant_by_slug(db: AsyncSession, slug: str):
    res = await db.execute(
        text(
            "SELECT id, business_name, business_type, slug, primary_language, timezone, is_active "
            "FROM tenants WHERE slug = :slug AND is_active = true LIMIT 1"
        ),
        {"slug": slug},
    )
    row = res.first()
    if not row:
        raise HTTPException(status_code=404, detail="Business not found")
    return row


@router.get("/business")
async def get_business_from_subdomain(request: Request, db: AsyncSession = Depends(get_db)):
    """Resolve public business profile using subdomain (Host: slug.bookbot.com)."""
    slug = getattr(request.state, "tenant_slug", None)
    if not slug:
        raise HTTPException(status_code=404, detail="No business subdomain in request")
    return await get_public_business(slug, db)


@router.get("/business/{slug}")
async def get_public_business(slug: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Public profile, services tree, staff profiles, branches, and booking rules."""
    tenant = await _get_tenant_by_slug(db, slug)
    tid = tenant.id

    # Settings
    ts_res = await db.execute(
        text(
            "SELECT advance_booking_days, min_cancel_hours, slot_increment_minutes, "
            "welcome_message, booking_confirmed_message, google_review_url, razorpay_enabled "
            "FROM tenant_settings WHERE tenant_id = :tid LIMIT 1"
        ),
        {"tid": tid},
    )
    ts = ts_res.first()
    settings_dict = dict(ts._mapping) if ts else {
        "advance_booking_days": 14,
        "min_cancel_hours": 2,
        "slot_increment_minutes": 15,
        "welcome_message": None,
        "booking_confirmed_message": None,
        "google_review_url": None,
        "razorpay_enabled": False,
    }

    # Branches
    br_res = await db.execute(
        text("SELECT id, name, address, is_primary, operating_hours FROM branches WHERE tenant_id = :tid ORDER BY is_primary DESC, name"),
        {"tid": tid},
    )
    branches = [
        {
            "id": str(b.id),
            "name": b.name,
            "address": b.address,
            "is_primary": b.is_primary,
            "operating_hours": b.operating_hours,
        }
        for b in br_res.fetchall()
    ]

    # Services (tree)
    svc_res = await db.execute(
        text(
            "SELECT id, parent_service_id, name, description, image_url, duration_minutes, "
            "buffer_minutes, price_paise, requires_deposit, deposit_amount_paise, sort_order "
            "FROM services WHERE tenant_id = :tid AND is_active = true ORDER BY sort_order, name"
        ),
        {"tid": tid},
    )
    by_id: dict[str, dict] = {}
    parents: list[dict] = []
    rows = []
    for s in svc_res.fetchall():
        item = {
            "id": str(s.id),
            "parent_service_id": str(s.parent_service_id) if s.parent_service_id else None,
            "name": s.name,
            "description": s.description,
            "image_url": s.image_url,
            "duration_minutes": s.duration_minutes,
            "buffer_minutes": s.buffer_minutes,
            "price_paise": s.price_paise,
            "requires_deposit": s.requires_deposit,
            "deposit_amount_paise": s.deposit_amount_paise,
            "sort_order": s.sort_order,
            "children": [],
        }
        by_id[item["id"]] = item
        rows.append(item)
    for item in rows:
        pid = item["parent_service_id"]
        if pid and pid in by_id:
            by_id[pid]["children"].append(item)
        else:
            parents.append(item)

    # Staff
    st_res = await db.execute(
        text(
            "SELECT id, branch_id, name, role, bio, specialty, skills "
            "FROM staff WHERE tenant_id = :tid AND is_active = true ORDER BY name"
        ),
        {"tid": tid},
    )
    staff = [
        {
            "id": str(s.id),
            "branch_id": str(s.branch_id) if s.branch_id else None,
            "name": s.name,
            "role": s.role,
            "bio": s.bio,
            "specialty": s.specialty,
            "skills": s.skills,
        }
        for s in st_res.fetchall()
    ]

    vertical = get_vertical(tenant.business_type)

    return {
        "business_name": tenant.business_name,
        "business_type": tenant.business_type,
        "slug": tenant.slug,
        "timezone": tenant.timezone,
        "primary_language": tenant.primary_language,
        "labels": vertical["labels"],
        "vertical": {
            "display_name": vertical["display_name"],
            "collect_reason": vertical["collect_reason"],
            "reason_placeholder": vertical["reason_placeholder"],
        },
        "settings": settings_dict,
        "branches": branches,
        "services": parents,
        "staff": staff,
    }


@router.get("/business/{slug}/slots")
async def get_public_slots(
    slug: str,
    service_id: str = Query(...),
    target_date: date = Query(..., alias="date"),
    staff_id: str | None = Query(None),
    branch_id: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Return live available slots for a service on a given date."""
    tenant = await _get_tenant_by_slug(db, slug)
    try:
        svc_uuid = uuid.UUID(service_id)
        staff_uuid = uuid.UUID(staff_id) if staff_id else None
        branch_uuid = uuid.UUID(branch_id) if branch_id else None
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid UUID parameter")

    slots = await get_available_slots(
        db=db,
        tenant_id=tenant.id,
        service_id=svc_uuid,
        target_date=target_date,
        preferred_staff_id=staff_uuid,
        tz_name=tenant.timezone or "Asia/Kolkata",
        branch_id=branch_uuid,
    )
    return {
        "date": target_date.isoformat(),
        "timezone": tenant.timezone or "Asia/Kolkata",
        "slots": [s.to_dict() for s in slots],
    }


@router.post("/business/{slug}/book", status_code=201)
async def create_public_booking(
    slug: str,
    body: PublicBookingRequest,
    db: AsyncSession = Depends(get_db),
):
    """Create a booking directly from the public web booking page."""
    tenant = await _get_tenant_by_slug(db, slug)
    tid = tenant.id

    try:
        svc_uuid = uuid.UUID(body.service_id)
        staff_uuid = uuid.UUID(body.staff_id) if body.staff_id else None
        branch_uuid = uuid.UUID(body.branch_id) if body.branch_id else None
        starts_at = datetime.fromisoformat(body.starts_at)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid input: {exc}")

    # Enforce monthly booking plan limit
    try:
        await enforce_plan_limit(db, tid, "bookings")
    except PlanLimitExceededError as exc:
        raise HTTPException(status_code=402, detail=f"Monthly booking limit reached ({exc})")

    # Look up service
    svc_res = await db.execute(
        text(
            "SELECT id, name, duration_minutes, price_paise, requires_deposit, deposit_amount_paise "
            "FROM services WHERE id = :sid AND tenant_id = :tid AND is_active = true"
        ),
        {"sid": svc_uuid, "tid": tid},
    )
    svc = svc_res.first()
    if not svc or svc.duration_minutes <= 0:
        raise HTTPException(status_code=404, detail="Service not found")

    from datetime import timedelta
    ends_at = starts_at + timedelta(minutes=svc.duration_minutes)

    # Pick staff if not explicitly specified (first eligible available staff at that exact slot)
    if not staff_uuid:
        slots = await get_available_slots(
            db=db,
            tenant_id=tid,
            service_id=svc_uuid,
            target_date=starts_at.date(),
            tz_name=tenant.timezone or "Asia/Kolkata",
            branch_id=branch_uuid,
        )
        matched = next((s for s in slots if s.start == starts_at), None)
        if not matched:
            raise HTTPException(status_code=409, detail="Selected time slot is no longer available")
        staff_uuid = matched.staff_id

    # Resolve branch from staff or primary branch
    if not branch_uuid:
        st_res = await db.execute(
            text("SELECT branch_id FROM staff WHERE id = :sid AND tenant_id = :tid"),
            {"sid": staff_uuid, "tid": tid},
        )
        st_row = st_res.first()
        if not st_row:
            raise HTTPException(status_code=404, detail="Staff member not found")
        branch_uuid = st_row.branch_id

    if not branch_uuid:
        br_res = await db.execute(
            text("SELECT id FROM branches WHERE tenant_id = :tid AND is_primary = true LIMIT 1"),
            {"tid": tid},
        )
        br_row = br_res.first()
        if not br_row:
            raise HTTPException(status_code=400, detail="Business has no configured branch")
        branch_uuid = br_row.id

    # Upsert customer
    phone = _normalize_phone(body.customer_phone)
    cust_res = await db.execute(
        text("SELECT id, name FROM customers WHERE tenant_id = :tid AND phone = :phone"),
        {"tid": tid, "phone": phone},
    )
    cust = cust_res.first()
    if not cust:
        cust_id = uuid.uuid4()
        await db.execute(
            text("INSERT INTO customers (id, tenant_id, phone, name, first_seen_at) VALUES (:id, :tid, :phone, :name, :now)"),
            {"id": cust_id, "tid": tid, "phone": phone, "name": body.customer_name.strip(), "now": datetime.now(timezone.utc)},
        )
        await db.commit()
    else:
        cust_id = cust.id
        if not cust.name and body.customer_name.strip():
            await db.execute(
                text("UPDATE customers SET name = :name WHERE id = :cid AND tenant_id = :tid"),
                {"name": body.customer_name.strip(), "cid": cust_id, "tid": tid},
            )
            await db.commit()

    # Enforce max_bookings_per_customer
    ts_res = await db.execute(
        text("SELECT max_bookings_per_customer FROM tenant_settings WHERE tenant_id = :tid LIMIT 1"),
        {"tid": tid},
    )
    ts_row = ts_res.first()
    max_active = (ts_row.max_bookings_per_customer if ts_row and ts_row.max_bookings_per_customer else 5)
    active_cnt = (await db.execute(
        text("""
            SELECT COUNT(*) FROM bookings
            WHERE tenant_id = :tid AND customer_id = :cid
              AND status IN ('confirmed', 'pending_deposit') AND starts_at > :now
        """),
        {"tid": tid, "cid": cust_id, "now": datetime.now(timezone.utc)},
    )).scalar() or 0
    if active_cnt >= max_active:
        raise HTTPException(
            status_code=429,
            detail=f"You already have {max_active} upcoming appointments. Please cancel one before booking again.",
        )

    try:
        booking = await create_booking(
            db=db,
            tenant_id=tid,
            branch_id=branch_uuid,
            staff_id=staff_uuid,
            customer_id=cust_id,
            service_id=svc_uuid,
            starts_at=starts_at,
            ends_at=ends_at,
            requires_deposit=bool(svc.requires_deposit),
            reason=body.reason.strip() if body.reason else None,
        )
    except SlotConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    payment_url = None
    if svc.requires_deposit and svc.deposit_amount_paise and app_settings.razorpay_key_id and app_settings.razorpay_key_secret:
        try:
            from app.payments.router import create_razorpay_order, generate_payment_link

            order = await create_razorpay_order(
                db=db, tenant_id=str(tid), booking_id=booking["id"], amount_paise=svc.deposit_amount_paise
            )
            payment_url = generate_payment_link(order["razorpay_order_id"], svc.deposit_amount_paise, tenant.business_name)
        except Exception as exc:
            logger.warning("public_booking_deposit_order_failed", error=str(exc))

    return {
        **booking,
        "reference": booking["id"][:8].upper(),
        "service_name": svc.name,
        "business_name": tenant.business_name,
        "requires_deposit": bool(svc.requires_deposit),
        "deposit_amount_paise": svc.deposit_amount_paise,
        "payment_url": payment_url,
    }
