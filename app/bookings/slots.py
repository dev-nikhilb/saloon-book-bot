"""Slot calculation engine — the most important code in the system.

Given a tenant, service, and date range, returns available booking slots.
Accounts for: working hours, existing bookings, blocked times, staff skills.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()

# Slot increment in minutes
SLOT_INCREMENT = 15


@dataclass(frozen=True)
class TimeSlot:
    staff_id: UUID
    staff_name: str
    start: datetime
    end: datetime
    staff_bio: str = ""
    staff_specialty: str = ""

    def to_dict(self) -> dict:
        d = {
            "staff_id": str(self.staff_id),
            "staff_name": self.staff_name,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "display": self.start.strftime("%I:%M %p").lstrip("0"),
        }
        if self.staff_bio:
            d["staff_bio"] = self.staff_bio
        if self.staff_specialty:
            d["staff_specialty"] = self.staff_specialty
        return d


@dataclass
class WorkingWindow:
    open_time: time
    close_time: time


async def get_available_slots(
    db: AsyncSession,
    tenant_id: UUID,
    service_id: UUID,
    target_date: date,
    preferred_staff_id: UUID | None = None,
    tz_name: str = "Asia/Kolkata",
    branch_id: UUID | None = None,
    slot_increment: int | None = None,
) -> list[TimeSlot]:
    """Get all available slots for a service on a given date.

    Algorithm:
    1. Get service duration & buffer (+ tenant slot_increment if not overridden)
    2. Get eligible staff (preferred or all with matching skills & optional branch)
    3. For each staff: get working hours, subtract bookings, generate slots
    4. Return merged sorted list
    """
    # 1. Get service (including buffer and optional per-service slot increment)
    svc_result = await db.execute(
        text(
            "SELECT duration_minutes, buffer_minutes, slot_increment_minutes "
            "FROM services WHERE id = :sid AND tenant_id = :tid AND is_active = true"
        ),
        {"sid": service_id, "tid": tenant_id},
    )
    svc = svc_result.first()
    if not svc or svc.duration_minutes <= 0:
        return []

    duration = timedelta(minutes=svc.duration_minutes)
    buffer = timedelta(minutes=svc.buffer_minutes or 0)

    # Determine slot increment: per-service override -> explicit/tenant setting -> SLOT_INCREMENT
    svc_inc = getattr(svc, "slot_increment_minutes", None)
    if svc_inc and int(svc_inc) > 0:
        slot_increment = int(svc_inc)
    elif slot_increment is None:
        ts_res = await db.execute(
            text("SELECT slot_increment_minutes FROM tenant_settings WHERE tenant_id = :tid LIMIT 1"),
            {"tid": tenant_id},
        )
        ts_row = ts_res.first()
        slot_increment = (ts_row.slot_increment_minutes if ts_row and ts_row.slot_increment_minutes else SLOT_INCREMENT)

    # 2. Get eligible staff (with profile info)
    if preferred_staff_id:
        staff_result = await db.execute(
            text("SELECT id, name, bio, specialty, working_hours, branch_id FROM staff WHERE id = :sid AND tenant_id = :tid AND is_active = true"),
            {"sid": preferred_staff_id, "tid": tenant_id},
        )
    else:
        staff_query = """
            SELECT id, name, bio, specialty, working_hours, branch_id FROM staff
            WHERE tenant_id = :tid AND is_active = true
            AND (skills IS NULL OR skills @> :skill_filter)
        """
        staff_params: dict = {"tid": tenant_id, "skill_filter": f'["{service_id}"]'}
        if branch_id:
            staff_query += " AND branch_id = :bid"
            staff_params["bid"] = branch_id
        staff_result = await db.execute(text(staff_query), staff_params)
    staff_rows = staff_result.fetchall()

    if not staff_rows:
        return []

    # Day name for working hours lookup
    day_name = target_date.strftime("%a").lower()  # mon, tue, wed, ...

    all_slots: list[TimeSlot] = []

    for staff in staff_rows:
        # 3a. Get working hours for this day
        windows = _get_working_windows(staff, day_name, target_date, db, tenant_id)
        if not windows:
            # Try getting from branch
            windows = await _get_branch_hours(db, staff.branch_id, day_name)

        if not windows:
            continue

        # 3b. Get existing bookings for this staff on this date
        booked = await _get_booked_ranges(db, staff.id, target_date, tz_name)

        # 3c. Generate slots (duration + buffer = total blocked time)
        total_block = duration + buffer
        for window in windows:
            slots = _generate_slots_for_window(
                staff_id=staff.id,
                staff_name=staff.name,
                target_date=target_date,
                window=window,
                duration=duration,
                booked=booked,
                tz_name=tz_name,
                total_block=total_block,
                staff_bio=getattr(staff, 'bio', '') or '',
                staff_specialty=getattr(staff, 'specialty', '') or '',
                slot_increment=slot_increment,
            )
            all_slots.extend(slots)

    # 4. Sort by start time, then staff name
    all_slots.sort(key=lambda s: (s.start, s.staff_name))

    # Filter out slots in the past
    now = datetime.now(timezone.utc)
    all_slots = [s for s in all_slots if s.start > now]

    return all_slots


def _get_working_windows(staff, day_name: str, target_date: date, db, tenant_id) -> list[WorkingWindow]:
    """Extract working windows from staff's working_hours JSON."""
    hours = staff.working_hours
    if not hours or not isinstance(hours, dict):
        return []

    day_hours = hours.get(day_name, [])
    windows = []
    for entry in day_hours:
        try:
            open_t = time.fromisoformat(entry["open"])
            close_t = time.fromisoformat(entry["close"])
            windows.append(WorkingWindow(open_time=open_t, close_time=close_t))
        except (KeyError, ValueError):
            continue
    return windows


async def _get_branch_hours(db: AsyncSession, branch_id: UUID | None, day_name: str) -> list[WorkingWindow]:
    """Fallback: get working hours from branch."""
    if not branch_id:
        return []

    result = await db.execute(
        text("SELECT operating_hours FROM branches WHERE id = :bid"),
        {"bid": branch_id},
    )
    row = result.first()
    if not row or not row.operating_hours:
        return []

    day_hours = row.operating_hours.get(day_name, [])
    windows = []
    for entry in day_hours:
        try:
            open_t = time.fromisoformat(entry["open"])
            close_t = time.fromisoformat(entry["close"])
            windows.append(WorkingWindow(open_time=open_t, close_time=close_t))
        except (KeyError, ValueError):
            continue
    return windows


async def _get_booked_ranges(
    db: AsyncSession, staff_id: UUID, target_date: date, tz_name: str
) -> list[tuple[datetime, datetime]]:
    """Get all booked time ranges for a staff member on a date."""
    # Convert date to UTC range using the tenant timezone
    import zoneinfo
    tz = zoneinfo.ZoneInfo(tz_name)
    day_start = datetime.combine(target_date, time.min, tzinfo=tz)
    day_end = datetime.combine(target_date, time.max, tzinfo=tz)

    result = await db.execute(
        text("""
            SELECT starts_at, ends_at FROM bookings
            WHERE staff_id = :sid
            AND starts_at < :day_end AND ends_at > :day_start
            AND status IN ('pending_deposit', 'confirmed')
        """),
        {"sid": staff_id, "day_start": day_start, "day_end": day_end},
    )
    return [(row.starts_at, row.ends_at) for row in result.fetchall()]


def _generate_slots_for_window(
    staff_id: UUID,
    staff_name: str,
    target_date: date,
    window: WorkingWindow,
    duration: timedelta,
    booked: list[tuple[datetime, datetime]],
    tz_name: str,
    total_block: timedelta | None = None,
    staff_bio: str = "",
    staff_specialty: str = "",
    slot_increment: int = SLOT_INCREMENT,
) -> list[TimeSlot]:
    """Generate available time slots within a working window.

    total_block = duration + buffer. Used for conflict checking.
    The actual booking end time is still duration (not buffer).
    But we check that duration+buffer fits without overlapping next booking.
    """
    import zoneinfo
    tz = zoneinfo.ZoneInfo(tz_name)

    if total_block is None:
        total_block = duration

    window_start = datetime.combine(target_date, window.open_time, tzinfo=tz)
    window_end = datetime.combine(target_date, window.close_time, tzinfo=tz)
    if window.close_time == time(0, 0) and window.open_time > time(0, 0):
        window_end += timedelta(days=1)

    slots: list[TimeSlot] = []
    current = window_start
    increment = timedelta(minutes=max(5, slot_increment))

    while current + duration <= window_end:
        slot_end = current + duration
        # Check with buffer: the full blocked range extends by buffer
        blocked_end = current + total_block

        # Check overlap with any existing booking (using buffered end)
        has_conflict = any(
            current < bk_end and blocked_end > bk_start
            for bk_start, bk_end in booked
        )

        if not has_conflict:
            slots.append(TimeSlot(
                staff_id=staff_id,
                staff_name=staff_name,
                start=current,
                end=slot_end,
                staff_bio=staff_bio,
                staff_specialty=staff_specialty,
            ))

        current += increment

    return slots


async def get_available_dates(
    db: AsyncSession,
    tenant_id: UUID,
    service_id: UUID,
    num_days: int = 7,
    tz_name: str = "Asia/Kolkata",
) -> list[date]:
    """Get the next N dates that have at least one available slot."""
    import zoneinfo
    tz = zoneinfo.ZoneInfo(tz_name)
    today = datetime.now(tz).date()

    available_dates = []
    for i in range(num_days + 14):  # check up to 3 weeks out
        d = today + timedelta(days=i)
        slots = await get_available_slots(db, tenant_id, service_id, d, tz_name=tz_name)
        if slots:
            available_dates.append(d)
        if len(available_dates) >= num_days:
            break

    return available_dates
