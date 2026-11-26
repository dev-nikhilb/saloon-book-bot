"""Celery task to expire unpaid pending_deposit holds so slots aren't locked forever."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.celery_app import celery
from app.database import async_session_factory

logger = structlog.get_logger()


@celery.task(name="payments.expire_unpaid_deposits")
def expire_unpaid_deposits() -> dict:
    return asyncio.run(_run_expire_deposits())


async def _run_expire_deposits() -> dict:
    async with async_session_factory() as db:
        return await expire_stale_deposit_holds(db)


async def expire_stale_deposit_holds(db: AsyncSession) -> dict:
    """Cancel pending_deposit bookings whose unpaid hold has expired (skips submitted_utr)."""
    now = datetime.now(timezone.utc)
    res = await db.execute(
        text("""
            SELECT p.id AS payment_id, p.booking_id, p.tenant_id,
                   c.phone AS customer_phone, s.name AS service_name,
                   t.business_name, t.waba_phone_number_id
            FROM payments p
            JOIN bookings b ON b.id = p.booking_id
            JOIN customers c ON c.id = b.customer_id
            JOIN services s ON s.id = b.service_id
            JOIN tenants t ON t.id = p.tenant_id
            WHERE b.status = 'pending_deposit'
              AND p.status = 'created'
              AND p.expires_at IS NOT NULL
              AND p.expires_at <= :now
        """),
        {"now": now},
    )
    rows = res.fetchall()
    expired_count = 0

    for row in rows:
        await db.execute(
            text("UPDATE payments SET status = 'expired' WHERE id = :pid"),
            {"pid": row.payment_id},
        )
        await db.execute(
            text("""
                UPDATE bookings
                SET status = 'cancelled',
                    cancellation_reason = 'Deposit hold expired',
                    updated_at = :now
                WHERE id = :bid AND status = 'pending_deposit'
            """),
            {"bid": row.booking_id, "now": now},
        )
        expired_count += 1

        if row.customer_phone and row.waba_phone_number_id:
            try:
                from app.messaging.whatsapp.client import WhatsAppClient

                client = await WhatsAppClient.for_tenant(db, row.tenant_id, row.waba_phone_number_id)
                await client.send_text(
                    row.customer_phone,
                    f"⏳ Your deposit hold for *{row.service_name}* at *{row.business_name}* expired "
                    "and the slot has been released. Reply *Hi* anytime to book a new slot.",
                )
            except Exception as exc:
                logger.warning("deposit_expiry_notify_failed", error=str(exc))

    await db.commit()
    logger.info("unpaid_deposits_expired", count=expired_count)
    return {"expired": expired_count}
