"""Test tenant profile and settings API endpoints."""

import random
import string

import pytest


def random_phone():
    return "+91" + "".join(random.choices(string.digits, k=10))


def random_slug():
    return "test-" + "".join(random.choices(string.ascii_lowercase, k=8))


def signup_and_get_token(client, plan_tier: str = "solo") -> tuple[str, str]:
    """Returns (token, tenant_id)."""
    resp = client.post("/api/auth/signup", json={
        "business_name": "Test Salon",
        "owner_name": "Tester",
        "owner_phone": random_phone(),
        "business_type": "salon",
        "slug": random_slug(),
        "plan_tier": plan_tier,
    })
    data = resp.json()
    return data["access_token"], data["tenant_id"]


def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestTenantProfile:
    def test_get_tenant(self, client):
        token, tenant_id = signup_and_get_token(client)
        resp = client.get("/api/tenant", headers=auth_headers(token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == tenant_id
        assert data["business_name"] == "Test Salon"
        assert data["owner_name"] == "Tester"
        assert data["business_type"] == "salon"
        assert data["is_active"] is True
        assert data["plan_tier"] == "solo"
        assert data["primary_language"] == "en"
        assert data["timezone"] == "Asia/Kolkata"

    def test_update_tenant_name(self, client):
        token, _ = signup_and_get_token(client)
        headers = auth_headers(token)

        resp = client.put("/api/tenant", headers=headers, json={
            "business_name": "New Salon Name",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["business_name"] == "New Salon Name"

    def test_update_tenant_owner_name(self, client):
        token, _ = signup_and_get_token(client)
        headers = auth_headers(token)

        resp = client.put("/api/tenant", headers=headers, json={
            "owner_name": "New Owner Name",
        })
        assert resp.status_code == 200
        assert resp.json()["owner_name"] == "New Owner Name"

    def test_update_tenant_language(self, client):
        token, _ = signup_and_get_token(client)
        headers = auth_headers(token)

        resp = client.put("/api/tenant", headers=headers, json={
            "primary_language": "hi",
        })
        assert resp.status_code == 200
        assert resp.json()["primary_language"] == "hi"

    def test_update_tenant_timezone(self, client):
        token, _ = signup_and_get_token(client)
        headers = auth_headers(token)

        resp = client.put("/api/tenant", headers=headers, json={
            "timezone": "America/New_York",
        })
        assert resp.status_code == 200
        assert resp.json()["timezone"] == "America/New_York"

    def test_update_tenant_no_fields(self, client):
        token, _ = signup_and_get_token(client)
        resp = client.put("/api/tenant", headers=auth_headers(token), json={})
        assert resp.status_code == 400

    def test_tenant_has_slug(self, client):
        slug = random_slug()
        resp = client.post("/api/auth/signup", json={
            "business_name": "Test Salon",
            "owner_name": "Tester",
            "owner_phone": random_phone(),
            "business_type": "salon",
            "slug": slug,
        })
        token = resp.json()["access_token"]
        tenant_resp = client.get("/api/tenant", headers=auth_headers(token))
        assert tenant_resp.status_code == 200
        assert tenant_resp.json()["slug"] == slug

    def test_tenant_has_timestamps(self, client):
        token, _ = signup_and_get_token(client)
        resp = client.get("/api/tenant", headers=auth_headers(token))
        data = resp.json()
        assert data["created_at"] is not None
        assert data["updated_at"] is not None

    def test_get_tenant_no_auth(self, client):
        resp = client.get("/api/tenant")
        assert resp.status_code in (401, 403)


class TestTenantSettings:
    def test_get_settings(self, client):
        token, _ = signup_and_get_token(client)
        resp = client.get("/api/tenant/settings", headers=auth_headers(token))
        assert resp.status_code == 200
        data = resp.json()
        # Default settings should exist (created during signup)
        assert "tenant_id" in data or "id" in data

    def test_update_booking_rules(self, client):
        token, _ = signup_and_get_token(client)
        headers = auth_headers(token)

        resp = client.put("/api/tenant/settings", headers=headers, json={
            "advance_booking_days": 30,
            "min_cancel_hours": 4,
            "slot_increment_minutes": 30,
            "max_bookings_per_customer": 3,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["advance_booking_days"] == 30
        assert data["min_cancel_hours"] == 4
        assert data["slot_increment_minutes"] == 30
        assert data["max_bookings_per_customer"] == 3

    def test_update_notifications(self, client):
        token, _ = signup_and_get_token(client)
        headers = auth_headers(token)

        resp = client.put("/api/tenant/settings", headers=headers, json={
            "send_reminders": False,
            "reminder_hours_before": 48,
            "send_feedback_request": False,
            "feedback_delay_hours": 4,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["send_reminders"] is False
        assert data["reminder_hours_before"] == 48
        assert data["send_feedback_request"] is False
        assert data["feedback_delay_hours"] == 4

    def test_update_display_messages(self, client):
        token, _ = signup_and_get_token(client)
        headers = auth_headers(token)

        resp = client.put("/api/tenant/settings", headers=headers, json={
            "welcome_message": "Welcome to our salon!",
            "booking_confirmed_message": "Your booking is confirmed.",
            "google_review_url": "https://g.page/review/mysalon",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["welcome_message"] == "Welcome to our salon!"
        assert data["booking_confirmed_message"] == "Your booking is confirmed."
        assert data["google_review_url"] == "https://g.page/review/mysalon"

    def test_update_integrations(self, client):
        token, _ = signup_and_get_token(client, plan_tier="growth")
        headers = auth_headers(token)

        resp = client.put("/api/tenant/settings", headers=headers, json={
            "google_calendar_enabled": True,
            "razorpay_enabled": True,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["google_calendar_enabled"] is True
        assert data["razorpay_enabled"] is True

    def test_partial_update(self, client):
        """Updating one field should not affect others."""
        token, _ = signup_and_get_token(client)
        headers = auth_headers(token)

        # First update
        client.put("/api/tenant/settings", headers=headers, json={
            "advance_booking_days": 21,
        })

        # Second update — different field
        resp = client.put("/api/tenant/settings", headers=headers, json={
            "send_reminders": False,
        })
        assert resp.status_code == 200
        data = resp.json()
        # First field should still be set
        assert data["advance_booking_days"] == 21
        assert data["send_reminders"] is False

    def test_update_settings_no_fields(self, client):
        token, _ = signup_and_get_token(client)
        resp = client.put("/api/tenant/settings", headers=auth_headers(token), json={})
        assert resp.status_code == 400

    def test_settings_no_auth(self, client):
        resp = client.get("/api/tenant/settings")
        assert resp.status_code in (401, 403)


class TestWhatsAppConfig:
    def test_save_whatsapp_config(self, client):
        token, _ = signup_and_get_token(client)
        headers = auth_headers(token)

        resp = client.put("/api/tenant/whatsapp", headers=headers, json={
            "waba_phone_number_id": "123456789012345",
            "waba_access_token": "EAABxxxxxxxxxxxxxxx",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["waba_phone_number_id"] == "123456789012345"

        # Verify it was saved on tenant profile
        tenant_resp = client.get("/api/tenant", headers=headers)
        assert tenant_resp.json()["waba_phone_number_id"] == "123456789012345"

    def test_save_whatsapp_config_phone_only(self, client):
        """Should work with just phone_number_id, no token."""
        token, _ = signup_and_get_token(client)
        headers = auth_headers(token)

        resp = client.put("/api/tenant/whatsapp", headers=headers, json={
            "waba_phone_number_id": "999888777666555",
        })
        assert resp.status_code == 200
        assert resp.json()["waba_phone_number_id"] == "999888777666555"

    def test_whatsapp_missing_phone_id(self, client):
        token, _ = signup_and_get_token(client)
        resp = client.put("/api/tenant/whatsapp", headers=auth_headers(token), json={})
        assert resp.status_code == 422

    def test_whatsapp_no_auth(self, client):
        resp = client.put("/api/tenant/whatsapp", json={
            "waba_phone_number_id": "123456789012345",
        })
        assert resp.status_code in (401, 403)
