"""Expanded tests for slot calculation engine."""
import uuid
from datetime import date, datetime, time, timedelta

import pytest
import zoneinfo

from app.bookings.slots import SLOT_INCREMENT, TimeSlot, WorkingWindow, _generate_slots_for_window

TZ = "Asia/Kolkata"
TZI = zoneinfo.ZoneInfo(TZ)
STAFF_ID = uuid.uuid4()
TARGET = date(2026, 6, 1)  # a Monday


def _make_slots(open_h, open_m, close_h, close_m, duration_min, booked=None, buffer_min=0,
                staff_bio="", staff_specialty=""):
    window = WorkingWindow(open_time=time(open_h, open_m), close_time=time(close_h, close_m))
    duration = timedelta(minutes=duration_min)
    total_block = timedelta(minutes=duration_min + buffer_min)
    return _generate_slots_for_window(
        staff_id=STAFF_ID, staff_name="Priya",
        target_date=TARGET, window=window,
        duration=duration, booked=booked or [], tz_name=TZ,
        total_block=total_block,
        staff_bio=staff_bio, staff_specialty=staff_specialty,
    )


class TestBasicSlotGeneration:
    def test_15_min_service(self):
        slots = _make_slots(9, 0, 10, 0, 15)
        # 9:00 to 10:00, 15-min service at 15-min increments = 9:00..9:45 = 4 slots
        assert len(slots) == 4

    def test_30_min_service(self):
        slots = _make_slots(9, 0, 11, 0, 30)
        # 9:00-11:00, 30min@15 increments: 9:00..10:30 = 7 slots
        assert len(slots) == 7

    def test_45_min_service(self):
        slots = _make_slots(9, 0, 11, 0, 45)
        # 9:00-11:00, 45min@15 increments: 9:00..10:15 = 6 slots
        assert len(slots) == 6

    def test_60_min_service(self):
        slots = _make_slots(9, 0, 11, 0, 60)
        # 9:00-11:00, 60min@15 increments: 9:00..10:00 = 5 slots
        assert len(slots) == 5

    def test_90_min_service(self):
        slots = _make_slots(9, 0, 12, 0, 90)
        # 9:00-12:00, 90min@15 increments: 9:00..10:30 = 7 slots
        assert len(slots) == 7

    def test_slot_increment_is_15(self):
        assert SLOT_INCREMENT == 15

    def test_increments_are_15_min_apart(self):
        slots = _make_slots(9, 0, 11, 0, 30)
        for i in range(len(slots) - 1):
            diff = (slots[i + 1].start - slots[i].start).total_seconds()
            assert diff == 900  # 15 minutes


class TestBufferTime:
    def test_10_min_buffer(self):
        # 30-min service + 10-min buffer = 40-min block
        # Without buffer: slot at 9:00 ends at 9:30; with buffer blocks until 9:40
        slots_no_buf = _make_slots(9, 0, 11, 0, 30, buffer_min=0)
        slots_buf = _make_slots(9, 0, 11, 0, 30, buffer_min=10)
        # Buffer doesn't change slot count when no bookings
        assert len(slots_no_buf) == len(slots_buf)

    def test_buffer_prevents_overlap_with_booking(self):
        # Booking at 10:00-10:30. With 15-min buffer, slot ending at 10:00 but
        # blocking until 10:15 would conflict with 10:00 booking.
        booked = [
            (datetime(2026, 6, 1, 10, 0, tzinfo=TZI),
             datetime(2026, 6, 1, 10, 30, tzinfo=TZI)),
        ]
        slots_no_buf = _make_slots(9, 0, 11, 0, 30, booked=booked, buffer_min=0)
        slots_buf = _make_slots(9, 0, 11, 0, 30, booked=booked, buffer_min=15)
        # Buffer should remove more slots
        assert len(slots_buf) < len(slots_no_buf)

    def test_30_min_buffer(self):
        booked = [
            (datetime(2026, 6, 1, 10, 0, tzinfo=TZI),
             datetime(2026, 6, 1, 10, 30, tzinfo=TZI)),
        ]
        slots = _make_slots(9, 0, 11, 0, 30, booked=booked, buffer_min=30)
        start_times = [(s.start.hour, s.start.minute) for s in slots]
        # With 30-min buffer, a slot at 9:30 blocks 9:30-10:30 which conflicts
        assert (9, 30) not in start_times


class TestBookingConflicts:
    def test_multiple_bookings_same_window(self):
        booked = [
            (datetime(2026, 6, 1, 9, 0, tzinfo=TZI), datetime(2026, 6, 1, 9, 30, tzinfo=TZI)),
            (datetime(2026, 6, 1, 10, 0, tzinfo=TZI), datetime(2026, 6, 1, 10, 30, tzinfo=TZI)),
        ]
        slots = _make_slots(9, 0, 11, 0, 30, booked=booked)
        start_times = [(s.start.hour, s.start.minute) for s in slots]
        assert (9, 0) not in start_times
        assert (10, 0) not in start_times
        # But 9:30 and 10:30 should be available
        assert (9, 30) in start_times
        assert (10, 30) in start_times

    def test_boundary_exact_match_no_overlap(self):
        """Booking ends exactly when slot starts => no conflict."""
        booked = [
            (datetime(2026, 6, 1, 9, 0, tzinfo=TZI), datetime(2026, 6, 1, 9, 30, tzinfo=TZI)),
        ]
        slots = _make_slots(9, 0, 11, 0, 30, booked=booked)
        start_times = [(s.start.hour, s.start.minute) for s in slots]
        # 9:30 starts exactly when booking ends => should be available
        assert (9, 30) in start_times

    def test_boundary_slot_ends_at_booking_start(self):
        """Slot ends exactly when booking starts => no conflict."""
        booked = [
            (datetime(2026, 6, 1, 10, 0, tzinfo=TZI), datetime(2026, 6, 1, 10, 30, tzinfo=TZI)),
        ]
        slots = _make_slots(9, 0, 11, 0, 30, booked=booked)
        start_times = [(s.start.hour, s.start.minute) for s in slots]
        assert (9, 30) in start_times  # 9:30-10:00 doesn't overlap 10:00-10:30

    def test_fully_booked_window(self):
        booked = [
            (datetime(2026, 6, 1, 9, 0, tzinfo=TZI), datetime(2026, 6, 1, 11, 0, tzinfo=TZI)),
        ]
        slots = _make_slots(9, 0, 11, 0, 30, booked=booked)
        assert slots == []


class TestEdgeCases:
    def test_empty_window_close_equals_open(self):
        slots = _make_slots(9, 0, 9, 0, 30)
        assert slots == []

    def test_service_duration_exceeds_window(self):
        # 90 min service in 60 min window
        slots = _make_slots(9, 0, 10, 0, 90)
        assert slots == []

    def test_zero_duration(self):
        # Zero-duration service: every 15-min slot from 9:00 to 10:00
        slots = _make_slots(9, 0, 10, 0, 0)
        # 0-min service fits at every increment: 9:00, 9:15, 9:30, 9:45, 10:00 = 5
        assert len(slots) == 5

    def test_window_smaller_than_increment(self):
        # 10-minute window with 15-min service
        slots = _make_slots(9, 0, 9, 10, 15)
        assert slots == []


class TestMultipleWindows:
    """Simulate split shifts by calling _generate_slots_for_window for each window."""

    def test_two_windows(self):
        morning = _make_slots(9, 0, 12, 0, 30)
        afternoon = _make_slots(14, 0, 17, 0, 30)
        combined = morning + afternoon
        # Verify no slots between 12:00 and 14:00
        for s in combined:
            assert not (12 <= s.start.hour < 14)
        assert len(combined) == len(morning) + len(afternoon)


class TestTimeSlotData:
    def test_staff_bio_passed_through(self):
        slots = _make_slots(9, 0, 10, 0, 15, staff_bio="Expert colorist")
        assert slots[0].staff_bio == "Expert colorist"

    def test_staff_specialty_passed_through(self):
        slots = _make_slots(9, 0, 10, 0, 15, staff_specialty="Bridal")
        assert slots[0].staff_specialty == "Bridal"

    def test_to_dict_basic_fields(self):
        slots = _make_slots(9, 0, 10, 0, 15)
        d = slots[0].to_dict()
        assert "staff_id" in d
        assert "staff_name" in d
        assert "start" in d
        assert "end" in d
        assert "display" in d

    def test_to_dict_display_format(self):
        slots = _make_slots(9, 0, 10, 0, 15)
        d = slots[0].to_dict()
        # 9:00 AM should display as "9:00 AM" (no leading zero)
        assert d["display"] == "9:00 AM"

    def test_to_dict_includes_bio_when_set(self):
        slots = _make_slots(9, 0, 10, 0, 15, staff_bio="bio text")
        d = slots[0].to_dict()
        assert d["staff_bio"] == "bio text"

    def test_to_dict_excludes_bio_when_empty(self):
        slots = _make_slots(9, 0, 10, 0, 15, staff_bio="")
        d = slots[0].to_dict()
        assert "staff_bio" not in d

    def test_timeslot_is_frozen(self):
        slots = _make_slots(9, 0, 10, 0, 15)
        with pytest.raises(AttributeError):
            slots[0].staff_name = "other"
