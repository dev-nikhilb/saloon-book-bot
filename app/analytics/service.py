"""Analytics service — raw SQL query functions for tenant dashboards.

All functions accept an async SQLAlchemy session, filter by tenant_id,
and return plain dicts suitable for JSON serialization.
"""
from __future__ import annotations

from datetime import date

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()


async def get_overview(
    db: AsyncSession,
    tenant_id: str,
    tz_name: str = "Asia/Kolkata",
) -> dict:
    """Dashboard overview: today's bookings, this week stats, total customers, revenue.

    All date comparisons are done in the tenant's local timezone.
    """
    result = await db.execute(
        text("""
            WITH today AS (
                SELECT
                    COUNT(*) FILTER (WHERE b.status IN ('confirmed', 'completed'))
                        AS today_bookings,
                    COUNT(*) FILTER (WHERE b.status = 'completed')
                        AS today_completed,
                    COUNT(*) FILTER (WHERE b.status = 'cancelled')
                        AS today_cancelled,
                    COALESCE(SUM(p.amount_paise) FILTER (
                        WHERE p.status = 'captured'
                        AND (b.starts_at AT TIME ZONE :tz)::date = (NOW() AT TIME ZONE :tz)::date
                    ), 0) AS today_revenue_paise
                FROM bookings b
                LEFT JOIN payments p ON p.booking_id = b.id AND p.status = 'captured'
                WHERE b.tenant_id = :tid
                  AND (b.starts_at AT TIME ZONE :tz)::date = (NOW() AT TIME ZONE :tz)::date
            ),
            week AS (
                SELECT
                    COUNT(*) FILTER (WHERE b.status IN ('confirmed', 'completed'))
                        AS week_bookings,
                    COUNT(*) FILTER (WHERE b.status = 'completed')
                        AS week_completed,
                    COALESCE(SUM(p.amount_paise) FILTER (WHERE p.status = 'captured'), 0)
                        AS week_revenue_paise
                FROM bookings b
                LEFT JOIN payments p ON p.booking_id = b.id AND p.status = 'captured'
                WHERE b.tenant_id = :tid
                  AND (b.starts_at AT TIME ZONE :tz)::date
                      >= date_trunc('week', (NOW() AT TIME ZONE :tz))::date
            ),
            totals AS (
                SELECT
                    COUNT(DISTINCT c.id) AS total_customers
                FROM customers c
                WHERE c.tenant_id = :tid
            )
            SELECT
                t.today_bookings,
                t.today_completed,
                t.today_cancelled,
                t.today_revenue_paise,
                w.week_bookings,
                w.week_completed,
                w.week_revenue_paise,
                tot.total_customers
            FROM today t, week w, totals tot
        """),
        {"tid": tenant_id, "tz": tz_name},
    )
    row = result.first()
    if not row:
        return {}

    return {
        "today": {
            "bookings": row.today_bookings,
            "completed": row.today_completed,
            "cancelled": row.today_cancelled,
            "revenue_paise": row.today_revenue_paise,
        },
        "this_week": {
            "bookings": row.week_bookings,
            "completed": row.week_completed,
            "revenue_paise": row.week_revenue_paise,
        },
        "total_customers": row.total_customers,
    }


async def get_booking_stats(
    db: AsyncSession,
    tenant_id: str,
    from_date: date,
    to_date: date,
) -> list[dict]:
    """Daily booking counts grouped by status for a date range."""
    result = await db.execute(
        text("""
            SELECT
                (b.starts_at AT TIME ZONE 'Asia/Kolkata')::date AS day,
                b.status,
                COUNT(*) AS count
            FROM bookings b
            WHERE b.tenant_id = :tid
              AND (b.starts_at AT TIME ZONE 'Asia/Kolkata')::date BETWEEN :fd AND :td
            GROUP BY day, b.status
            ORDER BY day, b.status
        """),
        {"tid": tenant_id, "fd": from_date, "td": to_date},
    )
    rows = result.fetchall()
    return [
        {"day": row.day.isoformat(), "status": row.status, "count": row.count}
        for row in rows
    ]


async def get_revenue_stats(
    db: AsyncSession,
    tenant_id: str,
    from_date: date,
    to_date: date,
) -> dict:
    """Revenue breakdown: by service and by day.

    Only counts captured payments.
    """
    # Revenue by service
    by_service_result = await db.execute(
        text("""
            SELECT
                s.name AS service_name,
                COUNT(p.id) AS payment_count,
                COALESCE(SUM(p.amount_paise), 0) AS total_paise
            FROM payments p
            JOIN bookings b ON p.booking_id = b.id
            JOIN services s ON b.service_id = s.id
            WHERE p.tenant_id = :tid
              AND p.status = 'captured'
              AND p.created_at::date BETWEEN :fd AND :td
            GROUP BY s.name
            ORDER BY total_paise DESC
        """),
        {"tid": tenant_id, "fd": from_date, "td": to_date},
    )
    by_service = [
        {
            "service_name": row.service_name,
            "payment_count": row.payment_count,
            "total_paise": row.total_paise,
        }
        for row in by_service_result.fetchall()
    ]

    # Revenue by day
    by_day_result = await db.execute(
        text("""
            SELECT
                p.created_at::date AS day,
                COUNT(p.id) AS payment_count,
                COALESCE(SUM(p.amount_paise), 0) AS total_paise
            FROM payments p
            WHERE p.tenant_id = :tid
              AND p.status = 'captured'
              AND p.created_at::date BETWEEN :fd AND :td
            GROUP BY day
            ORDER BY day
        """),
        {"tid": tenant_id, "fd": from_date, "td": to_date},
    )
    by_day = [
        {
            "day": row.day.isoformat(),
            "payment_count": row.payment_count,
            "total_paise": row.total_paise,
        }
        for row in by_day_result.fetchall()
    ]

    return {"by_service": by_service, "by_day": by_day}


async def get_staff_utilization(
    db: AsyncSession,
    tenant_id: str,
    from_date: date,
    to_date: date,
) -> list[dict]:
    """Bookings per staff member with hours utilized in the date range.

    Hours are calculated from actual booking durations (starts_at to ends_at).
    """
    result = await db.execute(
        text("""
            SELECT
                st.id AS staff_id,
                st.name AS staff_name,
                COUNT(b.id) AS booking_count,
                COUNT(b.id) FILTER (WHERE b.status = 'completed') AS completed_count,
                COUNT(b.id) FILTER (WHERE b.status = 'no_show') AS no_show_count,
                COALESCE(
                    SUM(EXTRACT(EPOCH FROM (b.ends_at - b.starts_at)) / 3600.0)
                    FILTER (WHERE b.status IN ('confirmed', 'completed')),
                    0
                ) AS hours_utilized
            FROM staff st
            LEFT JOIN bookings b
                ON b.staff_id = st.id
                AND b.tenant_id = st.tenant_id
                AND (b.starts_at AT TIME ZONE 'Asia/Kolkata')::date BETWEEN :fd AND :td
            WHERE st.tenant_id = :tid
              AND st.is_active = true
            GROUP BY st.id, st.name
            ORDER BY booking_count DESC
        """),
        {"tid": tenant_id, "fd": from_date, "td": to_date},
    )
    return [
        {
            "staff_id": str(row.staff_id),
            "staff_name": row.staff_name,
            "booking_count": row.booking_count,
            "completed_count": row.completed_count,
            "no_show_count": row.no_show_count,
            "hours_utilized": round(float(row.hours_utilized), 2),
        }
        for row in result.fetchall()
    ]


async def get_customer_metrics(
    db: AsyncSession,
    tenant_id: str,
) -> dict:
    """Customer metrics: new vs returning, top customers by visits, no-show rate."""
    # New vs returning (new = only 1 booking ever)
    segments_result = await db.execute(
        text("""
            SELECT
                COUNT(*) FILTER (WHERE c.total_bookings <= 1) AS new_customers,
                COUNT(*) FILTER (WHERE c.total_bookings > 1) AS returning_customers,
                COUNT(*) AS total_customers,
                CASE
                    WHEN SUM(c.total_bookings) > 0
                    THEN ROUND(SUM(c.total_no_shows)::numeric / SUM(c.total_bookings) * 100, 1)
                    ELSE 0
                END AS no_show_rate_pct
            FROM customers c
            WHERE c.tenant_id = :tid
        """),
        {"tid": tenant_id},
    )
    seg = segments_result.first()

    # Top 10 customers by bookings
    top_result = await db.execute(
        text("""
            SELECT
                c.id AS customer_id,
                c.name,
                c.phone,
                c.total_bookings,
                c.total_no_shows,
                c.last_visit_at
            FROM customers c
            WHERE c.tenant_id = :tid
            ORDER BY c.total_bookings DESC
            LIMIT 10
        """),
        {"tid": tenant_id},
    )
    top_customers = [
        {
            "customer_id": str(row.customer_id),
            "name": row.name,
            "phone": row.phone,
            "total_bookings": row.total_bookings,
            "total_no_shows": row.total_no_shows,
            "last_visit_at": row.last_visit_at.isoformat() if row.last_visit_at else None,
        }
        for row in top_result.fetchall()
    ]

    return {
        "new_customers": seg.new_customers if seg else 0,
        "returning_customers": seg.returning_customers if seg else 0,
        "total_customers": seg.total_customers if seg else 0,
        "no_show_rate_pct": float(seg.no_show_rate_pct) if seg and seg.no_show_rate_pct else 0.0,
        "top_customers": top_customers,
    }


async def get_popular_services(
    db: AsyncSession,
    tenant_id: str,
    from_date: date,
    to_date: date,
) -> list[dict]:
    """Most booked services in a date range, with revenue and average rating."""
    result = await db.execute(
        text("""
            SELECT
                s.id AS service_id,
                s.name AS service_name,
                COUNT(b.id) AS booking_count,
                COUNT(b.id) FILTER (WHERE b.status = 'completed') AS completed_count,
                COALESCE(SUM(p.amount_paise) FILTER (WHERE p.status = 'captured'), 0)
                    AS revenue_paise,
                ROUND(AVG(f.rating) FILTER (WHERE f.rating IS NOT NULL), 1)
                    AS avg_rating
            FROM services s
            LEFT JOIN bookings b
                ON b.service_id = s.id
                AND b.tenant_id = s.tenant_id
                AND (b.starts_at AT TIME ZONE 'Asia/Kolkata')::date BETWEEN :fd AND :td
            LEFT JOIN payments p
                ON p.booking_id = b.id
            LEFT JOIN feedback f
                ON f.booking_id = b.id
            WHERE s.tenant_id = :tid
              AND s.is_active = true
              AND s.duration_minutes > 0
            GROUP BY s.id, s.name
            ORDER BY booking_count DESC
        """),
        {"tid": tenant_id, "fd": from_date, "td": to_date},
    )
    return [
        {
            "service_id": str(row.service_id),
            "service_name": row.service_name,
            "booking_count": row.booking_count,
            "completed_count": row.completed_count,
            "revenue_paise": row.revenue_paise,
            "avg_rating": float(row.avg_rating) if row.avg_rating else None,
        }
        for row in result.fetchall()
    ]
