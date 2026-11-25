"""Expanded tests for booking state machine — all transitions and error paths."""
import pytest

from app.bookings.models import BookingStatus
from app.bookings.state_machine import VALID_TRANSITIONS, validate_transition
from app.exceptions import InvalidStateTransitionError


class TestValidTransitions:
    """All 5 valid transitions should succeed without raising."""

    def test_pending_to_confirmed(self):
        validate_transition(BookingStatus.PENDING_DEPOSIT, BookingStatus.CONFIRMED)

    def test_pending_to_cancelled(self):
        validate_transition(BookingStatus.PENDING_DEPOSIT, BookingStatus.CANCELLED)

    def test_confirmed_to_completed(self):
        validate_transition(BookingStatus.CONFIRMED, BookingStatus.COMPLETED)

    def test_confirmed_to_cancelled(self):
        validate_transition(BookingStatus.CONFIRMED, BookingStatus.CANCELLED)

    def test_confirmed_to_no_show(self):
        validate_transition(BookingStatus.CONFIRMED, BookingStatus.NO_SHOW)


class TestInvalidTransitionsFromTerminal:
    """Terminal states (completed, cancelled, no_show) cannot transition anywhere."""

    def test_completed_to_confirmed(self):
        with pytest.raises(InvalidStateTransitionError):
            validate_transition(BookingStatus.COMPLETED, BookingStatus.CONFIRMED)

    def test_completed_to_cancelled(self):
        with pytest.raises(InvalidStateTransitionError):
            validate_transition(BookingStatus.COMPLETED, BookingStatus.CANCELLED)

    def test_completed_to_no_show(self):
        with pytest.raises(InvalidStateTransitionError):
            validate_transition(BookingStatus.COMPLETED, BookingStatus.NO_SHOW)

    def test_cancelled_to_confirmed(self):
        with pytest.raises(InvalidStateTransitionError):
            validate_transition(BookingStatus.CANCELLED, BookingStatus.CONFIRMED)

    def test_cancelled_to_completed(self):
        with pytest.raises(InvalidStateTransitionError):
            validate_transition(BookingStatus.CANCELLED, BookingStatus.COMPLETED)

    def test_cancelled_to_no_show(self):
        with pytest.raises(InvalidStateTransitionError):
            validate_transition(BookingStatus.CANCELLED, BookingStatus.NO_SHOW)

    def test_no_show_to_confirmed(self):
        with pytest.raises(InvalidStateTransitionError):
            validate_transition(BookingStatus.NO_SHOW, BookingStatus.CONFIRMED)

    def test_no_show_to_completed(self):
        with pytest.raises(InvalidStateTransitionError):
            validate_transition(BookingStatus.NO_SHOW, BookingStatus.COMPLETED)

    def test_no_show_to_cancelled(self):
        with pytest.raises(InvalidStateTransitionError):
            validate_transition(BookingStatus.NO_SHOW, BookingStatus.CANCELLED)


class TestInvalidTransitionsFromNonTerminal:
    """Non-terminal states have restricted outgoing transitions too."""

    def test_pending_to_completed(self):
        with pytest.raises(InvalidStateTransitionError):
            validate_transition(BookingStatus.PENDING_DEPOSIT, BookingStatus.COMPLETED)

    def test_pending_to_no_show(self):
        with pytest.raises(InvalidStateTransitionError):
            validate_transition(BookingStatus.PENDING_DEPOSIT, BookingStatus.NO_SHOW)

    def test_pending_to_pending(self):
        with pytest.raises(InvalidStateTransitionError):
            validate_transition(BookingStatus.PENDING_DEPOSIT, BookingStatus.PENDING_DEPOSIT)

    def test_confirmed_to_confirmed(self):
        with pytest.raises(InvalidStateTransitionError):
            validate_transition(BookingStatus.CONFIRMED, BookingStatus.CONFIRMED)

    def test_confirmed_to_pending(self):
        with pytest.raises(InvalidStateTransitionError):
            validate_transition(BookingStatus.CONFIRMED, BookingStatus.PENDING_DEPOSIT)


class TestErrorMessages:
    def test_error_message_contains_both_states(self):
        with pytest.raises(InvalidStateTransitionError) as exc_info:
            validate_transition("completed", "confirmed")
        msg = str(exc_info.value)
        assert "completed" in msg
        assert "confirmed" in msg

    def test_error_stores_current_and_target(self):
        with pytest.raises(InvalidStateTransitionError) as exc_info:
            validate_transition("cancelled", "no_show")
        assert exc_info.value.current_status == "cancelled"
        assert exc_info.value.target_status == "no_show"

    def test_error_format_cannot_transition(self):
        with pytest.raises(InvalidStateTransitionError) as exc_info:
            validate_transition("no_show", "completed")
        assert "Cannot transition" in str(exc_info.value)


class TestValidTransitionsDict:
    """Ensure VALID_TRANSITIONS covers every BookingStatus."""

    def test_all_statuses_present(self):
        for status in BookingStatus:
            assert status.value in VALID_TRANSITIONS or status in VALID_TRANSITIONS

    def test_terminal_states_are_empty_sets(self):
        for terminal in (BookingStatus.COMPLETED, BookingStatus.CANCELLED, BookingStatus.NO_SHOW):
            assert VALID_TRANSITIONS[terminal] == set()

    def test_pending_deposit_targets(self):
        targets = VALID_TRANSITIONS[BookingStatus.PENDING_DEPOSIT]
        assert BookingStatus.CONFIRMED in targets
        assert BookingStatus.CANCELLED in targets
        assert len(targets) == 2

    def test_confirmed_targets(self):
        targets = VALID_TRANSITIONS[BookingStatus.CONFIRMED]
        assert BookingStatus.COMPLETED in targets
        assert BookingStatus.CANCELLED in targets
        assert BookingStatus.NO_SHOW in targets
        assert len(targets) == 3

    def test_unknown_current_state_raises(self):
        """A state not in the dict should raise (empty allowed set)."""
        with pytest.raises(InvalidStateTransitionError):
            validate_transition("nonexistent", "confirmed")
