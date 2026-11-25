"""Test signup and login API endpoints."""

import random
import string

import pytest


def random_phone():
    return "+91" + "".join(random.choices(string.digits, k=10))


def random_slug():
    return "test-" + "".join(random.choices(string.ascii_lowercase, k=8))


def signup_payload(**overrides):
    payload = {
        "business_name": "Test Salon",
        "owner_name": "Test Owner",
        "owner_phone": random_phone(),
        "business_type": "salon",
        "slug": random_slug(),
    }
    payload.update(overrides)
    return payload


class TestSignup:
    def test_signup_success(self, client):
        payload = signup_payload()
        resp = client.post("/api/auth/signup", json=payload)
        assert resp.status_code == 201
        data = resp.json()
        assert "access_token" in data
        assert data["tenant_id"]
        assert data["slug"] == payload["slug"]
        assert "dashboard_url" in data
        assert payload["slug"] in data["dashboard_url"]

    def test_signup_returns_valid_jwt(self, client):
        payload = signup_payload()
        resp = client.post("/api/auth/signup", json=payload)
        assert resp.status_code == 201
        token = resp.json()["access_token"]
        # Token should work on authenticated endpoints
        tenant_resp = client.get(
            "/api/tenant", headers={"Authorization": f"Bearer {token}"}
        )
        assert tenant_resp.status_code == 200
        assert tenant_resp.json()["business_name"] == payload["business_name"]

    def test_signup_duplicate_slug(self, client):
        payload = signup_payload()
        resp1 = client.post("/api/auth/signup", json=payload)
        assert resp1.status_code == 201

        # Same slug, different phone
        payload2 = signup_payload(slug=payload["slug"])
        resp2 = client.post("/api/auth/signup", json=payload2)
        assert resp2.status_code == 409
        assert "already taken" in resp2.json()["detail"].lower()

    def test_signup_duplicate_phone(self, client):
        phone = random_phone()
        payload1 = signup_payload(owner_phone=phone)
        resp1 = client.post("/api/auth/signup", json=payload1)
        assert resp1.status_code == 201

        # Same phone, different slug
        payload2 = signup_payload(owner_phone=phone)
        resp2 = client.post("/api/auth/signup", json=payload2)
        assert resp2.status_code == 409
        assert "phone" in resp2.json()["detail"].lower()

    def test_signup_invalid_slug_with_spaces(self, client):
        payload = signup_payload(slug="bad slug here")
        resp = client.post("/api/auth/signup", json=payload)
        assert resp.status_code == 422

    def test_signup_invalid_slug_uppercase(self, client):
        payload = signup_payload(slug="BadSlug")
        resp = client.post("/api/auth/signup", json=payload)
        assert resp.status_code == 422

    def test_signup_invalid_slug_special_chars(self, client):
        payload = signup_payload(slug="bad@slug!")
        resp = client.post("/api/auth/signup", json=payload)
        assert resp.status_code == 422

    def test_signup_slug_too_short(self, client):
        payload = signup_payload(slug="a")
        resp = client.post("/api/auth/signup", json=payload)
        assert resp.status_code == 422

    def test_signup_missing_business_name(self, client):
        payload = signup_payload()
        del payload["business_name"]
        resp = client.post("/api/auth/signup", json=payload)
        assert resp.status_code == 422

    def test_signup_missing_owner_phone(self, client):
        payload = signup_payload()
        del payload["owner_phone"]
        resp = client.post("/api/auth/signup", json=payload)
        assert resp.status_code == 422

    def test_signup_missing_slug(self, client):
        payload = signup_payload()
        del payload["slug"]
        resp = client.post("/api/auth/signup", json=payload)
        assert resp.status_code == 422

    def test_signup_short_name(self, client):
        payload = signup_payload(business_name="A")
        resp = client.post("/api/auth/signup", json=payload)
        assert resp.status_code == 422

    def test_signup_short_owner_name(self, client):
        payload = signup_payload(owner_name="X")
        resp = client.post("/api/auth/signup", json=payload)
        assert resp.status_code == 422

    def test_signup_creates_branch_and_staff(self, client):
        """Signup should auto-create a primary branch and owner staff."""
        payload = signup_payload()
        resp = client.post("/api/auth/signup", json=payload)
        assert resp.status_code == 201
        token = resp.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # Should have a branch
        branches = client.get("/api/branches", headers=headers)
        assert branches.status_code == 200
        branch_list = branches.json()
        assert len(branch_list) >= 1
        assert branch_list[0]["is_primary"] is True

        # Should have owner staff member
        staff = client.get("/api/staff", headers=headers)
        assert staff.status_code == 200
        staff_list = staff.json()
        assert len(staff_list) >= 1
        owner_staff = [s for s in staff_list if s["role"] == "owner"]
        assert len(owner_staff) == 1
        assert owner_staff[0]["name"] == payload["owner_name"]


class TestLogin:
    def test_login_success(self, client):
        # Signup first
        payload = signup_payload()
        signup_resp = client.post("/api/auth/signup", json=payload)
        assert signup_resp.status_code == 201

        # Login with same phone
        login_resp = client.post("/api/auth/login", json={
            "phone": payload["owner_phone"],
            "otp": "123456",
        })
        assert login_resp.status_code == 200
        data = login_resp.json()
        assert "access_token" in data
        assert data["tenant_id"] == signup_resp.json()["tenant_id"]
        assert data["business_name"] == payload["business_name"]
        assert data["token_type"] == "bearer"

    def test_login_wrong_phone(self, client):
        resp = client.post("/api/auth/login", json={
            "phone": "+910000000000",
            "otp": "123456",
        })
        assert resp.status_code == 404
        assert "no account" in resp.json()["detail"].lower()

    def test_login_invalid_otp_too_short(self, client):
        resp = client.post("/api/auth/login", json={
            "phone": random_phone(),
            "otp": "12345",
        })
        assert resp.status_code == 422

    def test_login_invalid_otp_too_long(self, client):
        resp = client.post("/api/auth/login", json={
            "phone": random_phone(),
            "otp": "1234567",
        })
        assert resp.status_code == 422

    def test_login_invalid_otp_letters(self, client):
        # Pydantic enforces length; the endpoint checks isdigit
        # Letters that are length 6 should hit the 400 check
        payload = signup_payload()
        client.post("/api/auth/signup", json=payload)
        resp = client.post("/api/auth/login", json={
            "phone": payload["owner_phone"],
            "otp": "abcdef",
        })
        assert resp.status_code == 400
        assert "6 digits" in resp.json()["detail"].lower()

    def test_login_returns_working_jwt(self, client):
        payload = signup_payload()
        client.post("/api/auth/signup", json=payload)
        login_resp = client.post("/api/auth/login", json={
            "phone": payload["owner_phone"],
            "otp": "999999",
        })
        assert login_resp.status_code == 200
        token = login_resp.json()["access_token"]

        # Use token to access protected endpoint
        tenant_resp = client.get(
            "/api/tenant", headers={"Authorization": f"Bearer {token}"}
        )
        assert tenant_resp.status_code == 200
        assert tenant_resp.json()["owner_phone"] == payload["owner_phone"]

    def test_login_empty_body(self, client):
        resp = client.post("/api/auth/login", json={})
        assert resp.status_code == 422

    def test_protected_endpoint_no_auth(self, client):
        resp = client.get("/api/tenant")
        assert resp.status_code in (401, 403)

    def test_protected_endpoint_bad_token(self, client):
        resp = client.get(
            "/api/tenant",
            headers={"Authorization": "Bearer invalid-token-here"},
        )
        assert resp.status_code == 401
