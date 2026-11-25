"""Tests for booking engine — slot calculation, conflict detection, state machine."""
import uuid
from datetime import date, datetime, time, timedelta, timezone

import pytest
import zoneinfo

from app.bookings.models import BookingStatus
from app.bookings.state_machine import VALID_TRANSITIONS, validate_transition
from app.bookings.slots import TimeSlot, WorkingWindow, _generate_slots_for_window
from app.exceptions import InvalidStateTransitionError


# --- State machine tests ---

class TestStateMachine:
    def test_valid_transitions(self):
        validate_transition(BookingStatus.CONFIRMED, BookingStatus.COMPLETED)
        validate_transition(BookingStatus.CONFIRMED, BookingStatus.CANCELLED)
        validate_transition(BookingStatus.CONFIRMED, BookingStatus.NO_SHOW)
        validate_transition(BookingStatus.PENDING_DEPOSIT, BookingStatus.CONFIRMED)
        validate_transition(BookingStatus.PENDING_DEPOSIT, BookingStatus.CANCELLED)

    def test_invalid_transitions(self):
        with pytest.raises(InvalidStateTransitionError):
            validate_transition(BookingStatus.COMPLETED, BookingStatus.CONFIRMED)

        with pytest.raises(InvalidStateTransitionError):
            validate_transition(BookingStatus.CANCELLED, BookingStatus.CONFIRMED)

        with pytest.raises(InvalidStateTransitionError):
            validate_transition(BookingStatus.NO_SHOW, BookingStatus.CONFIRMED)

        with pytest.raises(InvalidStateTransitionError):
            validate_transition(BookingStatus.COMPLETED, BookingStatus.CANCELLED)

    def test_terminal_states_have_no_transitions(self):
        assert VALID_TRANSITIONS[BookingStatus.COMPLETED] == set()
        assert VALID_TRANSITIONS[BookingStatus.CANCELLED] == set()
        assert VALID_TRANSITIONS[BookingStatus.NO_SHOW] == set()

    def test_error_message_includes_states(self):
        with pytest.raises(InvalidStateTransitionError) as exc_info:
            validate_transition("completed", "confirmed")
        assert "completed" in str(exc_info.value)
        assert "confirmed" in str(exc_info.value)


# --- Slot generation tests ---

class TestSlotGeneration:
    def test_basic_slot_generation(self):
        """30-min service in a 2-hour window should yield multiple slots."""
        staff_id = uuid.uuid4()
        window = WorkingWindow(open_time=time(9, 0), close_time=time(11, 0))
        duration = timedelta(minutes=30)
        target = date(2026, 5, 20)

        slots = _generate_slots_for_window(
            staff_id=staff_id, staff_name="Ravi",
            target_date=target, window=window,
            duration=duration, booked=[], tz_name="Asia/Kolkata",
        )

        # 9:00-11:00, 30min slots at 15min increments
        # 9:00, 9:15, 9:30, 9:45, 10:00, 10:15, 10:30 = 7 slots
        assert len(slots) == 7
        assert slots[0].start.hour == 9
        assert slots[0].start.minute == 0
        assert slots[-1].start.hour == 10
        assert slots[-1].start.minute == 30

    def test_slots_respect_existing_bookings(self):
        """Booked time should be excluded from available slots."""
        staff_id = uuid.uuid4()
        window = WorkingWindow(open_time=time(9, 0), close_time=time(11, 0))
        duration = timedelta(minutes=30)
        target = date(2026, 5, 20)
        tz = zoneinfo.ZoneInfo("Asia/Kolkata")

        # Book 9:30 - 10:00
        booked = [
            (datetime(2026, 5, 20, 9, 30, tzinfo=tz),
             datetime(2026, 5, 20, 10, 0, tzinfo=tz)),
        ]

        slots = _generate_slots_for_window(
            staff_id=staff_id, staff_name="Ravi",
            target_date=target, window=window,
            duration=duration, booked=booked, tz_name="Asia/Kolkata",
        )

        # Slots starting at 9:00 and 9:15 would overlap with 9:30-10:00
        # Wait: 9:00-9:30 doesn't overlap with 9:30-10:00 (ends exactly when booking starts)
        # 9:15-9:45 overlaps with 9:30-10:00
        # 9:30-10:00 overlaps
        # 9:45-10:15 overlaps
        start_times = [(s.start.hour, s.start.minute) for s in slots]
        assert (9, 0) in start_times   # 9:00-9:30 doesn't overlap
        assert (9, 15) not in start_times  # 9:15-9:45 overlaps
        assert (9, 30) not in start_times  # direct overlap
        assert (10, 0) in start_times  # 10:00-10:30 is fine

    def test_no_slots_when_fully_booked(self):
        """If entire window is booked, return empty."""
        staff_id = uuid.uuid4()
        window = WorkingWindow(open_time=time(9, 0), close_time=time(10, 0))
        duration = timedelta(minutes=30)
        target = date(2026, 5, 20)
        tz = zoneinfo.ZoneInfo("Asia/Kolkata")

        booked = [
            (datetime(2026, 5, 20, 9, 0, tzinfo=tz),
             datetime(2026, 5, 20, 10, 0, tzinfo=tz)),
        ]

        slots = _generate_slots_for_window(
            staff_id=staff_id, staff_name="Ravi",
            target_date=target, window=window,
            duration=duration, booked=booked, tz_name="Asia/Kolkata",
        )
        assert slots == []

    def test_slot_duration_must_fit_in_window(self):
        """90-min service in a 60-min window = no slots."""
        staff_id = uuid.uuid4()
        window = WorkingWindow(open_time=time(9, 0), close_time=time(10, 0))
        duration = timedelta(minutes=90)
        target = date(2026, 5, 20)

        slots = _generate_slots_for_window(
            staff_id=staff_id, staff_name="Ravi",
            target_date=target, window=window,
            duration=duration, booked=[], tz_name="Asia/Kolkata",
        )
        assert slots == []

    def test_back_to_back_bookings(self):
        """Two 30-min bookings back-to-back should still allow other slots."""
        staff_id = uuid.uuid4()
        window = WorkingWindow(open_time=time(9, 0), close_time=time(12, 0))
        duration = timedelta(minutes=30)
        target = date(2026, 5, 20)
        tz = zoneinfo.ZoneInfo("Asia/Kolkata")

        booked = [
            (datetime(2026, 5, 20, 9, 0, tzinfo=tz), datetime(2026, 5, 20, 9, 30, tzinfo=tz)),
            (datetime(2026, 5, 20, 9, 30, tzinfo=tz), datetime(2026, 5, 20, 10, 0, tzinfo=tz)),
        ]

        slots = _generate_slots_for_window(
            staff_id=staff_id, staff_name="Ravi",
            target_date=target, window=window,
            duration=duration, booked=booked, tz_name="Asia/Kolkata",
        )

        # 10:00 onwards should have slots
        assert all(s.start.hour >= 10 for s in slots)
        assert len(slots) > 0
