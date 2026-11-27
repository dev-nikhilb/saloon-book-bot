"""Menu-driven booking conversation flow with WhatsApp interactive messages.

Uses reply buttons (up to 3) and list messages (up to 10) for clean UX.
Honors tenant_settings (advance_booking_days, min_cancel_hours, slot_increment_minutes,
max_bookings_per_customer, custom welcome/confirmation messages, Razorpay deposits),
tenant timezone, business-type vocabulary, i18n templates, and optional Gemini NLU.

Flow:
IDLE -> show main menu (services list + optional My Bookings)
-> BOOK: list services -> (optional subcategory) -> list dates -> list slots -> confirm -> done (or deposit link)
-> MY_BOOKINGS: show upcoming -> Cancel / Reschedule
-> CANCEL: enforce min_cancel_hours -> confirm cancel
-> RESCHEDULE: pick new date -> pick new slot -> confirm (creates new + cancels old)
"""
from __future__ import annotations

import json
import zoneinfo
from datetime import date, datetime, timedelta, timezone
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.bookings.models import BookingStatus
from app.bookings.repository import create_booking, update_booking_status
from app.bookings.slots import get_available_slots
from app.config import settings as app_settings
from app.conversation.state import (
    ConversationState,
    ConversationStep,
    clear_state,
    get_state,
    set_state,
)
from app.labels import get_labels
from app.messaging.i18n import t

logger = structlog.get_logger()


# --- Response objects that the webhook handler interprets ---

class TextResponse:
    """Plain text reply."""
    def __init__(self, text: str):
        self.text = text
        self.type = "text"


class ButtonResponse:
    """Reply with up to 3 tappable buttons."""
    def __init__(self, body: str, buttons: list[dict], header: str = "", footer: str = ""):
        self.body = body
        self.buttons = buttons  # [{"id": "...", "title": "..."}]
        self.header = header
        self.footer = footer
        self.type = "buttons"


class ListResponse:
    """Reply with expandable list menu."""
    def __init__(self, body: str, button_text: str, sections: list[dict], header: str = "", footer: str = ""):
        self.body = body
        self.button_text = button_text
        self.sections = sections  # [{"title": "...", "rows": [{"id": "...", "title": "...", "description": "..."}]}]
        self.header = header
        self.footer = footer
        self.type = "list"


async def _load_tenant_settings(db: AsyncSession, tenant_id: str) -> dict:
    """Fetch tenant_settings row with safe defaults."""
    defaults = {
        "advance_booking_days": 14,
        "min_cancel_hours": 2,
        "slot_increment_minutes": 15,
        "max_bookings_per_customer": 5,
        "welcome_message": None,
        "booking_confirmed_message": None,
        "google_review_url": None,
        "google_maps_url": None,
        "razorpay_enabled": False,
        "upi_id": None,
        "deposit_mode": "auto",
        "deposit_hold_minutes": 15,
    }
    try:
        res = await db.execute(
            text(
                "SELECT advance_booking_days, min_cancel_hours, slot_increment_minutes, "
                "max_bookings_per_customer, welcome_message, booking_confirmed_message, "
                "google_review_url, google_maps_url, "
                "razorpay_enabled, upi_id, deposit_mode, deposit_hold_minutes "
                "FROM tenant_settings WHERE tenant_id = :tid LIMIT 1"
            ),
            {"tid": tenant_id},
        )
        row = res.first()
        if row:
            for k in defaults:
                val = getattr(row, k, None)
                if val is not None:
                    defaults[k] = val
    except Exception:
        pass
    return defaults


async def handle_customer_message(
    db: AsyncSession,
    tenant_id: str,
    customer_id: str,
    customer_phone: str,
    customer_name: str,
    body: str,
    business_name: str,
    tz_name: str = "Asia/Kolkata",
    business_type: str = "salon",
    lang: str = "en",
) -> TextResponse | ButtonResponse | ListResponse:
    """Process a customer message and return a structured reply."""
    body = body.strip()
    lower = body.lower()

    # Global commands
    if lower in ("cancel", "exit", "quit", "back", "0"):
        await clear_state(tenant_id, customer_phone)
        return TextResponse(t("booking_cancelled", lang=lang) if lang != "en" else "Cancelled. Send any message to start over.")

    if lower in ("human", "agent", "help me", "talk to human"):
        await clear_state(tenant_id, customer_phone)
        return TextResponse(t("human_handoff", lang=lang) if lang != "en" else "🙋 I've notified the business owner. They'll get back to you shortly.")

    if lower == "action_done":
        await clear_state(tenant_id, customer_phone)
        return TextResponse(
            f"You're welcome! 🙏 Looking forward to seeing you at *{business_name}*. "
            "Send *Hi* anytime if you need anything else!"
        )

    state = await get_state(tenant_id, customer_phone)
    tsettings = await _load_tenant_settings(db, tenant_id)

    if state.step == ConversationStep.IDLE and lower in (
        "thanks", "thank you", "thankyou", "thx", "ty", "ok thanks",
        "done", "all set", "nothing", "nothing more", "that's all", "thats all", "bye", "goodbye",
    ):
        return TextResponse(
            f"You're welcome! 🙏 Looking forward to seeing you at *{business_name}*. "
            "Send *Hi* anytime if you need anything else!"
        )

    # Global action button intercepts (work even after state was cleared)
    if lower.startswith("cancel_"):
        state.tenant_id = tenant_id
        state.customer_id = customer_id
        state.step = ConversationStep.AWAITING_CANCEL_CONFIRM
        return await _handle_cancel_confirm(db, state, tenant_id, customer_phone, body, tz_name, tsettings)

    if lower.startswith("reschedule_"):
        state.tenant_id = tenant_id
        state.customer_id = customer_id
        return await _start_reschedule(db, state, tenant_id, customer_phone, lower.replace("reschedule_", ""), tz_name, tsettings)

    if lower == "action_mybookings" or lower in ("my bookings", "my appointments", "appointments"):
        state.tenant_id = tenant_id
        state.customer_id = customer_id
        return await _show_my_bookings(db, state, tenant_id, customer_phone, business_type, tz_name)

    # Deposit payment verification buttons & UTR replies (always allowed even if subscription is paused)
    if lower.startswith("verify_pay_"):
        ref = lower.replace("verify_pay_", "").strip()
        state.tenant_id = tenant_id
        state.customer_id = customer_id
        state.step = ConversationStep.AWAITING_PAYMENT_UTR
        state.pending_payment_booking_id = ref
        await set_state(tenant_id, customer_phone, state)
        return TextResponse(
            f"💳 Please reply with your *12-digit UTR (Unique Transaction Reference)* from GPay / PhonePe / Paytm "
            f"for booking `{ref}`.\n\nExample: `412345678901`"
        )

    if lower.startswith("check_pay_"):
        ref = lower.replace("check_pay_", "").strip()
        from app.payments.router import check_payment_status_live

        try:
            res = await check_payment_status_live(db, tenant_id, ref)
            if res.get("verified"):
                await clear_state(tenant_id, customer_phone)
                return TextResponse(
                    f"✅ *Payment Verified!* Booking `{ref}` is confirmed. See you soon! 🙏"
                )
            return ButtonResponse(
                body=(
                    f"⏳ Payment for `{ref}` is still pending (status: {res.get('payment_status')}).\n"
                    "If you already paid via UPI, tap below to share your 12-digit UTR (Unique Transaction Reference) for quick confirmation."
                ),
                buttons=[
                    {"id": f"check_pay_{ref}", "title": "🔄 Check Again"},
                    {"id": f"verify_pay_{ref}", "title": "🧾 Send UPI UTR"},
                ],
            )
        except ValueError as exc:
            return TextResponse(str(exc))

    if lower.startswith("paid ") and len(body.split()) >= 2:
        parts = body.split()
        # Formats: "paid <utr>" or "paid <booking_ref> <utr>"
        utr_val = parts[-1]
        ref_val = parts[1] if len(parts) >= 3 else state.pending_payment_booking_id
        if not ref_val:
            # Find latest pending_deposit booking for this customer
            latest = (
                await db.execute(
                    text("""
                        SELECT id FROM bookings
                        WHERE tenant_id = :tid AND customer_id = :cid AND status = 'pending_deposit'
                        ORDER BY created_at DESC LIMIT 1
                    """),
                    {"tid": tenant_id, "cid": customer_id},
                )
            ).first()
            if latest:
                ref_val = str(latest.id)[:8]
        if ref_val:
            return await _handle_utr_submission(db, tenant_id, customer_phone, ref_val, utr_val)

    if state.step == ConversationStep.AWAITING_PAYMENT_UTR:
        return await _handle_utr_submission(
            db, tenant_id, customer_phone, state.pending_payment_booking_id, body
        )

    # Check tenant subscription state — when suspended, existing bookings can still be viewed/cancelled
    # above, while new booking flows pause gracefully.
    from app.billing.service import get_subscription_state

    sub_state = await get_subscription_state(db, UUID(tenant_id))
    if sub_state["access_mode"] == "suspended":
        await clear_state(tenant_id, customer_phone)
        return ButtonResponse(
            body=(
                f"Online booking for *{business_name}* is temporarily paused.\n"
                "Please contact the business directly, or tap below to manage an existing appointment."
            ),
            buttons=[{"id": "action_mybookings", "title": "📋 My Bookings"}],
        )

    if lower in ("hi", "hello", "hey", "hii", "book", "start", "menu"):
        return await _show_main_menu(
            db, tenant_id, customer_id, customer_phone, customer_name,
            business_name, business_type, tz_name, lang, tsettings,
        )

    if state.step == ConversationStep.IDLE:
        # Optional Gemini NLU shortcut if configured and user typed a free-form request
        nlu_reply = await _try_nlu_shortcut(
            db, tenant_id, customer_id, customer_phone, body, tz_name, business_type, tsettings
        )
        if nlu_reply is not None:
            return nlu_reply
        return await _show_main_menu(
            db, tenant_id, customer_id, customer_phone, customer_name,
            business_name, business_type, tz_name, lang, tsettings,
        )

    elif state.step == ConversationStep.AWAITING_SERVICE:
        return await _handle_service_selection(db, state, tenant_id, customer_phone, body, tz_name, business_type, tsettings)

    elif state.step == ConversationStep.AWAITING_SUBCATEGORY:
        return await _handle_subcategory_selection(db, state, tenant_id, customer_phone, body, tz_name, business_type, tsettings)

    elif state.step == ConversationStep.AWAITING_STAFF:
        return await _handle_staff_selection(db, state, tenant_id, customer_phone, body, tz_name, business_type, tsettings)

    elif state.step == ConversationStep.AWAITING_DATE:
        return await _handle_date_selection(db, state, tenant_id, customer_phone, body, tz_name, tsettings)

    elif state.step == ConversationStep.AWAITING_PERIOD:
        return await _handle_period_selection(db, state, tenant_id, customer_phone, body, tz_name)

    elif state.step == ConversationStep.AWAITING_SLOT:
        return await _handle_slot_selection(db, state, tenant_id, customer_phone, body, business_type)

    elif state.step == ConversationStep.AWAITING_CONFIRM:
        return await _handle_confirmation(db, state, tenant_id, customer_phone, body, tz_name, business_type, tsettings, business_name)

    elif state.step == ConversationStep.AWAITING_CANCEL_CONFIRM:
        return await _handle_cancel_confirm(db, state, tenant_id, customer_phone, body, tz_name, tsettings)

    else:
        await clear_state(tenant_id, customer_phone)
        return await _show_main_menu(
            db, tenant_id, customer_id, customer_phone, customer_name,
            business_name, business_type, tz_name, lang, tsettings,
        )


async def _try_nlu_shortcut(db, tenant_id, customer_id, customer_phone, body, tz_name, business_type, tsettings):
    """If Gemini NLU is enabled and extracts a matching service, jump directly into date/slot selection."""
    if not app_settings.gemini_api_key:
        return None

    from app.conversation.ai_nlu import extract_booking_intent

    res = await db.execute(
        text(
            "SELECT id, name, duration_minutes, price_paise FROM services "
            "WHERE tenant_id = :tid AND is_active = true AND duration_minutes > 0 ORDER BY sort_order"
        ),
        {"tid": tenant_id},
    )
    svcs = res.fetchall()
    if not svcs:
        return None

    tz = zoneinfo.ZoneInfo(tz_name)
    today = datetime.now(tz).date()
    intent = await extract_booking_intent(body, [s.name for s in svcs], today, business_type)
    if not intent or intent.confidence < 0.65:
        return None

    state = ConversationState(tenant_id=tenant_id, customer_id=customer_id)
    if intent.intent == "my_bookings":
        return await _show_my_bookings(db, state, tenant_id, customer_phone, business_type, tz_name)
    if intent.intent == "human":
        return TextResponse("🙋 I've notified the business owner. They'll get back to you shortly.")

    if intent.intent == "book" and intent.service_name:
        matched = next((s for s in svcs if s.name.lower() == intent.service_name.lower()), None)
        if matched:
            selected = {
                "id": str(matched.id),
                "name": matched.name,
                "duration": matched.duration_minutes,
                "price": matched.price_paise,
            }
            return await _proceed_after_service(
        db, state, tenant_id, customer_phone, selected, tz_name, business_type, tsettings
    )
    return None


async def _show_main_menu(
    db, tenant_id, customer_id, customer_phone, customer_name,
    business_name, business_type="salon", tz_name="Asia/Kolkata", lang="en", tsettings=None,
):
    """Show main menu with top-level services/categories."""
    labels = get_labels(business_type)
    tz = zoneinfo.ZoneInfo(tz_name)
    now = datetime.now(tz)

    upcoming = await db.execute(
        text("""SELECT COUNT(*) as cnt FROM bookings
                WHERE tenant_id = :tid AND customer_id = :cid
                AND status IN ('confirmed', 'pending_deposit') AND starts_at > :now"""),
        {"tid": tenant_id, "cid": customer_id, "now": now},
    )
    has_bookings = (upcoming.first().cnt or 0) > 0

    custom_welcome = (tsettings or {}).get("welcome_message")
    if custom_welcome:
        greeting = f"{labels['welcome_emoji']} {custom_welcome}"
    elif lang != "en":
        greeting = f"{labels['welcome_emoji']} {t('welcome_new', lang=lang, business_name=business_name)}"
    else:
        greeting = f"{labels['welcome_emoji']} Hi{(' ' + customer_name) if customer_name else ''}! Welcome to *{business_name}*"

    state = ConversationState(
        step=ConversationStep.AWAITING_SERVICE,
        tenant_id=tenant_id,
        customer_id=customer_id,
    )

    # Load top-level items: standalone services + parent categories
    result = await db.execute(
        text("""SELECT s.id, s.name, s.duration_minutes, s.price_paise, s.parent_service_id,
                      (SELECT COUNT(*) FROM services c WHERE c.parent_service_id = s.id AND c.is_active = true) as child_count
               FROM services s
               WHERE s.tenant_id = :tid AND s.is_active = true AND s.parent_service_id IS NULL
               ORDER BY s.sort_order, s.name"""),
        {"tid": tenant_id},
    )
    services = result.fetchall()
    services_data = [
        {
            "id": str(s.id),
            "name": s.name,
            "duration": s.duration_minutes,
            "price": s.price_paise,
            "child_count": s.child_count,
        }
        for s in services
    ]
    state.services_json = json.dumps(services_data)
    await set_state(tenant_id, customer_phone, state)

    if not services:
        return TextResponse(f"{greeting}\n\nNo {labels['services'].lower()} available right now.")

    rows = []
    for i, s in enumerate(services):
        if s.child_count > 0:
            rows.append({
                "id": f"svc_{i}",
                "title": f"\U0001f4c2 {s.name}"[:24],
                "description": f"{s.child_count} option{'s' if s.child_count != 1 else ''}"[:72],
            })
        else:
            price_str = f" · ₹{s.price_paise // 100}" if s.price_paise else ""
            rows.append({
                "id": f"svc_{i}",
                "title": s.name[:24],
                "description": f"{s.duration_minutes}min{price_str}"[:72],
            })

    sections = [{"title": labels["services"], "rows": rows[:9 if has_bookings else 10]}]
    if has_bookings:
        sections.append({
            "title": "Manage",
            "rows": [{"id": "action_mybookings", "title": f"📋 My {labels['bookings']}"[:24], "description": "View, cancel or reschedule"}],
        })

    return ListResponse(
        body=f"{greeting}\n\nWhat would you like to {labels['service_action'].lower()}?",
        button_text=f"View {labels['services']}",
        sections=sections,
        footer=f"Tap to see {labels['services'].lower()}",
    )


async def _proceed_after_service(
    db, state, tenant_id, customer_phone, selected, tz_name, business_type="salon", tsettings=None
):
    """If 2+ eligible staff can perform this service, ask the customer to pick a staff member (or Any Available).
    If only 1 staff is eligible, skip directly to date selection."""
    res = await db.execute(
        text("""
            SELECT id, name, role, specialty, bio
            FROM staff
            WHERE tenant_id = :tid AND is_active = true
              AND (skills IS NULL OR skills @> :skill_filter)
            ORDER BY name
        """),
        {"tid": tenant_id, "skill_filter": f'["{selected["id"]}"]'},
    )
    staff_rows = res.fetchall()

    if len(staff_rows) <= 1:
        state.preferred_staff_id = str(staff_rows[0].id) if len(staff_rows) == 1 else ""
        state.preferred_staff_name = staff_rows[0].name if len(staff_rows) == 1 else ""
        state.staff_options_json = "[]"
        return await _proceed_to_dates(db, state, tenant_id, customer_phone, selected, tz_name, tsettings)

    labels = get_labels(business_type)
    staff_label = labels["staff"]
    options = [
        {
            "id": str(s.id),
            "name": s.name,
            "role": s.role or "",
            "specialty": s.specialty or "",
            "bio": s.bio or "",
        }
        for s in staff_rows[:9]  # +1 row for "Any Available" = max 10 WhatsApp list rows
    ]
    state.step = ConversationStep.AWAITING_STAFF
    state.selected_service_id = selected["id"]
    state.selected_service_name = selected["name"]
    state.preferred_staff_id = ""
    state.preferred_staff_name = ""
    state.staff_options_json = json.dumps(options)
    await set_state(tenant_id, customer_phone, state)

    rows = [
        {
            "id": "staff_any",
            "title": f"Any Available {staff_label}"[:24],
            "description": "Fastest available slot",
        }
    ]
    for i, s in enumerate(options):
        desc = s["specialty"] or s["role"] or s["bio"] or ""
        rows.append({
            "id": f"staff_{i}",
            "title": s["name"][:24],
            "description": desc[:72],
        })

    return ListResponse(
        body=f"👤 *{selected['name']}*\n\nWho would you prefer?",
        button_text=f"Choose {staff_label}"[:20],
        sections=[{"title": f"Available {labels.get('staff_plural', 'Team')}"[:24], "rows": rows}],
    )


async def _handle_staff_selection(
    db, state, tenant_id, customer_phone, body, tz_name, business_type="salon", tsettings=None
):
    """Handle staff preference selection (specific staff member or Any Available)."""
    options = json.loads(state.staff_options_json or "[]")
    lower = body.strip().lower()

    chosen_id = None
    chosen_name = ""

    if lower in ("staff_any", "any", "anyone", "any available", "0"):
        chosen_id = ""
        chosen_name = ""
    elif lower.startswith("staff_"):
        try:
            idx = int(lower.replace("staff_", ""))
            if 0 <= idx < len(options):
                chosen_id = options[idx]["id"]
                chosen_name = options[idx]["name"]
        except ValueError:
            pass
    else:
        # Match by number (1 = Any Available, 2..N = specific staff) or staff name
        try:
            num = int(lower)
            if num == 1:
                chosen_id = ""
                chosen_name = ""
            elif 2 <= num <= len(options) + 1:
                chosen_id = options[num - 2]["id"]
                chosen_name = options[num - 2]["name"]
        except ValueError:
            for s in options:
                if s["id"] == body.strip() or s["name"].lower() == lower or lower in s["name"].lower():
                    chosen_id = s["id"]
                    chosen_name = s["name"]
                    break

    if chosen_id is None:
        labels = get_labels(business_type)
        menu_lines = [f"  1. Any Available {labels['staff']}"] + [
            f"  {i + 2}. {s['name']}" for i, s in enumerate(options)
        ]
        return TextResponse(f"Please choose a {labels['staff'].lower()}:\n" + "\n".join(menu_lines))

    state.preferred_staff_id = chosen_id
    state.preferred_staff_name = chosen_name
    selected = {"id": state.selected_service_id, "name": state.selected_service_name}
    return await _proceed_to_dates(db, state, tenant_id, customer_phone, selected, tz_name, tsettings)


async def _proceed_to_dates(db, state, tenant_id, customer_phone, selected, tz_name, tsettings):
    """Find available dates honoring tenant advance_booking_days and preferred staff, then transition to AWAITING_DATE."""
    tz = zoneinfo.ZoneInfo(tz_name)
    today = datetime.now(tz).date()
    max_days = max(1, min(90, int((tsettings or {}).get("advance_booking_days", 14))))
    slot_inc = int((tsettings or {}).get("slot_increment_minutes", 15))
    pref_staff = UUID(state.preferred_staff_id) if state.preferred_staff_id else None

    available_dates: list[tuple[date, int]] = []
    for i in range(max_days):
        d = today + timedelta(days=i)
        slots = await get_available_slots(
            db, UUID(tenant_id), UUID(selected["id"]), d,
            preferred_staff_id=pref_staff, tz_name=tz_name, slot_increment=slot_inc,
        )
        if slots:
            # Count unique bookable start times (matching the deduplicated slot list)
            unique_times = len({s.start.strftime("%H:%M") for s in slots})
            available_dates.append((d, unique_times))
        if len(available_dates) >= 7:
            break

    if not available_dates:
        await clear_state(tenant_id, customer_phone)
        who = f" with {state.preferred_staff_name}" if state.preferred_staff_name else ""
        return TextResponse(f"Sorry, no slots available for {selected['name']}{who} in the next {max_days} days.")

    state.step = ConversationStep.AWAITING_DATE
    state.selected_service_id = selected["id"]
    state.selected_service_name = selected["name"]
    state.available_slots_json = json.dumps([d.isoformat() for d, _ in available_dates])
    await set_state(tenant_id, customer_phone, state)

    rows = []
    for i, (d, cnt) in enumerate(available_dates):
        if d == today:
            title = f"Today ({d.strftime('%d %b')})"
        elif d == today + timedelta(days=1):
            title = f"Tomorrow ({d.strftime('%d %b')})"
        else:
            title = d.strftime("%a, %d %b")
        rows.append({
            "id": f"date_{i}",
            "title": title[:24],
            "description": f"{cnt} slot{'s' if cnt != 1 else ''} available",
        })

    prefix = "🔄 Rescheduling " if state.booking_id_to_cancel else "📅 "
    with_who = f" with *{state.preferred_staff_name}*" if state.preferred_staff_name else ""
    today_note = ""
    if available_dates[0][0] != today:
        today_note = "\n_No remaining slots for today — showing the next open dates._\n"
    return ListResponse(
        body=f"{prefix}*{selected['name']}*{with_who}{today_note}\nPick a date:",
        button_text="View Dates",
        sections=[{"title": "Available Dates", "rows": rows}],
    )


async def _handle_service_selection(db, state, tenant_id, customer_phone, body, tz_name, business_type="salon", tsettings=None):
    """Handle service selection from list, UUID, or number input."""
    services = json.loads(state.services_json)

    if body == "action_book":
        return await _show_service_list(services, tenant_id, customer_phone, state, business_type)

    if body == "action_mybookings":
        return await _show_my_bookings(db, state, tenant_id, customer_phone, business_type, tz_name)

    selected = None
    if body.startswith("svc_"):
        try:
            idx = int(body.replace("svc_", ""))
            if 0 <= idx < len(services):
                selected = services[idx]
        except ValueError:
            pass

    # Match by direct service UUID or exact name
    if not selected:
        for s in services:
            if s["id"] == body or s["name"].lower() == body.lower():
                selected = s
                break

    # Handle plain number input
    if not selected:
        try:
            choice = int(body)
            if 1 <= choice <= len(services):
                selected = services[choice - 1]
        except ValueError:
            pass

    if not selected:
        menu = "\n".join(f"  {i+1}. {s['name']}" for i, s in enumerate(services))
        return TextResponse(f"Please pick an option:\n{menu}\n\nReply with a number.")

    if selected.get("child_count", 0) > 0:
        return await _show_subcategory_list(db, state, tenant_id, customer_phone, selected)

    return await _proceed_after_service(
        db, state, tenant_id, customer_phone, selected, tz_name, business_type, tsettings
    )


def _slot_hour(slot_dict: dict, tz_name: str) -> int:
    dt = datetime.fromisoformat(slot_dict["start"])
    try:
        dt = dt.astimezone(zoneinfo.ZoneInfo(tz_name))
    except Exception:
        pass
    return dt.hour


def _render_slot_page(
    state: ConversationState,
    slots_data: list[dict],
    page: int = 0,
    period_label: str = "",
) -> ListResponse:
    """Render up to 10 WhatsApp list rows for slots, with Earlier/More pagination if >10 slots."""
    selected_date = date.fromisoformat(state.selected_date)
    date_label = selected_date.strftime("%A, %d %b")
    with_who = f" with *{state.preferred_staff_name}*" if state.preferred_staff_name else ""
    period_suffix = f" ({period_label})" if period_label else ""

    if len(slots_data) <= 10:
        page_items = [(i, s) for i, s in enumerate(slots_data)]
        has_prev = False
        has_next = False
    else:
        page_size = 8
        max_page = max(0, (len(slots_data) - 1) // page_size)
        page = max(0, min(page, max_page))
        start_idx = page * page_size
        end_idx = min(len(slots_data), start_idx + page_size)
        page_items = [(i, slots_data[i]) for i in range(start_idx, end_idx)]
        has_prev = page > 0
        has_next = end_idx < len(slots_data)

    rows = []
    if has_prev:
        rows.append({
            "id": f"slot_page_{page - 1}",
            "title": "⬅️ Earlier times",
            "description": "See previous available slots",
        })
    for idx, s in page_items:
        desc = ""
        if not state.preferred_staff_id and s.get("staff_name"):
            desc = f"with {s['staff_name']}"
        rows.append({
            "id": f"slot_{idx}",
            "title": str(s.get("display", ""))[:24],
            "description": desc[:72],
        })
    if has_next:
        remaining = len(slots_data) - (page + 1) * 8
        rows.append({
            "id": f"slot_page_{page + 1}",
            "title": "➡️ More times",
            "description": f"{remaining} more slot{'s' if remaining != 1 else ''} later",
        })

    return ListResponse(
        body=f"⏰ *{state.selected_service_name}*{with_who} on *{date_label}*{period_suffix}\n\nPick a time:",
        button_text="View Times",
        sections=[{"title": f"Available Slots{period_suffix}"[:24], "rows": rows}],
    )


async def _handle_date_selection(db, state, tenant_id, customer_phone, body, tz_name, tsettings=None):
    """Handle date selection, deduplicating times and grouping full-day slots by Morning/Afternoon/Evening."""
    dates = json.loads(state.available_slots_json)
    slot_inc = int((tsettings or {}).get("slot_increment_minutes", 15))
    pref_staff = UUID(state.preferred_staff_id) if state.preferred_staff_id else None

    idx = None
    if body.startswith("date_"):
        try:
            idx = int(body.replace("date_", ""))
        except ValueError:
            pass
    elif body.startswith("action_date_"):
        try:
            idx = int(body.replace("action_date_", ""))
        except ValueError:
            pass
    else:
        try:
            idx = int(body) - 1
        except ValueError:
            pass

    if idx is None or idx < 0 or idx >= len(dates):
        return TextResponse(f"Please pick a date (1-{len(dates)}).")

    selected_date = date.fromisoformat(dates[idx])
    slots = await get_available_slots(
        db, UUID(tenant_id), UUID(state.selected_service_id), selected_date,
        preferred_staff_id=pref_staff, tz_name=tz_name, slot_increment=slot_inc,
    )

    if not slots:
        return TextResponse(f"No slots on {selected_date.strftime('%A, %d %b')}. Pick another date.")

    # Always deduplicate by start time so identical times never appear twice across multiple staff
    seen: set[str] = set()
    dedup_slots = []
    for s in slots:
        key = s.start.strftime("%H:%M")
        if key not in seen:
            seen.add(key)
            dedup_slots.append(s)

    slots_data = [s.to_dict() for s in dedup_slots]
    state.selected_date = selected_date.isoformat()
    state.available_slots_json = json.dumps(slots_data)
    state.slot_page = 0

    # If >10 slots across multiple parts of the day, offer fast Morning/Afternoon/Evening buttons
    if len(slots_data) > 10:
        morning = [s for s in slots_data if _slot_hour(s, tz_name) < 12]
        afternoon = [s for s in slots_data if 12 <= _slot_hour(s, tz_name) < 17]
        evening = [s for s in slots_data if _slot_hour(s, tz_name) >= 17]
        buttons = []
        summary_lines = []
        if morning:
            n = len(morning)
            buttons.append({"id": "period_morning", "title": "🌅 Morning"})
            summary_lines.append(f"• 🌅 *Morning* (before 12 PM) — {n} slot{'s' if n != 1 else ''} available")
        if afternoon:
            n = len(afternoon)
            buttons.append({"id": "period_afternoon", "title": "☀️ Afternoon"})
            summary_lines.append(f"• ☀️ *Afternoon* (12–5 PM) — {n} slot{'s' if n != 1 else ''} available")
        if evening:
            n = len(evening)
            buttons.append({"id": "period_evening", "title": "🌆 Evening"})
            summary_lines.append(f"• 🌆 *Evening* (after 5 PM) — {n} slot{'s' if n != 1 else ''} available")

        if len(buttons) >= 2:
            state.step = ConversationStep.AWAITING_PERIOD
            await set_state(tenant_id, customer_phone, state)
            with_who = f" with *{state.preferred_staff_name}*" if state.preferred_staff_name else ""
            return ButtonResponse(
                body=(
                    f"⏰ *{state.selected_service_name}*{with_who} on *{selected_date.strftime('%A, %d %b')}*\n\n"
                    + "\n".join(summary_lines)
                    + "\n\nWhich part of the day works best?"
                ),
                buttons=buttons,
            )

    state.step = ConversationStep.AWAITING_SLOT
    await set_state(tenant_id, customer_phone, state)
    return _render_slot_page(state, slots_data, page=0)


async def _handle_period_selection(db, state, tenant_id, customer_phone, body, tz_name):
    """Filter full-day slots by Morning / Afternoon / Evening and show selectable times."""
    all_slots = json.loads(state.available_slots_json or "[]")
    lower = body.strip().lower()

    if lower in ("period_morning", "morning", "1"):
        filtered = [s for s in all_slots if _slot_hour(s, tz_name) < 12]
        label = "Morning"
    elif lower in ("period_afternoon", "afternoon", "2"):
        filtered = [s for s in all_slots if 12 <= _slot_hour(s, tz_name) < 17]
        label = "Afternoon"
    elif lower in ("period_evening", "evening", "3"):
        filtered = [s for s in all_slots if _slot_hour(s, tz_name) >= 17]
        label = "Evening"
    else:
        return TextResponse("Please pick *Morning*, *Afternoon*, or *Evening*.")

    if not filtered:
        return TextResponse(f"No {label.lower()} slots available on that date. Please pick another time of day.")

    state.step = ConversationStep.AWAITING_SLOT
    state.available_slots_json = json.dumps(filtered)
    state.slot_page = 0
    await set_state(tenant_id, customer_phone, state)
    return _render_slot_page(state, filtered, page=0, period_label=label)


async def _handle_slot_selection(db, state, tenant_id, customer_phone, body, business_type="salon"):
    """Handle slot selection (or Earlier/More pagination)."""
    labels = get_labels(business_type)
    slots = json.loads(state.available_slots_json)

    if body.startswith("slot_page_"):
        try:
            next_page = int(body.replace("slot_page_", ""))
            state.slot_page = max(0, next_page)
            await set_state(tenant_id, customer_phone, state)
            return _render_slot_page(state, slots, page=state.slot_page)
        except ValueError:
            pass

    idx = None
    if body.startswith("slot_"):
        try:
            idx = int(body.replace("slot_", ""))
        except ValueError:
            pass
    elif body.startswith("action_slot_"):
        try:
            idx = int(body.replace("action_slot_", ""))
        except ValueError:
            pass
    else:
        try:
            idx = int(body) - 1
        except ValueError:
            pass

    if idx is None or idx < 0 or idx >= len(slots):
        return TextResponse(f"Please pick a time (1-{len(slots)}).")

    selected = slots[idx]
    state.step = ConversationStep.AWAITING_CONFIRM
    state.selected_slot_index = idx
    state.selected_staff_id = selected["staff_id"]
    state.selected_staff_name = selected["staff_name"]
    state.selected_start = selected["start"]
    state.selected_end = selected["end"]
    await set_state(tenant_id, customer_phone, state)

    display_time = selected["display"]
    display_date = date.fromisoformat(state.selected_date).strftime("%A, %d %b")

    has_multiple_staff = bool(json.loads(state.staff_options_json or "[]"))
    show_staff = has_multiple_staff or bool(state.preferred_staff_name) or len({s["staff_id"] for s in slots}) > 1

    header = "🔄 *Confirm rescheduled" if state.booking_id_to_cancel else "✅ *Confirm your"
    body_lines = [
        f"{header} {labels['booking'].lower()}:*\n",
        f"{labels['welcome_emoji']} {state.selected_service_name}",
        f"{labels['booking_emoji']} {display_date}",
        f"⏰ {display_time}",
    ]
    if show_staff and selected.get("staff_name"):
        body_lines.append(f"👤 {selected['staff_name']}")
        bio = selected.get("staff_specialty") or selected.get("staff_bio")
        if bio:
            body_lines.append(f"    _{bio}_")

    return ButtonResponse(
        body="\n".join(body_lines),
        buttons=[
            {"id": "confirm_yes", "title": "✅ Confirm"},
            {"id": "confirm_no", "title": "❌ Cancel"},
        ],
    )


async def _handle_confirmation(
    db, state, tenant_id, customer_phone, body, tz_name,
    business_type="salon", tsettings=None, business_name="",
):
    """Handle booking confirmation (including max-bookings check, deposits, and rescheduling)."""
    lower = body.lower().strip()
    if lower in ("confirm_no", "no", "n", "0"):
        await clear_state(tenant_id, customer_phone)
        return TextResponse("Booking cancelled. Send any message to start over.")

    if lower not in ("confirm_yes", "action_confirm", "yes", "y", "confirm", "ok", "1"):
        return ButtonResponse(
            body="Confirm your booking?",
            buttons=[
                {"id": "confirm_yes", "title": "✅ Confirm"},
                {"id": "confirm_no", "title": "❌ Cancel"},
            ],
        )

    tsettings = tsettings or {}
    is_reschedule = bool(state.booking_id_to_cancel)

    # Enforce monthly booking quota & active subscription for new bookings
    if not is_reschedule:
        from app.billing.service import enforce_plan_limit
        from app.exceptions import PlanLimitExceededError, SubscriptionExpiredError

        try:
            await enforce_plan_limit(db, UUID(tenant_id), "bookings")
        except (PlanLimitExceededError, SubscriptionExpiredError):
            await clear_state(tenant_id, customer_phone)
            return TextResponse(
                f"Online booking for *{business_name}* is temporarily unavailable right now. "
                "Please contact the business directly to schedule your visit."
            )

    # Enforce max_bookings_per_customer for new bookings (not reschedules)
    if not is_reschedule:
        max_active = int(tsettings.get("max_bookings_per_customer", 5))
        cnt_res = await db.execute(
            text("""SELECT COUNT(*) FROM bookings
                    WHERE tenant_id = :tid AND customer_id = :cid
                    AND status IN ('confirmed', 'pending_deposit') AND starts_at > :now"""),
            {"tid": tenant_id, "cid": state.customer_id, "now": datetime.now(timezone.utc)},
        )
        if (cnt_res.scalar() or 0) >= max_active:
            await clear_state(tenant_id, customer_phone)
            return TextResponse(
                f"You already have {max_active} upcoming bookings (the maximum allowed). "
                "Please cancel or reschedule an existing one first."
            )

    # Get branch
    branch_result = await db.execute(
        text("SELECT branch_id FROM staff WHERE id = :sid"),
        {"sid": state.selected_staff_id},
    )
    branch_row = branch_result.first()
    branch_id = branch_row.branch_id if branch_row else None

    if not branch_id:
        br = await db.execute(
            text("SELECT id FROM branches WHERE tenant_id = :tid AND is_primary = true LIMIT 1"),
            {"tid": tenant_id},
        )
        row = br.first()
        branch_id = row.id if row else None

    if not branch_id:
        await clear_state(tenant_id, customer_phone)
        return TextResponse("Sorry, something went wrong. Please try again.")

    svc_result = await db.execute(
        text("SELECT requires_deposit, deposit_amount_paise FROM services WHERE id = :sid"),
        {"sid": state.selected_service_id},
    )
    svc = svc_result.first()
    requires_deposit = bool(svc.requires_deposit) if svc else False
    deposit_paise = (svc.deposit_amount_paise or 0) if svc else 0

    starts_at = datetime.fromisoformat(state.selected_start)
    ends_at = datetime.fromisoformat(state.selected_end)

    try:
        booking = await create_booking(
            db=db,
            tenant_id=UUID(tenant_id),
            branch_id=branch_id,
            staff_id=UUID(state.selected_staff_id),
            customer_id=UUID(state.customer_id),
            service_id=UUID(state.selected_service_id),
            starts_at=starts_at,
            ends_at=ends_at,
            requires_deposit=requires_deposit,
        )
    except Exception as e:
        logger.error("booking_failed", error=str(e))
        await clear_state(tenant_id, customer_phone)
        return TextResponse("Sorry, that slot was just taken. Please try again.")

    # If rescheduling, cancel the old booking now that the new one is safely booked
    if is_reschedule:
        try:
            await update_booking_status(
                db, UUID(tenant_id), UUID(state.booking_id_to_cancel),
                BookingStatus.CANCELLED, "Rescheduled by customer",
            )
        except Exception as e:
            logger.warning("reschedule_old_cancel_failed", error=str(e))

    await clear_state(tenant_id, customer_phone)

    display_date = date.fromisoformat(state.selected_date).strftime("%A, %d %b")
    display_time = datetime.fromisoformat(state.selected_start).strftime("%I:%M %p").lstrip("0")
    bid = booking["id"][:8]
    labels = get_labels(business_type)

    # Handle deposit-required bookings with Razorpay Payment Link OR Direct UPI + UTR verification
    if requires_deposit:
        pay_info = None
        if deposit_paise > 0:
            try:
                from app.billing.service import get_subscription_state
                from app.payments.router import create_whatsapp_deposit_payment

                sub_st = await get_subscription_state(db, UUID(tenant_id))
                if sub_st["entitlements"].get("deposits_enabled", False):
                    pay_info = await create_whatsapp_deposit_payment(
                        db=db,
                        tenant_id=tenant_id,
                        booking_id=booking["id"],
                        amount_paise=deposit_paise,
                        business_name=business_name or "Tivoria",
                        customer_phone=customer_phone,
                        deposit_mode=tsettings.get("deposit_mode", "auto"),
                        upi_id=tsettings.get("upi_id"),
                        hold_minutes=int(tsettings.get("deposit_hold_minutes", 15)),
                    )
            except Exception as exc:
                logger.warning("deposit_link_creation_failed", error=str(exc))

        deposit_str = f"₹{deposit_paise // 100}" if deposit_paise else "the required deposit"
        hold_mins = int(tsettings.get("deposit_hold_minutes", 15))
        pay_link_line = ""
        buttons = [
            {"id": f"verify_pay_{bid}", "title": "✅ I've Paid (UTR)"},
            {"id": f"cancel_{bid}", "title": "❌ Cancel This"},
        ]

        if pay_info and pay_info.get("payment_method") in ("razorpay_link", "razorpay"):
            pay_link_line = (
                f"\n\n💳 *Pay deposit ({deposit_str}) via UPI / Card:*\n{pay_info['url']}\n"
                f"_Auto-confirms immediately after payment (slot held {hold_mins} min)._"
            )
            buttons = [
                {"id": f"check_pay_{bid}", "title": "✅ Verify Payment"},
                {"id": f"cancel_{bid}", "title": "❌ Cancel This"},
            ]
        elif pay_info and pay_info.get("payment_method") == "upi_manual":
            pay_link_line = (
                f"\n\n💳 *Pay {deposit_str} via UPI (0% fee):*\n"
                f"UPI ID: `{pay_info['upi_id']}`\n"
                f"Tap to pay: {pay_info['url']}\n"
                f"_After paying, tap *I've Paid (UTR)* below or reply `paid <12-digit UTR>` "
                f"(Unique Transaction Reference from your UPI app; slot held {hold_mins} min)._"
            )
        elif (tsettings.get("upi_id") or "").strip():
            raw_upi = tsettings["upi_id"].strip()
            pay_link_line = (
                f"\n\n💳 *Pay {deposit_str} via UPI:*\n"
                f"UPI ID: `{raw_upi}`\n"
                f"_After paying, tap *I've Paid (UTR)* below or reply `paid <12-digit UTR>` "
                f"(Unique Transaction Reference from your UPI app; slot held {hold_mins} min)._"
            )

        return ButtonResponse(
            body=(
                f"⏳ *{labels['booking']} reserved — Deposit Required*\n\n"
                f"{labels['welcome_emoji']} {state.selected_service_name}\n"
                f"{labels['booking_emoji']} {display_date}\n"
                f"⏰ {display_time}\n"
                f"👤 {state.selected_staff_name}\n"
                f"🆔 {bid}\n\n"
                f"Please pay {deposit_str} to confirm your slot.{pay_link_line}"
            ),
            buttons=buttons,
            footer=f"Slot held for {hold_mins} mins pending deposit.",
        )

    custom_confirm = (tsettings.get("booking_confirmed_message") or "").strip()
    maps_url = (tsettings.get("google_maps_url") or "").strip()
    review_url = (tsettings.get("google_review_url") or "").strip()
    extras: list[str] = []
    if custom_confirm:
        formatted = (
            custom_confirm.replace("{business_name}", business_name or "")
            .replace("{service_name}", state.selected_service_name or "")
            .replace("{staff_name}", state.selected_staff_name or "")
            .replace("{date}", display_date)
            .replace("{time}", display_time)
            .replace("{maps_url}", maps_url)
            .replace("{review_url}", review_url)
        )
        extras.append(formatted)
    if maps_url and maps_url not in "\n".join(extras):
        extras.append(f"📍 *Location:* {maps_url}")
    footer_note = ("\n\n" + "\n\n".join(extras)) if extras else ""
    headline = f"🔄 *{labels['booking']} rescheduled!*" if is_reschedule else f"🎉 *{labels['booking']} confirmed!*"

    return ButtonResponse(
        body=(
            f"{headline}\n\n"
            f"{labels['welcome_emoji']} {state.selected_service_name}\n"
            f"{labels['booking_emoji']} {display_date}\n"
            f"⏰ {display_time}\n"
            f"👤 {state.selected_staff_name}\n"
            f"🆔 {bid}{footer_note}"
        ),
        buttons=[
            {"id": "action_done", "title": "👍 Thanks, All Set"},
            {"id": "action_book", "title": f"{labels['booking_emoji']} {labels['service_action']} Another"[:20]},
            {"id": f"cancel_{bid}", "title": "❌ Cancel This"},
        ],
        footer="See you then! 🙏",
    )


async def _show_my_bookings(db, state, tenant_id, customer_phone, business_type="salon", tz_name="Asia/Kolkata"):
    """Show customer's upcoming bookings with cancel & reschedule options."""
    labels = get_labels(business_type)
    tz = zoneinfo.ZoneInfo(tz_name)
    now = datetime.now(tz)

    result = await db.execute(
        text("""
            SELECT b.id, b.starts_at, b.status, s.name as service_name, st.name as staff_name
            FROM bookings b
            JOIN services s ON b.service_id = s.id
            LEFT JOIN staff st ON b.staff_id = st.id
            WHERE b.tenant_id = :tid AND b.customer_id = :cid
            AND b.status IN ('confirmed', 'pending_deposit') AND b.starts_at > :now
            ORDER BY b.starts_at LIMIT 5
        """),
        {"tid": tenant_id, "cid": state.customer_id, "now": now},
    )
    bookings = result.fetchall()

    if not bookings:
        await clear_state(tenant_id, customer_phone)
        return TextResponse(f"You have no upcoming {labels['bookings'].lower()}. Send any message to {labels['service_action'].lower()}.")

    if len(bookings) == 1:
        b = bookings[0]
        local_time = b.starts_at.astimezone(tz)
        bid = str(b.id)[:8]

        state.step = ConversationStep.AWAITING_CANCEL_CONFIRM
        state.booking_id_to_cancel = str(b.id)
        await set_state(tenant_id, customer_phone, state)

        return ButtonResponse(
            body=(
                f"📋 *Your upcoming {labels['booking'].lower()}:*\n\n"
                f"{labels['welcome_emoji']} {b.service_name}\n"
                f"📅 {local_time.strftime('%A, %d %b')}\n"
                f"⏰ {local_time.strftime('%I:%M %p').lstrip('0')}\n"
                f"👤 {b.staff_name or 'Any'}\n"
                f"🆔 {bid}"
            ),
            buttons=[
                {"id": f"reschedule_{bid}", "title": "🔄 Reschedule"},
                {"id": f"cancel_{bid}", "title": "❌ Cancel"},
                {"id": "keep_booking", "title": "👍 Keep It"},
            ],
        )

    rows = []
    for b in bookings:
        local_time = b.starts_at.astimezone(tz)
        bid = str(b.id)[:8]
        rows.append({
            "id": f"cancel_{bid}",
            "title": f"{b.service_name}"[:24],
            "description": f"{local_time.strftime('%d %b %I:%M%p').lstrip('0')} · {b.staff_name or 'Any'}"[:72],
        })

    state.step = ConversationStep.AWAITING_CANCEL_CONFIRM
    await set_state(tenant_id, customer_phone, state)

    return ListResponse(
        body=f"📋 *Your upcoming {labels['bookings'].lower()} ({len(bookings)}):*\n\nSelect one to manage:",
        button_text=f"View {labels['bookings']}",
        sections=[{"title": "Upcoming", "rows": rows}],
    )


async def _start_reschedule(db, state, tenant_id, customer_phone, ref, tz_name, tsettings):
    """Begin rescheduling an existing booking by selecting a new date/time for the same service."""
    result = await db.execute(
        text("""
            SELECT b.id, b.service_id, s.name as service_name, s.duration_minutes, s.price_paise
            FROM bookings b
            JOIN services s ON b.service_id = s.id
            WHERE b.tenant_id = :tid AND b.id::text LIKE :ref
            AND b.status IN ('confirmed', 'pending_deposit')
            LIMIT 1
        """),
        {"tid": tenant_id, "ref": f"{ref}%"},
    )
    row = result.first()
    if not row:
        await clear_state(tenant_id, customer_phone)
        return TextResponse(f"No active booking found with ID {ref}.")

    state.booking_id_to_cancel = str(row.id)
    selected = {
        "id": str(row.service_id),
        "name": row.service_name,
        "duration": row.duration_minutes,
        "price": row.price_paise,
    }
    return await _proceed_to_dates(db, state, tenant_id, customer_phone, selected, tz_name, tsettings)


async def _handle_cancel_confirm(db, state, tenant_id, customer_phone, body, tz_name="Asia/Kolkata", tsettings=None):
    """Handle cancel confirmation while enforcing min_cancel_hours."""
    lower = body.lower().strip()
    min_cancel_hours = int((tsettings or {}).get("min_cancel_hours", 0))

    if lower.startswith("cancel_"):
        ref = lower.replace("cancel_", "")
        result = await db.execute(
            text("""SELECT id, starts_at, status FROM bookings
                    WHERE tenant_id = :tid AND id::text LIKE :ref
                    AND status IN ('confirmed', 'pending_deposit')"""),
            {"tid": tenant_id, "ref": f"{ref}%"},
        )
        booking = result.first()

        if not booking:
            await clear_state(tenant_id, customer_phone)
            return TextResponse(f"No active booking found with ID {ref}.")

        # Enforce min_cancel_hours policy
        if min_cancel_hours > 0:
            now_utc = datetime.now(timezone.utc)
            if booking.starts_at - now_utc < timedelta(hours=min_cancel_hours):
                await clear_state(tenant_id, customer_phone)
                return TextResponse(
                    f"⚠️ Cancellations must be made at least {min_cancel_hours} hour(s) before the appointment. "
                    "Reply *human* to contact the business directly."
                )

        state.booking_id_to_cancel = str(booking.id)
        await set_state(tenant_id, customer_phone, state)

        return ButtonResponse(
            body=f"Are you sure you want to cancel booking *{ref}*?",
            buttons=[
                {"id": "do_cancel", "title": "❌ Yes, Cancel"},
                {"id": f"reschedule_{ref}", "title": "🔄 Reschedule"},
                {"id": "keep_booking", "title": "👍 Keep It"},
            ],
        )

    if lower in ("do_cancel", "yes", "y"):
        if state.booking_id_to_cancel:
            try:
                await update_booking_status(
                    db, UUID(tenant_id), UUID(state.booking_id_to_cancel),
                    BookingStatus.CANCELLED, "Cancelled by customer",
                )
                await clear_state(tenant_id, customer_phone)
                return TextResponse("❌ Booking cancelled. Send any message to book again.")
            except Exception as e:
                logger.error("cancel_failed", error=str(e))
                await clear_state(tenant_id, customer_phone)
                return TextResponse("Couldn't cancel. Please try again or contact the business.")

    if lower in ("keep_booking", "no", "n", "keep"):
        await clear_state(tenant_id, customer_phone)
        return TextResponse("👍 Booking kept. Send any message to book or manage.")

    return ButtonResponse(
        body="Cancel this booking?",
        buttons=[
            {"id": "do_cancel", "title": "❌ Yes, Cancel"},
            {"id": "keep_booking", "title": "👍 Keep It"},
        ],
    )


async def _show_subcategory_list(db, state, tenant_id, customer_phone, parent):
    """Show child services for a parent category."""
    result = await db.execute(
        text("""SELECT id, name, duration_minutes, price_paise
               FROM services
               WHERE parent_service_id = :pid AND tenant_id = :tid AND is_active = true
               ORDER BY sort_order, name"""),
        {"pid": parent["id"], "tid": tenant_id},
    )
    children = result.fetchall()

    if not children:
        return TextResponse(f"No options available under {parent['name']}.")

    children_data = [
        {"id": str(c.id), "name": c.name, "duration": c.duration_minutes, "price": c.price_paise}
        for c in children
    ]
    state.step = ConversationStep.AWAITING_SUBCATEGORY
    state.selected_parent_id = parent["id"]
    state.selected_parent_name = parent["name"]
    state.services_json = json.dumps(children_data)
    await set_state(tenant_id, customer_phone, state)

    rows = [
        {
            "id": f"subsvc_{i}",
            "title": c.name[:24],
            "description": f"{c.duration_minutes}min · ₹{c.price_paise // 100 if c.price_paise else 0}"[:72],
        }
        for i, c in enumerate(children)
    ]

    return ListResponse(
        body=f"\U0001f4c2 *{parent['name']}*\n\nPick a service:",
        button_text="View Options",
        sections=[{"title": parent["name"], "rows": rows}],
    )


async def _handle_subcategory_selection(
    db, state, tenant_id, customer_phone, body, tz_name, business_type="salon", tsettings=None
):
    """Handle child service selection from a parent category."""
    services = json.loads(state.services_json)

    selected = None
    if body.startswith("subsvc_"):
        try:
            idx = int(body.replace("subsvc_", ""))
            if 0 <= idx < len(services):
                selected = services[idx]
        except ValueError:
            pass

    if not selected:
        for s in services:
            if s["id"] == body or s["name"].lower() == body.lower():
                selected = s
                break

    if not selected:
        try:
            choice = int(body)
            if 1 <= choice <= len(services):
                selected = services[choice - 1]
        except ValueError:
            pass

    if not selected:
        menu = "\n".join(f"  {i+1}. {s['name']}" for i, s in enumerate(services))
        return TextResponse(
            f"\U0001f4c2 *{state.selected_parent_name}*\n\nPick a service:\n{menu}\n\nReply with a number."
        )

    return await _proceed_after_service(
        db, state, tenant_id, customer_phone, selected, tz_name, business_type, tsettings
    )


async def _handle_utr_submission(
    db: AsyncSession,
    tenant_id: str,
    customer_phone: str,
    booking_ref: str,
    utr_text: str,
) -> TextResponse:
    """Handle customer submitting their UPI UTR/reference number for verification."""
    from app.payments.router import submit_utr_reference

    try:
        res = await submit_utr_reference(db, tenant_id, booking_ref, utr_text)
        await clear_state(tenant_id, customer_phone)
        return TextResponse(
            f"🧾 *Unique Transaction Reference (UTR) Received (`{res['utr_reference']}`)* for booking `{res['booking_ref']}`!\n\n"
            "We've notified the business owner to verify your UPI payment. "
            "You'll receive a WhatsApp confirmation as soon as they approve it."
        )
    except ValueError as exc:
        return TextResponse(str(exc))


async def _show_service_list(services, tenant_id, customer_phone, state, business_type="salon"):
    """Show service selection as list message."""
    labels = get_labels(business_type)
    rows = []
    for i, s in enumerate(services):
        if s.get("child_count", 0) > 0:
            rows.append({
                "id": f"svc_{i}",
                "title": f"\U0001f4c2 {s['name']}"[:24],
                "description": f"{s['child_count']} option{'s' if s['child_count'] != 1 else ''}"[:72],
            })
        else:
            price_str = f" · ₹{s['price'] // 100}" if s.get("price") else ""
            rows.append({
                "id": f"svc_{i}",
                "title": s["name"][:24],
                "description": f"{s['duration']}min{price_str}"[:72],
            })

    state.step = ConversationStep.AWAITING_SERVICE
    await set_state(tenant_id, customer_phone, state)

    return ListResponse(
        body=f"What would you like to {labels['service_action'].lower()}?",
        button_text=f"View {labels['services']}",
        sections=[{"title": labels["services"], "rows": rows}],
    )
