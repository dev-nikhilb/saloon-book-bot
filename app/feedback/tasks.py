"""Feedback tasks — Celery beat scans for completed bookings and sends feedback requests.

Runs every 30 minutes. Finds completed bookings from the last 24 hours that
don't have a feedback row yet, then sends a WhatsApp message with rating buttons.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import text

from app.celery_app import celery
from app.database import async_session_factory

logger = structlog.get_logger()


@celery.task(name="feedback.scan_and_send")
def scan_and_send_feedback_requests():
    """Celery task: scan for completed bookings and send feedback requests."""
    import asyncio

    asyncio.run(_scan_and_send())


async def _scan_and_send():
    """Find completed bookings from last 24h without feedback and send rating request."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)

    async with async_session_factory() as db:
        result = await db.execute(
            text("""
                SELECT b.id AS booking_id,
                       b.tenant_id,
                       b.customer_id,
                       c.phone AS customer_phone,
                       c.name AS customer_name,
                       s.name AS service_name,
                       t.business_name,
                       t.waba_phone_number_id,
                       ts.send_feedback_request,
                       ts.feedback_delay_hours
                FROM bookings b
                JOIN customers c ON b.customer_id = c.id
                JOIN services s ON b.service_id = s.id
                JOIN tenants t ON b.tenant_id = t.id
                LEFT JOIN tenant_settings ts ON ts.tenant_id = t.id
                WHERE b.status = 'completed'
                  AND b.ends_at >= :cutoff
                  AND b.ends_at <= :now
                  AND NOT EXISTS (
                      SELECT 1 FROM feedback f WHERE f.booking_id = b.id
                  )
                  AND COALESCE(ts.send_feedback_request, true) = true
                  AND b.ends_at + (COALESCE(ts.feedback_delay_hours, 2) || ' hours')::interval <= :now
            """),
            {"cutoff": cutoff, "now": now},
        )
        bookings = result.fetchall()

        if not bookings:
            logger.debug("feedback_scan_no_pending")
            return

        logger.info("feedback_requests_found", count=len(bookings))

        for booking in bookings:
            try:
                await _send_feedback_request(db, booking)
            except Exception as e:
                logger.error(
                    "feedback_request_failed",
                    booking_id=str(booking.booking_id),
                    error=str(e),
                )


async def _send_feedback_request(db, booking):
    """Send a feedback request via WhatsApp and insert a placeholder feedback row."""
    from app.messaging.whatsapp.client import ReplyButton, send_reply_buttons

    name = booking.customer_name or "there"
    body = (
        f"Hi {name}! Thank you for visiting *{booking.business_name}*.\n\n"
        f"How was your *{booking.service_name}* experience?\n"
        f"Please rate us from 1 to 5:"
    )
    buttons = [
        ReplyButton(id=f"fb_{booking.booking_id}_5", title="5 - Excellent"),
        ReplyButton(id=f"fb_{booking.booking_id}_3", title="3 - Average"),
        ReplyButton(id=f"fb_{booking.booking_id}_1", title="1 - Poor"),
    ]

    if booking.waba_phone_number_id and booking.customer_phone:
        phone = booking.customer_phone.lstrip("+")
        await send_reply_buttons(
            phone_number_id=booking.waba_phone_number_id,
            to=phone,
            body=body,
            buttons=buttons,
            header="Rate Your Visit",
            footer="Reply with a number 1-5 for other ratings",
            tenant_id=str(booking.tenant_id),
        )

    # Insert placeholder feedback row (rating=NULL means request sent but not yet answered)
    await db.execute(
        text("""
            INSERT INTO feedback (id, booking_id, tenant_id, created_at)
            VALUES (:id, :bid, :tid, :now)
            ON CONFLICT DO NOTHING
        """),
        {
            "id": uuid.uuid4(),
            "bid": booking.booking_id,
            "tid": booking.tenant_id,
            "now": datetime.now(timezone.utc),
        },
    )
    await db.commit()

    logger.info(
        "feedback_request_sent",
        booking_id=str(booking.booking_id),
        customer_phone=booking.customer_phone,
    )
