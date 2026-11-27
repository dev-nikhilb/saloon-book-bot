"""Celery scheduled tasks for subscription expiry warnings, grace windows, and suspension.

Runs hourly via Celery Beat (`billing.check_subscription_expiries`):
1. 3 days before expiry -> sends WhatsApp warning to owner (idempotent via warning_sent_3d_at)
2. 1 day before expiry  -> sends urgent 24h warning to owner (idempotent via warning_sent_1d_at)
3. On expiry:
   - trial  -> status = 'expired' + WhatsApp suspension notice
   - active -> status = 'past_due' + grace_period_ends_at = now + grace_days + urgent warning
4. On grace period end -> status = 'expired' + WhatsApp suspension notice
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.celery_app import celery
from app.config import settings
from app.database import async_session_factory

logger = structlog.get_logger()


@celery.task(name="billing.check_subscription_expiries")
def check_subscription_expiries() -> dict:
    """Celery task entrypoint."""
    return asyncio.run(_run_expiry_scan())


async def _run_expiry_scan() -> dict:
    async with async_session_factory() as db:
        return await process_subscription_expiries(db)


async def _notify_owner_whatsapp(
    db: AsyncSession,
    tenant_id,
    owner_phone: str,
    waba_phone_number_id: str | None,
    message: str,
) -> bool:
    """Best-effort WhatsApp alert to tenant owner (safe when WABA isn't configured in dev)."""
    if not owner_phone or not waba_phone_number_id:
        logger.info("billing_alert_logged_no_waba", tenant_id=str(tenant_id), msg=message[:120])
        return False
    try:
        from app.messaging.whatsapp.client import WhatsAppClient

        client = await WhatsAppClient.for_tenant(db, tenant_id, waba_phone_number_id)
        await client.send_text(owner_phone, message)
        return True
    except Exception as exc:
        logger.warning("billing_whatsapp_alert_failed", tenant_id=str(tenant_id), error=str(exc))
        return False


async def process_subscription_expiries(db: AsyncSession) -> dict:
    """Core async scanner (directly testable with an AsyncSession)."""
    now = datetime.now(timezone.utc)
    in_3d = now + timedelta(days=3)
    in_1d = now + timedelta(days=1)
    renew_url = f"{settings.dashboard_url.rstrip('/')}#settings"

    stats = {"warned_3d": 0, "warned_1d": 0, "moved_past_due": 0, "expired": 0}

    # 1. Upcoming 3-day warnings (1d < end <= 3d)
    res_3d = await db.execute(
        text("""
            SELECT s.id, s.tenant_id, s.plan_tier, s.status, s.current_period_end, s.trial_ends_at,
                   t.business_name, t.owner_phone, t.waba_phone_number_id
            FROM subscriptions s
            JOIN tenants t ON t.id = s.tenant_id
            WHERE s.status IN ('trial', 'active')
              AND s.warning_sent_3d_at IS NULL
              AND COALESCE(CASE WHEN s.status = 'trial' THEN s.trial_ends_at END, s.current_period_end) > :in_1d
              AND COALESCE(CASE WHEN s.status = 'trial' THEN s.trial_ends_at END, s.current_period_end) <= :in_3d
        """),
        {"in_1d": in_1d, "in_3d": in_3d},
    )
    for row in res_3d.fetchall():
        kind = "free trial" if row.status == "trial" else f"{row.plan_tier.capitalize()} subscription"
        msg = (
            f"⏰ *Tivoria Renewal Reminder ({row.business_name})*\n\n"
            f"Your {kind} ends in *3 days*.\n"
            f"Renew now so WhatsApp & web bookings continue without interruption:\n{renew_url}"
        )
        await _notify_owner_whatsapp(db, row.tenant_id, row.owner_phone, row.waba_phone_number_id, msg)
        await db.execute(
            text("UPDATE subscriptions SET warning_sent_3d_at = :now WHERE id = :id"),
            {"now": now, "id": row.id},
        )
        stats["warned_3d"] += 1

    # 2. Urgent 1-day warnings (now < end <= 1d)
    res_1d = await db.execute(
        text("""
            SELECT s.id, s.tenant_id, s.plan_tier, s.status,
                   t.business_name, t.owner_phone, t.waba_phone_number_id
            FROM subscriptions s
            JOIN tenants t ON t.id = s.tenant_id
            WHERE s.status IN ('trial', 'active')
              AND s.warning_sent_1d_at IS NULL
              AND COALESCE(CASE WHEN s.status = 'trial' THEN s.trial_ends_at END, s.current_period_end) > :now
              AND COALESCE(CASE WHEN s.status = 'trial' THEN s.trial_ends_at END, s.current_period_end) <= :in_1d
        """),
        {"now": now, "in_1d": in_1d},
    )
    for row in res_1d.fetchall():
        kind = "free trial" if row.status == "trial" else f"{row.plan_tier.capitalize()} subscription"
        msg = (
            f"⚠️ *Final Notice — {row.business_name}*\n\n"
            f"Your Tivoria {kind} ends in *less than 24 hours*.\n"
            f"Renew here to keep bookings active:\n{renew_url}"
        )
        await _notify_owner_whatsapp(db, row.tenant_id, row.owner_phone, row.waba_phone_number_id, msg)
        await db.execute(
            text("UPDATE subscriptions SET warning_sent_1d_at = :now WHERE id = :id"),
            {"now": now, "id": row.id},
        )
        stats["warned_1d"] += 1

    # 3. Expired trials -> suspended immediately
    res_trial_exp = await db.execute(
        text("""
            SELECT s.id, s.tenant_id, t.business_name, t.owner_phone, t.waba_phone_number_id
            FROM subscriptions s
            JOIN tenants t ON t.id = s.tenant_id
            WHERE s.status = 'trial'
              AND COALESCE(s.trial_ends_at, s.current_period_end) <= :now
        """),
        {"now": now},
    )
    for row in res_trial_exp.fetchall():
        msg = (
            f"🔴 *Tivoria Trial Ended — {row.business_name}*\n\n"
            f"Your 14-day free trial has ended. New customer bookings are now paused, "
            f"while your dashboard and existing appointments remain readable.\n\n"
            f"Pick a plan (starting ₹499/mo) to resume immediately:\n{renew_url}"
        )
        await _notify_owner_whatsapp(db, row.tenant_id, row.owner_phone, row.waba_phone_number_id, msg)
        await db.execute(
            text("UPDATE subscriptions SET status = 'expired', expiry_notified_at = :now WHERE id = :id"),
            {"now": now, "id": row.id},
        )
        stats["expired"] += 1

    # 4. Active paid plans past period_end -> past_due with grace window
    res_active_exp = await db.execute(
        text("""
            SELECT s.id, s.tenant_id, s.plan_tier, t.business_name, t.owner_phone, t.waba_phone_number_id
            FROM subscriptions s
            JOIN tenants t ON t.id = s.tenant_id
            WHERE s.status = 'active'
              AND s.current_period_end <= :now
        """),
        {"now": now},
    )
    grace_end = now + timedelta(days=settings.subscription_grace_days)
    for row in res_active_exp.fetchall():
        msg = (
            f"⚠️ *Renewal Overdue — {row.business_name}*\n\n"
            f"Your {row.plan_tier.capitalize()} plan period ended. You have a "
            f"*{settings.subscription_grace_days}-day grace period* before new bookings pause.\n"
            f"Renew now:\n{renew_url}"
        )
        await _notify_owner_whatsapp(db, row.tenant_id, row.owner_phone, row.waba_phone_number_id, msg)
        await db.execute(
            text("""
                UPDATE subscriptions
                SET status = 'past_due', grace_period_ends_at = :grace_end
                WHERE id = :id
            """),
            {"grace_end": grace_end, "id": row.id},
        )
        stats["moved_past_due"] += 1

    # 5. Past-due subscriptions past grace_period_ends_at -> expired
    res_grace_exp = await db.execute(
        text("""
            SELECT s.id, s.tenant_id, t.business_name, t.owner_phone, t.waba_phone_number_id
            FROM subscriptions s
            JOIN tenants t ON t.id = s.tenant_id
            WHERE s.status = 'past_due'
              AND s.grace_period_ends_at IS NOT NULL
              AND s.grace_period_ends_at <= :now
        """),
        {"now": now},
    )
    for row in res_grace_exp.fetchall():
        msg = (
            f"🔴 *Tivoria Paused — {row.business_name}*\n\n"
            f"Your grace period ended and new bookings are now paused (your dashboard remains available in read-only mode).\n"
            f"Renew anytime to resume immediately:\n{renew_url}"
        )
        await _notify_owner_whatsapp(db, row.tenant_id, row.owner_phone, row.waba_phone_number_id, msg)
        await db.execute(
            text("UPDATE subscriptions SET status = 'expired', expiry_notified_at = :now WHERE id = :id"),
            {"now": now, "id": row.id},
        )
        stats["expired"] += 1

    await db.commit()
    logger.info("subscription_expiry_scan_complete", **stats)
    return stats
