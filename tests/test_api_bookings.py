"""Test booking API endpoints."""

import random
import string
import uuid
from datetime import date, datetime, timedelta, timezone

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


def seed_booking_via_simulate(client, tenant_phone: str, customer_phone: str) -> str | None:
    """Use /dev/seed + /dev/simulate to create a booking. Returns booking ID if possible."""
    # This relies on the seed endpoint; for unit testing bookings list/status
    # we test the API directly with pre-seeded data from signup.
    pass


class TestBookingsList:
    def test_list_empty(self, client):
        token, _ = signup_and_get_token(client)
        resp = client.get("/api/bookings", headers=auth_headers(token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 0
        assert data["page"] == 1
        assert data["per_page"] == 20

    def test_list_pagination_params(self, client):
        token, _ = signup_and_get_token(client)
        resp = client.get(
            "/api/bookings?page=2&per_page=5",
            headers=auth_headers(token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["page"] == 2
        assert data["per_page"] == 5

    def test_list_filter_by_status(self, client):
        token, _ = signup_and_get_token(client)
        resp = client.get(
            "/api/bookings?status=confirmed",
            headers=auth_headers(token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data["items"], list)

    def test_list_filter_comma_separated_status(self, client):
        token, _ = signup_and_get_token(client)
        resp = client.get(
            "/api/bookings?status=confirmed,completed",
            headers=auth_headers(token),
        )
        assert resp.status_code == 200

    def test_list_filter_by_date(self, client):
        token, _ = signup_and_get_token(client)
        today = date.today().isoformat()
        resp = client.get(
            f"/api/bookings?from_date={today}&to_date={today}",
            headers=auth_headers(token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data["items"], list)

    def test_list_no_auth(self, client):
        resp = client.get("/api/bookings")
        assert resp.status_code in (401, 403)

    def test_list_invalid_page(self, client):
        token, _ = signup_and_get_token(client)
        resp = client.get(
            "/api/bookings?page=0",
            headers=auth_headers(token),
        )
        assert resp.status_code == 422

    def test_list_per_page_too_large(self, client):
        token, _ = signup_and_get_token(client)
        resp = client.get(
            "/api/bookings?per_page=200",
            headers=auth_headers(token),
        )
        assert resp.status_code == 422

    def test_get_booking_not_found(self, client):
        token, _ = signup_and_get_token(client)
        fake_id = "00000000-0000-0000-0000-000000000000"
        resp = client.get(
            f"/api/bookings/{fake_id}",
            headers=auth_headers(token),
        )
        assert resp.status_code == 404


class TestBookingStatus:
    def test_update_status_not_found(self, client):
        token, _ = signup_and_get_token(client)
        fake_id = "00000000-0000-0000-0000-000000000000"
        resp = client.put(
            f"/api/bookings/{fake_id}/status",
            headers=auth_headers(token),
            json={"status": "completed"},
        )
        assert resp.status_code == 404

    def test_update_status_invalid_status(self, client):
        token, _ = signup_and_get_token(client)
        fake_id = "00000000-0000-0000-0000-000000000000"
        resp = client.put(
            f"/api/bookings/{fake_id}/status",
            headers=auth_headers(token),
            json={"status": "invalid_status"},
        )
        assert resp.status_code == 400
        assert "must be one of" in resp.json()["detail"].lower()

    def test_update_status_allowed_values(self, client):
        """Only cancelled, completed, no_show are allowed statuses."""
        token, _ = signup_and_get_token(client)
        fake_id = "00000000-0000-0000-0000-000000000000"

        for invalid in ["pending", "confirmed", "rescheduled", "approved"]:
            resp = client.put(
                f"/api/bookings/{fake_id}/status",
                headers=auth_headers(token),
                json={"status": invalid},
            )
            assert resp.status_code == 400

    def test_update_status_missing_status(self, client):
        token, _ = signup_and_get_token(client)
        fake_id = "00000000-0000-0000-0000-000000000000"
        resp = client.put(
            f"/api/bookings/{fake_id}/status",
            headers=auth_headers(token),
            json={},
        )
        assert resp.status_code == 422

    def test_update_status_no_auth(self, client):
        fake_id = "00000000-0000-0000-0000-000000000000"
        resp = client.put(
            f"/api/bookings/{fake_id}/status",
            json={"status": "completed"},
        )
        assert resp.status_code in (401, 403)

    def test_cancel_with_reason(self, client):
        """Cancel status should accept optional cancellation_reason."""
        token, _ = signup_and_get_token(client)
        fake_id = "00000000-0000-0000-0000-000000000000"
        resp = client.put(
            f"/api/bookings/{fake_id}/status",
            headers=auth_headers(token),
            json={
                "status": "cancelled",
                "cancellation_reason": "Customer requested",
            },
        )
        # Will be 404 because no booking, but validates the request body is accepted
        assert resp.status_code == 404

    def test_get_booking_invalid_uuid(self, client):
        token, _ = signup_and_get_token(client)
        resp = client.get(
            "/api/bookings/not-a-uuid",
            headers=auth_headers(token),
        )
        assert resp.status_code == 400
