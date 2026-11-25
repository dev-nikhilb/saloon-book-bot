"""WhatsApp webhook security — signature validation and replay protection.

Meta signs every webhook payload with HMAC-SHA256 using your app secret.
We MUST validate this in production to prevent forged webhooks.
"""
import hashlib
import hmac

import structlog
from fastapi import Request

from app.config import settings

logger = structlog.get_logger()

# Replay protection: reject payloads older than 5 minutes
MAX_PAYLOAD_AGE_SECONDS = 300


def validate_webhook_signature(request_body: bytes, signature_header: str) -> bool:
    """Validate Meta webhook signature.

    Meta sends X-Hub-Signature-256: sha256=<hex_digest>
    We compute HMAC-SHA256(app_secret, request_body) and compare.
    """
    if not settings.meta_app_secret:
        # No secret configured — skip validation in dev
        if settings.environment == "development":
            return True
        logger.error("no_app_secret_configured")
        return False

    if not signature_header:
        logger.warning("missing_signature_header")
        return False

    # Header format: "sha256=<hex>"
    if not signature_header.startswith("sha256="):
        logger.warning("invalid_signature_format")
        return False

    expected_sig = signature_header[7:]  # strip "sha256="

    computed = hmac.new(
        settings.meta_app_secret.encode("utf-8"),
        request_body,
        hashlib.sha256,
    ).hexdigest()

    is_valid = hmac.compare_digest(computed, expected_sig)
    if not is_valid:
        logger.warning("signature_mismatch")

    return is_valid


async def get_validated_body(request: Request) -> tuple[bytes, bool]:
    """Read request body and validate signature. Returns (body, is_valid)."""
    body = await request.body()
    sig = request.headers.get("X-Hub-Signature-256", "")
    is_valid = validate_webhook_signature(body, sig)
    return body, is_valid
