"""Tests for model field definitions and enums — no DB required.

We inspect the SQLAlchemy model classes to verify fields exist and have correct types.
"""
import pytest

from app.bookings.models import Booking, BookingStatus
from app.tenants.models import Branch, Service, Staff, Tenant, TenantSettings


class TestBookingStatusEnum:
    def test_has_pending_deposit(self):
        assert BookingStatus.PENDING_DEPOSIT == "pending_deposit"

    def test_has_confirmed(self):
        assert BookingStatus.CONFIRMED == "confirmed"

    def test_has_completed(self):
        assert BookingStatus.COMPLETED == "completed"

    def test_has_cancelled(self):
        assert BookingStatus.CANCELLED == "cancelled"

    def test_has_no_show(self):
        assert BookingStatus.NO_SHOW == "no_show"

    def test_total_count(self):
        assert len(BookingStatus) == 5

    def test_is_str_enum(self):
        assert isinstance(BookingStatus.PENDING_DEPOSIT, str)


class TestTenantModel:
    def test_has_id(self):
        assert hasattr(Tenant, "id")

    def test_has_business_name(self):
        assert hasattr(Tenant, "business_name")

    def test_has_business_type(self):
        assert hasattr(Tenant, "business_type")

    def test_has_owner_phone(self):
        assert hasattr(Tenant, "owner_phone")

    def test_has_owner_name(self):
        assert hasattr(Tenant, "owner_name")

    def test_has_slug(self):
        assert hasattr(Tenant, "slug")

    def test_has_primary_language(self):
        assert hasattr(Tenant, "primary_language")

    def test_has_timezone(self):
        assert hasattr(Tenant, "timezone")

    def test_has_plan_tier(self):
        assert hasattr(Tenant, "plan_tier")

    def test_has_is_active(self):
        assert hasattr(Tenant, "is_active")

    def test_has_waba_fields(self):
        assert hasattr(Tenant, "waba_phone_number_id")
        assert hasattr(Tenant, "waba_access_token_encrypted")

    def test_has_relationships(self):
        assert hasattr(Tenant, "branches")
        assert hasattr(Tenant, "staff")
        assert hasattr(Tenant, "services")
        assert hasattr(Tenant, "settings")

    def test_tablename(self):
        assert Tenant.__tablename__ == "tenants"


class TestServiceModel:
    def test_has_parent_service_id(self):
        assert hasattr(Service, "parent_service_id")

    def test_has_description(self):
        assert hasattr(Service, "description")

    def test_has_image_url(self):
        assert hasattr(Service, "image_url")

    def test_has_duration_minutes(self):
        assert hasattr(Service, "duration_minutes")

    def test_has_buffer_minutes(self):
        assert hasattr(Service, "buffer_minutes")

    def test_has_price_paise(self):
        assert hasattr(Service, "price_paise")

    def test_has_requires_deposit(self):
        assert hasattr(Service, "requires_deposit")

    def test_has_children_relationship(self):
        assert hasattr(Service, "children")

    def test_has_parent_relationship(self):
        assert hasattr(Service, "parent")

    def test_has_sort_order(self):
        assert hasattr(Service, "sort_order")

    def test_tablename(self):
        assert Service.__tablename__ == "services"


class TestTenantSettingsModel:
    """TenantSettings should have 13+ configurable fields."""

    EXPECTED_FIELDS = [
        "advance_booking_days",
        "min_cancel_hours",
        "slot_increment_minutes",
        "max_bookings_per_customer",
        "send_reminders",
        "reminder_hours_before",
        "send_feedback_request",
        "feedback_delay_hours",
        "welcome_message",
        "booking_confirmed_message",
        "google_review_url",
        "google_calendar_enabled",
        "razorpay_enabled",
    ]

    def test_has_all_configurable_fields(self):
        for field_name in self.EXPECTED_FIELDS:
            assert hasattr(TenantSettings, field_name), f"Missing field: {field_name}"

    def test_at_least_13_config_fields(self):
        assert len(self.EXPECTED_FIELDS) >= 13

    def test_has_tenant_relationship(self):
        assert hasattr(TenantSettings, "tenant")

    def test_has_tenant_id(self):
        assert hasattr(TenantSettings, "tenant_id")

    def test_tablename(self):
        assert TenantSettings.__tablename__ == "tenant_settings"


class TestBookingModel:
    def test_has_tenant_id(self):
        assert hasattr(Booking, "tenant_id")

    def test_has_staff_id(self):
        assert hasattr(Booking, "staff_id")

    def test_has_customer_id(self):
        assert hasattr(Booking, "customer_id")

    def test_has_service_id(self):
        assert hasattr(Booking, "service_id")

    def test_has_starts_at(self):
        assert hasattr(Booking, "starts_at")

    def test_has_ends_at(self):
        assert hasattr(Booking, "ends_at")

    def test_has_status(self):
        assert hasattr(Booking, "status")

    def test_has_idempotency_key(self):
        assert hasattr(Booking, "idempotency_key")

    def test_has_google_event_id(self):
        assert hasattr(Booking, "google_event_id")

    def test_tablename(self):
        assert Booking.__tablename__ == "bookings"


class TestStaffModel:
    def test_has_bio(self):
        assert hasattr(Staff, "bio")

    def test_has_specialty(self):
        assert hasattr(Staff, "specialty")

    def test_has_working_hours(self):
        assert hasattr(Staff, "working_hours")

    def test_has_skills(self):
        assert hasattr(Staff, "skills")

    def test_tablename(self):
        assert Staff.__tablename__ == "staff"
