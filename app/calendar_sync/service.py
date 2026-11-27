"""Google Calendar 2-way sync foundation.

Provides OAuth authorization URL generation, encrypted refresh-token storage,
and safe event push/delete helpers that gracefully no-op when Google OAuth
credentials are not configured.
"""
from __future__ import annotations

import urllib.parse
import uuid

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.crypto import decrypt_secret, encrypt_secret

logger = structlog.get_logger()

GOOGLE_CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"


def is_google_calendar_configured() -> bool:
    return bool(settings.google_oauth_client_id and settings.google_oauth_client_secret)


def build_oauth_url(state: str) -> str | None:
    """Return Google OAuth2 consent URL for connecting a staff member's calendar."""
    if not is_google_calendar_configured():
        return None
    params = urllib.parse.urlencode(
        {
            "client_id": settings.google_oauth_client_id,
            "redirect_uri": settings.google_oauth_redirect_uri,
            "response_type": "code",
            "scope": GOOGLE_CALENDAR_SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
    )
    return f"https://accounts.google.com/o/oauth2/v2/auth?{params}"


async def save_staff_calendar_credentials(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    staff_id: uuid.UUID,
    calendar_id: str,
    refresh_token: str,
) -> None:
    """Encrypt and store a staff member's Google Calendar refresh token."""
    encrypted = encrypt_secret(refresh_token)
    await db.execute(
        text("""
            UPDATE staff
            SET google_calendar_id = :cal_id,
                google_oauth_refresh_token_encrypted = :tok
            WHERE id = :sid AND tenant_id = :tid
        """),
        {"cal_id": calendar_id, "tok": encrypted, "sid": staff_id, "tid": tenant_id},
    )
    await db.commit()


async def sync_booking_to_calendar(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    booking_id: uuid.UUID,
) -> str | None:
    """Push a confirmed booking to the assigned staff member's Google Calendar.

    Returns created Google event ID, or None if calendar sync is disabled/unconfigured.
    """
    if not is_google_calendar_configured():
        return None

    res = await db.execute(
        text("""
            SELECT b.id, b.starts_at, b.ends_at, b.reason, b.google_event_id,
                   s.name AS service_name, c.name AS customer_name, c.phone AS customer_phone,
                   st.google_calendar_id, st.google_oauth_refresh_token_encrypted,
                   t.business_name, t.timezone,
                   COALESCE(ts.google_calendar_enabled, false) AS cal_enabled
            FROM bookings b
            JOIN services s ON s.id = b.service_id
            JOIN customers c ON c.id = b.customer_id
            JOIN tenants t ON t.id = b.tenant_id
            LEFT JOIN staff st ON st.id = b.staff_id
            LEFT JOIN tenant_settings ts ON ts.tenant_id = t.id
            WHERE b.id = :bid AND b.tenant_id = :tid
            LIMIT 1
        """),
        {"bid": booking_id, "tid": tenant_id},
    )
    row = res.first()
    if not row or not row.cal_enabled or not row.google_calendar_id or not row.google_oauth_refresh_token_encrypted:
        return None

    refresh_token = decrypt_secret(row.google_oauth_refresh_token_encrypted)
    if not refresh_token:
        return None

    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=settings.google_oauth_client_id,
            client_secret=settings.google_oauth_client_secret,
            scopes=[GOOGLE_CALENDAR_SCOPE],
        )
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        event_body = {
            "summary": f"{row.service_name} — {row.customer_name or row.customer_phone}",
            "description": f"Booked via Tivoria ({row.business_name})\nPhone: {row.customer_phone}"
            + (f"\nReason: {row.reason}" if row.reason else ""),
            "start": {"dateTime": row.starts_at.isoformat(), "timeZone": row.timezone or "Asia/Kolkata"},
            "end": {"dateTime": row.ends_at.isoformat(), "timeZone": row.timezone or "Asia/Kolkata"},
        }
        created = service.events().insert(calendarId=row.google_calendar_id, body=event_body).execute()
        event_id = created.get("id")
        if event_id:
            await db.execute(
                text("UPDATE bookings SET google_event_id = :eid WHERE id = :bid AND tenant_id = :tid"),
                {"eid": event_id, "bid": booking_id, "tid": tenant_id},
            )
            await db.commit()
        return event_id
    except Exception as exc:
        logger.warning("google_calendar_sync_failed", booking_id=str(booking_id), error=str(exc))
        return None
