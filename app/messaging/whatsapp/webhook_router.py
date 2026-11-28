"""WhatsApp Cloud API webhook endpoints.

GET  /webhooks/whatsapp — Meta verification handshake
POST /webhooks/whatsapp — Inbound messages + status updates
"""
import hmac
import json

import structlog
from fastapi import APIRouter, Query, Request, Response

from app.config import settings
from app.messaging.whatsapp.security import get_validated_body
from app.messaging.whatsapp.webhook import process_webhook_payload

logger = structlog.get_logger()

router = APIRouter(tags=["whatsapp-webhook"])


@router.get("/webhooks/whatsapp")
async def verify_webhook(
    hub_mode: str = Query(alias="hub.mode", default=""),
    hub_verify_token: str = Query(alias="hub.verify_token", default=""),
    hub_challenge: str = Query(alias="hub.challenge", default=""),
):
    """Meta webhook verification handshake."""
    if hub_mode == "subscribe" and hub_verify_token and hmac.compare_digest(
        hub_verify_token, settings.meta_webhook_verify_token
    ):
        logger.info("webhook_verified")
        return Response(content=hub_challenge, media_type="text/plain")

    logger.warning("webhook_verification_failed", mode=hub_mode)
    return Response(content="Forbidden", status_code=403)


@router.post("/webhooks/whatsapp")
async def receive_webhook(request: Request):
    """Receive inbound WhatsApp messages and status updates.
    Validates signature in production. Offloads to Celery when webhook_async=True.
    """
    body_bytes, is_valid = await get_validated_body(request)

    if not is_valid and settings.environment != "development":
        logger.warning("webhook_signature_invalid")
        return Response(content="Invalid signature", status_code=403)

    try:
        payload = json.loads(body_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.warning("webhook_invalid_json")
        return Response(content="Invalid JSON", status_code=400)

    logger.info("webhook_received", payload_keys=list(payload.keys()))

    if settings.webhook_async:
        try:
            from app.messaging.tasks import process_webhook_task

            process_webhook_task.delay(payload)
            return Response(content="OK", status_code=200)
        except Exception as exc:
            logger.warning("webhook_celery_dispatch_failed_falling_back_sync", error=str(exc))

    await process_webhook_payload(payload)

    return Response(content="OK", status_code=200)
