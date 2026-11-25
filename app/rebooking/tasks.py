"""Churn-prevention rebooking nudges via Celery Beat.

Runs daily. Identifies customers whose last completed visit was N days ago
(per vertical's natural repeat cycle, e.g., 28d for salons, 180d for dental)
with no upcoming appointment, and sends a gentle WhatsApp rebooking reminder.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import text

from app.celery_app import celery
from app.database import async_session_factory
from app.labels import get_labels
from app.verticals import get_vertical

logger = structlog.get_logger()


@celery.task(name="rebooking.scan_and_nudge")
def scan_and_nudge_rebookings():
    """Celery task: send rebooking nudges to lapsed repeat customers."""
    asyncio.run(_scan_and_nudge())


async def _scan_and_nudge() -> int:
    """Scan active Growth/Pro/Trial tenants for lapsed customers and send nudges."""
    now = datetime.now(timezone.utc)
    sent_count = 0

    async with async_session_factory() as db:
        tenants_res = await db.execute(
            text("""
                SELECT t.id, t.business_name, t.business_type, t.waba_phone_number_id, t.plan_tier
                FROM tenants t
                LEFT JOIN subscriptions sub ON sub.tenant_id = t.id
                WHERE t.is_active = true
                  AND t.waba_phone_number_id IS NOT NULL
                  AND (t.plan_tier IN ('growth', 'pro') OR sub.status = 'trial')
            """)
        )
        tenants = tenants_res.fetchall()

        for tenant in tenants:
            vertical = get_vertical(tenant.business_type or "salon")
            days = int(vertical.get("default_rebooking_days", 30))
            window_end = now - timedelta(days=days)
            window_start = window_end - timedelta(days=7)
            cooldown_cutoff = now - timedelta(days=30)

            cust_res = await db.execute(
                text("""
                    SELECT c.id, c.phone, c.name, c.last_visit_at
                    FROM customers c
                    WHERE c.tenant_id = :tid
                      AND c.last_visit_at BETWEEN :ws AND :we
                      AND NOT EXISTS (
                          SELECT 1 FROM bookings b
                          WHERE b.customer_id = c.id
                            AND b.tenant_id = :tid
                            AND b.status IN ('confirmed', 'pending_deposit')
                            AND b.starts_at > :now
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM messages m
                          WHERE m.tenant_id = :tid
                            AND m.direction = 'outbound'
                            AND m.template_name = 'rebooking_nudge'
                            AND m.customer_id = c.id
                            AND m.created_at >= :cooldown
                      )
                    LIMIT 50
                """),
                {
                    "tid": tenant.id,
                    "ws": window_start,
                    "we": window_end,
                    "now": now,
                    "cooldown": cooldown_cutoff,
                },
            )
            customers = cust_res.fetchall()

            labels = get_labels(tenant.business_type or "salon")
            for cust in customers:
                try:
                    from app.messaging.whatsapp.client import ReplyButton, send_reply_buttons

                    name = cust.name or "there"
                    body = (
                        f"{labels['welcome_emoji']} Hi {name}! It's been a while since your last visit to "
                        f"*{tenant.business_name}*.\n\n"
                        f"Ready for your next {labels['booking'].lower()}? Tap below to pick a slot in seconds."
                    )
                    await send_reply_buttons(
                        phone_number_id=tenant.waba_phone_number_id,
                        to=cust.phone.lstrip("+"),
                        body=body,
                        buttons=[
                            ReplyButton(id="action_book", title=f"{labels['booking_emoji']} {labels['service_action']}"[:20]),
                        ],
                        tenant_id=str(tenant.id),
                    )
                    sent_count += 1
                except Exception as exc:
                    logger.error("rebooking_nudge_failed", customer_id=str(cust.id), error=str(exc))

    logger.info("rebooking_scan_complete", sent=sent_count)
    return sent_count
