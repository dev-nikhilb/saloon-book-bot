"""Tests for custom exception classes."""
import pytest

from app.exceptions import (
    BookingNotFoundError,
    BookingSaaSError,
    CrossTenantAccessError,
    CustomerNotFoundError,
    InvalidStateTransitionError,
    PlanLimitExceededError,
    SlotConflictError,
    SlotNotAvailableError,
    TenantNotFoundError,
)


class TestBaseException:
    def test_base_instantiates(self):
        err = BookingSaaSError("test")
        assert str(err) == "test"

    def test_base_is_exception(self):
        assert issubclass(BookingSaaSError, Exception)


class TestSimpleExceptions:
    """Exceptions that just store a message."""

    def test_slot_conflict_error(self):
        err = SlotConflictError("conflict at 10am")
        assert isinstance(err, BookingSaaSError)
        assert "conflict" in str(err)

    def test_slot_not_available_error(self):
        err = SlotNotAvailableError("slot gone")
        assert isinstance(err, BookingSaaSError)
        assert "slot" in str(err).lower()

    def test_tenant_not_found_error(self):
        err = TenantNotFoundError("no tenant")
        assert isinstance(err, BookingSaaSError)

    def test_customer_not_found_error(self):
        err = CustomerNotFoundError("no customer")
        assert isinstance(err, BookingSaaSError)

    def test_booking_not_found_error(self):
        err = BookingNotFoundError("no booking")
        assert isinstance(err, BookingSaaSError)

    def test_cross_tenant_access_error(self):
        err = CrossTenantAccessError("security violation")
        assert isinstance(err, BookingSaaSError)

    def test_cross_tenant_access_no_message(self):
        err = CrossTenantAccessError()
        assert isinstance(err, BookingSaaSError)


class TestInvalidStateTransitionError:
    def test_stores_current_status(self):
        err = InvalidStateTransitionError("completed", "confirmed")
        assert err.current_status == "completed"

    def test_stores_target_status(self):
        err = InvalidStateTransitionError("completed", "confirmed")
        assert err.target_status == "confirmed"

    def test_message_format(self):
        err = InvalidStateTransitionError("cancelled", "no_show")
        assert "Cannot transition" in str(err)
        assert "cancelled" in str(err)
        assert "no_show" in str(err)

    def test_is_booking_saas_error(self):
        err = InvalidStateTransitionError("a", "b")
        assert isinstance(err, BookingSaaSError)

    def test_can_be_caught_as_base(self):
        with pytest.raises(BookingSaaSError):
            raise InvalidStateTransitionError("a", "b")


class TestPlanLimitExceededError:
    def test_stores_limit_name(self):
        err = PlanLimitExceededError("staff_count", 5, 3)
        assert err.limit_name == "staff_count"

    def test_stores_current(self):
        err = PlanLimitExceededError("bookings", 100, 50)
        assert err.current == 100

    def test_stores_maximum(self):
        err = PlanLimitExceededError("bookings", 100, 50)
        assert err.maximum == 50

    def test_message_format(self):
        err = PlanLimitExceededError("services", 10, 5)
        msg = str(err)
        assert "services" in msg
        assert "10" in msg
        assert "5" in msg

    def test_is_booking_saas_error(self):
        err = PlanLimitExceededError("x", 1, 0)
        assert isinstance(err, BookingSaaSError)

    def test_can_be_caught_as_base(self):
        with pytest.raises(BookingSaaSError):
            raise PlanLimitExceededError("limit", 2, 1)
