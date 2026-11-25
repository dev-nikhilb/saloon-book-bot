"""Per-customer conversation state stored in Redis.

Each customer has a conversation state that tracks where they are in the booking flow.
State expires after 30 minutes of inactivity.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from enum import Enum

from app.redis_client import get_redis

STATE_TTL = 1800  # 30 minutes


class ConversationStep(str, Enum):
    IDLE = "idle"
    AWAITING_SERVICE = "awaiting_service"
    AWAITING_SUBCATEGORY = "awaiting_subcategory"
    AWAITING_DATE = "awaiting_date"
    AWAITING_SLOT = "awaiting_slot"
    AWAITING_CONFIRM = "awaiting_confirm"
    AWAITING_CANCEL_CONFIRM = "awaiting_cancel_confirm"


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
    selected_start: str = ""  # ISO datetime
    selected_end: str = ""  # ISO datetime
    selected_parent_id: str = ""
    selected_parent_name: str = ""
    booking_id_to_cancel: str = ""
    available_slots_json: str = "[]"  # cached slots for display
    services_json: str = "[]"  # cached services for display

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, data: str) -> ConversationState:
        return cls(**json.loads(data))


def _key(tenant_id: str, customer_phone: str) -> str:
    return f"conv:{tenant_id}:{customer_phone}"


async def get_state(tenant_id: str, customer_phone: str) -> ConversationState:
    r = await get_redis()
    data = await r.get(_key(tenant_id, customer_phone))
    if data:
        return ConversationState.from_json(data)
    return ConversationState(tenant_id=tenant_id)


async def set_state(tenant_id: str, customer_phone: str, state: ConversationState) -> None:
    r = await get_redis()
    await r.set(_key(tenant_id, customer_phone), state.to_json(), ex=STATE_TTL)


async def clear_state(tenant_id: str, customer_phone: str) -> None:
    r = await get_redis()
    await r.delete(_key(tenant_id, customer_phone))
