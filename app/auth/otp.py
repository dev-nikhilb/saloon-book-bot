"""Free Email (Resend / Gmail SMTP) + WhatsApp OTP authentication service.

Storage strategy (zero external Redis required on Vercel):
  1. Uses Redis if configured and reachable.
  2. Falls back automatically to PostgreSQL `auth_otps` table (works across all
     Vercel Serverless instances on free Neon Postgres).

Delivery strategy (100% free options):
  1. Email via Resend HTTP API (`RESEND_API_KEY` — 3,000 emails/mo free)
  2. Email via SMTP (`SMTP_HOST`, `SMTP_USER`, `SMTP_PASS` — e.g. Gmail App Password, 500/day free)
  3. WhatsApp via Meta Cloud API (when tenant has WABA connected)
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import smtplib
import uuid
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import httpx
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.redis_client import get_redis

logger = structlog.get_logger()

OTP_TTL_SECONDS = 300  # 5 minutes
OTP_MAX_ATTEMPTS = 5


def _otp_key(identifier: str, purpose: str = "login") -> str:
    return f"auth:otp:{purpose}:{identifier.lower().strip()}"


def _attempts_key(identifier: str, purpose: str = "login") -> str:
    return f"auth:otp_attempts:{purpose}:{identifier.lower().strip()}"


def _hash_code(identifier: str, code: str) -> str:
    secret = (settings.jwt_secret or settings.encryption_key or "bookbot-otp-salt").encode()
    msg = f"{identifier.lower().strip()}:{code}".encode()
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()


def generate_otp() -> str:
    """Generate a cryptographically secure 6-digit numeric OTP."""
    return f"{secrets.randbelow(1_000_000):06d}"


def is_email_otp_configured() -> bool:
    """Return True if either Resend API or SMTP credentials are configured."""
    return bool(settings.resend_api_key or (settings.smtp_host and settings.smtp_user and settings.smtp_pass))


async def _send_email_otp(
    to_email: str, code: str, business_name: str = "Tivoria", purpose: str = "login"
) -> tuple[str | None, str | None]:
    """Send 6-digit OTP via Resend API or SMTP. Returns (channel_or_none, error_message_or_none)."""
    subject = f"{code} is your Tivoria {'verification' if purpose == 'signup' else 'login'} code"
    html_body = (
        f"<div style='font-family:-apple-system,BlinkMacSystemFont,sans-serif;max-width:460px;margin:0 auto;padding:24px'>"
        f"<h2 style='margin:0 0 8px;color:#1D1D1F'>Tivoria Verification Code</h2>"
        f"<p style='color:#6E6E73;margin:0 0 20px'>Use this 6-digit code to "
        f"{'verify your new workspace' if purpose == 'signup' else f'sign in to {business_name}'}:</p>"
        f"<div style='font-size:32px;font-weight:700;letter-spacing:6px;padding:16px 24px;"
        f"background:#F5F5F7;border-radius:14px;text-align:center;color:#007AFF'>{code}</div>"
        f"<p style='color:#86868B;font-size:13px;margin:20px 0 0'>Expires in 5 minutes. If you didn&rsquo;t request this, ignore this email.</p>"
        f"</div>"
    )
    plain_body = f"Your Tivoria code is {code}. Valid for 5 minutes."
    last_error: str | None = None

    # 1. Resend HTTP API (3,000 emails/mo free)
    if settings.resend_api_key:
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                r = await client.post(
                    "https://api.resend.com/emails",
                    headers={
                        "Authorization": f"Bearer {settings.resend_api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "from": settings.email_from,
                        "to": [to_email],
                        "subject": subject,
                        "html": html_body,
                        "text": plain_body,
                    },
                )
                if 200 <= r.status_code < 300:
                    return "email_resend", None
                try:
                    err_msg = r.json().get("message") or r.text[:200]
                except Exception:
                    err_msg = r.text[:200]
                last_error = f"Email provider error ({r.status_code}): {err_msg}"
                logger.warning("resend_otp_failed", status=r.status_code, body=r.text[:200])
        except Exception as exc:
            last_error = f"Email provider connection error: {exc}"
            logger.warning("resend_otp_error", error=str(exc))

    # 2. SMTP (Gmail App Password, Brevo, Zoho, etc. — free)
    if settings.smtp_host and settings.smtp_user and settings.smtp_pass:
        def _send_smtp():
            msg = EmailMessage()
            msg["Subject"] = subject
            msg["From"] = settings.smtp_user
            msg["To"] = to_email
            msg.set_content(plain_body)
            msg.add_alternative(html_body, subtype="html")
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as srv:
                srv.starttls()
                srv.login(settings.smtp_user, settings.smtp_pass)
                srv.send_message(msg)

        try:
            await asyncio.to_thread(_send_smtp)
            return "email_smtp", None
        except Exception as exc:
            last_error = f"SMTP email error: {exc}"
            logger.warning("smtp_otp_error", error=str(exc))

    return None, last_error


async def _store_otp(db: AsyncSession | None, identifier: str, code: str, purpose: str = "login") -> None:
    """Persist OTP in Redis (fast) AND/OR Postgres `auth_otps` (serverless-safe fallback)."""
    ident = identifier.lower().strip()
    stored_redis = False
    try:
        r = await get_redis()
        await r.set(_otp_key(ident, purpose), code, ex=OTP_TTL_SECONDS)
        await r.delete(_attempts_key(ident, purpose))
        # Backward compat key for legacy login tests reading auth:otp:{phone}
        if purpose == "login":
            await r.set(f"auth:otp:{ident}", code, ex=OTP_TTL_SECONDS)
            await r.delete(f"auth:otp_attempts:{ident}")
        stored_redis = True
    except Exception as exc:
        logger.debug("otp_redis_fallback_to_pg", error=str(exc))

    if db is not None:
        try:
            now = datetime.now(timezone.utc)
            exp = now + timedelta(seconds=OTP_TTL_SECONDS)
            await db.execute(
                text("DELETE FROM auth_otps WHERE identifier = :ident AND purpose = :purpose"),
                {"ident": ident, "purpose": purpose},
            )
            await db.execute(
                text("""
                    INSERT INTO auth_otps (id, identifier, purpose, code_hash, attempts, expires_at, created_at)
                    VALUES (:id, :ident, :purpose, :ch, 0, :exp, :now)
                """),
                {
                    "id": uuid.uuid4(),
                    "ident": ident,
                    "purpose": purpose,
                    "ch": _hash_code(ident, code),
                    "exp": exp,
                    "now": now,
                },
            )
            await db.commit()
        except Exception as exc:
            if not stored_redis:
                logger.warning("otp_pg_store_failed", error=str(exc))


async def request_signup_otp(db: AsyncSession, email: str, business_name: str = "Tivoria") -> dict:
    """Generate and email a 6-digit verification OTP before workspace signup."""
    ident = email.lower().strip()
    code = generate_otp()
    await _store_otp(db, ident, code, purpose="signup")

    delivered_via, delivery_error = await _send_email_otp(
        ident, code, business_name=business_name, purpose="signup"
    )
    if not delivered_via:
        delivered_via = "log"

    logger.info("auth.signup_otp_requested", email=ident, delivered_via=delivered_via)

    resp: dict = {
        "sent": delivered_via != "log" or settings.environment == "development",
        "otp_required": is_email_otp_configured(),
        "expires_in": OTP_TTL_SECONDS,
        "delivered_via": delivered_via,
        "delivery_error": delivery_error,
    }
    if settings.environment == "development":
        resp["dev_otp"] = code
    return resp


async def request_otp(db: AsyncSession, identifier: str) -> dict:
    """Generate and deliver a 6-digit login OTP for an existing owner account (phone OR email)."""
    ident = identifier.strip()
    is_email = "@" in ident

    if is_email:
        result = await db.execute(
            text(
                "SELECT id, business_name, owner_phone, owner_email, waba_phone_number_id, is_active "
                "FROM tenants WHERE LOWER(owner_email) = LOWER(:ident) LIMIT 1"
            ),
            {"ident": ident},
        )
    else:
        result = await db.execute(
            text(
                "SELECT id, business_name, owner_phone, owner_email, waba_phone_number_id, is_active "
                "FROM tenants WHERE owner_phone = :ident LIMIT 1"
            ),
            {"ident": ident},
        )

    tenant = result.first()
    if not tenant:
        return {"found": False, "active": False}
    if not tenant.is_active:
        return {"found": True, "active": False}

    code = generate_otp()
    # Store under the requested identifier AND canonical owner_phone/owner_email
    await _store_otp(db, ident, code, purpose="login")
    if tenant.owner_phone and tenant.owner_phone != ident:
        await _store_otp(db, tenant.owner_phone, code, purpose="login")
    if tenant.owner_email and tenant.owner_email.lower() != ident.lower():
        await _store_otp(db, tenant.owner_email, code, purpose="login")

    delivered_via = "log"

    # Prefer Email delivery when email is available and Resend/SMTP is configured (100% free!)
    target_email = tenant.owner_email or (ident if is_email else None)
    delivery_error: str | None = None
    if target_email and is_email_otp_configured():
        email_chan, delivery_error = await _send_email_otp(
            target_email, code, business_name=tenant.business_name, purpose="login"
        )
        if email_chan:
            delivered_via = email_chan

    # Also deliver via WhatsApp if tenant has WABA connected
    if tenant.waba_phone_number_id and tenant.owner_phone:
        try:
            from app.messaging.whatsapp.client import send_text_message

            sent = await send_text_message(
                phone_number_id=tenant.waba_phone_number_id,
                to=tenant.owner_phone.lstrip("+"),
                text=(
                    f"🔐 Your Tivoria login code for *{tenant.business_name}* is *{code}*.\n"
                    f"Valid for 5 minutes. Do not share this code."
                ),
                tenant_id=str(tenant.id),
            )
            if sent and delivered_via == "log":
                delivered_via = "whatsapp"
        except Exception as exc:
            logger.warning("otp_whatsapp_send_failed", error=str(exc))

    logger.info("auth.otp_requested", identifier=ident, delivered_via=delivered_via)

    resp: dict = {
        "found": True,
        "active": True,
        "expires_in": OTP_TTL_SECONDS,
        "delivered_via": delivered_via,
        "email_configured": is_email_otp_configured(),
        "delivery_error": delivery_error,
    }
    if settings.environment == "development":
        resp["dev_otp"] = code
    return resp


async def verify_otp(
    identifier: str,
    otp: str,
    db: AsyncSession | None = None,
    purpose: str = "login",
) -> tuple[bool, str]:
    """Verify a 6-digit OTP via Redis first, with automatic PostgreSQL fallback."""
    if not otp.isdigit() or len(otp) != 6:
        return False, "OTP must be exactly 6 digits"

    ident = identifier.lower().strip()

    # 1. Try Redis first
    redis_checked = False
    try:
        r = await get_redis()
        attempts = int(await r.get(_attempts_key(ident, purpose)) or 0)
        if attempts >= OTP_MAX_ATTEMPTS:
            return False, "Too many failed OTP attempts. Please request a new code."

        stored = await r.get(_otp_key(ident, purpose))
        if stored is None and purpose == "login":
            stored = await r.get(f"auth:otp:{ident}")
        redis_checked = True

        if stored is not None:
            if hmac.compare_digest(stored, otp):
                try:
                    await r.delete(_otp_key(ident, purpose), _attempts_key(ident, purpose))
                    if purpose == "login":
                        await r.delete(f"auth:otp:{ident}", f"auth:otp_attempts:{ident}")
                except Exception:
                    pass
                return True, ""
            new_attempts = await r.incr(_attempts_key(ident, purpose))
            if new_attempts == 1:
                await r.expire(_attempts_key(ident, purpose), OTP_TTL_SECONDS)
            return False, "Invalid OTP code"
    except Exception as exc:
        logger.debug("otp_verify_redis_unavailable", error=str(exc))

    # 2. Fallback to Postgres `auth_otps` (works on Vercel Serverless without Redis!)
    if db is not None:
        try:
            row = (
                await db.execute(
                    text("""
                        SELECT id, code_hash, attempts, expires_at
                        FROM auth_otps
                        WHERE identifier = :ident AND purpose = :purpose
                        ORDER BY created_at DESC
                        LIMIT 1
                    """),
                    {"ident": ident, "purpose": purpose},
                )
            ).first()
            if row is not None:
                now = datetime.now(timezone.utc)
                if row.attempts >= OTP_MAX_ATTEMPTS:
                    return False, "Too many failed OTP attempts. Please request a new code."
                if row.expires_at <= now:
                    return False, "OTP expired. Please request a new code."
                if hmac.compare_digest(row.code_hash, _hash_code(ident, otp)):
                    await db.execute(text("DELETE FROM auth_otps WHERE id = :id"), {"id": row.id})
                    await db.commit()
                    return True, ""
                await db.execute(
                    text("UPDATE auth_otps SET attempts = attempts + 1 WHERE id = :id"),
                    {"id": row.id},
                )
                await db.commit()
                return False, "Invalid OTP code"
        except Exception as exc:
            logger.warning("otp_pg_verify_failed", error=str(exc))

    if settings.environment == "development":
        return True, ""
    if not redis_checked and db is None:
        return False, "OTP verification service temporarily unavailable"
    return False, "OTP expired or not requested. Please request a new code."
