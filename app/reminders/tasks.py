"""Reminder tasks — Celery beat scans for upcoming appointments and sends reminders.

Runs every 5 minutes. Sends 2h-before reminders for confirmed bookings.
Uses reminder_log to prevent duplicate sends.
"""
from datetime import datetime, timezone

import structlog
from sqlalchemy import text

from app.celery_app import celery
from app.database import async_session_factory

logger = structlog.get_logger()

# How far ahead to look for reminders
REMINDER_HOURS_BEFORE = 2
REMINDER_WINDOW_MINUTES = 10  # tolerance window to avoid missing reminders


@celery.task(name="reminders.scan_and_send")
def scan_and_send_reminders():
    """Celery task: scan for bookings needing reminders and send them."""
    import asyncio
    asyncio.run(_scan_and_send())


async def _scan_and_send():
    """Find confirmed bookings inside each tenant's configured reminder window."""
    now = datetime.now(timezone.utc)

    async with async_session_factory() as db:
        # Honor tenant_settings.send_reminders and reminder_hours_before (default 2h if unset)
        result = await db.execute(
            text("""
                SELECT b.id, b.tenant_id, b.starts_at, b.customer_id,
                       c.phone as customer_phone, c.name as customer_name,
                       s.name as service_name, st.name as staff_name,
                       t.business_name, t.business_type, t.timezone,
                       t.waba_phone_number_id,
                       COALESCE(ts.reminder_hours_before, :default_hrs) as hours_before
                FROM bookings b
                JOIN customers c ON b.customer_id = c.id
                JOIN services s ON b.service_id = s.id
                LEFT JOIN staff st ON b.staff_id = st.id
                JOIN tenants t ON b.tenant_id = t.id
                LEFT JOIN tenant_settings ts ON ts.tenant_id = t.id
                WHERE b.status = 'confirmed'
                AND b.starts_at > :now
                AND COALESCE(ts.send_reminders, true) = true
                AND b.starts_at BETWEEN
                    (:now + ((COALESCE(ts.reminder_hours_before, :default_hrs) || ' hours')::interval) - (:tol || ' minutes')::interval)
                    AND
                    (:now + ((COALESCE(ts.reminder_hours_before, :default_hrs) || ' hours')::interval) + (:tol || ' minutes')::interval)
                AND b.cancellation_reason IS DISTINCT FROM 'BLOCKED'
                AND NOT EXISTS (
                    SELECT 1 FROM reminder_log rl
                    WHERE rl.booking_id = b.id
                )
            """),
            {"now": now, "default_hrs": REMINDER_HOURS_BEFORE, "tol": REMINDER_WINDOW_MINUTES},
        )
        bookings = result.fetchall()

        if not bookings:
            return

        logger.info("reminders_found", count=len(bookings))

        for booking in bookings:
            try:
                await _send_reminder(db, booking)
            except Exception as e:
                logger.error("reminder_failed", booking_id=str(booking.id), error=str(e))


async def _send_reminder(db, booking):
    """Send a single reminder using business-type vocabulary and tenant timezone."""
    import uuid
    import zoneinfo

    from app.labels import get_labels

    tz_name = getattr(booking, "timezone", None) or "Asia/Kolkata"
    tz = zoneinfo.ZoneInfo(tz_name)
    local_time = booking.starts_at.astimezone(tz)
    time_str = local_time.strftime("%I:%M %p").lstrip("0")
    date_str = local_time.strftime("%d %b")

    labels = get_labels(getattr(booking, "business_type", None) or "salon")

    # Build reminder message with vertical-appropriate labels & emoji
    message = (
        f"⏰ Reminder: You have an upcoming {labels['booking'].lower()} at *{booking.business_name}*\n\n"
        f"  📅 {date_str} at {time_str}\n"
        f"  {labels['welcome_emoji']} {booking.service_name}"
    )
    if booking.staff_name:
        message += f"\n  👤 {booking.staff_name}"

    message += "\n\nTo cancel or reschedule, reply *menu*."

    # Send via WhatsApp if WABA configured
    if booking.waba_phone_number_id and booking.customer_phone:
        from app.messaging.whatsapp.client import send_text_message
        phone = booking.customer_phone.lstrip("+")
        await send_text_message(
            phone_number_id=booking.waba_phone_number_id,
            to=phone,
            text=message,
            tenant_id=str(booking.tenant_id),
        )

    # Log to reminder_log (prevent duplicate sends)
    hrs = getattr(booking, "hours_before", REMINDER_HOURS_BEFORE)
    default_rule_id = uuid.uuid5(uuid.NAMESPACE_DNS, f"{booking.tenant_id}-{hrs}h-reminder")
    await db.execute(
        text("INSERT INTO reminder_log (id, booking_id, rule_id, sent_at) VALUES (:id, :bid, :rid, :now) ON CONFLICT DO NOTHING"),
        {"id": uuid.uuid4(), "bid": booking.id, "rid": default_rule_id, "now": datetime.now(timezone.utc)},
    )
    await db.commit()

    logger.info("reminder_sent", booking_id=str(booking.id), customer_phone=booking.customer_phone)
