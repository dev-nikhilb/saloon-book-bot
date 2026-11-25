"""Test staff API endpoints."""

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


class TestStaffCRUD:
    def test_list_staff_has_owner(self, client):
        """Signup auto-creates an owner staff member."""
        token, _ = signup_and_get_token(client)
        resp = client.get("/api/staff", headers=auth_headers(token))
        assert resp.status_code == 200
        staff = resp.json()
        assert len(staff) >= 1
        owner = [s for s in staff if s["role"] == "owner"]
        assert len(owner) == 1

    def test_add_staff(self, client):
        token, _ = signup_and_get_token(client)
        resp = client.post("/api/staff", headers=auth_headers(token), json={
            "name": "Ravi Kumar",
            "role": "stylist",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Ravi Kumar"
        assert data["role"] == "stylist"
        assert data["is_active"] is True
        assert "id" in data

    def test_add_staff_with_bio_and_specialty(self, client):
        token, _ = signup_and_get_token(client)
        resp = client.post("/api/staff", headers=auth_headers(token), json={
            "name": "Meena",
            "role": "stylist",
            "bio": "10 years experience in hair coloring",
            "specialty": "Hair Color, Bridal",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["bio"] == "10 years experience in hair coloring"
        assert data["specialty"] == "Hair Color, Bridal"

    def test_add_staff_with_skills(self, client):
        """Skills can be a list of service IDs."""
        token, _ = signup_and_get_token(client)
        headers = auth_headers(token)

        # Create a service first
        svc = client.post("/api/services", headers=headers, json={
            "name": "Haircut",
            "duration_minutes": 30,
        }).json()

        resp = client.post("/api/staff", headers=headers, json={
            "name": "Ravi",
            "role": "stylist",
            "skills": [svc["id"]],
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["skills"] is not None
        assert svc["id"] in data["skills"]

    def test_add_staff_with_working_hours(self, client):
        token, _ = signup_and_get_token(client)
        hours = {
            "mon": [{"open": "09:00", "close": "18:00"}],
            "tue": [{"open": "09:00", "close": "18:00"}],
            "wed": [{"open": "09:00", "close": "18:00"}],
        }
        resp = client.post("/api/staff", headers=auth_headers(token), json={
            "name": "Ravi",
            "role": "stylist",
            "working_hours": hours,
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["working_hours"] is not None
        assert "mon" in data["working_hours"]
        assert data["working_hours"]["mon"][0]["open"] == "09:00"

    def test_update_staff(self, client):
        token, _ = signup_and_get_token(client)
        headers = auth_headers(token)

        staff = client.post("/api/staff", headers=headers, json={
            "name": "Ravi",
            "role": "stylist",
        }).json()

        resp = client.put(f"/api/staff/{staff['id']}", headers=headers, json={
            "name": "Ravi Kumar",
            "bio": "Updated bio",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Ravi Kumar"
        assert data["bio"] == "Updated bio"
        assert data["role"] == "stylist"  # unchanged

    def test_update_staff_skills(self, client):
        token, _ = signup_and_get_token(client)
        headers = auth_headers(token)

        staff = client.post("/api/staff", headers=headers, json={
            "name": "Ravi",
        }).json()

        svc = client.post("/api/services", headers=headers, json={
            "name": "Facial",
            "duration_minutes": 60,
        }).json()

        resp = client.put(f"/api/staff/{staff['id']}", headers=headers, json={
            "skills": [svc["id"]],
        })
        assert resp.status_code == 200
        assert svc["id"] in resp.json()["skills"]

    def test_deactivate_staff(self, client):
        token, _ = signup_and_get_token(client)
        headers = auth_headers(token)

        staff = client.post("/api/staff", headers=headers, json={
            "name": "Ravi",
            "role": "stylist",
        }).json()

        resp = client.put(f"/api/staff/{staff['id']}", headers=headers, json={
            "is_active": False,
        })
        assert resp.status_code == 200
        assert resp.json()["is_active"] is False

        # Should not appear in active staff listing
        listing = client.get("/api/staff", headers=headers).json()
        ids = [s["id"] for s in listing]
        assert staff["id"] not in ids

    def test_reactivate_staff(self, client):
        token, _ = signup_and_get_token(client)
        headers = auth_headers(token)

        staff = client.post("/api/staff", headers=headers, json={
            "name": "Ravi",
        }).json()

        # Deactivate
        client.put(f"/api/staff/{staff['id']}", headers=headers, json={
            "is_active": False,
        })

        # Reactivate
        resp = client.put(f"/api/staff/{staff['id']}", headers=headers, json={
            "is_active": True,
        })
        assert resp.status_code == 200
        assert resp.json()["is_active"] is True

        # Should appear in listing again
        listing = client.get("/api/staff", headers=headers).json()
        ids = [s["id"] for s in listing]
        assert staff["id"] in ids

    def test_delete_staff(self, client):
        token, _ = signup_and_get_token(client)
        headers = auth_headers(token)

        staff = client.post("/api/staff", headers=headers, json={
            "name": "Temp Staff",
        }).json()

        resp = client.delete(f"/api/staff/{staff['id']}", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["message"] == "Staff member deleted"

        # Should not appear in listing
        listing = client.get("/api/staff", headers=headers).json()
        ids = [s["id"] for s in listing]
        assert staff["id"] not in ids

    def test_add_staff_no_name(self, client):
        token, _ = signup_and_get_token(client)
        resp = client.post("/api/staff", headers=auth_headers(token), json={
            "role": "stylist",
        })
        assert resp.status_code == 422

    def test_staff_no_auth(self, client):
        resp = client.get("/api/staff")
        assert resp.status_code in (401, 403)

    def test_create_staff_no_auth(self, client):
        resp = client.post("/api/staff", json={"name": "Ravi"})
        assert resp.status_code in (401, 403)

    def test_update_nonexistent_staff(self, client):
        token, _ = signup_and_get_token(client)
        fake_id = "00000000-0000-0000-0000-000000000000"
        resp = client.put(f"/api/staff/{fake_id}", headers=auth_headers(token), json={
            "name": "Updated",
        })
        assert resp.status_code == 404

    def test_delete_nonexistent_staff(self, client):
        token, _ = signup_and_get_token(client)
        fake_id = "00000000-0000-0000-0000-000000000000"
        resp = client.delete(f"/api/staff/{fake_id}", headers=auth_headers(token))
        assert resp.status_code == 404

    def test_update_staff_no_fields(self, client):
        token, _ = signup_and_get_token(client)
        headers = auth_headers(token)
        staff = client.post("/api/staff", headers=headers, json={
            "name": "Ravi",
        }).json()
        resp = client.put(f"/api/staff/{staff['id']}", headers=headers, json={})
        assert resp.status_code == 400

    def test_staff_auto_assigned_primary_branch(self, client):
        """Staff without explicit branch_id should be assigned to primary branch."""
        token, _ = signup_and_get_token(client)
        headers = auth_headers(token)

        staff = client.post("/api/staff", headers=headers, json={
            "name": "Ravi",
        }).json()

        branches = client.get("/api/branches", headers=headers).json()
        primary_branch = [b for b in branches if b["is_primary"]][0]

        assert staff["branch_id"] == primary_branch["id"]
