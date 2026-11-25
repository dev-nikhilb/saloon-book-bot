"""Onboarding flow — guided WhatsApp conversation to set up a new business.

Steps:
1. Business name
2. Business type (salon/barbershop/beauty parlour)
3. Owner name
4. Operating hours (or accept defaults)
5. Add services (name, duration, price) — one at a time, "done" to finish
6. Confirmation + go live

State stored in Redis, keyed by phone number.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.redis_client import get_redis

logger = structlog.get_logger()

ONBOARDING_TTL = 3600  # 1 hour


class OnboardingStep(str, Enum):
    WELCOME = "welcome"
    BUSINESS_NAME = "business_name"
    BUSINESS_TYPE = "business_type"
    OWNER_NAME = "owner_name"
    HOURS = "hours"
    ADD_SERVICE = "add_service"
    SERVICE_DURATION = "service_duration"
    SERVICE_PRICE = "service_price"
    MORE_SERVICES = "more_services"
    CONFIRM = "confirm"
    DONE = "done"


@dataclass
class OnboardingState:
    step: str = OnboardingStep.WELCOME
    phone: str = ""
    business_name: str = ""
    business_type: str = ""
    owner_name: str = ""
    hours_json: str = ""
    services: str = "[]"  # JSON list of {name, duration, price}
    current_service_name: str = ""
    current_service_duration: int = 0

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, data: str) -> OnboardingState:
        return cls(**json.loads(data))

    def get_services(self) -> list[dict]:
        return json.loads(self.services)

    def add_service(self, name: str, duration: int, price: int):
        svcs = self.get_services()
        svcs.append({"name": name, "duration": duration, "price": price})
        self.services = json.dumps(svcs)


def _key(phone: str) -> str:
    return f"onboard:{phone}"


async def get_onboarding_state(phone: str) -> OnboardingState | None:
    r = await get_redis()
    data = await r.get(_key(phone))
    if data:
        return OnboardingState.from_json(data)
    return None


async def set_onboarding_state(phone: str, state: OnboardingState) -> None:
    r = await get_redis()
    await r.set(_key(phone), state.to_json(), ex=ONBOARDING_TTL)


async def clear_onboarding_state(phone: str) -> None:
    r = await get_redis()
    await r.delete(_key(phone))


async def handle_onboarding_message(
    db: AsyncSession,
    phone: str,
    body: str,
) -> str:
    """Process a message in the onboarding flow. Returns reply text."""
    state = await get_onboarding_state(phone)

    if not state:
        # Start new onboarding
        state = OnboardingState(step=OnboardingStep.BUSINESS_NAME, phone=phone)
        await set_onboarding_state(phone, state)
        return (
            "👋 Welcome! Let's set up your business on WhatsApp.\n\n"
            "What's your *business name*?\n"
            "(e.g., Style Studio, Priya's Salon)"
        )

    body = body.strip()

    match state.step:
        case OnboardingStep.BUSINESS_NAME:
            return await _handle_business_name(state, phone, body)
        case OnboardingStep.BUSINESS_TYPE:
            return await _handle_business_type(state, phone, body)
        case OnboardingStep.OWNER_NAME:
            return await _handle_owner_name(state, phone, body)
        case OnboardingStep.HOURS:
            return await _handle_hours(state, phone, body)
        case OnboardingStep.ADD_SERVICE:
            return await _handle_add_service(state, phone, body)
        case OnboardingStep.SERVICE_DURATION:
            return await _handle_service_duration(state, phone, body)
        case OnboardingStep.SERVICE_PRICE:
            return await _handle_service_price(state, phone, body)
        case OnboardingStep.MORE_SERVICES:
            return await _handle_more_services(state, phone, body)
        case OnboardingStep.CONFIRM:
            return await _handle_confirm(db, state, phone, body)
        case _:
            await clear_onboarding_state(phone)
            return "Something went wrong. Send any message to restart setup."


async def _handle_business_name(state: OnboardingState, phone: str, body: str) -> str:
    if len(body) < 2:
        return "Please enter a valid business name (at least 2 characters)."
    state.business_name = body
    state.step = OnboardingStep.BUSINESS_TYPE
    await set_onboarding_state(phone, state)
    return (
        f"Great! *{body}* it is.\n\n"
        "What type of business?\n"
        "  1. Salon\n"
        "  2. Barbershop\n"
        "  3. Beauty Parlour\n"
        "  4. Clinic / Doctor\n"
        "  5. Dental Clinic\n"
        "  6. Spa / Wellness\n"
        "  7. Fitness / Yoga\n"
        "  8. Tutoring / Coaching\n"
        "  9. Consulting / Other\n\n"
        "Reply with a number."
    )


async def _handle_business_type(state: OnboardingState, phone: str, body: str) -> str:
    types = {
        "1": "salon",
        "2": "barbershop",
        "3": "beauty_parlour",
        "4": "clinic",
        "5": "dental",
        "6": "spa",
        "7": "fitness",
        "8": "tutor",
        "9": "consulting",
    }
    raw = body.strip().lower()
    btype = types.get(raw, raw)
    valid = set(types.values()) | {"diagnostics", "veterinary", "other"}
    if btype not in valid:
        return "Please reply with a number 1–9."
    state.business_type = btype
    state.step = OnboardingStep.OWNER_NAME
    await set_onboarding_state(phone, state)
    return "What's *your name*? (The owner/manager)"


async def _handle_owner_name(state: OnboardingState, phone: str, body: str) -> str:
    if len(body) < 2:
        return "Please enter your name."
    state.owner_name = body
    state.step = OnboardingStep.HOURS
    await set_onboarding_state(phone, state)
    return (
        f"Hi {body}! 👋\n\n"
        "What are your business hours?\n\n"
        "  1. Mon-Sat 9am-8pm (default)\n"
        "  2. Mon-Sat 10am-9pm\n"
        "  3. All days 10am-8pm\n"
        "  4. Custom (you'll type it)\n\n"
        "Reply with a number."
    )


async def _handle_hours(state: OnboardingState, phone: str, body: str) -> str:
    presets = {
        "1": _hours_preset("09:00", "20:00", include_sun=False),
        "2": _hours_preset("10:00", "21:00", include_sun=False),
        "3": _hours_preset("10:00", "20:00", include_sun=True),
    }

    if body.strip() in presets:
        state.hours_json = json.dumps(presets[body.strip()])
    elif body.strip() == "4":
        # For now, use default and note for later customization
        state.hours_json = json.dumps(presets["1"])
    else:
        return "Please reply 1, 2, 3, or 4."

    state.step = OnboardingStep.ADD_SERVICE
    await set_onboarding_state(phone, state)
    return (
        "Now let's add your services. 💇\n\n"
        "What's the *name* of your first service?\n"
        "(e.g., Haircut, Beard Trim, Facial)"
    )


async def _handle_add_service(state: OnboardingState, phone: str, body: str) -> str:
    if len(body) < 2:
        return "Please enter a service name."
    state.current_service_name = body
    state.step = OnboardingStep.SERVICE_DURATION
    await set_onboarding_state(phone, state)
    return f"How long does *{body}* take? (in minutes)\n\n(e.g., 30, 45, 60)"


async def _handle_service_duration(state: OnboardingState, phone: str, body: str) -> str:
    try:
        dur = int(body.strip().replace("min", "").replace("mins", "").replace("minutes", ""))
        if dur < 5 or dur > 480:
            raise ValueError
    except ValueError:
        return "Please enter a number of minutes (5-480). e.g., 30"
    state.current_service_duration = dur
    state.step = OnboardingStep.SERVICE_PRICE
    await set_onboarding_state(phone, state)
    return f"What do you charge for *{state.current_service_name}*? (in ₹)\n\n(e.g., 500, 200)"


async def _handle_service_price(state: OnboardingState, phone: str, body: str) -> str:
    try:
        price = int(body.strip().replace("₹", "").replace("rs", "").replace("Rs", "").replace(",", ""))
        if price < 0:
            raise ValueError
    except ValueError:
        return "Please enter a price in rupees. e.g., 500"

    state.add_service(state.current_service_name, state.current_service_duration, price)
    state.current_service_name = ""
    state.current_service_duration = 0
    state.step = OnboardingStep.MORE_SERVICES
    await set_onboarding_state(phone, state)

    count = len(state.get_services())
    return (
        f"✅ Added! ({count} service{'s' if count > 1 else ''} so far)\n\n"
        "Add another service?\n"
        "  1. Yes, add more\n"
        "  2. No, that's all\n\n"
        "Reply 1 or 2."
    )


async def _handle_more_services(state: OnboardingState, phone: str, body: str) -> str:
    if body.strip() == "1":
        state.step = OnboardingStep.ADD_SERVICE
        await set_onboarding_state(phone, state)
        return "What's the *name* of the next service?"
    elif body.strip() == "2":
        state.step = OnboardingStep.CONFIRM
        await set_onboarding_state(phone, state)
        return _build_summary(state)
    else:
        return "Reply 1 (add more) or 2 (done)."


async def _handle_confirm(db: AsyncSession, state: OnboardingState, phone: str, body: str) -> str:
    lower = body.strip().lower()
    if lower in ("yes", "y", "confirm", "1"):
        tenant_id = await _create_tenant(db, state)
        await clear_onboarding_state(phone)
        return (
            f"🎉 *{state.business_name}* is live!\n\n"
            f"Your customers can now book by messaging this WhatsApp number.\n\n"
            f"Owner commands:\n"
            f"  *today* — see today's bookings\n"
            f"  *help* — all commands\n\n"
            f"Tenant ID: {tenant_id[:8]}"
        )
    elif lower in ("no", "n", "0", "restart"):
        await clear_onboarding_state(phone)
        return "Setup cancelled. Send any message to start over."
    else:
        return "Reply *YES* to confirm or *0* to restart."


def _build_summary(state: OnboardingState) -> str:
    services = state.get_services()
    svc_lines = "\n".join(
        f"  • {s['name']} — {s['duration']}min — ₹{s['price']}"
        for s in services
    )
    return (
        f"📋 *Setup Summary*\n\n"
        f"Business: *{state.business_name}*\n"
        f"Type: {state.business_type}\n"
        f"Owner: {state.owner_name}\n\n"
        f"Services:\n{svc_lines}\n\n"
        f"Reply *YES* to go live or *0* to restart."
    )


async def _create_tenant(db: AsyncSession, state: OnboardingState) -> str:
    """Create tenant, branch, staff, and services in the database."""
    import re
    tenant_id = uuid.uuid4()
    branch_id = uuid.uuid4()
    staff_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    hours = state.hours_json or json.dumps(_hours_preset("09:00", "20:00", include_sun=False))

    # Generate slug from business name
    slug = re.sub(r'[^a-z0-9]+', '-', state.business_name.lower()).strip('-')

    # Tenant
    await db.execute(
        text("""INSERT INTO tenants (id, business_name, business_type, owner_phone, owner_name, slug,
                primary_language, timezone, plan_tier, is_active, onboarded_at, created_at, updated_at)
                VALUES (:id, :name, :type, :phone, :owner, :slug, 'en', 'Asia/Kolkata', 'solo', true, :now, :now, :now)"""),
        {"id": tenant_id, "name": state.business_name, "type": state.business_type,
         "phone": state.phone, "owner": state.owner_name, "slug": slug, "now": now},
    )

    # Branch
    await db.execute(
        text("""INSERT INTO branches (id, tenant_id, name, is_primary, operating_hours, created_at)
                VALUES (:id, :tid, 'Main', true, :hours, :now)"""),
        {"id": branch_id, "tid": tenant_id, "hours": hours, "now": now},
    )

    # Default staff = owner
    service_ids = []
    for svc in state.get_services():
        sid = uuid.uuid4()
        service_ids.append(sid)
        await db.execute(
            text("""INSERT INTO services (id, tenant_id, name, duration_minutes, price_paise,
                    is_active, sort_order, created_at)
                    VALUES (:id, :tid, :name, :dur, :price, true, :sort, :now)"""),
            {"id": sid, "tid": tenant_id, "name": svc["name"],
             "dur": svc["duration"], "price": svc["price"] * 100,  # rupees → paise
             "sort": len(service_ids), "now": now},
        )

    # Staff (owner as sole staff)
    await db.execute(
        text("""INSERT INTO staff (id, tenant_id, branch_id, name, role, skills, working_hours, is_active, created_at)
                VALUES (:id, :tid, :bid, :name, 'owner', :skills, :hours, true, :now)"""),
        {"id": staff_id, "tid": tenant_id, "bid": branch_id,
         "name": state.owner_name,
         "skills": json.dumps([str(s) for s in service_ids]),
         "hours": hours, "now": now},
    )

    # Default tenant settings
    settings_id = uuid.uuid4()
    await db.execute(
        text("""INSERT INTO tenant_settings (id, tenant_id, created_at, updated_at)
                VALUES (:id, :tid, :now, :now)"""),
        {"id": settings_id, "tid": tenant_id, "now": now},
    )

    await db.commit()
    logger.info("tenant_onboarded", tenant_id=str(tenant_id), business=state.business_name)
    return str(tenant_id)


def _hours_preset(open_time: str, close_time: str, include_sun: bool) -> dict:
    days = ["mon", "tue", "wed", "thu", "fri", "sat"]
    if include_sun:
        days.append("sun")
    return {d: [{"open": open_time, "close": close_time}] for d in days}
