"""Test service API endpoints including hierarchy."""

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


class TestServiceCRUD:
    def test_list_services_empty(self, client):
        token, _ = signup_and_get_token(client)
        resp = client.get("/api/services", headers=auth_headers(token))
        assert resp.status_code == 200
        assert resp.json() == []

    def test_create_service(self, client):
        token, _ = signup_and_get_token(client)
        resp = client.post("/api/services", headers=auth_headers(token), json={
            "name": "Haircut",
            "duration_minutes": 30,
            "price_paise": 50000,
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Haircut"
        assert data["duration_minutes"] == 30
        assert data["price_paise"] == 50000
        assert data["is_active"] is True
        assert "id" in data

    def test_create_service_with_all_fields(self, client):
        token, _ = signup_and_get_token(client, plan_tier="growth")
        resp = client.post("/api/services", headers=auth_headers(token), json={
            "name": "Hair Color",
            "description": "Full hair coloring service",
            "duration_minutes": 90,
            "buffer_minutes": 15,
            "price_paise": 200000,
            "requires_deposit": True,
            "deposit_amount_paise": 50000,
            "sort_order": 5,
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["description"] == "Full hair coloring service"
        assert data["buffer_minutes"] == 15
        assert data["requires_deposit"] is True
        assert data["deposit_amount_paise"] == 50000
        assert data["sort_order"] == 5

    def test_create_category(self, client):
        """A category is a service with duration=0 (used as grouping parent)."""
        token, _ = signup_and_get_token(client)
        resp = client.post("/api/services", headers=auth_headers(token), json={
            "name": "Hair Services",
            "duration_minutes": 0,
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Hair Services"
        assert data["duration_minutes"] == 0

    def test_create_child_service(self, client):
        token, _ = signup_and_get_token(client)
        # Create parent
        parent_resp = client.post("/api/services", headers=auth_headers(token), json={
            "name": "Hair Services",
            "duration_minutes": 0,
        })
        assert parent_resp.status_code == 201
        parent_id = parent_resp.json()["id"]

        # Create child
        child_resp = client.post("/api/services", headers=auth_headers(token), json={
            "name": "Men's Haircut",
            "duration_minutes": 30,
            "price_paise": 40000,
            "parent_service_id": parent_id,
        })
        assert child_resp.status_code == 201
        assert child_resp.json()["parent_service_id"] == parent_id

    def test_service_tree_structure(self, client):
        """GET /api/services returns parent-child hierarchy."""
        token, _ = signup_and_get_token(client)
        headers = auth_headers(token)

        # Create parent
        parent = client.post("/api/services", headers=headers, json={
            "name": "Hair Services",
            "duration_minutes": 0,
        }).json()

        # Create two children
        child1 = client.post("/api/services", headers=headers, json={
            "name": "Men's Haircut",
            "duration_minutes": 30,
            "price_paise": 40000,
            "parent_service_id": parent["id"],
        }).json()

        child2 = client.post("/api/services", headers=headers, json={
            "name": "Women's Haircut",
            "duration_minutes": 45,
            "price_paise": 60000,
            "parent_service_id": parent["id"],
        }).json()

        # List services — should be nested
        resp = client.get("/api/services", headers=headers)
        assert resp.status_code == 200
        tree = resp.json()

        # Find parent in tree
        parent_in_tree = [s for s in tree if s["id"] == parent["id"]]
        assert len(parent_in_tree) == 1
        assert len(parent_in_tree[0]["children"]) == 2
        child_names = {c["name"] for c in parent_in_tree[0]["children"]}
        assert child_names == {"Men's Haircut", "Women's Haircut"}

    def test_update_service(self, client):
        token, _ = signup_and_get_token(client)
        headers = auth_headers(token)

        # Create
        svc = client.post("/api/services", headers=headers, json={
            "name": "Haircut",
            "duration_minutes": 30,
            "price_paise": 50000,
        }).json()

        # Update
        resp = client.put(f"/api/services/{svc['id']}", headers=headers, json={
            "name": "Premium Haircut",
            "price_paise": 75000,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Premium Haircut"
        assert data["price_paise"] == 75000
        # Unchanged fields should remain
        assert data["duration_minutes"] == 30

    def test_toggle_service_active(self, client):
        token, _ = signup_and_get_token(client)
        headers = auth_headers(token)

        svc = client.post("/api/services", headers=headers, json={
            "name": "Facial",
            "duration_minutes": 60,
        }).json()

        # Deactivate
        resp = client.put(f"/api/services/{svc['id']}", headers=headers, json={
            "is_active": False,
        })
        assert resp.status_code == 200
        assert resp.json()["is_active"] is False

        # Should not appear in list (list only shows active)
        listing = client.get("/api/services", headers=headers).json()
        ids = [s["id"] for s in listing]
        assert svc["id"] not in ids

        # Re-activate
        resp2 = client.put(f"/api/services/{svc['id']}", headers=headers, json={
            "is_active": True,
        })
        assert resp2.status_code == 200
        assert resp2.json()["is_active"] is True

    def test_delete_service(self, client):
        token, _ = signup_and_get_token(client)
        headers = auth_headers(token)

        svc = client.post("/api/services", headers=headers, json={
            "name": "Massage",
            "duration_minutes": 45,
        }).json()

        resp = client.delete(f"/api/services/{svc['id']}", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["message"] == "Service deleted"

        # Should not appear in listing
        listing = client.get("/api/services", headers=headers).json()
        ids = [s["id"] for s in listing]
        assert svc["id"] not in ids

    def test_delete_parent_deletes_children(self, client):
        token, _ = signup_and_get_token(client)
        headers = auth_headers(token)

        parent = client.post("/api/services", headers=headers, json={
            "name": "Hair Services",
            "duration_minutes": 0,
        }).json()

        child = client.post("/api/services", headers=headers, json={
            "name": "Haircut",
            "duration_minutes": 30,
            "parent_service_id": parent["id"],
        }).json()

        # Delete parent
        resp = client.delete(f"/api/services/{parent['id']}", headers=headers)
        assert resp.status_code == 200

        # Both parent and child should be gone from listing
        listing = client.get("/api/services", headers=headers).json()
        all_ids = []
        for s in listing:
            all_ids.append(s["id"])
            for c in s.get("children", []):
                all_ids.append(c["id"])
        assert parent["id"] not in all_ids
        assert child["id"] not in all_ids

    def test_create_service_no_auth(self, client):
        resp = client.post("/api/services", json={
            "name": "Haircut",
            "duration_minutes": 30,
        })
        assert resp.status_code in (401, 403)

    def test_service_validation_no_name(self, client):
        token, _ = signup_and_get_token(client)
        resp = client.post("/api/services", headers=auth_headers(token), json={
            "duration_minutes": 30,
        })
        assert resp.status_code == 422

    def test_update_nonexistent_service(self, client):
        token, _ = signup_and_get_token(client)
        fake_id = "00000000-0000-0000-0000-000000000000"
        resp = client.put(f"/api/services/{fake_id}", headers=auth_headers(token), json={
            "name": "Updated",
        })
        assert resp.status_code == 404

    def test_delete_nonexistent_service(self, client):
        token, _ = signup_and_get_token(client)
        fake_id = "00000000-0000-0000-0000-000000000000"
        resp = client.delete(f"/api/services/{fake_id}", headers=auth_headers(token))
        assert resp.status_code == 404

    def test_update_service_no_fields(self, client):
        token, _ = signup_and_get_token(client)
        headers = auth_headers(token)
        svc = client.post("/api/services", headers=headers, json={
            "name": "Haircut",
            "duration_minutes": 30,
        }).json()
        resp = client.put(f"/api/services/{svc['id']}", headers=headers, json={})
        assert resp.status_code == 400

    def test_create_child_with_invalid_parent(self, client):
        token, _ = signup_and_get_token(client)
        fake_parent = "00000000-0000-0000-0000-000000000000"
        resp = client.post("/api/services", headers=auth_headers(token), json={
            "name": "Haircut",
            "duration_minutes": 30,
            "parent_service_id": fake_parent,
        })
        assert resp.status_code == 404

    def test_per_service_slot_increment_overrides_business_default(self, client):
        """Haircut (45m, slots every 45m) and Beard Trim (20m, slots every 20m) generate independent slot steps."""
        from datetime import date, datetime, timedelta

        slug = random_slug()
        signup_resp = client.post("/api/auth/signup", json={
            "business_name": "Slot Config Salon",
            "owner_name": "Tester",
            "owner_phone": random_phone(),
            "business_type": "salon",
            "slug": slug,
        })
        assert signup_resp.status_code == 201
        headers = auth_headers(signup_resp.json()["access_token"])

        # Business default slot increment is 15m
        haircut = client.post("/api/services", headers=headers, json={
            "name": "Haircut",
            "duration_minutes": 45,
            "slot_increment_minutes": 45,
            "price_paise": 50000,
        })
        assert haircut.status_code == 201
        assert haircut.json()["slot_increment_minutes"] == 45

        beard = client.post("/api/services", headers=headers, json={
            "name": "Beard Trim",
            "duration_minutes": 20,
            "slot_increment_minutes": 20,
            "price_paise": 20000,
        })
        assert beard.status_code == 201
        assert beard.json()["slot_increment_minutes"] == 20

        # Pick next weekday (Mon-Sat) at least 2 days ahead so operating hours apply
        target = date.today() + timedelta(days=2)
        while target.weekday() == 6:  # skip Sunday
            target += timedelta(days=1)

        hc_slots = client.get(
            f"/api/public/business/{slug}/slots",
            params={"service_id": haircut.json()["id"], "date": target.isoformat()},
        ).json()["slots"]
        assert len(hc_slots) >= 2
        hc_gap = (datetime.fromisoformat(hc_slots[1]["start"]) - datetime.fromisoformat(hc_slots[0]["start"])).total_seconds()
        assert hc_gap == 45 * 60
        hc_dur = (datetime.fromisoformat(hc_slots[0]["end"]) - datetime.fromisoformat(hc_slots[0]["start"])).total_seconds()
        assert hc_dur == 45 * 60

        bt_slots = client.get(
            f"/api/public/business/{slug}/slots",
            params={"service_id": beard.json()["id"], "date": target.isoformat()},
        ).json()["slots"]
        assert len(bt_slots) >= 2
        bt_gap = (datetime.fromisoformat(bt_slots[1]["start"]) - datetime.fromisoformat(bt_slots[0]["start"])).total_seconds()
        assert bt_gap == 20 * 60
        bt_dur = (datetime.fromisoformat(bt_slots[0]["end"]) - datetime.fromisoformat(bt_slots[0]["start"])).total_seconds()
        assert bt_dur == 20 * 60

        # Clearing slot_increment_minutes back to null falls back to tenant default (15m)
        upd = client.put(
            f"/api/services/{beard.json()['id']}",
            headers=headers,
            json={"slot_increment_minutes": None},
        )
        assert upd.status_code == 200
        assert upd.json()["slot_increment_minutes"] is None
        bt_default_slots = client.get(
            f"/api/public/business/{slug}/slots",
            params={"service_id": beard.json()["id"], "date": target.isoformat()},
        ).json()["slots"]
        bt_default_gap = (
            datetime.fromisoformat(bt_default_slots[1]["start"]) - datetime.fromisoformat(bt_default_slots[0]["start"])
        ).total_seconds()
        assert bt_default_gap == 15 * 60

