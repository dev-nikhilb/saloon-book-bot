"""Tests for MicroSaaS upgrades & bug fixes:
- Service reorder route shadowing fix & GET /api/services/{id}
- Fernet token encryption & backward compatibility
- Real OTP request & verification flow
- Multi-vertical templates (salons, clinics, dental, spas, tutors, etc.)
- Public shareable booking page (/book/{slug}, slots, booking, double-booking guard, subdomain)
- Manual bookings, booking notes/reason, pending deposit confirmation, state machine enforcement
- Plan limits, trials, multi-branch CRUD, and billing endpoints
- Payment endpoint auth & URL encoding
- Owner console `week` command & WhatsApp deposit flow
"""
from __future__ import annotations

import random
import string
from datetime import date, timedelta

from app.crypto import decrypt_secret, encrypt_secret
from app.owner_console.parser import OwnerCommand, parse_command
from app.payments.router import generate_payment_link


def random_phone() -> str:
    return "+91" + "".join(random.choices(string.digits, k=10))


def random_slug() -> str:
    return "biz-" + "".join(random.choices(string.ascii_lowercase, k=8))


def signup(client, business_type: str = "clinic", business_name: str = "Mehta Clinic", plan_tier: str = "solo") -> dict:
    payload = {
        "business_name": business_name,
        "owner_name": "Dr. Mehta",
        "owner_phone": random_phone(),
        "business_type": business_type,
        "slug": random_slug(),
        "plan_tier": plan_tier,
    }
    resp = client.post("/api/auth/signup", json=payload)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    data["owner_phone"] = payload["owner_phone"]
    data["headers"] = {"Authorization": f"Bearer {data['access_token']}"}
    return data


class TestSecurityAndBugFixes:
    def test_crypto_roundtrip_and_legacy_fallback(self):
        secret = "EAAB_test_waba_token_123456"
        enc = encrypt_secret(secret)
        assert enc != secret.encode("utf-8")
        assert decrypt_secret(enc) == secret
        # Legacy plaintext bytes still decode gracefully
        assert decrypt_secret(b"legacy-plain-token") == "legacy-plain-token"
        assert decrypt_secret(None) is None

    def test_service_reorder_route_not_shadowed(self, client):
        biz = signup(client, "salon", "Reorder Salon")
        s1 = client.post("/api/services", headers=biz["headers"], json={"name": "Cut", "duration_minutes": 30, "sort_order": 1}).json()
        s2 = client.post("/api/services", headers=biz["headers"], json={"name": "Color", "duration_minutes": 60, "sort_order": 2}).json()

        # Previously shadowed by PUT /api/services/{service_id} -> 400 Invalid service_id: reorder
        resp = client.put("/api/services/reorder", headers=biz["headers"], json={
            "items": [{"id": s1["id"], "sort_order": 10}, {"id": s2["id"], "sort_order": 5}]
        })
        assert resp.status_code == 200
        assert resp.json()["message"] == "Services reordered"

        # GET /api/services/{id} also works
        get_resp = client.get(f"/api/services/{s1['id']}", headers=biz["headers"])
        assert get_resp.status_code == 200
        assert get_resp.json()["sort_order"] == 10

    def test_update_tenant_business_type(self, client):
        biz = signup(client, "salon", "Adaptive Biz")
        resp = client.put("/api/tenant", headers=biz["headers"], json={"business_type": "dental"})
        assert resp.status_code == 200
        assert resp.json()["business_type"] == "dental"

    def test_otp_request_and_strict_verification(self, client):
        biz = signup(client, "clinic", "OTP Clinic")
        req_resp = client.post("/api/auth/request-otp", json={"phone": biz["owner_phone"]})
        assert req_resp.status_code == 200
        data = req_resp.json()
        assert data["sent"] is True
        dev_otp = data["dev_otp"]
        assert len(dev_otp) == 6

        # Wrong OTP must be rejected once an OTP was requested
        wrong = "000000" if dev_otp != "000000" else "111111"
        bad_resp = client.post("/api/auth/login", json={"phone": biz["owner_phone"], "otp": wrong})
        assert bad_resp.status_code == 401

        # Correct OTP succeeds
        ok_resp = client.post("/api/auth/login", json={"phone": biz["owner_phone"], "otp": dev_otp})
        assert ok_resp.status_code == 200
        assert ok_resp.json()["access_token"]

    def test_payments_endpoints_require_auth_and_encode_url(self, client):
        assert client.post("/payments/create-order", json={"booking_id": "x", "amount_paise": 5000}).status_code in (401, 403)
        assert client.post("/payments/verify", json={"razorpay_payment_id": "p", "razorpay_order_id": "o", "razorpay_signature": "s"}).status_code in (401, 403)
        assert client.get("/payments/00000000-0000-0000-0000-000000000000/link").status_code in (401, 403)

        link = generate_payment_link("order_123", 50000, "Dr. Rao & Sons Clinic")
        assert "Dr.+Rao+%26+Sons+Clinic" in link or "Dr.%20Rao%20%26%20Sons%20Clinic" in link

    def test_owner_week_command_parsed(self):
        assert parse_command("week").command == OwnerCommand.WEEK
        assert parse_command("this week").command == OwnerCommand.WEEK
        assert parse_command("weekly").command == OwnerCommand.WEEK


class TestMultiVerticalAndPublicBooking:
    def test_list_verticals_and_apply_clinic_template(self, client):
        v_resp = client.get("/api/verticals")
        assert v_resp.status_code == 200
        keys = {v["key"] for v in v_resp.json()}
        assert {"salon", "clinic", "dental", "spa", "fitness", "tutor", "consulting", "diagnostics", "veterinary", "home_services"}.issubset(keys)

        clinic = signup(client, "clinic", "Apollo Care Clinic")
        tpl_resp = client.post("/api/onboarding/apply-template", headers=clinic["headers"], json={"business_type": "clinic"})
        assert tpl_resp.status_code == 201
        tpl = tpl_resp.json()
        assert tpl["created_count"] >= 3
        names = [s["name"] for s in tpl["services"]]
        assert "General Consultation" in names

        # Applying again is idempotent (no duplicate services created)
        tpl_resp2 = client.post("/api/onboarding/apply-template", headers=clinic["headers"], json={"business_type": "clinic"})
        assert tpl_resp2.json()["created_count"] == 0

    def test_public_booking_page_and_end_to_end_booking_with_conflict_guard(self, client):
        clinic = signup(client, "clinic", "Sunrise Health Clinic")
        client.post("/api/onboarding/apply-template", headers=clinic["headers"], json={"business_type": "clinic"})

        # Public HTML page served
        html_resp = client.get(f"/book/{clinic['slug']}")
        assert html_resp.status_code == 200
        assert "Tivoria" in html_resp.text

        # Public business profile has clinic vocabulary
        pub_resp = client.get(f"/api/public/business/{clinic['slug']}")
        assert pub_resp.status_code == 200
        pub = pub_resp.json()
        assert pub["labels"]["staff"] == "Doctor"
        assert pub["labels"]["customer"] == "Patient"
        assert pub["vertical"]["collect_reason"] is True
        service_id = pub["services"][0]["id"]

        # Find a future working day (Monday-Saturday)
        target = date.today() + timedelta(days=1)
        if target.weekday() == 6:  # Sunday closed by default
            target += timedelta(days=1)

        slots_resp = client.get(
            f"/api/public/business/{clinic['slug']}/slots",
            params={"service_id": service_id, "date": target.isoformat()},
        )
        assert slots_resp.status_code == 200
        slots = slots_resp.json()["slots"]
        assert len(slots) > 0
        chosen = slots[0]

        # Patient books slot with symptoms/reason
        book_resp = client.post(f"/api/public/business/{clinic['slug']}/book", json={
            "service_id": service_id,
            "staff_id": chosen["staff_id"],
            "starts_at": chosen["start"],
            "customer_name": "Aarav Patel",
            "customer_phone": "9876501234",
            "reason": "Persistent cough for 3 days",
        })
        assert book_resp.status_code == 201, book_resp.text
        booking = book_resp.json()
        assert booking["status"] == "confirmed"
        assert booking["reason"] == "Persistent cough for 3 days"

        # Double booking the exact same slot must return 409 Conflict
        dup_resp = client.post(f"/api/public/business/{clinic['slug']}/book", json={
            "service_id": service_id,
            "staff_id": chosen["staff_id"],
            "starts_at": chosen["start"],
            "customer_name": "Another Patient",
            "customer_phone": "9876509999",
        })
        assert dup_resp.status_code == 409

        # Doctor adds post-visit notes and marks completed
        notes_resp = client.put(
            f"/api/bookings/{booking['id']}",
            headers=clinic["headers"],
            json={"notes": "Prescribed steam inhalation & rest"},
        )
        assert notes_resp.status_code == 200
        assert notes_resp.json()["notes"] == "Prescribed steam inhalation & rest"

        comp_resp = client.put(
            f"/api/bookings/{booking['id']}/status",
            headers=clinic["headers"],
            json={"status": "completed"},
        )
        assert comp_resp.status_code == 200
        assert comp_resp.json()["status"] == "completed"

        # Terminal state transition must be rejected with 409 Conflict
        invalid_resp = client.put(
            f"/api/bookings/{booking['id']}/status",
            headers=clinic["headers"],
            json={"status": "cancelled"},
        )
        assert invalid_resp.status_code == 409

    def test_manual_booking_and_pending_deposit_confirm(self, client):
        salon = signup(client, "salon", "Glow Studio", plan_tier="growth")
        svc = client.post("/api/services", headers=salon["headers"], json={
            "name": "Bridal Makeup",
            "duration_minutes": 90,
            "price_paise": 500000,
            "requires_deposit": True,
            "deposit_amount_paise": 100000,
        }).json()

        tomorrow = (date.today() + timedelta(days=2)).isoformat()
        create_resp = client.post("/api/bookings", headers=salon["headers"], json={
            "service_id": svc["id"],
            "starts_at": f"{tomorrow}T11:00:00+05:30",
            "customer_name": "Neha",
            "customer_phone": "9988776655",
            "requires_deposit": True,
            "reason": "Wedding trial",
        })
        assert create_resp.status_code == 201
        bk = create_resp.json()
        assert bk["status"] == "pending_deposit"

        # Confirm pending deposit manually via POST /api/bookings/{id}/confirm
        confirm_resp = client.post(f"/api/bookings/{bk['id']}/confirm", headers=salon["headers"])
        assert confirm_resp.status_code == 200
        assert confirm_resp.json()["status"] == "confirmed"


class TestBillingPlansAndBranches:
    def test_billing_trial_plan_limits_and_multi_branch(self, client):
        biz = signup(client, "dental", "Bright Smile Dental")

        # Trial subscription auto-provisioned on signup
        sub_resp = client.get("/api/billing/subscription", headers=biz["headers"])
        assert sub_resp.status_code == 200
        sub = sub_resp.json()
        assert sub["status"] == "trial"
        assert sub["usage"]["staff"] == 1
        assert sub["usage"]["branches"] == 1

        # Switch to active solo plan (max_staff=2, max_branches=1)
        plan_resp = client.put("/api/billing/plan", headers=biz["headers"], json={"plan_tier": "solo", "billing_cycle": "monthly"})
        assert plan_resp.status_code == 200
        assert plan_resp.json()["status"] == "active"

        # Adding 2nd branch on solo plan must return 402
        br_fail = client.post("/api/branches", headers=biz["headers"], json={"name": "East Wing"})
        assert br_fail.status_code == 402

        # Upgrade to Pro (unlimited branches & staff)
        pro_resp = client.put("/api/billing/plan", headers=biz["headers"], json={"plan_tier": "pro", "billing_cycle": "annual"})
        assert pro_resp.status_code == 200
        assert pro_resp.json()["limits"]["max_branches"] is None

        # Now creating and deleting a secondary branch works
        br_ok = client.post("/api/branches", headers=biz["headers"], json={"name": "East Wing", "address": "Indiranagar"})
        assert br_ok.status_code == 201
        br_id = br_ok.json()["id"]

        del_resp = client.delete(f"/api/branches/{br_id}", headers=biz["headers"])
        assert del_resp.status_code == 200

        # Primary branch cannot be deleted
        branches = client.get("/api/branches", headers=biz["headers"]).json()
        primary_id = [b for b in branches if b["is_primary"]][0]["id"]
        assert client.delete(f"/api/branches/{primary_id}", headers=biz["headers"]).status_code == 400


class TestWhatsAppDepositFlow:
    def test_deposit_service_reports_pending_deposit_not_confirmed(self, client):
        seed = client.post("/dev/seed").json()
        cust_phone = random_phone()

        # Service index 2 in seed is Hair Color (requires_deposit=True, ₹500 deposit)
        hair_color_id = seed["service_ids"][2]
        client.post("/dev/simulate", json={"from_phone": cust_phone, "body": "hi", "to_tenant_phone": seed["owner_phone"]})
        r_svc = client.post("/dev/simulate", json={"from_phone": cust_phone, "body": hair_color_id, "to_tenant_phone": seed["owner_phone"]}).json()
        if "who would you prefer" in r_svc["message"].lower():
            client.post("/dev/simulate", json={"from_phone": cust_phone, "body": "staff_any", "to_tenant_phone": seed["owner_phone"]})
        r_date = client.post("/dev/simulate", json={"from_phone": cust_phone, "body": "date_1", "to_tenant_phone": seed["owner_phone"]}).json()
        if "part of the day" in r_date["message"].lower():
            client.post("/dev/simulate", json={"from_phone": cust_phone, "body": "period_morning", "to_tenant_phone": seed["owner_phone"]})
        client.post("/dev/simulate", json={"from_phone": cust_phone, "body": "slot_0", "to_tenant_phone": seed["owner_phone"]})
        final = client.post("/dev/simulate", json={"from_phone": cust_phone, "body": "confirm_yes", "to_tenant_phone": seed["owner_phone"]}).json()

        assert "deposit required" in final["message"].lower()
        assert "500" in final["message"]
