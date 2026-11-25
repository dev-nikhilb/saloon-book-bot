"""Booking repository — all DB operations for bookings.

CRITICAL: Every method takes tenant_id. Every query filters by tenant_id.
Booking creation uses SELECT FOR UPDATE to prevent race conditions.
PostgreSQL exclusion constraint is the final safety net.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.bookings.models import BookingStatus
from app.bookings.state_machine import validate_transition
from app.exceptions import BookingNotFoundError, SlotConflictError

logger = structlog.get_logger()


async def create_booking(
    db: AsyncSession,
    tenant_id: UUID,
    branch_id: UUID,
    staff_id: UUID,
    customer_id: UUID,
    service_id: UUID,
    starts_at: datetime,
    ends_at: datetime,
    requires_deposit: bool = False,
    idempotency_key: str | None = None,
    reason: str | None = None,
    notes: str | None = None,
) -> dict:
    """Create a booking with row-level locking to prevent double-booking.

    Steps:
    1. SELECT FOR UPDATE on staff's bookings for the day (lock)
    2. Check for conflicts in application code
    3. INSERT booking
    4. PostgreSQL exclusion constraint is the final defence

    Returns dict with booking details.
    """
    booking_id = uuid.uuid4()
    status = BookingStatus.PENDING_DEPOSIT if requires_deposit else BookingStatus.CONFIRMED
    now = datetime.now(timezone.utc)

    try:
        # 1. Lock existing bookings for this staff on this day to prevent races
        await db.execute(
            text("""
                SELECT id FROM bookings
                WHERE staff_id = :staff_id
                AND starts_at::date = :booking_date
                AND status IN ('pending_deposit', 'confirmed')
                FOR UPDATE
            """),
            {"staff_id": staff_id, "booking_date": starts_at.date()},
        )

        # 2. Check for conflicts
        conflict = await db.execute(
            text("""
                SELECT id FROM bookings
                WHERE staff_id = :staff_id
                AND status IN ('pending_deposit', 'confirmed')
                AND starts_at < :ends_at AND ends_at > :starts_at
                LIMIT 1
            """),
            {"staff_id": staff_id, "starts_at": starts_at, "ends_at": ends_at},
        )
        if conflict.first():
            raise SlotConflictError("This time slot is no longer available")

        # 3. Insert booking
        await db.execute(
            text("""
                INSERT INTO bookings (id, tenant_id, branch_id, staff_id, customer_id, service_id,
                    starts_at, ends_at, status, idempotency_key, reason, notes, created_at, updated_at)
                VALUES (:id, :tid, :bid, :staff_id, :cid, :svc_id,
                    :starts_at, :ends_at, :status, :idem_key, :reason, :notes, :now, :now)
            """),
            {
                "id": booking_id, "tid": tenant_id, "bid": branch_id,
                "staff_id": staff_id, "cid": customer_id, "svc_id": service_id,
                "starts_at": starts_at, "ends_at": ends_at,
                "status": status, "idem_key": idempotency_key,
                "reason": reason, "notes": notes, "now": now,
            },
        )

        # 4. Update customer stats
        await db.execute(
            text("UPDATE customers SET total_bookings = total_bookings + 1 WHERE id = :cid AND tenant_id = :tid"),
            {"cid": customer_id, "tid": tenant_id},
        )

        await db.commit()

        logger.info("booking_created", booking_id=str(booking_id), staff_id=str(staff_id),
                     starts_at=starts_at.isoformat(), status=status)

        return {
            "id": str(booking_id),
            "tenant_id": str(tenant_id),
            "staff_id": str(staff_id),
            "customer_id": str(customer_id),
            "service_id": str(service_id),
            "starts_at": starts_at.isoformat(),
            "ends_at": ends_at.isoformat(),
            "status": status,
            "reason": reason,
            "notes": notes,
        }

    except SlotConflictError:
        await db.rollback()
        raise
    except Exception as e:
        await db.rollback()
        # Check if it's the exclusion constraint
        error_str = str(e)
        if "no_overlapping_staff_bookings" in error_str:
            raise SlotConflictError("This time slot is no longer available (constraint)")
        raise


async def update_booking_status(
    db: AsyncSession,
    tenant_id: UUID,
    booking_id: UUID,
    new_status: str,
    cancellation_reason: str | None = None,
) -> dict:
    """Transition a booking to a new status with validation."""
    result = await db.execute(
        text("SELECT id, status, starts_at, ends_at, staff_id FROM bookings WHERE id = :bid AND tenant_id = :tid"),
        {"bid": booking_id, "tid": tenant_id},
    )
    booking = result.first()
    if not booking:
        raise BookingNotFoundError(f"Booking {booking_id} not found")

    validate_transition(booking.status, new_status)

    now = datetime.now(timezone.utc)

    if new_status == BookingStatus.CANCELLED:
        await db.execute(
            text("UPDATE bookings SET status = :status, cancelled_at = :now, cancellation_reason = :reason, updated_at = :now WHERE id = :bid AND tenant_id = :tid"),
            {"status": new_status, "now": now, "reason": cancellation_reason, "bid": booking_id, "tid": tenant_id},
        )
    elif new_status == BookingStatus.NO_SHOW:
        await db.execute(
            text("UPDATE bookings SET status = :status, updated_at = :now WHERE id = :bid AND tenant_id = :tid"),
            {"status": new_status, "now": now, "bid": booking_id, "tid": tenant_id},
        )
        # Increment no-show count
        await db.execute(
            text("""UPDATE customers SET total_no_shows = total_no_shows + 1
                    WHERE id = (SELECT customer_id FROM bookings WHERE id = :bid) AND tenant_id = :tid"""),
            {"bid": booking_id, "tid": tenant_id},
        )
    elif new_status == BookingStatus.COMPLETED:
        await db.execute(
            text("UPDATE bookings SET status = :status, updated_at = :now WHERE id = :bid AND tenant_id = :tid"),
            {"status": new_status, "now": now, "bid": booking_id, "tid": tenant_id},
        )
        await db.execute(
            text("""UPDATE customers SET last_visit_at = :now
                    WHERE id = (SELECT customer_id FROM bookings WHERE id = :bid) AND tenant_id = :tid"""),
            {"now": now, "bid": booking_id, "tid": tenant_id},
        )
    else:
        await db.execute(
            text("UPDATE bookings SET status = :status, updated_at = :now WHERE id = :bid AND tenant_id = :tid"),
            {"status": new_status, "now": now, "bid": booking_id, "tid": tenant_id},
        )

    await db.commit()
    logger.info("booking_status_updated", booking_id=str(booking_id), old_status=booking.status, new_status=new_status)

    return {"id": str(booking_id), "old_status": booking.status, "new_status": new_status}


async def get_bookings_for_date(
    db: AsyncSession,
    tenant_id: UUID,
    target_date: datetime,
    staff_id: UUID | None = None,
) -> list[dict]:
    """Get all bookings for a tenant on a given date."""
    query = """
        SELECT b.id, b.starts_at, b.ends_at, b.status,
               s.name as service_name, st.name as staff_name,
               c.name as customer_name, c.phone as customer_phone
        FROM bookings b
        JOIN services s ON b.service_id = s.id
        LEFT JOIN staff st ON b.staff_id = st.id
        JOIN customers c ON b.customer_id = c.id
        WHERE b.tenant_id = :tid
        AND b.starts_at::date = :target_date
    """
    params: dict = {"tid": tenant_id, "target_date": target_date}

    if staff_id:
        query += " AND b.staff_id = :staff_id"
        params["staff_id"] = staff_id

    query += " ORDER BY b.starts_at"

    result = await db.execute(text(query), params)
    rows = result.fetchall()

    return [
        {
            "id": str(r.id),
            "starts_at": r.starts_at.isoformat(),
            "ends_at": r.ends_at.isoformat(),
            "status": r.status,
            "service": r.service_name,
            "staff": r.staff_name,
            "customer": r.customer_name,
            "phone": r.customer_phone,
        }
        for r in rows
    ]
