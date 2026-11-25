"""Test branch API endpoints."""

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


class TestBranches:
    def test_list_branches(self, client):
        token, _ = signup_and_get_token(client)
        resp = client.get("/api/branches", headers=auth_headers(token))
        assert resp.status_code == 200
        branches = resp.json()
        assert isinstance(branches, list)
        assert len(branches) >= 1
        # Should have a primary branch from signup
        primary = [b for b in branches if b["is_primary"]]
        assert len(primary) == 1
        assert primary[0]["name"] == "Main"

    def test_branch_has_operating_hours(self, client):
        token, _ = signup_and_get_token(client)
        resp = client.get("/api/branches", headers=auth_headers(token))
        branch = resp.json()[0]
        assert "operating_hours" in branch
        hours = branch["operating_hours"]
        assert isinstance(hours, dict)
        # Signup creates mon-sat hours
        assert "mon" in hours

    def test_update_branch_name(self, client):
        token, _ = signup_and_get_token(client)
        headers = auth_headers(token)

        branches = client.get("/api/branches", headers=headers).json()
        branch_id = branches[0]["id"]

        resp = client.put(f"/api/branches/{branch_id}", headers=headers, json={
            "name": "Downtown Branch",
        })
        assert resp.status_code == 200
        assert resp.json()["name"] == "Downtown Branch"

    def test_update_branch_address(self, client):
        token, _ = signup_and_get_token(client)
        headers = auth_headers(token)

        branches = client.get("/api/branches", headers=headers).json()
        branch_id = branches[0]["id"]

        resp = client.put(f"/api/branches/{branch_id}", headers=headers, json={
            "address": "123 Main Street, Mumbai",
        })
        assert resp.status_code == 200
        assert resp.json()["address"] == "123 Main Street, Mumbai"

    def test_update_operating_hours(self, client):
        token, _ = signup_and_get_token(client)
        headers = auth_headers(token)

        branches = client.get("/api/branches", headers=headers).json()
        branch_id = branches[0]["id"]

        new_hours = {
            "mon": [{"open": "10:00", "close": "19:00"}],
            "tue": [{"open": "10:00", "close": "19:00"}],
            "wed": [{"open": "10:00", "close": "19:00"}],
            "thu": [{"open": "10:00", "close": "19:00"}],
            "fri": [{"open": "10:00", "close": "21:00"}],
            "sat": [{"open": "11:00", "close": "21:00"}],
            "sun": [{"open": "12:00", "close": "17:00"}],
        }

        resp = client.put(f"/api/branches/{branch_id}", headers=headers, json={
            "operating_hours": new_hours,
        })
        assert resp.status_code == 200
        updated = resp.json()
        assert updated["operating_hours"]["sun"][0]["open"] == "12:00"
        assert updated["operating_hours"]["fri"][0]["close"] == "21:00"

    def test_update_branch_multiple_fields(self, client):
        token, _ = signup_and_get_token(client)
        headers = auth_headers(token)

        branches = client.get("/api/branches", headers=headers).json()
        branch_id = branches[0]["id"]

        resp = client.put(f"/api/branches/{branch_id}", headers=headers, json={
            "name": "New Name",
            "address": "New Address",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "New Name"
        assert data["address"] == "New Address"

    def test_update_branch_no_fields(self, client):
        token, _ = signup_and_get_token(client)
        headers = auth_headers(token)

        branches = client.get("/api/branches", headers=headers).json()
        branch_id = branches[0]["id"]

        resp = client.put(f"/api/branches/{branch_id}", headers=headers, json={})
        assert resp.status_code == 400

    def test_update_nonexistent_branch(self, client):
        token, _ = signup_and_get_token(client)
        fake_id = "00000000-0000-0000-0000-000000000000"
        resp = client.put(f"/api/branches/{fake_id}", headers=auth_headers(token), json={
            "name": "Ghost Branch",
        })
        assert resp.status_code == 404

    def test_branches_no_auth(self, client):
        resp = client.get("/api/branches")
        assert resp.status_code in (401, 403)

    def test_update_branch_no_auth(self, client):
        fake_id = "00000000-0000-0000-0000-000000000000"
        resp = client.put(f"/api/branches/{fake_id}", json={"name": "test"})
        assert resp.status_code in (401, 403)
