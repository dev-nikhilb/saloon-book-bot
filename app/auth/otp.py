"""WhatsApp/Redis OTP authentication service for business owners."""
from __future__ import annotations

import hmac
import secrets

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.redis_client import get_redis

logger = structlog.get_logger()

OTP_TTL_SECONDS = 300  # 5 minutes
OTP_MAX_ATTEMPTS = 5


def _otp_key(phone: str) -> str:
    return f"auth:otp:{phone}"


def _attempts_key(phone: str) -> str:
    return f"auth:otp_attempts:{phone}"


def generate_otp() -> str:
    """Generate a cryptographically secure 6-digit numeric OTP."""
    return f"{secrets.randbelow(1_000_000):06d}"


async def request_otp(db: AsyncSession, phone: str) -> dict:
    """Generate and store a 6-digit OTP for an existing active tenant owner.

    Sends the OTP over WhatsApp if the tenant has WABA configured.
    In development, returns `dev_otp` in the response for convenient testing.
    """
    result = await db.execute(
        text(
            "SELECT id, business_name, owner_phone, waba_phone_number_id, is_active "
            "FROM tenants WHERE owner_phone = :phone LIMIT 1"
        ),
        {"phone": phone},
    )
    tenant = result.first()
    if not tenant:
        return {"found": False, "active": False}
    if not tenant.is_active:
        return {"found": True, "active": False}

    code = generate_otp()
    try:
        r = await get_redis()
        await r.set(_otp_key(phone), code, ex=OTP_TTL_SECONDS)
        await r.delete(_attempts_key(phone))
    except Exception as exc:
        logger.warning("otp_redis_unavailable", error=str(exc))

    # Deliver via WhatsApp if tenant has WABA configured
    delivered_via = "log"
    if tenant.waba_phone_number_id:
        try:
            from app.messaging.whatsapp.client import send_text_message

            sent = await send_text_message(
                phone_number_id=tenant.waba_phone_number_id,
                to=phone.lstrip("+"),
                text=(
                    f"🔐 Your BookBot login code for *{tenant.business_name}* is *{code}*.\n"
                    f"Valid for 5 minutes. Do not share this code."
                ),
                tenant_id=str(tenant.id),
            )
            if sent:
                delivered_via = "whatsapp"
        except Exception as exc:
            logger.warning("otp_whatsapp_send_failed", error=str(exc))

    logger.info("auth.otp_requested", phone=phone, delivered_via=delivered_via)

    resp: dict = {
        "found": True,
        "active": True,
        "expires_in": OTP_TTL_SECONDS,
        "delivered_via": delivered_via,
    }
    if settings.environment == "development":
        resp["dev_otp"] = code
    return resp


async def verify_otp(phone: str, otp: str) -> tuple[bool, str]:
    """Verify a 6-digit OTP for a phone number.

    Returns (is_valid, error_message).
    - In production: strictly checks against the stored Redis OTP with attempt throttling.
    - In development: verifies against stored Redis OTP if one was requested; if no OTP
      was requested, allows any valid 6-digit code for fast local dev & tests.
    """
    if not otp.isdigit() or len(otp) != 6:
        return False, "OTP must be exactly 6 digits"

    try:
        r = await get_redis()
        attempts = int(await r.get(_attempts_key(phone)) or 0)
        if attempts >= OTP_MAX_ATTEMPTS:
            return False, "Too many failed OTP attempts. Please request a new code."

        stored = await r.get(_otp_key(phone))
    except Exception as exc:
        logger.warning("otp_verify_redis_unavailable", error=str(exc))
        if settings.environment == "development":
            return True, ""
        return False, "OTP verification service temporarily unavailable"

    if stored is None:
        if settings.environment == "development":
            # Allow direct 6-digit dev login when OTP wasn't explicitly requested
            return True, ""
        return False, "OTP expired or not requested. Please request a new code."

    if hmac.compare_digest(stored, otp):
        try:
            await r.delete(_otp_key(phone), _attempts_key(phone))
        except Exception:
            pass
        return True, ""

    try:
        new_attempts = await r.incr(_attempts_key(phone))
        if new_attempts == 1:
            await r.expire(_attempts_key(phone), OTP_TTL_SECONDS)
    except Exception:
        pass
    return False, "Invalid OTP code"
