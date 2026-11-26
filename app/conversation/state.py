"""Per-customer conversation state stored in Redis with automatic Postgres + in-memory fallback.

Each customer has a conversation state that tracks where they are in the booking flow.
State expires after 30 minutes of inactivity. When Redis is unreachable (e.g. Vercel
serverless without external REDIS_URL), persists cleanly in Postgres `conversation_states`
so multi-step WhatsApp bookings never lose state across serverless cold starts.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from sqlalchemy import text

from app.config import settings
from app.redis_client import get_redis

STATE_TTL = 1800  # 30 minutes
_mem_store: dict[str, tuple[str, float]] = {}


class ConversationStep(str, Enum):
    IDLE = "idle"
    AWAITING_SERVICE = "awaiting_service"
    AWAITING_SUBCATEGORY = "awaiting_subcategory"
    AWAITING_STAFF = "awaiting_staff"
    AWAITING_DATE = "awaiting_date"
    AWAITING_PERIOD = "awaiting_period"
    AWAITING_SLOT = "awaiting_slot"
    AWAITING_CONFIRM = "awaiting_confirm"
    AWAITING_CANCEL_CONFIRM = "awaiting_cancel_confirm"
    AWAITING_PAYMENT_UTR = "awaiting_payment_utr"


@dataclass
class ConversationState:
    step: str = ConversationStep.IDLE
    tenant_id: str = ""
    customer_id: str = ""
    selected_service_id: str = ""
    selected_service_name: str = ""
    selected_date: str = ""  # ISO date
    selected_slot_index: int = -1
    selected_staff_id: str = ""
    selected_staff_name: str = ""
    preferred_staff_id: str = ""
    preferred_staff_name: str = ""
    selected_start: str = ""  # ISO datetime
    selected_end: str = ""  # ISO datetime
    selected_parent_id: str = ""
    selected_parent_name: str = ""
    booking_id_to_cancel: str = ""
    pending_payment_booking_id: str = ""
    slot_page: int = 0
    staff_options_json: str = "[]"
    available_slots_json: str = "[]"  # cached slots for display
    services_json: str = "[]"  # cached services for display

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, data: str) -> ConversationState:
        raw = json.loads(data)
        valid_fields = cls.__dataclass_fields__.keys()
        return cls(**{k: v for k, v in raw.items() if k in valid_fields})


def _key(tenant_id: str, customer_phone: str) -> str:
    return f"conv:{tenant_id}:{customer_phone}"


def _skip_redis() -> bool:
    return bool(
        os.environ.get("VERCEL")
        and ("localhost" in settings.redis_url or "127.0.0.1" in settings.redis_url)
    )


def _mem_get(key: str) -> str | None:
    item = _mem_store.get(key)
    if not item:
        return None
    raw, exp = item
    if time.monotonic() > exp:
        _mem_store.pop(key, None)
        return None
    return raw


def _mem_set(key: str, raw: str) -> None:
    _mem_store[key] = (raw, time.monotonic() + STATE_TTL)


async def _db_get(key: str) -> str | None:
    try:
        from app.database import async_session_factory

        now = datetime.now(timezone.utc)
        async with async_session_factory() as db:
            row = (
                await db.execute(
                    text(
                        "SELECT payload_json FROM conversation_states "
                        "WHERE state_key = :k AND expires_at > :now LIMIT 1"
                    ),
                    {"k": key, "now": now},
                )
            ).first()
            return row.payload_json if row else None
    except Exception:
        return None


async def _db_set(key: str, raw: str) -> None:
    try:
        from app.database import async_session_factory

        now = datetime.now(timezone.utc)
        exp = now + timedelta(seconds=STATE_TTL)
        async with async_session_factory() as db:
            await db.execute(
                text("""
                    INSERT INTO conversation_states (state_key, payload_json, expires_at, updated_at)
                    VALUES (:k, :p, :exp, :now)
                    ON CONFLICT (state_key) DO UPDATE
                    SET payload_json = EXCLUDED.payload_json,
                        expires_at = EXCLUDED.expires_at,
                        updated_at = EXCLUDED.updated_at
                """),
                {"k": key, "p": raw, "exp": exp, "now": now},
            )
            await db.commit()
    except Exception:
        pass


async def _db_delete(key: str) -> None:
    try:
        from app.database import async_session_factory

        async with async_session_factory() as db:
            await db.execute(
                text("DELETE FROM conversation_states WHERE state_key = :k"),
                {"k": key},
            )
            await db.commit()
    except Exception:
        pass


async def get_state(tenant_id: str, customer_phone: str) -> ConversationState:
    key = _key(tenant_id, customer_phone)
    if not _skip_redis():
        try:
            r = await get_redis()
            data = await r.get(key)
            if data:
                return ConversationState.from_json(data)
            return ConversationState(tenant_id=tenant_id)
        except Exception:
            pass
    db_raw = await _db_get(key)
    if db_raw:
        _mem_set(key, db_raw)
        return ConversationState.from_json(db_raw)
    raw = _mem_get(key)
    if raw:
        return ConversationState.from_json(raw)
    return ConversationState(tenant_id=tenant_id)


async def set_state(tenant_id: str, customer_phone: str, state: ConversationState) -> None:
    key = _key(tenant_id, customer_phone)
    raw = state.to_json()
    _mem_set(key, raw)
    if not _skip_redis():
        try:
            r = await get_redis()
            await r.set(key, raw, ex=STATE_TTL)
            return
        except Exception:
            pass
    await _db_set(key, raw)


async def clear_state(tenant_id: str, customer_phone: str) -> None:
    key = _key(tenant_id, customer_phone)
    _mem_store.pop(key, None)
    if not _skip_redis():
        try:
            r = await get_redis()
            await r.delete(key)
            return
        except Exception:
            pass
    await _db_delete(key)
