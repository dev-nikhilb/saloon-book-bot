"""Test customer API endpoints."""

import random
import string

import pytest


def random_phone():
    return "+91" + "".join(random.choices(string.digits, k=10))


def random_slug():
    return "test-" + "".join(random.choices(string.ascii_lowercase, k=8))


def signup_and_get_token(client) -> tuple[str, str]:
    """Returns (token, tenant_id)."""
    resp = client.post("/api/auth/signup", json={
        "business_name": "Test Salon",
        "owner_name": "Tester",
        "owner_phone": random_phone(),
        "business_type": "salon",
        "slug": random_slug(),
    })
    data = resp.json()
    return data["access_token"], data["tenant_id"]


def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestCustomers:
    def test_list_empty(self, client):
        token, _ = signup_and_get_token(client)
        resp = client.get("/api/customers", headers=auth_headers(token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 0
        assert data["page"] == 1
        assert data["per_page"] == 20

    def test_list_pagination_params(self, client):
        token, _ = signup_and_get_token(client)
        resp = client.get(
            "/api/customers?page=1&per_page=10",
            headers=auth_headers(token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["per_page"] == 10

    def test_customer_search_empty_result(self, client):
        token, _ = signup_and_get_token(client)
        resp = client.get(
            "/api/customers?search=nonexistent",
            headers=auth_headers(token),
        )
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    def test_customer_detail_not_found(self, client):
        token, _ = signup_and_get_token(client)
        fake_id = "00000000-0000-0000-0000-000000000000"
        resp = client.get(
            f"/api/customers/{fake_id}",
            headers=auth_headers(token),
        )
        assert resp.status_code == 404

    def test_customer_detail_invalid_uuid(self, client):
        token, _ = signup_and_get_token(client)
        resp = client.get(
            "/api/customers/not-a-uuid",
            headers=auth_headers(token),
        )
        assert resp.status_code == 400

    def test_customers_no_auth(self, client):
        resp = client.get("/api/customers")
        assert resp.status_code in (401, 403)

    def test_customer_detail_no_auth(self, client):
        fake_id = "00000000-0000-0000-0000-000000000000"
        resp = client.get(f"/api/customers/{fake_id}")
        assert resp.status_code in (401, 403)

    def test_pagination_invalid_page(self, client):
        token, _ = signup_and_get_token(client)
        resp = client.get(
            "/api/customers?page=0",
            headers=auth_headers(token),
        )
        assert resp.status_code == 422

    def test_pagination_per_page_too_large(self, client):
        token, _ = signup_and_get_token(client)
        resp = client.get(
            "/api/customers?per_page=200",
            headers=auth_headers(token),
        )
        assert resp.status_code == 422

    def test_customer_created_via_simulate(self, client):
        """Simulate a customer message via seed tenant to create a customer."""
        # Seed demo tenant
        seed_resp = client.post("/dev/seed")
        assert seed_resp.status_code == 200
        seed = seed_resp.json()

        # Login as demo tenant owner
        login_resp = client.post("/api/auth/login", json={
            "phone": seed["owner_phone"],
            "otp": "123456",
        })
        assert login_resp.status_code == 200
        token = login_resp.json()["access_token"]
        headers = auth_headers(token)

        # Simulate a customer message
        sim_resp = client.post("/dev/simulate", json={
            "from_phone": "+919876543210",
            "body": "hi",
            "to_tenant_phone": seed["owner_phone"],
        })
        assert sim_resp.status_code == 200

        # Search for customer
        resp = client.get(
            "/api/customers?search=9876543210",
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1

    def test_customer_detail_has_bookings_and_feedback(self, client):
        """Customer detail endpoint returns bookings and feedback arrays."""
        # Seed demo tenant
        seed_resp = client.post("/dev/seed")
        seed = seed_resp.json()

        login_resp = client.post("/api/auth/login", json={
            "phone": seed["owner_phone"],
            "otp": "123456",
        })
        token = login_resp.json()["access_token"]
        headers = auth_headers(token)

        # Create customer via simulate
        cust_phone = "+919876543210"
        sim_resp = client.post("/dev/simulate", json={
            "from_phone": cust_phone,
            "body": "hi",
            "to_tenant_phone": seed["owner_phone"],
        })
        assert sim_resp.status_code == 200
        customer_id = sim_resp.json().get("customer_id")

        # Get detail
        resp = client.get(f"/api/customers/{customer_id}", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "customer" in data
        assert "bookings" in data
        assert "feedback" in data
        assert data["customer"]["phone"] == cust_phone
        assert isinstance(data["bookings"], list)
        assert isinstance(data["feedback"], list)
