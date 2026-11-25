"""Owner command handler — executes parsed commands against the booking system."""
from __future__ import annotations

import zoneinfo
from datetime import date, datetime, timedelta, timezone
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.bookings.models import BookingStatus
from app.bookings.repository import get_bookings_for_date, update_booking_status
from app.labels import get_labels
from app.owner_console.parser import OwnerCommand, ParsedCommand, parse_command

logger = structlog.get_logger()


async def handle_owner_message(
    db: AsyncSession,
    tenant_id: str,
    body: str,
    business_name: str,
    tz_name: str = "Asia/Kolkata",
    business_type: str = "salon",
) -> str:
    """Process an owner WhatsApp message and return the reply."""
    parsed = parse_command(body, tz_name)

    match parsed.command:
        case OwnerCommand.TODAY | OwnerCommand.TOMORROW | OwnerCommand.BOOKINGS:
            return await _cmd_bookings(db, tenant_id, parsed, tz_name)
        case OwnerCommand.WEEK:
            return await _cmd_week(db, tenant_id, tz_name, business_type)
        case OwnerCommand.CANCEL:
            return await _cmd_cancel(db, tenant_id, parsed)
        case OwnerCommand.BLOCK:
            return await _cmd_block(db, tenant_id, parsed, tz_name)
        case OwnerCommand.UNBLOCK:
            return await _cmd_unblock(db, tenant_id, parsed, tz_name)
        case OwnerCommand.STATS:
            return await _cmd_stats(db, tenant_id, tz_name)
        case OwnerCommand.FREE:
            return await _cmd_free(db, tenant_id, parsed, tz_name)
        case OwnerCommand.PAUSE:
            return await _cmd_pause(db, tenant_id)
        case OwnerCommand.RESUME:
            return await _cmd_resume(db, tenant_id)
        case OwnerCommand.HELP:
            return _cmd_help(business_name, business_type)
        case OwnerCommand.UNKNOWN:
            # Check for extended commands before giving up
            return await _try_extended_commands(db, tenant_id, body, business_name)


async def _cmd_bookings(db: AsyncSession, tenant_id: str, parsed: ParsedCommand, tz_name: str) -> str:
    """List bookings for a date."""
    target = parsed.target_date
    if not target:
        tz = zoneinfo.ZoneInfo(tz_name)
        target = datetime.now(tz).date()

    bookings = await get_bookings_for_date(db, UUID(tenant_id), target)

    day_label = _day_label(target, tz_name)

    if not bookings:
        return f"📅 {day_label}: No bookings."

    lines = [f"📅 {day_label} ({len(bookings)} bookings):"]
    for b in bookings:
        start = datetime.fromisoformat(b["starts_at"])
        tz = zoneinfo.ZoneInfo(tz_name)
        local_time = start.astimezone(tz).strftime("%I:%M %p").lstrip("0")
        status_icon = {"confirmed": "✅", "pending_deposit": "⏳", "completed": "✔️", "cancelled": "❌", "no_show": "👻"}.get(b["status"], "")
        lines.append(f"  {status_icon} {local_time} — {b['service']} — {b['customer'] or 'Unknown'} ({b['phone'][-4:]})")
        lines.append(f"    ID: {b['id'][:8]} | Staff: {b['staff']}")

    return "\n".join(lines)


async def _cmd_cancel(db: AsyncSession, tenant_id: str, parsed: ParsedCommand) -> str:
    """Cancel a booking by reference (first 8 chars of UUID)."""
    ref = parsed.booking_ref
    if not ref:
        return "Usage: *cancel <booking-id>*\n\nGet booking IDs from *today* command."

    ref = ref.strip()

    # Find booking by partial ID
    result = await db.execute(
        text("SELECT id, status, starts_at FROM bookings WHERE tenant_id = :tid AND id::text LIKE :ref AND status IN ('confirmed', 'pending_deposit')"),
        {"tid": tenant_id, "ref": f"{ref}%"},
    )
    booking = result.first()

    if not booking:
        return f"No active booking found starting with *{ref}*."

    try:
        await update_booking_status(
            db, UUID(tenant_id), booking.id,
            BookingStatus.CANCELLED, cancellation_reason="Cancelled by owner"
        )
        return f"❌ Booking {str(booking.id)[:8]} cancelled."
    except Exception as e:
        return f"Error cancelling: {e}"


async def _cmd_block(db: AsyncSession, tenant_id: str, parsed: ParsedCommand, tz_name: str) -> str:
    """Block a time slot (creates a blocking booking)."""
    target = parsed.target_date
    if not target:
        return "Usage: *block tomorrow 4-6pm*"

    if not parsed.time_start:
        return "Please include a time range. Example: *block tomorrow 4-6pm*"

    tz = zoneinfo.ZoneInfo(tz_name)
    start = datetime.combine(target, parsed.time_start, tzinfo=tz)
    end = start + timedelta(hours=2)  # default 2h block
    if parsed.time_end:
        end = datetime.combine(target, parsed.time_end, tzinfo=tz)

    # Get all staff and block them
    staff_result = await db.execute(
        text("SELECT id, name FROM staff WHERE tenant_id = :tid AND is_active = true"),
        {"tid": tenant_id},
    )
    staff = staff_result.fetchall()

    if not staff:
        return "No staff found."

    import uuid as uuid_mod
    blocked = 0
    for s in staff:
        try:
            await db.execute(
                text("""INSERT INTO bookings (id, tenant_id, branch_id, staff_id, customer_id, service_id,
                    starts_at, ends_at, status, cancellation_reason, created_at, updated_at)
                    SELECT :id, :tid, b.id, :sid, :cid, :svc_id, :start, :end, 'confirmed', 'BLOCKED', :now, :now
                    FROM branches b WHERE b.tenant_id = :tid AND b.is_primary = true LIMIT 1"""),
                {
                    "id": uuid_mod.uuid4(), "tid": tenant_id, "sid": s.id,
                    "cid": uuid_mod.uuid4(), "svc_id": uuid_mod.uuid4(),
                    "start": start, "end": end, "now": datetime.now(timezone.utc),
                },
            )
            blocked += 1
        except Exception:
            pass  # Overlap with existing booking — fine

    await db.commit()

    day_label = _day_label(target, tz_name)
    time_range = f"{parsed.time_start.strftime('%I:%M %p').lstrip('0')} - {end.astimezone(tz).strftime('%I:%M %p').lstrip('0')}"
    return f"🚫 Blocked {day_label} {time_range} for {blocked} staff."


async def _cmd_week(db: AsyncSession, tenant_id: str, tz_name: str, business_type: str = "salon") -> str:
    """Summary of bookings for the next 7 days."""
    tz = zoneinfo.ZoneInfo(tz_name)
    today = datetime.now(tz).date()
    end_date = today + timedelta(days=6)
    labels = get_labels(business_type)

    result = await db.execute(
        text("""
            SELECT (b.starts_at AT TIME ZONE :tz)::date AS day,
                   COUNT(*) FILTER (WHERE b.status IN ('confirmed', 'pending_deposit')) AS upcoming,
                   COUNT(*) FILTER (WHERE b.status = 'completed') AS completed
            FROM bookings b
            WHERE b.tenant_id = :tid
              AND b.cancellation_reason IS DISTINCT FROM 'BLOCKED'
              AND (b.starts_at AT TIME ZONE :tz)::date BETWEEN :start_d AND :end_d
            GROUP BY day
            ORDER BY day
        """),
        {"tid": tenant_id, "tz": tz_name, "start_d": today, "end_d": end_date},
    )
    by_day = {r.day: r for r in result.fetchall()}
    lines = [f"📅 *Next 7 Days ({labels['bookings']}):*\n"]
    total = 0
    for i in range(7):
        d = today + timedelta(days=i)
        r = by_day.get(d)
        cnt = (r.upcoming + r.completed) if r else 0
        total += cnt
        label_str = _day_label(d, tz_name)
        lines.append(f"  • {label_str}: {cnt}" if cnt else f"  • {label_str}: —")
    lines.append(f"\nTotal: *{total}* {labels['bookings'].lower()}")
    return "\n".join(lines)


async def _cmd_unblock(db: AsyncSession, tenant_id: str, parsed: ParsedCommand, tz_name: str = "Asia/Kolkata") -> str:
    """Remove blocked time slots (optionally scoped to a time window)."""
    params: dict = {"tid": tenant_id, "d": parsed.target_date}
    query = "DELETE FROM bookings WHERE tenant_id = :tid AND cancellation_reason = 'BLOCKED' AND starts_at::date = :d"
    if parsed.time_start and parsed.target_date:
        tz = zoneinfo.ZoneInfo(tz_name)
        start = datetime.combine(parsed.target_date, parsed.time_start, tzinfo=tz)
        end = datetime.combine(parsed.target_date, parsed.time_end, tzinfo=tz) if parsed.time_end else start + timedelta(hours=2)
        query += " AND starts_at < :end AND ends_at > :start"
        params["start"] = start
        params["end"] = end
    query += " RETURNING id"

    result = await db.execute(text(query), params)
    deleted = result.fetchall()
    await db.commit()

    if deleted:
        return f"✅ Unblocked {len(deleted)} slot(s)."
    return "No blocked slots found for that date."


async def _cmd_stats(db: AsyncSession, tenant_id: str, tz_name: str) -> str:
    """Weekly stats summary."""
    tz = zoneinfo.ZoneInfo(tz_name)
    today = datetime.now(tz).date()
    week_start = today - timedelta(days=today.weekday())

    result = await db.execute(
        text("""
            SELECT
                COUNT(*) FILTER (WHERE status = 'confirmed') as confirmed,
                COUNT(*) FILTER (WHERE status = 'completed') as completed,
                COUNT(*) FILTER (WHERE status = 'cancelled') as cancelled,
                COUNT(*) FILTER (WHERE status = 'no_show') as no_shows,
                COUNT(*) as total
            FROM bookings
            WHERE tenant_id = :tid AND starts_at >= :week_start
        """),
        {"tid": tenant_id, "week_start": datetime.combine(week_start, datetime.min.time(), tzinfo=tz)},
    )
    s = result.first()

    # Customer count this week
    cust_result = await db.execute(
        text("SELECT COUNT(DISTINCT customer_id) as cnt FROM bookings WHERE tenant_id = :tid AND starts_at >= :ws"),
        {"tid": tenant_id, "ws": datetime.combine(week_start, datetime.min.time(), tzinfo=tz)},
    )
    cust_count = cust_result.first().cnt

    no_show_rate = (s.no_shows / s.total * 100) if s.total > 0 else 0

    return (
        f"📊 This week ({week_start.strftime('%d %b')} – {today.strftime('%d %b')}):\n\n"
        f"  Total bookings: {s.total}\n"
        f"  ✅ Confirmed: {s.confirmed}\n"
        f"  ✔️ Completed: {s.completed}\n"
        f"  ❌ Cancelled: {s.cancelled}\n"
        f"  👻 No-shows: {s.no_shows} ({no_show_rate:.0f}%)\n"
        f"  👥 Unique customers: {cust_count}"
    )


async def _cmd_free(db: AsyncSession, tenant_id: str, parsed: ParsedCommand, tz_name: str) -> str:
    """Show free slots for a date."""
    from app.bookings.slots import get_available_slots

    target = parsed.target_date
    if not target:
        tz = zoneinfo.ZoneInfo(tz_name)
        target = datetime.now(tz).date()

    # Get all services to show slots for the first one (or just show time windows)
    svc_result = await db.execute(
        text("SELECT id, name, duration_minutes FROM services WHERE tenant_id = :tid AND is_active = true ORDER BY sort_order LIMIT 1"),
        {"tid": tenant_id},
    )
    svc = svc_result.first()
    if not svc:
        return "No services configured."

    slots = await get_available_slots(db, UUID(tenant_id), svc.id, target, tz_name=tz_name)

    day_label = _day_label(target, tz_name)

    if not slots:
        return f"📅 {day_label}: Fully booked!"

    # Group by staff
    by_staff: dict[str, list] = {}
    for s in slots:
        by_staff.setdefault(s.staff_name, []).append(s)

    lines = [f"📅 {day_label} free slots ({svc.name}, {svc.duration_minutes}min):"]
    for staff_name, staff_slots in by_staff.items():
        times = [s.start.strftime("%I:%M%p").lstrip("0").lower() for s in staff_slots[:8]]
        lines.append(f"  {staff_name}: {', '.join(times)}{'...' if len(staff_slots) > 8 else ''}")

    return "\n".join(lines)


async def _cmd_pause(db: AsyncSession, tenant_id: str) -> str:
    """Temporarily disable new bookings."""
    await db.execute(
        text("UPDATE tenants SET is_active = false WHERE id = :tid"),
        {"tid": tenant_id},
    )
    await db.commit()
    return "⏸️ Bookings paused. Customers will be told you're unavailable. Type *resume* to re-enable."


async def _cmd_resume(db: AsyncSession, tenant_id: str) -> str:
    """Re-enable bookings."""
    await db.execute(
        text("UPDATE tenants SET is_active = true WHERE id = :tid"),
        {"tid": tenant_id},
    )
    await db.commit()
    return "▶️ Bookings resumed! You're back online."


async def _try_extended_commands(db: AsyncSession, tenant_id: str, body: str, business_name: str) -> str:
    """Try to match extended management commands."""
    from app.owner_console.commands import (
        handle_add_service,
        handle_add_staff,
        handle_list_services,
        handle_list_staff,
        handle_remove_service,
        handle_set_buffer,
    )

    lower = body.strip().lower()

    if lower.startswith("add service "):
        return await handle_add_service(db, tenant_id, body[12:])
    if lower.startswith("remove service "):
        return await handle_remove_service(db, tenant_id, body[15:])
    if lower.startswith("add staff "):
        return await handle_add_staff(db, tenant_id, body[10:])
    if lower in ("services", "my services", "list services"):
        return await handle_list_services(db, tenant_id)
    if lower in ("staff", "my staff", "team", "list staff"):
        return await handle_list_staff(db, tenant_id)
    if lower.startswith("set buffer "):
        return await handle_set_buffer(db, tenant_id, body[11:])

    return f"Didn't understand: \"{body}\"\n\nType *help* for commands."


def _cmd_help(business_name: str, business_type: str = "salon") -> str:
    labels = get_labels(business_type)
    return (
        f"🔧 *{business_name}* Commands:\n\n"
        f"*📅 Schedule*\n"
        f"  *today* — today's {labels['bookings'].lower()}\n"
        f"  *tomorrow* — tomorrow's {labels['bookings'].lower()}\n"
        f"  *free* — available slots\n"
        f"  *stats* — weekly summary\n\n"
        f"*🔧 Manage*\n"
        f"  *{labels['services'].lower()}* — list {labels['services'].lower()}\n"
        f"  *{labels['staff_plural'].lower()}* — list {labels['staff_plural'].lower()}\n"
        f"  *add service <name> <mins> <price>*\n"
        f"  *remove service <name>*\n"
        f"  *add staff <name> <role>*\n"
        f"  *set buffer 10* — gap between {labels['bookings'].lower()}\n\n"
        f"*⚡ Actions*\n"
        f"  *cancel <id>* — cancel {labels['booking'].lower()}\n"
        f"  *block tomorrow 4-6pm*\n"
        f"  *unblock tomorrow*\n"
        f"  *pause* / *resume*\n"
    )


def _day_label(d: date, tz_name: str) -> str:
    tz = zoneinfo.ZoneInfo(tz_name)
    today = datetime.now(tz).date()
    if d == today:
        return "Today"
    if d == today + timedelta(days=1):
        return "Tomorrow"
    return d.strftime("%A, %d %b")
