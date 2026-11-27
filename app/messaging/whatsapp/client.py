"""WhatsApp Cloud API outbound client.

Supports: text, reply buttons (up to 3), list messages (up to 10 items), CTA URLs.
POST https://graph.facebook.com/v21.0/{phone_number_id}/messages
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
import structlog
from sqlalchemy import text

from app.config import settings
from app.database import async_session_factory

logger = structlog.get_logger()

GRAPH_API_VERSION = "v25.0"
GRAPH_API_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"


@dataclass
class ReplyButton:
    id: str
    title: str  # max 20 chars


@dataclass
class ListRow:
    id: str
    title: str  # max 24 chars
    description: str = ""  # max 72 chars


@dataclass
class ListSection:
    title: str
    rows: list[ListRow]


async def send_text_message(
    phone_number_id: str,
    to: str,
    text_body: str = "",
    text: str = "",
    tenant_id: str = "",
) -> dict | None:
    """Send a plain text message."""
    body = text_body or text
    if not body:
        return None

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"preview_url": False, "body": body},
    }
    return await _send(phone_number_id, to, payload, tenant_id, body[:500])


async def send_reply_buttons(
    phone_number_id: str,
    to: str,
    body: str,
    buttons: list[ReplyButton],
    header: str = "",
    footer: str = "",
    tenant_id: str = "",
) -> dict | None:
    """Send a message with up to 3 tappable reply buttons.

    Buttons appear as chips below the message. Customer taps one.
    Max 3 buttons. Title max 20 chars each.
    """
    if len(buttons) > 3:
        buttons = buttons[:3]

    interactive = {
        "type": "button",
        "body": {"text": body},
        "action": {
            "buttons": [
                {"type": "reply", "reply": {"id": b.id, "title": b.title[:20]}}
                for b in buttons
            ]
        },
    }
    if header:
        interactive["header"] = {"type": "text", "text": header}
    if footer:
        interactive["footer"] = {"text": footer}

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "interactive",
        "interactive": interactive,
    }
    return await _send(phone_number_id, to, payload, tenant_id, body[:500])


async def send_list_message(
    phone_number_id: str,
    to: str,
    body: str,
    button_text: str,
    sections: list[ListSection],
    header: str = "",
    footer: str = "",
    tenant_id: str = "",
) -> dict | None:
    """Send an expandable list message with up to 10 items.

    Customer taps the button to expand, then picks from the list.
    Max 10 rows total across all sections.
    """
    interactive = {
        "type": "list",
        "body": {"text": body},
        "action": {
            "button": button_text[:20],
            "sections": [
                {
                    "title": s.title[:24],
                    "rows": [
                        {
                            "id": r.id[:200],
                            "title": r.title[:24],
                            **({"description": r.description[:72]} if r.description else {}),
                        }
                        for r in s.rows[:10]
                    ],
                }
                for s in sections
            ],
        },
    }
    if header:
        interactive["header"] = {"type": "text", "text": header}
    if footer:
        interactive["footer"] = {"text": footer}

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "interactive",
        "interactive": interactive,
    }
    return await _send(phone_number_id, to, payload, tenant_id, body[:500])


async def send_template_message(
    phone_number_id: str,
    to: str,
    template_name: str,
    language_code: str = "en",
    components: list | None = None,
    tenant_id: str = "",
) -> dict | None:
    """Send a pre-approved template message."""
    template_obj = {
        "name": template_name,
        "language": {"code": language_code},
    }
    if components:
        template_obj["components"] = components

    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": template_obj,
    }
    return await _send(phone_number_id, to, payload, tenant_id, f"template:{template_name}")


async def _send(
    phone_number_id: str,
    to: str,
    payload: dict,
    tenant_id: str,
    log_body: str,
) -> dict | None:
    """Send any payload to Meta API and log to messages table."""
    access_token = await _get_access_token(phone_number_id)
    if not access_token:
        logger.error("no_access_token", phone_number_id=phone_number_id)
        return None

    url = f"{GRAPH_API_BASE}/{phone_number_id}/messages"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            response.raise_for_status()
            result = response.json()

        wamid = result.get("messages", [{}])[0].get("id", "")
        logger.info("message_sent", to=to, wamid=wamid, type=payload.get("type"))

        if tenant_id:
            async with async_session_factory() as db:
                await db.execute(
                    text("""INSERT INTO messages (id, tenant_id, direction, body, status, waba_message_id, created_at)
                            VALUES (:id, :tid, 'outbound', :body, 'sent', :wamid, :now)"""),
                    {"id": uuid.uuid4(), "tid": tenant_id, "body": log_body, "wamid": wamid, "now": datetime.now(timezone.utc)},
                )
                await db.commit()

        return result

    except httpx.HTTPStatusError as e:
        logger.error("whatsapp_api_error", status=e.response.status_code, body=e.response.text[:200])
        return None
    except httpx.RequestError as e:
        logger.error("whatsapp_request_error", error=str(e))
        return None


async def _get_access_token(phone_number_id: str) -> str | None:
    """Get WABA access token. Checks DB first (decrypting Fernet ciphertext), falls back to env."""
    from app.crypto import decrypt_secret

    async with async_session_factory() as db:
        result = await db.execute(
            text("SELECT waba_access_token_encrypted FROM tenants WHERE waba_phone_number_id = :pid LIMIT 1"),
            {"pid": phone_number_id},
        )
        row = result.first()
        if row and row.waba_access_token_encrypted:
            return decrypt_secret(row.waba_access_token_encrypted)

    if settings.meta_access_token:
        return settings.meta_access_token

    return None
