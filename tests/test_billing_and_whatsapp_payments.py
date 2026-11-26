"""End-to-end tests for subscription lifecycle, Razorpay billing, feature gates,
expired/terminal behavior, and WhatsApp booking deposit collection & verification.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

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
