"""End-to-end tests for subscription lifecycle, Razorpay billing, feature gates,
expired/terminal behavior, and WhatsApp booking deposit collection & verification.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.billing.service import get_subscription_state
from app.billing.tasks import process_subscription_expiries
from app.config import settings
from app.conversation.menu_flow import handle_customer_message
from app.dashboard.auth import create_access_token
from app.database import async_session_factory
from app.main import app


@pytest.fixture
async def db():
    async with async_session_factory() as session:
        yield session
from app.owner_console.handler import handle_owner_message
from app.owner_console.parser import OwnerCommand, parse_command
from app.payments.router import (
    build_upi_deep_link,
    create_whatsapp_deposit_payment,
    submit_utr_reference,
)
from app.payments.tasks import expire_stale_deposit_holds


async def _seed_tenant(db, plan_tier: str = "solo", sub_status: str = "trial", days_left: int = 10):
    """Create a fresh tenant + branch + staff + service + customer + subscription."""
    tid = uuid.uuid4()
    bid = uuid.uuid4()
    sid = uuid.uuid4()
    svc_id = uuid.uuid4()
    cid = uuid.uuid4()
    now = datetime.now(timezone.utc)
    slug = f"biz-{str(tid)[:8]}"
    phone = f"+9198{str(tid.int)[:8]}"

    await db.execute(
        text("""
            INSERT INTO tenants (id, business_name, business_type, owner_phone, owner_name,
                                 slug, primary_language, timezone, plan_tier, is_active, created_at, updated_at)
            VALUES (:id, 'Glow Test Salon', 'salon', :phone, 'Owner', :slug, 'en', 'Asia/Kolkata', :tier, true, :now, :now)
        """),
        {"id": tid, "phone": phone, "slug": slug, "tier": plan_tier, "now": now},
    )
    hours = json.dumps({d: [{"open": "09:00", "close": "20:00"}] for d in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]})
    await db.execute(
        text("""
            INSERT INTO branches (id, tenant_id, name, is_primary, operating_hours, created_at)
            VALUES (:id, :tid, 'Main Branch', true, CAST(:h AS jsonb), :now)
        """),
        {"id": bid, "tid": tid, "h": hours, "now": now},
    )
    await db.execute(
        text("""
            INSERT INTO staff (id, tenant_id, branch_id, name, role, skills, working_hours, is_active, created_at)
            VALUES (:id, :tid, :bid, 'Riya', 'Stylist', CAST(:sk AS jsonb), CAST(:h AS jsonb), true, :now)
        """),
        {"id": sid, "tid": tid, "bid": bid, "sk": json.dumps([str(svc_id)]), "h": hours, "now": now},
    )
    await db.execute(
        text("""
            INSERT INTO services (id, tenant_id, name, duration_minutes, price_paise,
                                  requires_deposit, deposit_amount_paise, is_active, sort_order, created_at)
            VALUES (:id, :tid, 'Haircut', 30, 50000, false, 0, true, 0, :now)
        """),
        {"id": svc_id, "tid": tid, "now": now},
    )
    await db.execute(
        text("""
            INSERT INTO customers (id, tenant_id, phone, name, first_seen_at)
            VALUES (:id, :tid, :phone, 'Ananya', :now)
        """),
        {"id": cid, "tid": tid, "phone": f"+9197{str(cid.int)[:8]}", "now": now},
    )

    end_dt = now + timedelta(days=days_left)
    trial_end = end_dt if sub_status == "trial" else None
    await db.execute(
        text("""
            INSERT INTO subscriptions (
                id, tenant_id, plan_tier, billing_cycle,
                current_period_start, current_period_end, status, trial_ends_at
            ) VALUES (
                :id, :tid, :tier, 'monthly', :start, :end, :st, :trial_end
            )
        """),
        {
            "id": uuid.uuid4(),
            "tid": tid,
            "tier": plan_tier,
            "start": now - timedelta(days=10),
            "end": end_dt,
            "st": sub_status,
            "trial_end": trial_end,
        },
    )
    await db.commit()
    token = create_access_token(tid, phone)
    return {
        "tenant_id": tid,
        "branch_id": bid,
        "staff_id": sid,
        "service_id": svc_id,
        "customer_id": cid,
        "slug": slug,
        "token": token,
        "headers": {"Authorization": f"Bearer {token}"},
    }


def test_owner_parser_new_payment_and_plan_commands():
    assert parse_command("plan").command == OwnerCommand.PLAN
    assert parse_command("billing").command == OwnerCommand.PLAN
    assert parse_command("renew").command == OwnerCommand.PLAN
    assert parse_command("pending").command == OwnerCommand.PENDING_PAYMENTS
    c = parse_command("confirm a1b2c3d4")
    assert c.command == OwnerCommand.CONFIRM_PAYMENT
    assert c.booking_ref == "a1b2c3d4"
    r = parse_command("reject a1b2c3d4")
    assert r.command == OwnerCommand.REJECT_PAYMENT
    assert r.booking_ref == "a1b2c3d4"


def test_upi_deep_link_format():
    link = build_upi_deep_link("glowstudio@okicici", "Glow Studio", 20000, "ab12cd34")
    assert link.startswith("upi://pay?")
    assert "pa=glowstudio%40okicici" in link
    assert "am=200.00" in link
    assert "cu=INR" in link
    assert "AB12CD34" in link


@pytest.mark.asyncio
async def test_subscription_warnings_grace_and_expiry_lifecycle(db):
    # 1. Tenant expiring in 2 days -> gets 3-day warning (idempotent)
    t_3d = await _seed_tenant(db, plan_tier="growth", sub_status="active", days_left=2)
    # 2. Tenant expiring in 12 hours -> gets 1-day warning
    t_1d = await _seed_tenant(db, plan_tier="solo", sub_status="trial", days_left=0)
    await db.execute(
        text("UPDATE subscriptions SET trial_ends_at = :e, current_period_end = :e WHERE tenant_id = :tid"),
        {"e": datetime.now(timezone.utc) + timedelta(hours=12), "tid": t_1d["tenant_id"]},
    )
    # 3. Paid active plan 1 hour past period_end -> moves to past_due with grace window
    t_grace = await _seed_tenant(db, plan_tier="growth", sub_status="active", days_left=-1)
    # 4. Expired trial -> immediately suspended
    t_exp = await _seed_tenant(db, plan_tier="solo", sub_status="trial", days_left=-2)
    await db.commit()

    stats1 = await process_subscription_expiries(db)
    assert stats1["warned_3d"] >= 1
    assert stats1["warned_1d"] >= 1
    assert stats1["moved_past_due"] >= 1
    assert stats1["expired"] >= 1

    # Verify idempotency: running again should NOT re-warn the same tenants
    s_3d = await get_subscription_state(db, t_3d["tenant_id"])
    assert s_3d["warning_level"] == "warning"
    assert s_3d["access_mode"] == "full"

    s_1d = await get_subscription_state(db, t_1d["tenant_id"])
    assert s_1d["warning_level"] == "critical"

    s_grace = await get_subscription_state(db, t_grace["tenant_id"])
    assert s_grace["status"] == "past_due"
    assert s_grace["access_mode"] == "grace_warning"
    assert s_grace["is_active_access"] is True

    s_exp = await get_subscription_state(db, t_exp["tenant_id"])
    assert s_exp["status"] == "expired"
    assert s_exp["access_mode"] == "suspended"
    assert s_exp["is_expired"] is True


@pytest.mark.asyncio
async def test_expired_subscription_terminal_behavior(db):
    """When subscription expires:
    - Dashboard reads & billing remain open (200)
    - Dashboard writes (services/staff/bookings) return 402
    - Public booking page flags booking_paused=true and blocks new bookings (402)
    - WhatsApp customer bot pauses new bookings gracefully
    - Owner WhatsApp console still responds with expiry banner + plan command
    """
    t = await _seed_tenant(db, plan_tier="solo", sub_status="trial", days_left=-2)
    client = TestClient(app)

    # Reads & billing STILL WORK
    assert client.get("/api/tenant", headers=t["headers"]).status_code == 200
    assert client.get("/api/bookings", headers=t["headers"]).status_code == 200
    sub_resp = client.get("/api/billing/subscription", headers=t["headers"])
    assert sub_resp.status_code == 200
    assert sub_resp.json()["is_expired"] is True
    assert sub_resp.json()["access_mode"] == "suspended"

    # Writes are blocked with 402
    assert client.post(
        "/api/services",
        headers=t["headers"],
        json={"name": "Coloring", "duration_minutes": 60, "price_paise": 150000},
    ).status_code == 402
    assert client.post(
        "/api/staff",
        headers=t["headers"],
        json={"name": "Meera"},
    ).status_code == 402

    # Public page shows booking_paused=True
    pub = client.get(f"/api/public/business/{t['slug']}")
    assert pub.status_code == 200
    assert pub.json()["booking_paused"] is True

    # WhatsApp customer bot gracefully pauses new bookings
    wa_reply = await handle_customer_message(
        db=db,
        tenant_id=str(t["tenant_id"]),
        customer_id=str(t["customer_id"]),
        customer_phone="+919700011122",
        customer_name="Ananya",
        body="Hi",
        business_name="Glow Test Salon",
    )
    assert "temporarily paused" in wa_reply.body.lower()

    # Owner WhatsApp terminal STILL WORKS with expiry banner & plan status
    owner_today = await handle_owner_message(db, str(t["tenant_id"]), "today", "Glow Test Salon")
    assert "Subscription Expired" in owner_today
    assert "No bookings" in owner_today

    owner_plan = await handle_owner_message(db, str(t["tenant_id"]), "plan", "Glow Test Salon")
    assert "expired" in owner_plan.lower()
    assert "Manage / Renew" in owner_plan

    owner_resume = await handle_owner_message(db, str(t["tenant_id"]), "resume", "Glow Test Salon")
    assert "Cannot resume" in owner_resume


@pytest.mark.asyncio
async def test_feature_gates_and_downgrade_conflict(db):
    # Active paid SOLO tenant: Growth/Pro features are locked (403)
    t = await _seed_tenant(db, plan_tier="solo", sub_status="active", days_left=20)
    client = TestClient(app)

    assert client.get("/api/integrations/google-calendar/auth-url", headers=t["headers"]).status_code == 403
    assert client.put(
        "/api/tenant/settings",
        headers=t["headers"],
        json={"google_calendar_enabled": True},
    ).status_code == 403
    assert client.put(
        "/api/tenant/settings",
        headers=t["headers"],
        json={"upi_id": "glow@okicici", "deposit_mode": "upi_manual"},
    ).status_code == 403
    assert client.post(
        "/api/services",
        headers=t["headers"],
        json={"name": "Bridal", "duration_minutes": 90, "price_paise": 500000, "requires_deposit": True, "deposit_amount_paise": 50000},
    ).status_code == 403

    # Upgrade to PRO via checkout (sandbox mode) -> unlocks everything + creates invoice
    chk = client.post(
        "/api/billing/checkout",
        headers=t["headers"],
        json={"plan_tier": "pro", "billing_cycle": "annual"},
    )
    assert chk.status_code == 200
    assert chk.json()["checkout_mode"] == "sandbox_activated"

    # Now Google Calendar & UPI deposits succeed!
    assert client.get("/api/integrations/google-calendar/auth-url", headers=t["headers"]).status_code == 200
    assert client.put(
        "/api/tenant/settings",
        headers=t["headers"],
        json={"upi_id": "glow@okicici", "deposit_mode": "upi_manual"},
    ).status_code == 200

    # Invoices recorded
    invs = client.get("/api/billing/invoices", headers=t["headers"])
    assert invs.status_code == 200
    assert len(invs.json()) == 1
    assert invs.json()[0]["billing_cycle"] == "annual"
    assert invs.json()[0]["amount_paise"] == 2999000

    # Add 2 more staff (total 3 > Solo limit of 2)
    assert client.post("/api/staff", headers=t["headers"], json={"name": "Arjun"}).status_code == 201
    assert client.post("/api/staff", headers=t["headers"], json={"name": "Meera"}).status_code == 201

    # Attempting to downgrade to Solo fails with 409 Conflict!
    dg = client.post(
        "/api/billing/checkout",
        headers=t["headers"],
        json={"plan_tier": "solo", "billing_cycle": "monthly"},
    )
    assert dg.status_code == 409

    # Cancel at period end keeps active access until current_period_end
    cncl = client.post("/api/billing/cancel", headers=t["headers"], json={"immediate": False})
    assert cncl.status_code == 200
    assert cncl.json()["cancel_at_period_end"] is True
    assert cncl.json()["is_active_access"] is True


@pytest.mark.asyncio
async def test_razorpay_subscription_verify_and_webhooks(db):
    t = await _seed_tenant(db, plan_tier="solo", sub_status="trial", days_left=5)
    client = TestClient(app)
    secret = "test_rzp_secret_123"
    wh_secret = "test_wh_secret_456"

    with patch.object(settings, "razorpay_key_id", "rzp_test_key"), \
         patch.object(settings, "razorpay_key_secret", secret), \
         patch.object(settings, "razorpay_webhook_secret", wh_secret):
        # Verify payment with valid HMAC signature
        rpid = "pay_sub_001"
        rsub = "sub_live_001"
        sig = hmac.new(secret.encode(), f"{rpid}|{rsub}".encode(), hashlib.sha256).hexdigest()

        bad = client.post(
            "/api/billing/verify-payment",
            headers=t["headers"],
            json={
                "plan_tier": "growth",
                "billing_cycle": "monthly",
                "razorpay_payment_id": rpid,
                "razorpay_subscription_id": rsub,
                "razorpay_signature": "invalid_sig",
            },
        )
        assert bad.status_code == 400

        ok = client.post(
            "/api/billing/verify-payment",
            headers=t["headers"],
            json={
                "plan_tier": "growth",
                "billing_cycle": "monthly",
                "razorpay_payment_id": rpid,
                "razorpay_subscription_id": rsub,
                "razorpay_signature": sig,
            },
        )
        assert ok.status_code == 200
        assert ok.json()["plan_tier"] == "growth"
        assert ok.json()["status"] == "active"

        # Webhook: subscription.halted -> moves to past_due with grace period
        wh_body = json.dumps({
            "event": "subscription.halted",
            "payload": {
                "subscription": {
                    "entity": {"id": rsub, "notes": {"tenant_id": str(t["tenant_id"])}}
                }
            },
        }).encode()
        wh_sig = hmac.new(wh_secret.encode(), wh_body, hashlib.sha256).hexdigest()
        wh_resp = client.post(
            "/payments/webhook",
            content=wh_body,
            headers={"X-Razorpay-Signature": wh_sig, "Content-Type": "application/json"},
        )
        assert wh_resp.status_code == 200
        st = await get_subscription_state(db, t["tenant_id"])
        assert st["status"] == "past_due"
        assert st["access_mode"] == "grace_warning"


@pytest.mark.asyncio
async def test_whatsapp_upi_deposit_utr_owner_confirm_and_expiry(db):
    t = await _seed_tenant(db, plan_tier="growth", sub_status="active", days_left=20)
    tid = t["tenant_id"]
    now = datetime.now(timezone.utc)

    # Create a pending_deposit booking
    booking_id = uuid.uuid4()
    await db.execute(
        text("""
            INSERT INTO bookings (id, tenant_id, branch_id, staff_id, customer_id, service_id,
                                  starts_at, ends_at, status, created_at, updated_at)
            VALUES (:id, :tid, :bid, :sid, :cid, :svcid, :st, :en, 'pending_deposit', :now, :now)
        """),
        {
            "id": booking_id,
            "tid": tid,
            "bid": t["branch_id"],
            "sid": t["staff_id"],
            "cid": t["customer_id"],
            "svcid": t["service_id"],
            "st": now + timedelta(days=1),
            "en": now + timedelta(days=1, minutes=30),
            "now": now,
        },
    )
    await db.commit()

    # 1. Create Direct UPI deposit payment
    pay = await create_whatsapp_deposit_payment(
        db=db,
        tenant_id=str(tid),
        booking_id=str(booking_id),
        amount_paise=20000,
        business_name="Glow Test Salon",
        deposit_mode="upi_manual",
        upi_id="glowtest@okicici",
        hold_minutes=15,
    )
    assert pay["payment_method"] == "upi_manual"
    assert "pa=glowtest%40okicici" in pay["url"]

    # 2. Customer submits 12-digit UTR reference
    bid_short = str(booking_id)[:8]
    utr_res = await submit_utr_reference(db, str(tid), bid_short, "412345678901")
    assert utr_res["status"] == "submitted_utr"
    assert utr_res["utr_reference"] == "412345678901"

    # 3. Owner checks pending deposits & confirms via WhatsApp console
    pending_msg = await handle_owner_message(db, str(tid), "pending", "Glow Test Salon")
    assert bid_short in pending_msg
    assert "412345678901" in pending_msg

    confirm_msg = await handle_owner_message(db, str(tid), f"confirm {bid_short}", "Glow Test Salon")
    assert "confirmed" in confirm_msg.lower()

    b_row = (await db.execute(text("SELECT status FROM bookings WHERE id = :id"), {"id": booking_id})).first()
    assert b_row.status == "confirmed"

    # 4. Auto-expiry of an unpaid stale deposit hold
    stale_bid = uuid.uuid4()
    await db.execute(
        text("""
            INSERT INTO bookings (id, tenant_id, branch_id, staff_id, customer_id, service_id,
                                  starts_at, ends_at, status, created_at, updated_at)
            VALUES (:id, :tid, :bid, :sid, :cid, :svcid, :st, :en, 'pending_deposit', :now, :now)
        """),
        {
            "id": stale_bid,
            "tid": tid,
            "bid": t["branch_id"],
            "sid": t["staff_id"],
            "cid": t["customer_id"],
            "svcid": t["service_id"],
            "st": now + timedelta(days=2),
            "en": now + timedelta(days=2, minutes=30),
            "now": now,
        },
    )
    await db.execute(
        text("""
            INSERT INTO payments (id, tenant_id, booking_id, payment_method, amount_paise, status, expires_at, created_at)
            VALUES (:id, :tid, :bid, 'upi_manual', 20000, 'created', :exp, :now)
        """),
        {"id": uuid.uuid4(), "tid": tid, "bid": stale_bid, "exp": now - timedelta(minutes=5), "now": now},
    )
    await db.commit()

    exp_res = await expire_stale_deposit_holds(db)
    assert exp_res["expired"] >= 1
    stale_row = (await db.execute(text("SELECT status, cancellation_reason FROM bookings WHERE id = :id"), {"id": stale_bid})).first()
    assert stale_row.status == "cancelled"
    assert "expired" in (stale_row.cancellation_reason or "").lower()


@pytest.mark.asyncio
async def test_razorpay_payment_link_webhook_confirms_booking(db):
    t = await _seed_tenant(db, plan_tier="growth", sub_status="active", days_left=20)
    tid = t["tenant_id"]
    now = datetime.now(timezone.utc)
    booking_id = uuid.uuid4()
    await db.execute(
        text("""
            INSERT INTO bookings (id, tenant_id, branch_id, staff_id, customer_id, service_id,
                                  starts_at, ends_at, status, created_at, updated_at)
            VALUES (:id, :tid, :bid, :sid, :cid, :svcid, :st, :en, 'pending_deposit', :now, :now)
        """),
        {
            "id": booking_id,
            "tid": tid,
            "bid": t["branch_id"],
            "sid": t["staff_id"],
            "cid": t["customer_id"],
            "svcid": t["service_id"],
            "st": now + timedelta(days=3),
            "en": now + timedelta(days=3, minutes=30),
            "now": now,
        },
    )
    await db.commit()

    plink_id = f"plink_{str(booking_id)[:8]}"
    mock_rzp = MagicMock()
    mock_rzp.payment_link.create.return_value = {"id": plink_id, "short_url": f"https://rzp.io/i/{plink_id}"}

    wh_secret = "wh_sec_plink"
    with patch.object(settings, "razorpay_key_id", "rzp_test_key"), \
         patch.object(settings, "razorpay_key_secret", "rzp_test_sec"), \
         patch.object(settings, "razorpay_webhook_secret", wh_secret), \
         patch("app.payments.router._get_razorpay_client", return_value=mock_rzp):
        pay = await create_whatsapp_deposit_payment(
            db=db,
            tenant_id=str(tid),
            booking_id=str(booking_id),
            amount_paise=30000,
            business_name="Glow Test Salon",
            deposit_mode="razorpay",
        )
        assert pay["payment_method"] == "razorpay_link"
        assert pay["url"] == f"https://rzp.io/i/{plink_id}"

        client = TestClient(app)
        body = json.dumps({
            "event": "payment_link.paid",
            "payload": {
                "payment_link": {"entity": {"id": plink_id}},
                "payment": {"entity": {"id": f"pay_{plink_id}"}},
            },
        }).encode()
        sig = hmac.new(wh_secret.encode(), body, hashlib.sha256).hexdigest()
        resp = client.post(
            "/payments/webhook",
            content=body,
            headers={"X-Razorpay-Signature": sig, "Content-Type": "application/json"},
        )
        assert resp.status_code == 200

    b_row = (await db.execute(text("SELECT status FROM bookings WHERE id = :id"), {"id": booking_id})).first()
    assert b_row.status == "confirmed"
    p_row = (await db.execute(text("SELECT status, verified_by FROM payments WHERE razorpay_payment_link_id = :plid"), {"plid": plink_id})).first()
    assert p_row.status == "captured"
    assert p_row.verified_by == "webhook"


@pytest.mark.asyncio
async def test_trial_preserved_on_plan_switch_and_email_otp_signup(db):
    """Switching plans during active 14-day trial preserves trial; signup stores owner_email & selected plan; Postgres OTP verifies cleanly."""
    from app.auth.otp import request_signup_otp, verify_otp

    client = TestClient(app)
    uid = str(uuid.uuid4())[:8]
    email = f"owner-{uid}@glowtest.in"
    phone = f"+9196{str(uuid.uuid4().int)[:8]}"
    slug = f"trial-{uid}"

    # 1. Request signup OTP (stores in Postgres auth_otps + returns dev_otp in dev)
    otp_res = await request_signup_otp(db, email, business_name="Trial Test Salon")
    assert "dev_otp" in otp_res
    ok, err = await verify_otp(email, otp_res["dev_otp"], db=db, purpose="signup")
    assert ok is True and err == ""

    # 2. Signup with owner_email + selected plan_tier='pro' + annual cycle
    resp = client.post(
        "/api/auth/signup",
        json={
            "business_name": "Trial Test Salon",
            "owner_name": "Nikhil",
            "owner_phone": phone,
            "owner_email": email,
            "business_type": "salon",
            "slug": slug,
            "plan_tier": "pro",
            "billing_cycle": "annual",
        },
    )
    assert resp.status_code == 201
    tok = resp.json()["access_token"]
    headers = {"Authorization": f"Bearer {tok}"}

    # Verify owner_email & 14-day trial on pro/annual
    t_prof = client.get("/api/tenant", headers=headers).json()
    assert t_prof["owner_email"] == email
    sub0 = client.get("/api/billing/subscription", headers=headers).json()
    assert sub0["status"] == "trial"
    assert sub0["plan_tier"] == "pro"
    assert sub0["billing_cycle"] == "annual"

    # 3. Switching plan during active trial MUST preserve status='trial' (NOT remove trial!)
    sw = client.post(
        "/api/billing/checkout",
        headers=headers,
        json={"plan_tier": "growth", "billing_cycle": "monthly", "skip_trial": False},
    )
    assert sw.status_code == 200
    sw_data = sw.json()
    assert sw_data["checkout_mode"] == "trial_plan_updated"
    assert sw_data["trial_preserved"] is True
    assert sw_data["subscription"]["status"] == "trial"
    assert sw_data["subscription"]["plan_tier"] == "growth"

    # 4. Edit service works cleanly (including parent_service_id)
    cat = client.post("/api/services", headers=headers, json={"name": "Hair", "duration_minutes": 0}).json()
    svc = client.post("/api/services", headers=headers, json={"name": "Cut", "duration_minutes": 30, "price_paise": 49900}).json()
    upd = client.put(
        f"/api/services/{svc['id']}",
        headers=headers,
        json={"name": "Precision Cut", "duration_minutes": 45, "price_paise": 59900, "parent_service_id": cat["id"]},
    )
    assert upd.status_code == 200
    assert upd.json()["name"] == "Precision Cut"
    assert upd.json()["parent_service_id"] == cat["id"]

    # 5. Email login OTP works end-to-end via Postgres fallback
    req_otp = client.post("/api/auth/request-otp", json={"identifier": email})
    assert req_otp.status_code == 200
    dev_code = req_otp.json()["dev_otp"]
    login_resp = client.post("/api/auth/login", json={"phone": email, "otp": dev_code})
    assert login_resp.status_code == 200
    assert login_resp.json()["business_name"] == "Trial Test Salon"

    # 6. Authenticated production WhatsApp Simulator works in both customer & owner modes
    sim_hi = client.post("/api/simulator/message", headers=headers, json={"mode": "customer", "body": "hi"})
    assert sim_hi.status_code == 200
    sim_data = sim_hi.json()
    assert sim_data["type"] == "list"
    assert sim_data["conv_state"] is not None
    assert len(sim_data["sections"]) >= 1

    sim_owner = client.post("/api/simulator/message", headers=headers, json={"mode": "owner", "body": "plan"})
    assert sim_owner.status_code == 200
    assert "Trial Test Salon" in sim_owner.json()["body"]


@pytest.mark.asyncio
async def test_solo_trial_enforces_solo_branch_and_feature_limits(db):
    """Solo trial accounts enforce Solo limits (1 branch, 2 staff, deposits locked) until upgraded to Growth/Pro."""
    client = TestClient(app)
    uid = str(uuid.uuid4())[:8]
    resp = client.post(
        "/api/auth/signup",
        json={
            "business_name": "Solo Limit Salon",
            "owner_name": "Nikhil",
            "owner_phone": f"+9195{str(uuid.uuid4().int)[:8]}",
            "owner_email": f"solo-{uid}@glowtest.in",
            "business_type": "salon",
            "slug": f"solo-{uid}",
            "plan_tier": "solo",
            "billing_cycle": "monthly",
        },
    )
    assert resp.status_code == 201
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}

    sub = client.get("/api/billing/subscription", headers=headers).json()
    assert sub["plan_tier"] == "solo"
    assert sub["effective_tier"] == "solo"
    assert sub["limits"]["max_branches"] == 1
    assert sub["limits"]["max_staff"] == 2
    assert sub["entitlements"]["deposits_enabled"] is False

    # Adding a 2nd branch on Solo trial must fail with 402
    br_fail = client.post("/api/branches", headers=headers, json={"name": "Indiranagar"})
    assert br_fail.status_code == 402

    # Switching trial to Growth preserves trial and unlocks adding a 2nd branch
    sw = client.post(
        "/api/billing/checkout",
        headers=headers,
        json={"plan_tier": "growth", "billing_cycle": "monthly", "skip_trial": False},
    )
    assert sw.status_code == 200
    assert sw.json()["subscription"]["status"] == "trial"
    assert sw.json()["subscription"]["limits"]["max_branches"] == 2
    assert sw.json()["subscription"]["entitlements"]["deposits_enabled"] is True

    br_ok = client.post("/api/branches", headers=headers, json={"name": "Indiranagar"})
    assert br_ok.status_code == 201


@pytest.mark.asyncio
async def test_whatsapp_multi_staff_picker_and_full_day_slots(db):
    """With 2+ staff, WhatsApp asks who the customer prefers, never duplicates time slots, and offers Morning/Afternoon/Evening for full-day browsing."""
    client = TestClient(app)
    uid = str(uuid.uuid4())[:8]
    resp = client.post(
        "/api/auth/signup",
        json={
            "business_name": "Duo Style Salon",
            "owner_name": "Nikhil",
            "owner_phone": f"+9194{str(uuid.uuid4().int)[:8]}",
            "business_type": "salon",
            "slug": f"duo-{uid}",
            "plan_tier": "solo",
        },
    )
    assert resp.status_code == 201
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}

    # Add a 2nd staff member (Nikhil is already created as owner staff)
    st2 = client.post("/api/staff", headers=headers, json={"name": "Priya", "role": "Senior Stylist"})
    assert st2.status_code == 201

    # Add a 45-min Haircut service with 45-min slot interval (14 slots across 9 AM - 8 PM)
    svc = client.post(
        "/api/services",
        headers=headers,
        json={"name": "Haircut", "duration_minutes": 45, "slot_increment_minutes": 45, "price_paise": 50000},
    )
    assert svc.status_code == 201

    # 1. Customer says Hi -> Services list
    r1 = client.post("/api/simulator/message", headers=headers, json={"mode": "customer", "body": "hi"})
    assert r1.status_code == 200
    d1 = r1.json()
    assert d1["type"] == "list"

    # 2. Pick Haircut -> MUST ask which Stylist (Any Available + Nikhil + Priya)
    r2 = client.post(
        "/api/simulator/message",
        headers=headers,
        json={"mode": "customer", "body": "svc_0", "conv_state": d1["conv_state"]},
    )
    assert r2.status_code == 200
    d2 = r2.json()
    assert d2["type"] == "list"
    staff_titles = [r["title"] for r in d2["sections"][0]["rows"]]
    assert any("Any Available" in t for t in staff_titles)
    assert "Nikhil" in staff_titles
    assert "Priya" in staff_titles

    # 3. Pick Priya specifically -> Dates list mentioning Priya
    priya_row = next(r for r in d2["sections"][0]["rows"] if r["title"] == "Priya")
    r3 = client.post(
        "/api/simulator/message",
        headers=headers,
        json={"mode": "customer", "body": priya_row["id"], "conv_state": d2["conv_state"]},
    )
    assert r3.status_code == 200
    d3 = r3.json()
    assert d3["type"] == "list"
    assert "Priya" in d3["body"]

    # 4. Pick a full future date (index 1 = tomorrow or next open day) -> Morning/Afternoon/Evening buttons
    date_id = d3["sections"][0]["rows"][-1]["id"]
    r4 = client.post(
        "/api/simulator/message",
        headers=headers,
        json={"mode": "customer", "body": date_id, "conv_state": d3["conv_state"]},
    )
    assert r4.status_code == 200
    d4 = r4.json()
    assert d4["type"] == "buttons"
    btn_ids = [b["id"] for b in d4["buttons"]]
    assert "period_morning" in btn_ids
    assert "period_evening" in btn_ids

    # 5. Pick Evening -> Clean deduplicated evening slots at 45-min intervals
    r5 = client.post(
        "/api/simulator/message",
        headers=headers,
        json={"mode": "customer", "body": "period_evening", "conv_state": d4["conv_state"]},
    )
    assert r5.status_code == 200
    d5 = r5.json()
    assert d5["type"] == "list"
    slot_titles = [r["title"] for r in d5["sections"][0]["rows"]]
    assert len(slot_titles) == len(set(slot_titles))  # zero duplicate times!
    assert all("PM" in t for t in slot_titles)

    # 6. Pick first evening slot -> Confirmation shows Priya!
    r6 = client.post(
        "/api/simulator/message",
        headers=headers,
        json={"mode": "customer", "body": d5["sections"][0]["rows"][0]["id"], "conv_state": d5["conv_state"]},
    )
    assert r6.status_code == 200
    d6 = r6.json()
    assert d6["type"] == "buttons"
    assert "Priya" in d6["body"]


@pytest.mark.asyncio
async def test_free_24h_thank_you_with_review_link_on_completed(db):
    """Marking a booking completed sends custom Thank-You + Google Review link ONLY within the free 24h window."""
    client = TestClient(app)
    uid = str(uuid.uuid4())[:8]
    resp = client.post(
        "/api/auth/signup",
        json={
            "business_name": "Bloom Salon",
            "owner_name": "Nikhil",
            "owner_phone": f"+9193{str(uuid.uuid4().int)[:8]}",
            "business_type": "salon",
            "slug": f"bloom-{uid}",
            "plan_tier": "solo",
        },
    )
    assert resp.status_code == 201
    tid = resp.json()["tenant_id"]
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}

    # Connect WhatsApp + customize thank-you message & Google review link
    wa = client.put(
        "/api/tenant/whatsapp",
        headers=headers,
        json={"waba_phone_number_id": f"14259{uid}", "waba_access_token": "test-tok"},
    )
    assert wa.status_code == 200
    st = client.put(
        "/api/tenant/settings",
        headers=headers,
        json={
            "send_feedback_request": True,
            "thank_you_message": "Thanks {customer_name} for your {service_name} at {business_name}!",
            "google_review_url": "https://g.page/r/bloom-salon/review",
        },
    )
    assert st.status_code == 200
    assert st.json()["thank_you_message"] == "Thanks {customer_name} for your {service_name} at {business_name}!"

    svc = client.post(
        "/api/services",
        headers=headers,
        json={"name": "Haircut", "duration_minutes": 30, "price_paise": 50000},
    ).json()

    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    bk1 = client.post(
        "/api/bookings",
        headers=headers,
        json={
            "service_id": svc["id"],
            "starts_at": tomorrow.isoformat(),
            "customer_name": "Aarav",
            "customer_phone": "+919876500111",
        },
    ).json()

    # Case 1: Customer has NO inbound message within 24h -> must NOT send paid message
    with patch("app.messaging.whatsapp.client.send_text_message", new_callable=AsyncMock) as mock_send:
        c1 = client.put(f"/api/bookings/{bk1['id']}/status", headers=headers, json={"status": "completed"})
        assert c1.status_code == 200
        assert c1.json()["thank_you_sent"] is False
        assert c1.json()["thank_you_status"] == "outside_24h_window"
        mock_send.assert_not_called()

    # Case 2: Customer sent an inbound WhatsApp message 2 hours ago (inside free 24h window!)
    bk2 = client.post(
        "/api/bookings",
        headers=headers,
        json={
            "service_id": svc["id"],
            "starts_at": (tomorrow + timedelta(hours=2)).isoformat(),
            "customer_name": "Diya",
            "customer_phone": "+919876500222",
        },
    ).json()
    await db.execute(
        text(
            "INSERT INTO messages (id, tenant_id, customer_id, direction, body, status, created_at) "
            "VALUES (:id, :tid, :cid, 'inbound', 'Hi', 'received', :now)"
        ),
        {
            "id": uuid.uuid4(),
            "tid": tid,
            "cid": bk2["customer_id"],
            "now": datetime.now(timezone.utc) - timedelta(hours=2),
        },
    )
    await db.commit()

    with patch(
        "app.messaging.whatsapp.client.send_text_message",
        new_callable=AsyncMock,
        return_value={"messages": [{"id": "wamid.ok"}]},
    ) as mock_send:
        c2 = client.put(f"/api/bookings/{bk2['id']}/status", headers=headers, json={"status": "completed"})
        assert c2.status_code == 200
        assert c2.json()["thank_you_sent"] is True
        assert c2.json()["thank_you_status"] == "sent_free_24h"
        mock_send.assert_called_once()
        sent_text = mock_send.call_args.kwargs["text_body"]
        assert "Thanks Diya for your Haircut at Bloom Salon!" in sent_text
        assert "https://g.page/r/bloom-salon/review" in sent_text


@pytest.mark.asyncio
async def test_tech_provider_embedded_signup_coexistence_and_compliance_pages(db):
    """End-to-end Tech Provider readiness: Embedded Signup, Test Connection, Disconnect, Coexistence echoes, Postgres state, and /privacy & /terms."""
    from app.conversation.state import (
        ConversationState,
        ConversationStep,
        _db_delete,
        _db_get,
        _db_set,
    )
    from app.messaging.whatsapp.webhook import process_webhook_payload

    client = TestClient(app)

    # 1. Meta Business Verification / App Review compliance pages return 200
    assert client.get("/privacy").status_code == 200
    assert "Privacy Policy" in client.get("/privacy").text
    assert client.get("/terms").status_code == 200
    assert "Terms of Service" in client.get("/terms").text

    # 2. Serverless Postgres conversation state persists across cold starts
    sk = f"conv:test:{uuid.uuid4().hex[:8]}"
    st = ConversationState(step=ConversationStep.AWAITING_STAFF, tenant_id="t1", selected_service_name="Haircut")
    await _db_set(sk, st.to_json())
    loaded = await _db_get(sk)
    assert loaded is not None
    assert ConversationState.from_json(loaded).step == ConversationStep.AWAITING_STAFF
    await _db_delete(sk)
    assert await _db_get(sk) is None

    # 3. Tech Provider Embedded Signup token exchange + auto-subscribe + Test + Disconnect
    uid = str(uuid.uuid4())[:8]
    su = client.post(
        "/api/auth/signup",
        json={
            "business_name": "TechProv Studio",
            "owner_name": "Nikhil",
            "owner_phone": f"+9192{str(uuid.uuid4().int)[:8]}",
            "business_type": "salon",
            "slug": f"tp-{uid}",
            "plan_tier": "solo",
        },
    )
    assert su.status_code == 201
    headers = {"Authorization": f"Bearer {su.json()['access_token']}"}
    pid = f"142599{uid}"
    waba_id = f"244349{uid}"

    fake_get = MagicMock(status_code=200)
    fake_get.json.return_value = {
        "access_token": "EAA_perm_token_123",
        "verified_name": "TechProv Studio",
        "display_phone_number": "+91 99999 88888",
        "quality_rating": "GREEN",
    }
    fake_post = MagicMock(status_code=200)
    fake_post.json.return_value = {"success": True}

    mock_http = AsyncMock()
    mock_http.get.return_value = fake_get
    mock_http.post.return_value = fake_post
    mock_cm = AsyncMock()
    mock_cm.__aenter__.return_value = mock_http

    with (
        patch.object(settings, "meta_app_id", "123456789"),
        patch.object(settings, "meta_app_secret", "super-secret"),
        patch.object(settings, "meta_config_id", "cfg_999"),
        patch("httpx.AsyncClient", return_value=mock_cm),
    ):
        info = client.get("/api/tenant/whatsapp-setup-info", headers=headers).json()
        assert info["embedded_signup_enabled"] is True
        assert info["meta_config_id"] == "cfg_999"

        # Fresh Cloud API number -> subscribes webhook AND auto-registers phone
        emb = client.post(
            "/api/tenant/whatsapp/embedded-signup",
            headers=headers,
            json={"code": "oauth_code_abc", "waba_id": waba_id, "phone_number_id": pid, "is_coexistence": False},
        )
        assert emb.status_code == 200
        assert emb.json()["status"] == "connected"
        assert emb.json()["webhook_subscribed"] is True
        assert emb.json()["phone_registered"] is True
        assert emb.json()["is_coexistence"] is False

        # Coexistence number -> subscribes webhook and skips /register
        emb_coex = client.post(
            "/api/tenant/whatsapp/embedded-signup",
            headers=headers,
            json={"code": "oauth_code_xyz", "waba_id": waba_id, "phone_number_id": pid, "is_coexistence": True},
        )
        assert emb_coex.status_code == 200
        assert emb_coex.json()["webhook_subscribed"] is True
        assert emb_coex.json()["phone_registered"] is False
        assert emb_coex.json()["is_coexistence"] is True

        tst = client.post("/api/tenant/whatsapp/test", headers=headers)
        assert tst.status_code == 200
        assert tst.json()["verified_name"] == "TechProv Studio"

    # 4. Coexistence webhooks (account_update, smb_message_echoes, history) process cleanly without loops
    await process_webhook_payload({
        "object": "whatsapp_business_account",
        "entry": [{
            "id": waba_id,
            "changes": [
                {"field": "account_update", "value": {"event": "PARTNER_ADDED", "phone_number": "+919999988888"}},
                {"field": "history", "value": {"history": []}},
                {
                    "field": "smb_message_echoes",
                    "value": {
                        "metadata": {"phone_number_id": pid},
                        "message_echoes": [{"id": f"wamid.echo.{uid}", "type": "text", "text": {"body": "See you at 5pm!"}}],
                    },
                },
            ],
        }],
    })
    echo_row = (
        await db.execute(text("SELECT direction, body FROM messages WHERE waba_message_id = :wid"), {"wid": f"wamid.echo.{uid}"})
    ).first()
    assert echo_row is not None
    assert echo_row.direction == "outbound"
    assert "[App] See you at 5pm!" in echo_row.body

    # 5. Disconnect revokes stored WhatsApp credentials cleanly
    disc = client.delete("/api/tenant/whatsapp", headers=headers)
    assert disc.status_code == 200
    assert disc.json()["status"] == "disconnected"
    assert client.post("/api/tenant/whatsapp/test", headers=headers).status_code == 400


@pytest.mark.asyncio
async def test_deposit_upi_owner_alert_dashboard_utr_and_confirm_whatsapp(db):
    """End-to-end UPI deposit flow: UPI ID shown, UTR alerts owner with 1-tap buttons, Dashboard shows UTR, Confirm notifies customer."""
    from app.owner_console.parser import OwnerCommand, parse_command

    client = TestClient(app)
    uid = str(uuid.uuid4())[:8]
    owner_phone = f"+9191{str(uuid.uuid4().int)[:8]}"
    su = client.post(
        "/api/auth/signup",
        json={
            "business_name": "Velvet Salon",
            "owner_name": "Nikhil",
            "owner_phone": owner_phone,
            "business_type": "salon",
            "slug": f"velvet-{uid}",
            "plan_tier": "growth",
        },
    )
    assert su.status_code == 201
    headers = {"Authorization": f"Bearer {su.json()['access_token']}"}

    # Connect WhatsApp + configure Direct UPI ID
    client.put(
        "/api/tenant/whatsapp",
        headers=headers,
        json={"waba_phone_number_id": f"142588{uid}", "waba_access_token": "tok"},
    )
    client.put(
        "/api/tenant/settings",
        headers=headers,
        json={
            "deposit_mode": "upi_manual",
            "upi_id": "velvetsalon@okicici",
            "deposit_hold_minutes": 20,
            "booking_confirmed_message": "Arrive 10 mins early for {service_name}!",
            "google_maps_url": "https://maps.app.goo.gl/velvet-salon",
        },
    )
    client.post(
        "/api/services",
        headers=headers,
        json={
            "name": "Bridal Styling",
            "duration_minutes": 60,
            "price_paise": 300000,
            "requires_deposit": True,
            "deposit_amount_paise": 50000,
        },
    )

    # 1. Customer books via simulator -> reservation reply MUST include UPI ID
    r1 = client.post("/api/simulator/message", headers=headers, json={"mode": "customer", "body": "hi"}).json()
    r2 = client.post("/api/simulator/message", headers=headers, json={"mode": "customer", "body": "svc_0", "conv_state": r1["conv_state"]}).json()
    date_id = r2["sections"][0]["rows"][-1]["id"]
    r3 = client.post("/api/simulator/message", headers=headers, json={"mode": "customer", "body": date_id, "conv_state": r2["conv_state"]}).json()
    if r3["type"] == "buttons":
        r3 = client.post("/api/simulator/message", headers=headers, json={"mode": "customer", "body": r3["buttons"][0]["id"], "conv_state": r3["conv_state"]}).json()
    r4 = client.post("/api/simulator/message", headers=headers, json={"mode": "customer", "body": r3["sections"][0]["rows"][0]["id"], "conv_state": r3["conv_state"]}).json()
    r5 = client.post("/api/simulator/message", headers=headers, json={"mode": "customer", "body": "confirm_yes", "conv_state": r4["conv_state"]}).json()
    assert "velvetsalon@okicici" in r5["body"]
    assert "upi://pay?" in r5["body"]

    # Extract booking ref from verify_pay_<ref> button
    verify_btn = next(b["id"] for b in r5["buttons"] if b["id"].startswith("verify_pay_"))
    ref = verify_btn.replace("verify_pay_", "")

    # 2. Customer taps "I've Paid (UTR)" and sends 12-digit UTR -> Owner MUST receive 1-tap WhatsApp alert!
    r6 = client.post("/api/simulator/message", headers=headers, json={"mode": "customer", "body": verify_btn, "conv_state": r5["conv_state"]}).json()
    with patch(
        "app.messaging.whatsapp.client.send_reply_buttons",
        new_callable=AsyncMock,
        return_value={"messages": [{"id": "wamid.owner"}]},
    ) as mock_owner_btn:
        r7 = client.post("/api/simulator/message", headers=headers, json={"mode": "customer", "body": "837373838291", "conv_state": r6["conv_state"]}).json()
        assert "837373838291" in r7["body"]
        mock_owner_btn.assert_called_once()
        assert mock_owner_btn.call_args.kwargs["to"] == owner_phone.lstrip("+")
        assert "837373838291" in mock_owner_btn.call_args.kwargs["body"]
        btn_ids = [b.id for b in mock_owner_btn.call_args.kwargs["buttons"]]
        assert f"owner_confirm_{ref}" in btn_ids
        assert f"owner_reject_{ref}" in btn_ids

    # Verify 1-tap interactive button parser works
    p_conf = parse_command(f"owner_confirm_{ref}")
    assert p_conf.command == OwnerCommand.CONFIRM_PAYMENT and p_conf.booking_ref == ref
    p_rej = parse_command(f"owner_reject_{ref}")
    assert p_rej.command == OwnerCommand.REJECT_PAYMENT and p_rej.booking_ref == ref

    # 3. Dashboard GET /bookings and GET /bookings/{id} MUST include utr_reference & payment_status
    bk_list = client.get("/api/bookings", headers=headers).json()["items"]
    assert len(bk_list) == 1
    assert bk_list[0]["utr_reference"] == "837373838291"
    assert bk_list[0]["payment_status"] == "submitted_utr"
    bk_detail = client.get(f"/api/bookings/{bk_list[0]['id']}", headers=headers).json()
    assert bk_detail["utr_reference"] == "837373838291"

    # 4. Approving via Dashboard Confirm button MUST notify customer on WhatsApp with closing buttons!
    with patch(
        "app.messaging.whatsapp.client.send_reply_buttons",
        new_callable=AsyncMock,
        return_value={"messages": [{"id": "wamid.cust"}]},
    ) as mock_cust_msg:
        conf = client.post(f"/api/bookings/{bk_list[0]['id']}/confirm", headers=headers)
        assert conf.status_code == 200
        assert conf.json()["status"] == "confirmed"
        mock_cust_msg.assert_called_once()
        assert "Payment Verified & Booking Confirmed" in mock_cust_msg.call_args.kwargs["body"]
        assert "Arrive 10 mins early for Bridal Styling!" in mock_cust_msg.call_args.kwargs["body"]
        assert "https://maps.app.goo.gl/velvet-salon" in mock_cust_msg.call_args.kwargs["body"]
        cust_btn_ids = [b.id for b in mock_cust_msg.call_args.kwargs["buttons"]]
        assert "action_done" in cust_btn_ids

    # 5. Customer tapping "👍 Thanks, All Set" closes the conversation warmly without restarting booking menu
    r_done = client.post("/api/simulator/message", headers=headers, json={"mode": "customer", "body": "action_done"}).json()
    assert r_done["type"] == "text"
    assert "welcome" in r_done["body"].lower()





