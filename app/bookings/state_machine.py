"""Booking state machine.

Valid transitions:
    pending_deposit → confirmed (payment received)
    pending_deposit → cancelled (timeout or customer cancel)
    confirmed → completed (service delivered)
    confirmed → cancelled (customer/owner cancel)
    confirmed → no_show (customer didn't show)
"""
from app.bookings.models import BookingStatus
from app.exceptions import InvalidStateTransitionError

VALID_TRANSITIONS: dict[str, set[str]] = {
    BookingStatus.PENDING_DEPOSIT: {BookingStatus.CONFIRMED, BookingStatus.CANCELLED},
    BookingStatus.CONFIRMED: {BookingStatus.COMPLETED, BookingStatus.CANCELLED, BookingStatus.NO_SHOW},
    # Terminal states — no transitions out
    BookingStatus.COMPLETED: set(),
    BookingStatus.CANCELLED: set(),
    BookingStatus.NO_SHOW: set(),
}


def validate_transition(current: str, target: str) -> None:
    """Raise InvalidStateTransitionError if transition is not allowed."""
    allowed = VALID_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise InvalidStateTransitionError(current, target)
