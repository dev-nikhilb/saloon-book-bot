"""Extended owner commands for managing services, staff, and business settings.

These are simple text commands that let owners add/edit services and staff
without needing a dashboard.

Usage:
  add service Haircut 30 500          → adds service: 30min, ₹500
  add service "Hair Color" 90 2000    → adds service: 90min, ₹2000
  remove service Haircut              → deactivates service
  add staff Meena stylist             → adds staff member
  set buffer 10                       → set 10min buffer between all bookings
  set hours 9am-8pm                   → set working hours
  services                            → list all services
  staff                               → list all staff
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()


async def handle_add_service(db: AsyncSession, tenant_id: str, args: str) -> str:
    """Add a new service. Format: add service <name> <duration_min> <price_rupees>"""
    # Parse: "Haircut 30 500" or '"Hair Color" 90 2000'
    match = re.match(r'(?:"([^"]+)"|(\S+))\s+(\d+)\s+(\d+)', args.strip())
    if not match:
        return "Usage: *add service <name> <minutes> <price>*\nExample: add service Haircut 30 500"

    name = match.group(1) or match.group(2)
    duration = int(match.group(3))
    price = int(match.group(4))

    sid = uuid.uuid4()
    await db.execute(
        text("""INSERT INTO services (id, tenant_id, name, duration_minutes, price_paise, is_active, sort_order, created_at)
                VALUES (:id, :tid, :name, :dur, :price, true, 0, :now)"""),
        {"id": sid, "tid": tenant_id, "name": name, "dur": duration, "price": price * 100, "now": datetime.now(timezone.utc)},
    )

    # Add to all existing staff skills
    await db.execute(
        text("""UPDATE staff SET skills = COALESCE(skills, '[]'::jsonb) || to_jsonb(CAST(:sid AS text))
                WHERE tenant_id = :tid AND is_active = true"""),
        {"sid": str(sid), "tid": tenant_id},
    )
    await db.commit()

    return f"✅ Added: *{name}* ({duration}min, ₹{price})"


async def handle_remove_service(db: AsyncSession, tenant_id: str, name: str) -> str:
    """Deactivate a service by name."""
    result = await db.execute(
        text("UPDATE services SET is_active = false WHERE tenant_id = :tid AND LOWER(name) = LOWER(:name) AND is_active = true RETURNING name"),
        {"tid": tenant_id, "name": name.strip()},
    )
    row = result.first()
    await db.commit()

    if row:
        return f"❌ Removed: *{row.name}*"
    return f"No active service named '{name}' found."


async def handle_add_staff(db: AsyncSession, tenant_id: str, args: str) -> str:
    """Add a staff member. Format: add staff <name> [role] [bio]"""
    parts = args.strip().split(maxsplit=2)
    if not parts:
        return "Usage: *add staff <name> [role]*\nExample: add staff Meena stylist"

    name = parts[0]
    role = parts[1] if len(parts) > 1 else None
    bio = parts[2] if len(parts) > 2 else None

    # Get branch and existing staff hours as template
    branch_result = await db.execute(
        text("SELECT id FROM branches WHERE tenant_id = :tid AND is_primary = true LIMIT 1"),
        {"tid": tenant_id},
    )
    branch = branch_result.first()

    # Copy working hours from first existing staff
    hours_result = await db.execute(
        text("SELECT working_hours FROM staff WHERE tenant_id = :tid AND is_active = true LIMIT 1"),
        {"tid": tenant_id},
    )
    hours_row = hours_result.first()
    hours = json.dumps(hours_row.working_hours) if hours_row and hours_row.working_hours else None

    # Get all active service IDs
    svc_result = await db.execute(
        text("SELECT id FROM services WHERE tenant_id = :tid AND is_active = true"),
        {"tid": tenant_id},
    )
    service_ids = [str(row.id) for row in svc_result.fetchall()]

    sid = uuid.uuid4()
    await db.execute(
        text("""INSERT INTO staff (id, tenant_id, branch_id, name, role, bio, skills, working_hours, is_active, created_at)
                VALUES (:id, :tid, :bid, :name, :role, :bio, :skills, :hours, true, :now)"""),
        {
            "id": sid, "tid": tenant_id, "bid": branch.id if branch else None,
            "name": name, "role": role, "bio": bio,
            "skills": json.dumps(service_ids), "hours": hours,
            "now": datetime.now(timezone.utc),
        },
    )
    await db.commit()

    return f"✅ Added staff: *{name}*" + (f" ({role})" if role else "") + "\nThey can do all services. Same hours as existing staff."


async def handle_list_services(db: AsyncSession, tenant_id: str) -> str:
    """List all active services."""
    result = await db.execute(
        text("SELECT name, duration_minutes, price_paise, buffer_minutes FROM services WHERE tenant_id = :tid AND is_active = true ORDER BY sort_order"),
        {"tid": tenant_id},
    )
    services = result.fetchall()
    if not services:
        return "No services configured. Use *add service <name> <minutes> <price>* to add one."

    lines = ["💇 *Your services:*\n"]
    for s in services:
        line = f"  • {s.name} — {s.duration_minutes}min — ₹{s.price_paise // 100}"
        if s.buffer_minutes:
            line += f" (+{s.buffer_minutes}min buffer)"
        lines.append(line)
    return "\n".join(lines)


async def handle_list_staff(db: AsyncSession, tenant_id: str) -> str:
    """List all active staff."""
    result = await db.execute(
        text("SELECT name, role, bio, specialty FROM staff WHERE tenant_id = :tid AND is_active = true"),
        {"tid": tenant_id},
    )
    staff = result.fetchall()
    if not staff:
        return "No staff configured."

    lines = ["👥 *Your team:*\n"]
    for s in staff:
        line = f"  • *{s.name}*"
        if s.role:
            line += f" — {s.role}"
        if s.bio:
            line += f"\n    _{s.bio}_"
        if s.specialty:
            line += f"\n    Specialty: {s.specialty}"
        lines.append(line)
    return "\n".join(lines)


async def handle_set_buffer(db: AsyncSession, tenant_id: str, minutes_str: str) -> str:
    """Set buffer time between bookings for all services."""
    try:
        minutes = int(minutes_str.strip())
        if minutes < 0 or minutes > 60:
            raise ValueError
    except ValueError:
        return "Usage: *set buffer <minutes>* (0-60)\nExample: set buffer 10"

    await db.execute(
        text("UPDATE services SET buffer_minutes = :mins WHERE tenant_id = :tid AND is_active = true"),
        {"mins": minutes, "tid": tenant_id},
    )
    await db.commit()

    if minutes == 0:
        return "✅ Buffer removed. Bookings can be back-to-back."
    return f"✅ {minutes}min buffer set between all bookings."
