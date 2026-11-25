"""SaaS Billing, Plan Tiers, Trials, and Usage Limit Enforcement."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import PlanLimitExceededError

PLANS: dict[str, dict] = {
    "solo": {
        "tier": "solo",
        "name": "Solo",
        "price_monthly_inr": 499,
        "price_annual_inr": 4990,
        "max_staff": 2,
        "max_branches": 1,
        "max_monthly_bookings": 300,
        "deposits_enabled": False,
        "rebooking_enabled": False,
        "google_calendar_enabled": False,
        "public_booking_page": True,
        "features": [
            "Up to 2 team members",
            "1 location / branch",
            "300 bookings / month",
            "WhatsApp booking & owner commands",
            "Shareable web booking page",
            "Automated appointment reminders",
        ],
    },
    "growth": {
        "tier": "growth",
        "name": "Growth",
        "price_monthly_inr": 1499,
        "price_annual_inr": 14990,
        "max_staff": 10,
        "max_branches": 2,
        "max_monthly_bookings": 1500,
        "deposits_enabled": True,
        "rebooking_enabled": True,
        "google_calendar_enabled": True,
        "public_booking_page": True,
        "features": [
            "Up to 10 team members",
            "Up to 2 locations / branches",
            "1,500 bookings / month",
            "Razorpay deposit collection",
            "Automated feedback + Google Reviews",
            "Churn-prevention rebooking nudges",
            "Full analytics dashboard",
        ],
    },
    "pro": {
        "tier": "pro",
        "name": "Pro",
        "price_monthly_inr": 2999,
        "price_annual_inr": 29990,
        "max_staff": None,  # Unlimited
        "max_branches": None,  # Unlimited
        "max_monthly_bookings": None,  # Unlimited
        "deposits_enabled": True,
        "rebooking_enabled": True,
        "google_calendar_enabled": True,
        "public_booking_page": True,
        "features": [
            "Unlimited team members",
            "Unlimited branches / locations",
            "Unlimited bookings",
            "All Growth features + priority support",
            "2-way Google Calendar sync",
            "Custom subdomain branding",
        ],
    },
}

TRIAL_DAYS = 14


def get_plan(tier: str) -> dict:
    return PLANS.get(tier, PLANS["solo"])


def list_plans() -> list[dict]:
    return [PLANS["solo"], PLANS["growth"], PLANS["pro"]]


async def ensure_trial_subscription(db: AsyncSession, tenant_id: uuid.UUID, plan_tier: str = "solo") -> dict:
    """Create a 14-day trial subscription if the tenant does not already have one."""
    res = await db.execute(
        text("SELECT * FROM subscriptions WHERE tenant_id = :tid ORDER BY current_period_end DESC LIMIT 1"),
        {"tid": tenant_id},
    )
    row = res.first()
    if row:
        return dict(row._mapping)

    now = datetime.now(timezone.utc)
    trial_end = now + timedelta(days=TRIAL_DAYS)
    sub_id = uuid.uuid4()
    await db.execute(
        text("""
            INSERT INTO subscriptions (
                id, tenant_id, plan_tier, billing_cycle,
                current_period_start, current_period_end, status, trial_ends_at
            ) VALUES (
                :id, :tid, :tier, 'monthly', :start, :end, 'trial', :end
            )
        """),
        {"id": sub_id, "tid": tenant_id, "tier": plan_tier, "start": now, "end": trial_end},
    )
    return {
        "id": sub_id,
        "tenant_id": tenant_id,
        "plan_tier": plan_tier,
        "billing_cycle": "monthly",
        "current_period_start": now,
        "current_period_end": trial_end,
        "status": "trial",
        "trial_ends_at": trial_end,
    }


async def get_effective_plan(db: AsyncSession, tenant_id: uuid.UUID) -> tuple[dict, str]:
    """Return (plan_dict, subscription_status).

    Active 14-day trials unlock Growth-level limits so new owners can evaluate
    multi-staff and full features before upgrading.
    """
    t_res = await db.execute(
        text("SELECT plan_tier FROM tenants WHERE id = :tid LIMIT 1"),
        {"tid": tenant_id},
    )
    t_row = t_res.first()
    tier = (t_row.plan_tier if t_row else "solo") or "solo"

    sub_res = await db.execute(
        text("SELECT plan_tier, status, trial_ends_at FROM subscriptions WHERE tenant_id = :tid ORDER BY current_period_end DESC LIMIT 1"),
        {"tid": tenant_id},
    )
    sub = sub_res.first()
    sub_status = sub.status if sub else "active"

    if sub and sub.status == "trial" and sub.trial_ends_at and sub.trial_ends_at > datetime.now(timezone.utc):
        if tier == "solo":
            return get_plan("growth"), "trial"
    return get_plan(tier), sub_status


async def get_usage_summary(db: AsyncSession, tenant_id: uuid.UUID) -> dict:
    """Return current tenant plan, subscription info, and live resource usage."""
    t_res = await db.execute(
        text("SELECT plan_tier FROM tenants WHERE id = :tid"),
        {"tid": tenant_id},
    )
    t_row = t_res.first()
    tier = (t_row.plan_tier if t_row else "solo") or "solo"

    sub_res = await db.execute(
        text("SELECT id, plan_tier, billing_cycle, status, current_period_start, current_period_end, trial_ends_at FROM subscriptions WHERE tenant_id = :tid ORDER BY current_period_end DESC LIMIT 1"),
        {"tid": tenant_id},
    )
    sub = sub_res.first()

    effective_plan, sub_status = await get_effective_plan(db, tenant_id)

    staff_cnt = (await db.execute(
        text("SELECT COUNT(*) FROM staff WHERE tenant_id = :tid AND is_active = true"),
        {"tid": tenant_id},
    )).scalar() or 0

    branch_cnt = (await db.execute(
        text("SELECT COUNT(*) FROM branches WHERE tenant_id = :tid"),
        {"tid": tenant_id},
    )).scalar() or 0

    month_bookings = (await db.execute(
        text("""
            SELECT COUNT(*) FROM bookings
            WHERE tenant_id = :tid
              AND cancellation_reason IS DISTINCT FROM 'BLOCKED'
              AND created_at >= date_trunc('month', NOW())
        """),
        {"tid": tenant_id},
    )).scalar() or 0

    return {
        "plan_tier": tier,
        "effective_tier": effective_plan["tier"],
        "status": sub_status,
        "billing_cycle": sub.billing_cycle if sub else "monthly",
        "trial_ends_at": sub.trial_ends_at.isoformat() if sub and sub.trial_ends_at else None,
        "current_period_end": sub.current_period_end.isoformat() if sub and sub.current_period_end else None,
        "limits": {
            "max_staff": effective_plan["max_staff"],
            "max_branches": effective_plan["max_branches"],
            "max_monthly_bookings": effective_plan["max_monthly_bookings"],
        },
        "usage": {
            "staff": staff_cnt,
            "branches": branch_cnt,
            "monthly_bookings": month_bookings,
        },
        "plan": get_plan(tier),
    }


async def enforce_plan_limit(db: AsyncSession, tenant_id: uuid.UUID, resource: str) -> None:
    """Raise PlanLimitExceededError if creating one more `resource` ('staff', 'branches', 'bookings') exceeds plan."""
    effective_plan, _ = await get_effective_plan(db, tenant_id)

    if resource == "staff":
        maximum = effective_plan["max_staff"]
        if maximum is None:
            return
        current = (await db.execute(
            text("SELECT COUNT(*) FROM staff WHERE tenant_id = :tid AND is_active = true"),
            {"tid": tenant_id},
        )).scalar() or 0
        if current >= maximum:
            raise PlanLimitExceededError("staff", current, maximum)

    elif resource == "branches":
        maximum = effective_plan["max_branches"]
        if maximum is None:
            return
        current = (await db.execute(
            text("SELECT COUNT(*) FROM branches WHERE tenant_id = :tid"),
            {"tid": tenant_id},
        )).scalar() or 0
        if current >= maximum:
            raise PlanLimitExceededError("branches", current, maximum)

    elif resource == "bookings":
        maximum = effective_plan["max_monthly_bookings"]
        if maximum is None:
            return
        current = (await db.execute(
            text("""
                SELECT COUNT(*) FROM bookings
                WHERE tenant_id = :tid
                  AND cancellation_reason IS DISTINCT FROM 'BLOCKED'
                  AND created_at >= date_trunc('month', NOW())
            """),
            {"tid": tenant_id},
        )).scalar() or 0
        if current >= maximum:
            raise PlanLimitExceededError("monthly_bookings", current, maximum)
