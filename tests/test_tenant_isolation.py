"""Test multi-tenant isolation — one tenant cannot access another's data."""

import random
import string

import pytest


def random_phone():
    return "+91" + "".join(random.choices(string.digits, k=10))


def random_slug():
    return "test-" + "".join(random.choices(string.ascii_lowercase, k=8))


def signup_and_get_token(client, name: str = "Test Salon") -> tuple[str, str]:
    """Returns (token, tenant_id)."""
    resp = client.post("/api/auth/signup", json={
        "business_name": name,
        "owner_name": "Tester",
        "owner_phone": random_phone(),
        "business_type": "salon",
        "slug": random_slug(),
    })
    assert resp.status_code == 201
    data = resp.json()
    return data["access_token"], data["tenant_id"]


def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestTenantIsolation:
    def test_cannot_see_other_tenant_services(self, client):
        """Services created by tenant A should not be visible to tenant B."""
        token_a, _ = signup_and_get_token(client, "Salon A")
        token_b, _ = signup_and_get_token(client, "Salon B")

        # Tenant A creates a service
        svc_resp = client.post("/api/services", headers=auth_headers(token_a), json={
            "name": "Secret Haircut A",
            "duration_minutes": 30,
            "price_paise": 50000,
        })
        assert svc_resp.status_code == 201
        svc_id = svc_resp.json()["id"]

        # Tenant B should not see it
        listing_b = client.get("/api/services", headers=auth_headers(token_b))
        assert listing_b.status_code == 200
        ids_b = [s["id"] for s in listing_b.json()]
        assert svc_id not in ids_b

        # Tenant A should see it
        listing_a = client.get("/api/services", headers=auth_headers(token_a))
        ids_a = []
        for s in listing_a.json():
            ids_a.append(s["id"])
            for c in s.get("children", []):
                ids_a.append(c["id"])
        assert svc_id in ids_a

    def test_cannot_update_other_tenant_service(self, client):
        """Tenant B cannot update tenant A's service."""
        token_a, _ = signup_and_get_token(client, "Salon A")
        token_b, _ = signup_and_get_token(client, "Salon B")

        svc = client.post("/api/services", headers=auth_headers(token_a), json={
            "name": "Exclusive Service",
            "duration_minutes": 30,
        }).json()

        # Try to update with tenant B's token
        resp = client.put(
            f"/api/services/{svc['id']}",
            headers=auth_headers(token_b),
            json={"name": "Hacked!"},
        )
        assert resp.status_code == 404  # Not found in B's scope

    def test_cannot_delete_other_tenant_service(self, client):
        """Tenant B cannot delete tenant A's service."""
        token_a, _ = signup_and_get_token(client, "Salon A")
        token_b, _ = signup_and_get_token(client, "Salon B")

        svc = client.post("/api/services", headers=auth_headers(token_a), json={
            "name": "Protected Service",
            "duration_minutes": 30,
        }).json()

        resp = client.delete(
            f"/api/services/{svc['id']}",
            headers=auth_headers(token_b),
        )
        assert resp.status_code == 404

        # Verify it still exists for tenant A
        listing_a = client.get("/api/services", headers=auth_headers(token_a)).json()
        ids_a = [s["id"] for s in listing_a]
        assert svc["id"] in ids_a

    def test_cannot_see_other_tenant_staff(self, client):
        """Staff created by tenant A should not be visible to tenant B."""
        token_a, _ = signup_and_get_token(client, "Salon A")
        token_b, _ = signup_and_get_token(client, "Salon B")

        # Tenant A adds staff
        staff = client.post("/api/staff", headers=auth_headers(token_a), json={
            "name": "Secret Staff A",
            "role": "stylist",
        }).json()

        # Tenant B should not see it
        listing_b = client.get("/api/staff", headers=auth_headers(token_b))
        ids_b = [s["id"] for s in listing_b.json()]
        assert staff["id"] not in ids_b

    def test_cannot_update_other_tenant_staff(self, client):
        token_a, _ = signup_and_get_token(client, "Salon A")
        token_b, _ = signup_and_get_token(client, "Salon B")

        staff = client.post("/api/staff", headers=auth_headers(token_a), json={
            "name": "Staff A",
        }).json()

        resp = client.put(
            f"/api/staff/{staff['id']}",
            headers=auth_headers(token_b),
            json={"name": "Hacked Staff"},
        )
        assert resp.status_code == 404

    def test_cannot_delete_other_tenant_staff(self, client):
        token_a, _ = signup_and_get_token(client, "Salon A")
        token_b, _ = signup_and_get_token(client, "Salon B")

        staff = client.post("/api/staff", headers=auth_headers(token_a), json={
            "name": "Staff A",
        }).json()

        resp = client.delete(
            f"/api/staff/{staff['id']}",
            headers=auth_headers(token_b),
        )
        assert resp.status_code == 404

    def test_cannot_see_other_tenant_bookings(self, client):
        """Tenant B should see zero bookings (even if tenant A has some)."""
        token_a, _ = signup_and_get_token(client, "Salon A")
        token_b, _ = signup_and_get_token(client, "Salon B")

        # Both list bookings — each should only see their own
        resp_a = client.get("/api/bookings", headers=auth_headers(token_a))
        resp_b = client.get("/api/bookings", headers=auth_headers(token_b))

        assert resp_a.status_code == 200
        assert resp_b.status_code == 200

        # Fresh tenants have no bookings
        assert resp_a.json()["total"] == 0
        assert resp_b.json()["total"] == 0

    def test_cannot_see_other_tenant_customers(self, client):
        """Customers created via simulate for tenant A should not appear for tenant B."""
        token_a, _ = signup_and_get_token(client, "Salon A")
        token_b, _ = signup_and_get_token(client, "Salon B")

        # Both have zero customers initially
        resp_a = client.get("/api/customers", headers=auth_headers(token_a))
        resp_b = client.get("/api/customers", headers=auth_headers(token_b))
        assert resp_a.json()["total"] == 0
        assert resp_b.json()["total"] == 0

    def test_cannot_see_other_tenant_branches(self, client):
        """Tenant B should only see their own branch."""
        token_a, _ = signup_and_get_token(client, "Salon A")
        token_b, _ = signup_and_get_token(client, "Salon B")

        branches_a = client.get("/api/branches", headers=auth_headers(token_a)).json()
        branches_b = client.get("/api/branches", headers=auth_headers(token_b)).json()

        ids_a = {b["id"] for b in branches_a}
        ids_b = {b["id"] for b in branches_b}

        # No overlap
        assert ids_a.isdisjoint(ids_b)

    def test_cannot_update_other_tenant_branch(self, client):
        token_a, _ = signup_and_get_token(client, "Salon A")
        token_b, _ = signup_and_get_token(client, "Salon B")

        branches_a = client.get("/api/branches", headers=auth_headers(token_a)).json()
        branch_id_a = branches_a[0]["id"]

        resp = client.put(
            f"/api/branches/{branch_id_a}",
            headers=auth_headers(token_b),
            json={"name": "Hacked Branch"},
        )
        assert resp.status_code == 404

    def test_cannot_see_other_tenant_analytics(self, client):
        """Analytics should be scoped to the authenticated tenant."""
        token_a, _ = signup_and_get_token(client, "Salon A")
        token_b, _ = signup_and_get_token(client, "Salon B")

        overview_a = client.get("/api/analytics/overview", headers=auth_headers(token_a))
        overview_b = client.get("/api/analytics/overview", headers=auth_headers(token_b))

        assert overview_a.status_code == 200
        assert overview_b.status_code == 200

        # Both should have 0 customers (freshly created)
        assert overview_a.json()["total_customers"] == 0
        assert overview_b.json()["total_customers"] == 0

    def test_cannot_see_other_tenant_settings(self, client):
        """Each tenant has its own settings."""
        token_a, _ = signup_and_get_token(client, "Salon A")
        token_b, _ = signup_and_get_token(client, "Salon B")

        # Update settings for A
        client.put("/api/tenant/settings", headers=auth_headers(token_a), json={
            "welcome_message": "Welcome to Salon A!",
        })

        # B should not see A's welcome message
        settings_b = client.get("/api/tenant/settings", headers=auth_headers(token_b))
        assert settings_b.status_code == 200
        assert settings_b.json().get("welcome_message") != "Welcome to Salon A!"

    def test_tenant_profile_isolated(self, client):
        """Each tenant only sees their own profile."""
        token_a, tid_a = signup_and_get_token(client, "Salon A")
        token_b, tid_b = signup_and_get_token(client, "Salon B")

        profile_a = client.get("/api/tenant", headers=auth_headers(token_a)).json()
        profile_b = client.get("/api/tenant", headers=auth_headers(token_b)).json()

        assert profile_a["id"] == tid_a
        assert profile_b["id"] == tid_b
        assert profile_a["business_name"] == "Salon A"
        assert profile_b["business_name"] == "Salon B"
        assert profile_a["id"] != profile_b["id"]
