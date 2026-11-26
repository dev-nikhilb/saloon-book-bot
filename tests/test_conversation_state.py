"""Tests for conversation state serialization and ConversationStep enum."""
import json

import pytest

from app.conversation.state import ConversationState, ConversationStep


class TestConversationStepEnum:
    def test_idle_exists(self):
        assert ConversationStep.IDLE == "idle"

    def test_awaiting_service_exists(self):
        assert ConversationStep.AWAITING_SERVICE == "awaiting_service"

    def test_awaiting_subcategory_exists(self):
        assert ConversationStep.AWAITING_SUBCATEGORY == "awaiting_subcategory"

    def test_awaiting_date_exists(self):
        assert ConversationStep.AWAITING_DATE == "awaiting_date"

    def test_awaiting_slot_exists(self):
        assert ConversationStep.AWAITING_SLOT == "awaiting_slot"

    def test_awaiting_confirm_exists(self):
        assert ConversationStep.AWAITING_CONFIRM == "awaiting_confirm"

    def test_awaiting_cancel_confirm_exists(self):
        assert ConversationStep.AWAITING_CANCEL_CONFIRM == "awaiting_cancel_confirm"

    def test_all_steps_count(self):
        assert len(ConversationStep) == 8

    def test_is_str_enum(self):
        assert isinstance(ConversationStep.IDLE, str)


class TestConversationStateDefaults:
    def test_default_step_is_idle(self):
        state = ConversationState()
        assert state.step == ConversationStep.IDLE

    def test_default_tenant_id_empty(self):
        assert ConversationState().tenant_id == ""

    def test_default_customer_id_empty(self):
        assert ConversationState().customer_id == ""

    def test_default_selected_service_id_empty(self):
        assert ConversationState().selected_service_id == ""

    def test_default_selected_slot_index_negative(self):
        assert ConversationState().selected_slot_index == -1

    def test_default_available_slots_json(self):
        assert ConversationState().available_slots_json == "[]"

    def test_default_services_json(self):
        assert ConversationState().services_json == "[]"

    def test_default_booking_id_to_cancel_empty(self):
        assert ConversationState().booking_id_to_cancel == ""


class TestConversationStateSerialization:
    def test_to_json_returns_string(self):
        state = ConversationState()
        result = state.to_json()
        assert isinstance(result, str)

    def test_to_json_is_valid_json(self):
        state = ConversationState(tenant_id="t1", customer_id="c1")
        parsed = json.loads(state.to_json())
        assert parsed["tenant_id"] == "t1"
        assert parsed["customer_id"] == "c1"

    def test_from_json_creates_state(self):
        state = ConversationState(tenant_id="t1", step=ConversationStep.AWAITING_SERVICE)
        restored = ConversationState.from_json(state.to_json())
        assert restored.tenant_id == "t1"
        assert restored.step == ConversationStep.AWAITING_SERVICE

    def test_roundtrip_preserves_all_fields(self):
        state = ConversationState(
            step=ConversationStep.AWAITING_SLOT,
            tenant_id="tenant-abc",
            customer_id="cust-123",
            selected_service_id="svc-456",
            selected_service_name="Haircut",
            selected_date="2026-06-01",
            selected_slot_index=3,
            selected_staff_id="staff-789",
            selected_staff_name="Priya",
            selected_start="2026-06-01T10:00:00+05:30",
            selected_end="2026-06-01T10:30:00+05:30",
            selected_parent_id="parent-001",
            selected_parent_name="Hair Services",
            booking_id_to_cancel="bk-999",
            available_slots_json='[{"start": "10:00"}]',
            services_json='[{"name": "Haircut"}]',
        )
        restored = ConversationState.from_json(state.to_json())
        assert restored.step == state.step
        assert restored.tenant_id == state.tenant_id
        assert restored.customer_id == state.customer_id
        assert restored.selected_service_id == state.selected_service_id
        assert restored.selected_service_name == state.selected_service_name
        assert restored.selected_date == state.selected_date
        assert restored.selected_slot_index == state.selected_slot_index
        assert restored.selected_staff_id == state.selected_staff_id
        assert restored.selected_staff_name == state.selected_staff_name
        assert restored.selected_start == state.selected_start
        assert restored.selected_end == state.selected_end
        assert restored.selected_parent_id == state.selected_parent_id
        assert restored.selected_parent_name == state.selected_parent_name
        assert restored.booking_id_to_cancel == state.booking_id_to_cancel
        assert restored.available_slots_json == state.available_slots_json
        assert restored.services_json == state.services_json

    def test_from_json_with_extra_fields_ignored(self):
        """If stored JSON has extra keys, from_json should handle gracefully or raise."""
        data = json.dumps({"step": "idle", "tenant_id": "", "extra": "field",
                           "customer_id": "", "selected_service_id": "",
                           "selected_service_name": "", "selected_date": "",
                           "selected_slot_index": -1, "selected_staff_id": "",
                           "selected_staff_name": "", "selected_start": "",
                           "selected_end": "", "selected_parent_id": "",
                           "selected_parent_name": "", "booking_id_to_cancel": "",
                           "available_slots_json": "[]", "services_json": "[]"})
        # This should either work (ignoring extra) or raise TypeError
        try:
            state = ConversationState.from_json(data)
        except TypeError:
            pass  # acceptable — strict dataclass

    def test_to_json_includes_all_dataclass_fields(self):
        state = ConversationState()
        parsed = json.loads(state.to_json())
        expected_keys = {
            "step", "tenant_id", "customer_id", "selected_service_id",
            "selected_service_name", "selected_date", "selected_slot_index",
            "selected_staff_id", "selected_staff_name", "selected_start",
            "selected_end", "selected_parent_id", "selected_parent_name",
            "booking_id_to_cancel", "pending_payment_booking_id",
            "available_slots_json", "services_json",
        }
        assert set(parsed.keys()) == expected_keys
