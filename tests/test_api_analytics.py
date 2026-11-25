"""Test analytics API endpoints."""

import random
import string
from datetime import date, timedelta

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


class TestAnalytics:
    def test_overview(self, client):
        token, _ = signup_and_get_token(client)
        resp = client.get("/api/analytics/overview", headers=auth_headers(token))
        assert resp.status_code == 200
        data = resp.json()
        assert "today_bookings" in data
        assert "week_revenue" in data
        assert "total_customers" in data
        assert "popular_services" in data
        assert "ratings" in data

    def test_overview_structure(self, client):
        token, _ = signup_and_get_token(client)
        resp = client.get("/api/analytics/overview", headers=auth_headers(token))
        data = resp.json()

        # Today bookings should have count/confirmed/completed/cancelled
        today = data["today_bookings"]
        assert "count" in today
        assert "confirmed" in today
        assert "completed" in today
        assert "cancelled" in today

        # Week revenue should have total_paise and payment_count
        revenue = data["week_revenue"]
        assert "total_paise" in revenue
        assert "payment_count" in revenue

        # Ratings should have avg_rating and review_count
        ratings = data["ratings"]
        assert "avg_rating" in ratings
        assert "review_count" in ratings

    def test_overview_empty_tenant(self, client):
        """Fresh tenant should have zero counts."""
        token, _ = signup_and_get_token(client)
        resp = client.get("/api/analytics/overview", headers=auth_headers(token))
        data = resp.json()
        assert data["total_customers"] == 0
        assert data["popular_services"] == []

    def test_bookings_stats(self, client):
        token, _ = signup_and_get_token(client)
        today = date.today().isoformat()
        week_ago = (date.today() - timedelta(days=7)).isoformat()

        resp = client.get(
            f"/api/analytics/bookings?from_date={week_ago}&to_date={today}",
            headers=auth_headers(token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["from_date"] == week_ago
        assert data["to_date"] == today
        assert "total" in data
        assert "status_breakdown" in data
        assert "daily" in data
        assert isinstance(data["daily"], list)

    def test_bookings_stats_empty(self, client):
        token, _ = signup_and_get_token(client)
        today = date.today().isoformat()
        resp = client.get(
            f"/api/analytics/bookings?from_date={today}&to_date={today}",
            headers=auth_headers(token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0

    def test_revenue_stats(self, client):
        token, _ = signup_and_get_token(client)
        today = date.today().isoformat()
        month_ago = (date.today() - timedelta(days=30)).isoformat()

        resp = client.get(
            f"/api/analytics/revenue?from_date={month_ago}&to_date={today}",
            headers=auth_headers(token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["from_date"] == month_ago
        assert data["to_date"] == today
        assert "total_paise" in data
        assert "daily" in data
        assert "by_service" in data
        assert isinstance(data["daily"], list)
        assert isinstance(data["by_service"], list)

    def test_revenue_stats_empty(self, client):
        token, _ = signup_and_get_token(client)
        today = date.today().isoformat()
        resp = client.get(
            f"/api/analytics/revenue?from_date={today}&to_date={today}",
            headers=auth_headers(token),
        )
        assert resp.status_code == 200
        assert resp.json()["total_paise"] == 0

    def test_staff_stats(self, client):
        token, _ = signup_and_get_token(client)
        today = date.today().isoformat()
        week_ago = (date.today() - timedelta(days=7)).isoformat()

        resp = client.get(
            f"/api/analytics/staff?from_date={week_ago}&to_date={today}",
            headers=auth_headers(token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["from_date"] == week_ago
        assert data["to_date"] == today
        assert "staff" in data
        assert isinstance(data["staff"], list)
        # Should have at least the owner staff member
        assert len(data["staff"]) >= 1

    def test_staff_stats_has_metrics(self, client):
        token, _ = signup_and_get_token(client)
        today = date.today().isoformat()
        week_ago = (date.today() - timedelta(days=7)).isoformat()

        resp = client.get(
            f"/api/analytics/staff?from_date={week_ago}&to_date={today}",
            headers=auth_headers(token),
        )
        data = resp.json()
        for staff_entry in data["staff"]:
            assert "name" in staff_entry
            assert "total_bookings" in staff_entry
            assert "completed" in staff_entry
            assert "cancelled" in staff_entry
            assert "no_show" in staff_entry
            assert "revenue_paise" in staff_entry
            assert "completion_rate" in staff_entry

    def test_bookings_stats_missing_from_date(self, client):
        token, _ = signup_and_get_token(client)
        today = date.today().isoformat()
        resp = client.get(
            f"/api/analytics/bookings?to_date={today}",
            headers=auth_headers(token),
        )
        assert resp.status_code == 422

    def test_bookings_stats_missing_to_date(self, client):
        token, _ = signup_and_get_token(client)
        today = date.today().isoformat()
        resp = client.get(
            f"/api/analytics/bookings?from_date={today}",
            headers=auth_headers(token),
        )
        assert resp.status_code == 422

    def test_revenue_stats_missing_dates(self, client):
        token, _ = signup_and_get_token(client)
        resp = client.get(
            "/api/analytics/revenue",
            headers=auth_headers(token),
        )
        assert resp.status_code == 422

    def test_staff_stats_missing_dates(self, client):
        token, _ = signup_and_get_token(client)
        resp = client.get(
            "/api/analytics/staff",
            headers=auth_headers(token),
        )
        assert resp.status_code == 422

    def test_analytics_no_auth(self, client):
        resp = client.get("/api/analytics/overview")
        assert resp.status_code in (401, 403)

    def test_bookings_analytics_no_auth(self, client):
        today = date.today().isoformat()
        resp = client.get(f"/api/analytics/bookings?from_date={today}&to_date={today}")
        assert resp.status_code in (401, 403)
