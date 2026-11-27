"""SaaS Billing, Plan Tiers, Entitlements, Lifecycle, and Razorpay Subscriptions."""
from __future__ import annotations

import hashlib
import hmac
import math
import uuid
from datetime import datetime, timedelta, timezone

import razorpay
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.exceptions import (
    DowngradeConflictError,
    FeatureNotAvailableError,
    PlanLimitExceededError,
    SubscriptionExpiredError,
)

logger = structlog.get_logger()

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
        "custom_subdomain": False,
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
        "custom_subdomain": False,
        "public_booking_page": True,
        "features": [
            "Up to 10 team members",
            "Up to 2 locations / branches",
            "1,500 bookings / month",
            "WhatsApp deposits (Razorpay + UPI)",
            "Automated feedback + Google Reviews",
            "Churn-prevention rebooking nudges",
            "Google Calendar sync & full analytics",
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
        "custom_subdomain": True,
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

FEATURE_MIN_PLAN: dict[str, str] = {
    "deposits_enabled": "growth",
    "rebooking_enabled": "growth",
    "google_calendar_enabled": "growth",
    "custom_subdomain": "pro",
    "public_booking_page": "solo",
}

TRIAL_DAYS = 14


def get_plan(tier: str) -> dict:
    return PLANS.get(tier, PLANS["solo"])


def list_plans() -> list[dict]:
    return [PLANS["solo"], PLANS["growth"], PLANS["pro"]]


def get_plan_amount_paise(tier: str, billing_cycle: str = "monthly") -> int:
    plan = get_plan(tier)
    inr = plan["price_annual_inr"] if billing_cycle == "annual" else plan["price_monthly_inr"]
    return int(inr) * 100


def get_razorpay_plan_id(tier: str, billing_cycle: str = "monthly") -> str:
    """Resolve configured Razorpay Plan ID for a tier + billing cycle."""
    attr = f"razorpay_plan_{tier}_{billing_cycle}"
    return getattr(settings, attr, "") or ""


def is_razorpay_configured() -> bool:
    return bool(settings.razorpay_key_id and settings.razorpay_key_secret)


def _get_razorpay_client() -> razorpay.Client:
    return razorpay.Client(auth=(settings.razorpay_key_id, settings.razorpay_key_secret))


async def ensure_trial_subscription(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    plan_tier: str = "solo",
    billing_cycle: str = "monthly",
) -> dict:
    """Create a 14-day trial subscription if the tenant does not already have one."""
    res = await db.execute(
        text(
            "SELECT * FROM subscriptions WHERE tenant_id = :tid "
            "ORDER BY current_period_end DESC LIMIT 1"
        ),
        {"tid": tenant_id},
    )
    row = res.first()
    if row:
        return dict(row._mapping)

    tier = plan_tier if plan_tier in PLANS else "solo"
    cycle = billing_cycle if billing_cycle in ("monthly", "annual") else "monthly"
    now = datetime.now(timezone.utc)
    trial_end = now + timedelta(days=TRIAL_DAYS)
    sub_id = uuid.uuid4()
    await db.execute(
        text("""
            INSERT INTO subscriptions (
                id, tenant_id, plan_tier, billing_cycle,
                current_period_start, current_period_end, status, trial_ends_at
            ) VALUES (
                :id, :tid, :tier, :cycle, :start, :end, 'trial', :end
            )
        """),
        {"id": sub_id, "tid": tenant_id, "tier": tier, "cycle": cycle, "start": now, "end": trial_end},
    )
    return {
        "id": sub_id,
        "tenant_id": tenant_id,
        "plan_tier": tier,
        "billing_cycle": cycle,
        "current_period_start": now,
        "current_period_end": trial_end,
        "status": "trial",
        "trial_ends_at": trial_end,
    }


async def get_subscription_state(db: AsyncSession, tenant_id: uuid.UUID) -> dict:
    """Compute canonical subscription status, access_mode, warnings, and effective entitlements.

    access_mode:
      - 'full'          : trial, active, or cancelled-before-period-end
      - 'grace_warning' : past_due within grace_period_ends_at (all ops allowed + loud warning)
      - 'suspended'     : trial expired, past_due past grace, expired, or cancelled past period end
                          (read-only dashboard & billing allowed; new bookings/writes paused)
    """
    t_res = await db.execute(
        text("SELECT plan_tier FROM tenants WHERE id = :tid LIMIT 1"),
        {"tid": tenant_id},
    )
    t_row = t_res.first()
    tier = (t_row.plan_tier if t_row else "solo") or "solo"

    sub_res = await db.execute(
        text("""
            SELECT id, plan_tier, billing_cycle, status,
                   current_period_start, current_period_end, trial_ends_at,
                   razorpay_subscription_id, razorpay_plan_id,
                   grace_period_ends_at, cancelled_at, cancel_at_period_end
            FROM subscriptions
            WHERE tenant_id = :tid
            ORDER BY current_period_end DESC
            LIMIT 1
        """),
        {"tid": tenant_id},
    )
    sub = sub_res.first()
    now = datetime.now(timezone.utc)

    # Legacy/seeded tenant with no subscription row yet: treat as active on their plan_tier
    if not sub:
        plan = get_plan(tier)
        return {
            "subscription_id": None,
            "plan_tier": tier,
            "effective_tier": plan["tier"],
            "status": "active",
            "billing_cycle": "monthly",
            "access_mode": "full",
            "is_active_access": True,
            "is_expired": False,
            "days_remaining": None,
            "warning_level": None,
            "warning_message": None,
            "trial_ends_at": None,
            "current_period_end": None,
            "grace_period_ends_at": None,
            "cancel_at_period_end": False,
            "razorpay_subscription_id": None,
            "plan": plan,
            "entitlements": _extract_entitlements(plan),
        }

    status = sub.status
    end_dt = sub.trial_ends_at if status == "trial" and sub.trial_ends_at else sub.current_period_end
    grace_end = sub.grace_period_ends_at
    cancel_at_end = bool(getattr(sub, "cancel_at_period_end", False))

    access_mode = "full"
    effective_status = status
    warning_level: str | None = None
    warning_message: str | None = None
    days_remaining: int | None = None

    if status == "trial":
        if end_dt and end_dt <= now:
            effective_status = "expired"
            access_mode = "suspended"
            days_remaining = 0
            warning_level = "expired"
            warning_message = (
                "Your 14-day free trial has ended. New bookings are paused — choose a plan to resume immediately."
            )
        else:
            days_remaining = max(0, math.ceil((end_dt - now).total_seconds() / 86400)) if end_dt else TRIAL_DAYS
            if days_remaining <= 1:
                warning_level = "critical"
                warning_message = "Your free trial ends within 24 hours. Upgrade now to avoid pausing bookings."
            elif days_remaining <= 3:
                warning_level = "warning"
                warning_message = f"Your free trial ends in {days_remaining} days. Choose a plan to stay uninterrupted."
            elif days_remaining <= 7:
                warning_level = "info"
                warning_message = f"{days_remaining} days left in your Growth-tier free trial."

    elif status == "active":
        if end_dt and end_dt <= now:
            # Auto-apply grace window even before Celery task runs
            eff_grace = grace_end or (end_dt + timedelta(days=settings.subscription_grace_days))
            if eff_grace > now:
                effective_status = "past_due"
                access_mode = "grace_warning"
                days_remaining = max(1, math.ceil((eff_grace - now).total_seconds() / 86400))
                warning_level = "critical"
                warning_message = (
                    f"Renewal overdue — {days_remaining} day(s) of grace period remaining before bookings pause."
                )
            else:
                effective_status = "expired"
                access_mode = "suspended"
                days_remaining = 0
                warning_level = "expired"
                warning_message = "Your subscription has expired. Renew now to resume accepting new bookings."
        else:
            days_remaining = max(0, math.ceil((end_dt - now).total_seconds() / 86400)) if end_dt else None
            if cancel_at_end and days_remaining is not None:
                warning_level = "warning" if days_remaining <= 3 else "info"
                warning_message = f"Subscription set to cancel in {days_remaining} day(s)."
            elif days_remaining is not None and days_remaining <= 1:
                warning_level = "critical"
                warning_message = "Your subscription renews/expires within 24 hours."
            elif days_remaining is not None and days_remaining <= 3:
                warning_level = "warning"
                warning_message = f"Your subscription period ends in {days_remaining} days."

    elif status == "past_due":
        eff_grace = grace_end or (
            (end_dt + timedelta(days=settings.subscription_grace_days)) if end_dt else None
        )
        if eff_grace and eff_grace > now:
            access_mode = "grace_warning"
            days_remaining = max(1, math.ceil((eff_grace - now).total_seconds() / 86400))
            warning_level = "critical"
            warning_message = (
                f"Payment failed — {days_remaining} day(s) left in grace period before bookings pause. Please renew."
            )
        else:
            effective_status = "expired"
            access_mode = "suspended"
            days_remaining = 0
            warning_level = "expired"
            warning_message = "Your subscription grace period ended. Renew to resume accepting new bookings."

    elif status == "cancelled":
        if end_dt and end_dt > now:
            access_mode = "full"
            days_remaining = max(1, math.ceil((end_dt - now).total_seconds() / 86400))
            warning_level = "warning" if days_remaining <= 3 else "info"
            warning_message = f"Subscription cancelled — access continues for {days_remaining} more day(s)."
        else:
            effective_status = "expired"
            access_mode = "suspended"
            days_remaining = 0
            warning_level = "expired"
            warning_message = "Your cancelled subscription period has ended. Reactivate any plan to resume."

    else:
        # expired / suspended / halted
        effective_status = "expired"
        access_mode = "suspended"
        days_remaining = 0
        warning_level = "expired"
        warning_message = "Your subscription is inactive. Renew a plan to resume accepting new bookings."

    # Enforce limits matching the tenant's selected plan tier (users can switch tiers freely during trial)
    effective_plan = get_plan(tier)

    return {
        "subscription_id": str(sub.id),
        "plan_tier": tier,
        "effective_tier": effective_plan["tier"],
        "status": effective_status,
        "billing_cycle": sub.billing_cycle or "monthly",
        "access_mode": access_mode,
        "is_active_access": access_mode != "suspended",
        "is_expired": access_mode == "suspended",
        "days_remaining": days_remaining,
        "warning_level": warning_level,
        "warning_message": warning_message,
        "trial_ends_at": sub.trial_ends_at.isoformat() if sub.trial_ends_at else None,
        "current_period_end": sub.current_period_end.isoformat() if sub.current_period_end else None,
        "grace_period_ends_at": sub.grace_period_ends_at.isoformat() if sub.grace_period_ends_at else None,
        "cancel_at_period_end": cancel_at_end,
        "razorpay_subscription_id": sub.razorpay_subscription_id,
        "plan": get_plan(tier),
        "effective_plan": effective_plan,
        "entitlements": _extract_entitlements(effective_plan),
    }


def _extract_entitlements(plan: dict) -> dict:
    return {
        "max_staff": plan["max_staff"],
        "max_branches": plan["max_branches"],
        "max_monthly_bookings": plan["max_monthly_bookings"],
        "deposits_enabled": bool(plan.get("deposits_enabled", False)),
        "rebooking_enabled": bool(plan.get("rebooking_enabled", False)),
        "google_calendar_enabled": bool(plan.get("google_calendar_enabled", False)),
        "custom_subdomain": bool(plan.get("custom_subdomain", False)),
        "public_booking_page": bool(plan.get("public_booking_page", True)),
    }


async def get_effective_plan(db: AsyncSession, tenant_id: uuid.UUID) -> tuple[dict, str]:
    """Return (effective_plan_dict, subscription_status)."""
    state = await get_subscription_state(db, tenant_id)
    return state.get("effective_plan", state["plan"]), state["status"]


async def require_active_subscription(db: AsyncSession, tenant_id: uuid.UUID) -> dict:
    """Raise SubscriptionExpiredError if tenant is suspended/expired past grace."""
    state = await get_subscription_state(db, tenant_id)
    if state["access_mode"] == "suspended":
        raise SubscriptionExpiredError(status=state["status"], message=state["warning_message"])
    return state


async def require_feature(db: AsyncSession, tenant_id: uuid.UUID, feature: str) -> dict:
    """Verify tenant has an active subscription AND the requested boolean feature."""
    state = await require_active_subscription(db, tenant_id)
    ent = state["entitlements"]
    if not ent.get(feature, False):
        required = FEATURE_MIN_PLAN.get(feature, "growth")
        raise FeatureNotAvailableError(feature=feature, required_plan=required)
    return state


async def _get_live_usage(db: AsyncSession, tenant_id: uuid.UUID) -> dict[str, int]:
    staff_cnt = (
        await db.execute(
            text("SELECT COUNT(*) FROM staff WHERE tenant_id = :tid AND is_active = true"),
            {"tid": tenant_id},
        )
    ).scalar() or 0

    branch_cnt = (
        await db.execute(
            text("SELECT COUNT(*) FROM branches WHERE tenant_id = :tid"),
            {"tid": tenant_id},
        )
    ).scalar() or 0

    month_bookings = (
        await db.execute(
            text("""
                SELECT COUNT(*) FROM bookings
                WHERE tenant_id = :tid
                  AND cancellation_reason IS DISTINCT FROM 'BLOCKED'
                  AND created_at >= date_trunc('month', NOW())
            """),
            {"tid": tenant_id},
        )
    ).scalar() or 0

    return {
        "staff": int(staff_cnt),
        "branches": int(branch_cnt),
        "monthly_bookings": int(month_bookings),
    }


async def get_usage_summary(db: AsyncSession, tenant_id: uuid.UUID) -> dict:
    """Return current tenant plan, subscription lifecycle state, entitlements, and live usage."""
    state = await get_subscription_state(db, tenant_id)
    usage = await _get_live_usage(db, tenant_id)
    ent = state["entitlements"]

    return {
        "plan_tier": state["plan_tier"],
        "effective_tier": state["effective_tier"],
        "status": state["status"],
        "access_mode": state["access_mode"],
        "is_active_access": state["is_active_access"],
        "is_expired": state["is_expired"],
        "days_remaining": state["days_remaining"],
        "warning_level": state["warning_level"],
        "warning_message": state["warning_message"],
        "billing_cycle": state["billing_cycle"],
        "trial_ends_at": state["trial_ends_at"],
        "current_period_end": state["current_period_end"],
        "grace_period_ends_at": state["grace_period_ends_at"],
        "cancel_at_period_end": state["cancel_at_period_end"],
        "razorpay_configured": is_razorpay_configured(),
        "limits": {
            "max_staff": ent["max_staff"],
            "max_branches": ent["max_branches"],
            "max_monthly_bookings": ent["max_monthly_bookings"],
        },
        "entitlements": ent,
        "usage": usage,
        "plan": state["plan"],
    }


async def enforce_plan_limit(db: AsyncSession, tenant_id: uuid.UUID, resource: str) -> None:
    """Verify subscription is active AND creating one more `resource` is within plan quota."""
    state = await require_active_subscription(db, tenant_id)
    ent = state["entitlements"]

    if resource == "staff":
        maximum = ent["max_staff"]
        if maximum is None:
            return
        current = (
            await db.execute(
                text("SELECT COUNT(*) FROM staff WHERE tenant_id = :tid AND is_active = true"),
                {"tid": tenant_id},
            )
        ).scalar() or 0
        if current >= maximum:
            raise PlanLimitExceededError("staff", current, maximum)

    elif resource == "branches":
        maximum = ent["max_branches"]
        if maximum is None:
            return
        current = (
            await db.execute(
                text("SELECT COUNT(*) FROM branches WHERE tenant_id = :tid"),
                {"tid": tenant_id},
            )
        ).scalar() or 0
        if current >= maximum:
            raise PlanLimitExceededError("branches", current, maximum)

    elif resource == "bookings":
        maximum = ent["max_monthly_bookings"]
        if maximum is None:
            return
        current = (
            await db.execute(
                text("""
                    SELECT COUNT(*) FROM bookings
                    WHERE tenant_id = :tid
                      AND cancellation_reason IS DISTINCT FROM 'BLOCKED'
                      AND created_at >= date_trunc('month', NOW())
                """),
                {"tid": tenant_id},
            )
        ).scalar() or 0
        if current >= maximum:
            raise PlanLimitExceededError("monthly_bookings", current, maximum)


async def validate_plan_change(db: AsyncSession, tenant_id: uuid.UUID, target_tier: str) -> None:
    """Ensure downgrading to `target_tier` does not violate currently active staff/branch usage."""
    if target_tier not in PLANS:
        raise ValueError(f"Unknown plan tier: {target_tier}")
    target = PLANS[target_tier]
    usage = await _get_live_usage(db, tenant_id)
    conflicts: list[dict] = []

    if target["max_staff"] is not None and usage["staff"] > target["max_staff"]:
        conflicts.append({
            "resource": "staff",
            "current": usage["staff"],
            "limit": target["max_staff"],
            "message": f"You have {usage['staff']} active team members, but {target['name']} allows up to {target['max_staff']}. Deactivate {usage['staff'] - target['max_staff']} member(s) first.",
        })
    if target["max_branches"] is not None and usage["branches"] > target["max_branches"]:
        conflicts.append({
            "resource": "branches",
            "current": usage["branches"],
            "limit": target["max_branches"],
            "message": f"You have {usage['branches']} branches, but {target['name']} allows up to {target['max_branches']}. Remove {usage['branches'] - target['max_branches']} branch(es) first.",
        })

    if conflicts:
        raise DowngradeConflictError(
            " ".join(c["message"] for c in conflicts),
            conflicts=conflicts,
        )


async def activate_subscription(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    plan_tier: str,
    billing_cycle: str = "monthly",
    razorpay_subscription_id: str | None = None,
    razorpay_plan_id: str | None = None,
    razorpay_payment_id: str | None = None,
    razorpay_order_id: str | None = None,
    razorpay_invoice_id: str | None = None,
    payment_provider: str = "razorpay",
    record_invoice: bool = True,
) -> dict:
    """Activate/renew a tenant subscription, reset warnings, and optionally log a paid invoice."""
    now = datetime.now(timezone.utc)
    period_end = now + timedelta(days=365 if billing_cycle == "annual" else 30)

    await db.execute(
        text("UPDATE tenants SET plan_tier = :tier, updated_at = :now WHERE id = :tid"),
        {"tier": plan_tier, "now": now, "tid": tenant_id},
    )

    existing = (
        await db.execute(
            text(
                "SELECT id FROM subscriptions WHERE tenant_id = :tid "
                "ORDER BY current_period_end DESC LIMIT 1"
            ),
            {"tid": tenant_id},
        )
    ).first()

    if existing:
        sub_id = existing.id
        await db.execute(
            text("""
                UPDATE subscriptions
                SET plan_tier = :tier,
                    billing_cycle = :cycle,
                    status = 'active',
                    current_period_start = :start,
                    current_period_end = :end,
                    grace_period_ends_at = NULL,
                    cancelled_at = NULL,
                    cancel_at_period_end = false,
                    warning_sent_3d_at = NULL,
                    warning_sent_1d_at = NULL,
                    expiry_notified_at = NULL,
                    razorpay_subscription_id = COALESCE(:rzp_sub, razorpay_subscription_id),
                    razorpay_plan_id = COALESCE(:rzp_plan, razorpay_plan_id)
                WHERE id = :id
            """),
            {
                "tier": plan_tier,
                "cycle": billing_cycle,
                "start": now,
                "end": period_end,
                "rzp_sub": razorpay_subscription_id,
                "rzp_plan": razorpay_plan_id,
                "id": sub_id,
            },
        )
    else:
        sub_id = uuid.uuid4()
        await db.execute(
            text("""
                INSERT INTO subscriptions (
                    id, tenant_id, plan_tier, billing_cycle,
                    current_period_start, current_period_end, status,
                    razorpay_subscription_id, razorpay_plan_id
                ) VALUES (
                    :id, :tid, :tier, :cycle, :start, :end, 'active', :rzp_sub, :rzp_plan
                )
            """),
            {
                "id": sub_id,
                "tid": tenant_id,
                "tier": plan_tier,
                "cycle": billing_cycle,
                "start": now,
                "end": period_end,
                "rzp_sub": razorpay_subscription_id,
                "rzp_plan": razorpay_plan_id,
            },
        )

    if record_invoice:
        amount_paise = get_plan_amount_paise(plan_tier, billing_cycle)
        await db.execute(
            text("""
                INSERT INTO subscription_invoices (
                    id, tenant_id, subscription_id, plan_tier, billing_cycle,
                    amount_paise, status, payment_provider,
                    razorpay_payment_id, razorpay_invoice_id, razorpay_order_id,
                    period_start, period_end, created_at
                ) VALUES (
                    :id, :tid, :sub_id, :tier, :cycle,
                    :amt, 'paid', :provider,
                    :rpid, :riid, :roid,
                    :start, :end, :now
                )
            """),
            {
                "id": uuid.uuid4(),
                "tid": tenant_id,
                "sub_id": sub_id,
                "tier": plan_tier,
                "cycle": billing_cycle,
                "amt": amount_paise,
                "provider": payment_provider,
                "rpid": razorpay_payment_id,
                "riid": razorpay_invoice_id,
                "roid": razorpay_order_id,
                "start": now,
                "end": period_end,
                "now": now,
            },
        )

    await db.commit()
    return await get_usage_summary(db, tenant_id)


async def create_subscription_checkout(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    plan_tier: str,
    billing_cycle: str = "monthly",
    skip_trial: bool = False,
) -> dict:
    """Start a subscription upgrade/renewal checkout.

    - Validates plan tier, billing cycle, and downgrade constraints.
    - If tenant is in an active 14-day trial and skip_trial=False -> preserves their
      14-day free trial while updating their chosen plan_tier & billing_cycle!
    - When Razorpay keys + Razorpay Plan ID are configured -> creates Razorpay Subscription.
    - When Razorpay keys (no pre-created Plan ID) are configured -> creates Razorpay Order.
    - When Razorpay is not configured (dev/sandbox) -> activates immediately in sandbox mode.
    """
    if plan_tier not in PLANS:
        raise ValueError(f"Plan must be one of: {', '.join(PLANS.keys())}")
    if billing_cycle not in ("monthly", "annual"):
        raise ValueError("billing_cycle must be 'monthly' or 'annual'")

    await validate_plan_change(db, tenant_id, plan_tier)

    # Preserve active 14-day free trial unless user explicitly skips trial to pay early
    current_state = await get_subscription_state(db, tenant_id)
    if (
        not skip_trial
        and current_state["status"] == "trial"
        and current_state["access_mode"] == "full"
    ):
        now = datetime.now(timezone.utc)
        await db.execute(
            text("UPDATE tenants SET plan_tier = :tier, updated_at = :now WHERE id = :tid"),
            {"tier": plan_tier, "now": now, "tid": tenant_id},
        )
        await db.execute(
            text("""
                UPDATE subscriptions
                SET plan_tier = :tier, billing_cycle = :cycle
                WHERE id = (
                    SELECT id FROM subscriptions WHERE tenant_id = :tid
                    ORDER BY current_period_end DESC LIMIT 1
                )
            """),
            {"tier": plan_tier, "cycle": billing_cycle, "tid": tenant_id},
        )
        await db.commit()
        summary = await get_usage_summary(db, tenant_id)
        return {
            "checkout_mode": "trial_plan_updated",
            "plan_tier": plan_tier,
            "billing_cycle": billing_cycle,
            "amount_paise": 0,
            "trial_preserved": True,
            "subscription": summary,
        }

    amount_paise = get_plan_amount_paise(plan_tier, billing_cycle)
    t_row = (
        await db.execute(
            text("SELECT business_name, owner_phone FROM tenants WHERE id = :tid"),
            {"tid": tenant_id},
        )
    ).first()
    biz_name = t_row.business_name if t_row else "Tivoria Workspace"

    if not is_razorpay_configured():
        summary = await activate_subscription(
            db,
            tenant_id=tenant_id,
            plan_tier=plan_tier,
            billing_cycle=billing_cycle,
            payment_provider="sandbox",
            record_invoice=True,
        )
        return {
            "checkout_mode": "sandbox_activated",
            "plan_tier": plan_tier,
            "billing_cycle": billing_cycle,
            "amount_paise": amount_paise,
            "subscription": summary,
        }

    client = _get_razorpay_client()
    rzp_plan_id = get_razorpay_plan_id(plan_tier, billing_cycle)

    if rzp_plan_id:
        rzp_sub = client.subscription.create(
            {
                "plan_id": rzp_plan_id,
                "total_count": 12 if billing_cycle == "monthly" else 5,
                "customer_notify": 1,
                "notes": {
                    "tenant_id": str(tenant_id),
                    "plan_tier": plan_tier,
                    "billing_cycle": billing_cycle,
                    "purpose": "saas_subscription",
                },
            }
        )
        # Persist pending razorpay_subscription_id on subscription row for webhook correlation
        await ensure_trial_subscription(db, tenant_id)
        await db.execute(
            text("""
                UPDATE subscriptions
                SET razorpay_subscription_id = :rsub, razorpay_plan_id = :rplan
                WHERE id = (
                    SELECT id FROM subscriptions WHERE tenant_id = :tid
                    ORDER BY current_period_end DESC LIMIT 1
                )
            """),
            {"rsub": rzp_sub["id"], "rplan": rzp_plan_id, "tid": tenant_id},
        )
        await db.commit()
        return {
            "checkout_mode": "razorpay_subscription",
            "key_id": settings.razorpay_key_id,
            "razorpay_subscription_id": rzp_sub["id"],
            "short_url": rzp_sub.get("short_url"),
            "plan_tier": plan_tier,
            "billing_cycle": billing_cycle,
            "amount_paise": amount_paise,
            "business_name": biz_name,
        }

    # Fallback: Razorpay Order checkout (works with just RAZORPAY_KEY_ID + SECRET)
    order = client.order.create(
        {
            "amount": amount_paise,
            "currency": "INR",
            "receipt": f"sub_{str(tenant_id)[:8]}_{plan_tier}",
            "notes": {
                "tenant_id": str(tenant_id),
                "plan_tier": plan_tier,
                "billing_cycle": billing_cycle,
                "purpose": "saas_subscription",
            },
        }
    )
    return {
        "checkout_mode": "razorpay_order",
        "key_id": settings.razorpay_key_id,
        "razorpay_order_id": order["id"],
        "plan_tier": plan_tier,
        "billing_cycle": billing_cycle,
        "amount_paise": amount_paise,
        "business_name": biz_name,
    }


async def verify_subscription_payment(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    plan_tier: str,
    billing_cycle: str,
    razorpay_payment_id: str,
    razorpay_signature: str,
    razorpay_subscription_id: str | None = None,
    razorpay_order_id: str | None = None,
) -> dict:
    """Verify Razorpay Checkout HMAC signature and activate the tenant subscription."""
    if not settings.razorpay_key_secret:
        raise ValueError("Razorpay is not configured on this server")

    if razorpay_subscription_id:
        message = f"{razorpay_payment_id}|{razorpay_subscription_id}"
    elif razorpay_order_id:
        message = f"{razorpay_order_id}|{razorpay_payment_id}"
    else:
        raise ValueError("Either razorpay_subscription_id or razorpay_order_id is required")

    expected = hmac.new(
        settings.razorpay_key_secret.encode(),
        message.encode(),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, razorpay_signature):
        logger.warning(
            "subscription_signature_mismatch",
            tenant_id=str(tenant_id),
            razorpay_payment_id=razorpay_payment_id,
        )
        raise ValueError("Invalid Razorpay payment signature")

    return await activate_subscription(
        db,
        tenant_id=tenant_id,
        plan_tier=plan_tier,
        billing_cycle=billing_cycle,
        razorpay_subscription_id=razorpay_subscription_id,
        razorpay_payment_id=razorpay_payment_id,
        razorpay_order_id=razorpay_order_id,
        payment_provider="razorpay",
        record_invoice=True,
    )


async def mark_subscription_past_due(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    grace_days: int | None = None,
) -> None:
    """Transition an active subscription to past_due with a grace window."""
    now = datetime.now(timezone.utc)
    grace_end = now + timedelta(days=grace_days if grace_days is not None else settings.subscription_grace_days)
    await db.execute(
        text("""
            UPDATE subscriptions
            SET status = 'past_due',
                grace_period_ends_at = COALESCE(grace_period_ends_at, :grace_end)
            WHERE id = (
                SELECT id FROM subscriptions WHERE tenant_id = :tid
                ORDER BY current_period_end DESC LIMIT 1
            )
        """),
        {"grace_end": grace_end, "tid": tenant_id},
    )
    await db.commit()


async def cancel_subscription(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    immediate: bool = False,
) -> dict:
    """Cancel tenant subscription (by default at end of current billing period)."""
    now = datetime.now(timezone.utc)
    sub = (
        await db.execute(
            text(
                "SELECT id, razorpay_subscription_id FROM subscriptions "
                "WHERE tenant_id = :tid ORDER BY current_period_end DESC LIMIT 1"
            ),
            {"tid": tenant_id},
        )
    ).first()
    if not sub:
        raise ValueError("No active subscription found")

    if sub.razorpay_subscription_id and is_razorpay_configured():
        try:
            client = _get_razorpay_client()
            client.subscription.cancel(
                sub.razorpay_subscription_id,
                {"cancel_at_cycle_end": 0 if immediate else 1},
            )
        except Exception as exc:
            logger.warning("razorpay_subscription_cancel_failed", error=str(exc))

    if immediate:
        await db.execute(
            text("""
                UPDATE subscriptions
                SET status = 'cancelled', cancelled_at = :now,
                    current_period_end = :now, cancel_at_period_end = false
                WHERE id = :id
            """),
            {"now": now, "id": sub.id},
        )
    else:
        await db.execute(
            text("""
                UPDATE subscriptions
                SET status = 'cancelled', cancelled_at = :now, cancel_at_period_end = true
                WHERE id = :id
            """),
            {"now": now, "id": sub.id},
        )
    await db.commit()
    return await get_usage_summary(db, tenant_id)


async def list_invoices(db: AsyncSession, tenant_id: uuid.UUID, limit: int = 24) -> list[dict]:
    """Return billing invoice history for the tenant."""
    res = await db.execute(
        text("""
            SELECT id, plan_tier, billing_cycle, amount_paise, status, payment_provider,
                   razorpay_payment_id, razorpay_invoice_id, razorpay_order_id,
                   period_start, period_end, created_at
            FROM subscription_invoices
            WHERE tenant_id = :tid
            ORDER BY created_at DESC
            LIMIT :lim
        """),
        {"tid": tenant_id, "lim": limit},
    )
    return [
        {
            "id": str(r.id),
            "plan_tier": r.plan_tier,
            "billing_cycle": r.billing_cycle,
            "amount_paise": r.amount_paise,
            "status": r.status,
            "payment_provider": r.payment_provider,
            "razorpay_payment_id": r.razorpay_payment_id,
            "razorpay_invoice_id": r.razorpay_invoice_id,
            "razorpay_order_id": r.razorpay_order_id,
            "period_start": r.period_start.isoformat() if r.period_start else None,
            "period_end": r.period_end.isoformat() if r.period_end else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in res.fetchall()
    ]
