"""Process inbound WhatsApp webhook payloads from Meta Cloud API.

Meta webhook payload structure:
{
  "object": "whatsapp_business_account",
  "entry": [{
    "id": "<WABA_ID>",
    "changes": [{
      "value": {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "...", "phone_number_id": "..."},
        "contacts": [{"profile": {"name": "..."}, "wa_id": "..."}],
        "messages": [{
          "from": "919876543210",
          "id": "wamid.xxx",
          "timestamp": "...",
          "type": "text",
          "text": {"body": "hello"}
        }]
      },
      "field": "messages"
    }]
  }]
}
"""
import uuid
from datetime import datetime, timezone

import structlog
from sqlalchemy import text

from app.database import async_session_factory
from app.messaging.whatsapp.client import send_text_message

logger = structlog.get_logger()


async def process_webhook_payload(payload: dict) -> None:
    """Extract messages from Meta webhook payload and process each one."""
    if payload.get("object") != "whatsapp_business_account":
        logger.debug("ignoring_non_whatsapp_payload", object=payload.get("object"))
        return

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            field = change.get("field")

            if field == "account_update":
                # Tech Provider Embedded Signup & WABA lifecycle events
                logger.info(
                    "whatsapp_account_update",
                    waba_id=entry.get("id"),
                    event_name=value.get("event"),
                    phone_number=value.get("phone_number"),
                )
                continue

            if field == "smb_message_echoes":
                # Coexistence mode: owner replied directly from their native WhatsApp Business mobile app.
                # Log echo cleanly as outbound without triggering bot auto-replies.
                await _handle_smb_message_echoes(value)
                continue

            if field in ("history", "smb_app_state_sync"):
                # Coexistence chat/contact sync events — acknowledge cleanly
                logger.info("whatsapp_coexistence_sync", field=field, waba_id=entry.get("id"))
                continue

            if field != "messages":
                logger.debug("webhook_field_ignored", field=field)
                continue

            phone_number_id = value.get("metadata", {}).get("phone_number_id", "")
            messages = value.get("messages", [])
            contacts = value.get("contacts", [])

            for msg in messages:
                await _handle_inbound_message(
                    phone_number_id=phone_number_id,
                    message=msg,
                    contacts=contacts,
                )

            # Handle status updates
            statuses = value.get("statuses", [])
            for status in statuses:
                await _handle_status_update(status)


async def _handle_inbound_message(
    phone_number_id: str,
    message: dict,
    contacts: list[dict],
) -> None:
    """Process a single inbound message."""
    sender_phone = message.get("from", "")  # e.g. "919876543210" (no +)
    msg_id = message.get("id", "")
    msg_type = message.get("type", "")

    # Normalize phone: Meta sends without +, we store with +
    if sender_phone and not sender_phone.startswith("+"):
        sender_phone = f"+{sender_phone}"

    # Extract text body
    if msg_type == "text":
        body = message.get("text", {}).get("body", "")
    elif msg_type == "interactive":
        # Button reply or list reply — use ID for routing (conversation flow uses IDs)
        interactive = message.get("interactive", {})
        if interactive.get("type") == "button_reply":
            body = interactive.get("button_reply", {}).get("id", "") or interactive.get("button_reply", {}).get("title", "")
        elif interactive.get("type") == "list_reply":
            body = interactive.get("list_reply", {}).get("id", "") or interactive.get("list_reply", {}).get("title", "")
        else:
            body = ""
    else:
        body = f"[{msg_type} message]"

    # Get sender name from contacts
    sender_name = ""
    if contacts:
        sender_name = contacts[0].get("profile", {}).get("name", "")

    logger.info(
        "inbound_message",
        phone_number_id=phone_number_id,
        sender=sender_phone,
        sender_name=sender_name,
        msg_type=msg_type,
        body=body[:100],
        wamid=msg_id,
    )

    async with async_session_factory() as db:
        # Find tenant by their WABA phone number ID
        result = await db.execute(
            text(
                "SELECT id, business_name, business_type, owner_phone, primary_language, timezone "
                "FROM tenants WHERE waba_phone_number_id = :pid AND is_active = true LIMIT 1"
            ),
            {"pid": phone_number_id},
        )
        tenant = result.first()

        if not tenant:
            logger.warning("no_tenant_for_phone_number_id", phone_number_id=phone_number_id)
            return

        tz_name = getattr(tenant, "timezone", None) or "Asia/Kolkata"
        btype = getattr(tenant, "business_type", None) or "salon"
        is_owner = sender_phone == tenant.owner_phone

        if is_owner:
            from app.owner_console.handler import handle_owner_message
            reply = await handle_owner_message(
                db, str(tenant.id), body, tenant.business_name, tz_name=tz_name, business_type=btype
            )
        else:
            reply = await _handle_customer_message(
                db=db,
                tenant=tenant,
                sender_phone=sender_phone,
                sender_name=sender_name,
                body=body,
            )

        # Log inbound message (including customer_id so free 24h service window is accurately tracked)
        inbound_cust_id = None
        if not is_owner:
            cid_row = (
                await db.execute(
                    text("SELECT id FROM customers WHERE tenant_id = :tid AND phone = :phone LIMIT 1"),
                    {"tid": tenant.id, "phone": sender_phone},
                )
            ).first()
            if cid_row:
                inbound_cust_id = cid_row.id

        await db.execute(
            text("""INSERT INTO messages (id, tenant_id, customer_id, direction, body, status, waba_message_id, created_at)
                    VALUES (:id, :tid, :cid, 'inbound', :body, 'received', :wamid, :now)"""),
            {
                "id": uuid.uuid4(),
                "tid": tenant.id,
                "cid": inbound_cust_id,
                "body": body,
                "wamid": msg_id,
                "now": datetime.now(timezone.utc),
            },
        )
        await db.commit()

        # Send reply via WhatsApp
        if reply:
            to_number = sender_phone.lstrip("+")
            tid = str(tenant.id)

            if hasattr(reply, 'type'):
                # Structured response from conversation flow
                from app.messaging.whatsapp.client import (
                    ListRow,
                    ListSection,
                    ReplyButton,
                    send_list_message,
                    send_reply_buttons,
                )
                if reply.type == "buttons":
                    await send_reply_buttons(
                        phone_number_id=phone_number_id, to=to_number,
                        body=reply.body,
                        buttons=[ReplyButton(id=b["id"], title=b["title"]) for b in reply.buttons],
                        header=getattr(reply, "header", ""),
                        footer=getattr(reply, "footer", ""),
                        tenant_id=tid,
                    )
                elif reply.type == "list":
                    sections = [
                        ListSection(
                            title=s["title"],
                            rows=[ListRow(id=r["id"], title=r["title"], description=r.get("description", "")) for r in s["rows"]],
                        )
                        for s in reply.sections
                    ]
                    await send_list_message(
                        phone_number_id=phone_number_id, to=to_number,
                        body=reply.body, button_text=reply.button_text,
                        sections=sections,
                        header=getattr(reply, "header", ""),
                        footer=getattr(reply, "footer", ""),
                        tenant_id=tid,
                    )
                else:
                    await send_text_message(
                        phone_number_id=phone_number_id, to=to_number,
                        text=reply.text, tenant_id=tid,
                    )
            elif isinstance(reply, str):
                await send_text_message(
                    phone_number_id=phone_number_id, to=to_number,
                    text=reply, tenant_id=tid,
                )


async def _handle_customer_message(db, tenant, sender_phone: str, sender_name: str, body: str) -> str | object:
    """Handle message from a customer using the conversation flow engine."""
    from app.conversation.menu_flow import handle_customer_message
    from app.feedback.handler import handle_feedback_response

    # Find or create customer
    cust_result = await db.execute(
        text("SELECT id, name, preferred_language, total_bookings FROM customers WHERE tenant_id = :tid AND phone = :phone"),
        {"tid": tenant.id, "phone": sender_phone},
    )
    customer = cust_result.first()

    if not customer:
        cust_id = uuid.uuid4()
        await db.execute(
            text("INSERT INTO customers (id, tenant_id, phone, name, first_seen_at) VALUES (:id, :tid, :phone, :name, :now)"),
            {"id": cust_id, "tid": tenant.id, "phone": sender_phone, "name": sender_name, "now": datetime.now(timezone.utc)},
        )
        await db.commit()
        cust_lang = getattr(tenant, "primary_language", None) or "en"
    else:
        cust_id = customer.id
        cust_lang = customer.preferred_language or getattr(tenant, "primary_language", None) or "en"

    # Check if this is a feedback response first (rating buttons or digit)
    feedback_reply = await handle_feedback_response(
        db=db, tenant_id=str(tenant.id),
        customer_phone=sender_phone, body=body,
    )
    if feedback_reply:
        return feedback_reply

    return await handle_customer_message(
        db=db,
        tenant_id=str(tenant.id),
        customer_id=str(cust_id),
        customer_phone=sender_phone,
        customer_name=sender_name,
        body=body,
        business_name=tenant.business_name,
        tz_name=getattr(tenant, "timezone", None) or "Asia/Kolkata",
        business_type=getattr(tenant, "business_type", None) or "salon",
        lang=cust_lang,
    )


async def _handle_smb_message_echoes(value: dict) -> None:
    """Log outbound messages sent directly by the business owner via the native WhatsApp Business app (Coexistence)."""
    phone_number_id = value.get("metadata", {}).get("phone_number_id", "")
    echoes = value.get("message_echoes") or value.get("messages") or []
    if not phone_number_id or not echoes:
        return

    try:
        async with async_session_factory() as db:
            t_row = (
                await db.execute(
                    text("SELECT id FROM tenants WHERE waba_phone_number_id = :pid AND is_active = true LIMIT 1"),
                    {"pid": phone_number_id},
                )
            ).first()
            if not t_row:
                return
            for echo in echoes:
                body = echo.get("text", {}).get("body", "") if echo.get("type") == "text" else f"[{echo.get('type', 'media')}]"
                wamid = echo.get("id", "")
                await db.execute(
                    text(
                        "INSERT INTO messages (id, tenant_id, direction, body, status, waba_message_id, created_at) "
                        "VALUES (:id, :tid, 'outbound', :body, 'sent', :wamid, :now)"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "tid": t_row.id,
                        "body": f"[App] {body}"[:500],
                        "wamid": wamid,
                        "now": datetime.now(timezone.utc),
                    },
                )
            await db.commit()
    except Exception as exc:
        logger.warning("whatsapp_smb_echo_log_failed", error=str(exc))


async def _handle_status_update(status: dict) -> None:
    """Handle message status update (sent, delivered, read, failed)."""
    wamid = status.get("id", "")
    new_status = status.get("status", "")  # sent, delivered, read, failed
    recipient = status.get("recipient_id", "")

    logger.info("message_status_update", wamid=wamid, status=new_status, recipient=recipient)

    # Update message status in DB
    async with async_session_factory() as db:
        await db.execute(
            text("UPDATE messages SET status = :status WHERE waba_message_id = :wamid"),
            {"status": new_status, "wamid": wamid},
        )
        await db.commit()
