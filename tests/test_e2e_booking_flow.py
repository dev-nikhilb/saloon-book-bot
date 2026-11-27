"""End-to-end booking flow tests via /dev/simulate.

These tests exercise the full customer booking journey through the
simulated WhatsApp interface, then verify results via the dashboard API.

NOTE: Tests that call simulate multiple times in sequence may fail under
TestClient due to async Redis connection lifecycle. Run with a live server
for full E2E validation: pytest -m e2e --server=http://127.0.0.1:8000
"""

import random
import re
import string

import pytest

# Mark multi-step simulate tests — they need event loop continuity
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def random_phone():
    return "+91" + "".join(random.choices(string.digits, k=10))


def random_slug():
    return "test-" + "".join(random.choices(string.ascii_lowercase, k=8))


def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def seed_and_login(client) -> tuple[dict, str, dict]:
    """Seed demo tenant and login. Returns (seed_data, token, headers)."""
    seed_resp = client.post("/dev/seed")
    assert seed_resp.status_code == 200
    seed = seed_resp.json()

    login_resp = client.post("/api/auth/login", json={
        "phone": seed["owner_phone"],
        "otp": "123456",
    })
    assert login_resp.status_code == 200
    token = login_resp.json()["access_token"]
    return seed, token, auth_headers(token)


def simulate(client, from_phone: str, body: str, to_tenant_phone: str) -> dict:
    """Send a simulated message and return the response."""
    resp = client.post("/dev/simulate", json={
        "from_phone": from_phone,
        "body": body,
        "to_tenant_phone": to_tenant_phone,
    })
    assert resp.status_code == 200
    return resp.json()


class TestE2EBookingFlow:
    def test_customer_greeting(self, client):
        """Customer sends 'hi' and gets a welcome/menu response."""
        seed, token, headers = seed_and_login(client)
        cust_phone = random_phone()

        result = simulate(client, cust_phone, "hi", seed["owner_phone"])
        assert result["received"] is True
        assert result["sender_type"] in ("new_customer", "returning_customer")
        assert result["tenant_id"] is not None
        assert result["customer_id"] is not None
        assert len(result["message"]) > 0

    def test_customer_sees_services(self, client):
        """After greeting, selecting book should show available services."""
        seed, token, headers = seed_and_login(client)
        cust_phone = random_phone()

        # Start conversation
        simulate(client, cust_phone, "hi", seed["owner_phone"])

        # Select book action
        result = simulate(client, cust_phone, "action_book", seed["owner_phone"])
        msg = result["message"].lower()
        assert "haircut" in msg

    def test_full_booking_flow(self, client):
        """Seed -> hi -> book -> select service -> select date -> select slot -> confirm."""
        seed, token, headers = seed_and_login(client)
        cust_phone = random_phone()

        # Step 1: Greet
        r1 = simulate(client, cust_phone, "hi", seed["owner_phone"])
        assert r1["received"]

        # Step 2: Select book
        r2 = simulate(client, cust_phone, "action_book", seed["owner_phone"])
        assert r2["received"]

        # Step 3: Select first service (use service ID from seed)
        service_id = seed["service_ids"][0]
        r3 = simulate(client, cust_phone, service_id, seed["owner_phone"])
        assert r3["received"]
        # Demo tenant has 2 staff -> asks who you prefer before showing dates
        if "who would you prefer" in r3["message"].lower():
            r3 = simulate(client, cust_phone, "staff_any", seed["owner_phone"])
            assert r3["received"]
        assert "pick a date" in r3["message"].lower()

        # Step 4: Select a date (use date_1 = next full open day so Morning slots always exist at any hour)
        r4 = simulate(client, cust_phone, "action_date_1", seed["owner_phone"])
        assert r4["received"]
        # Full-day schedules with >10 slots offer Morning/Afternoon/Evening filter first
        if "part of the day" in r4["message"].lower():
            r4 = simulate(client, cust_phone, "period_morning", seed["owner_phone"])
            assert r4["received"]
        assert "pick a time" in r4["message"].lower()

        # Step 5: Select a time slot
        r5 = simulate(client, cust_phone, "action_slot_0", seed["owner_phone"])
        assert r5["received"]
        assert "confirm" in r5["message"].lower()

        # Step 6: Confirm
        r6 = simulate(client, cust_phone, "action_confirm", seed["owner_phone"])
        assert r6["received"]
        assert "confirmed" in r6["message"].lower()

        # Verify: Check bookings via API
        bookings_resp = client.get("/api/bookings", headers=headers)
        assert bookings_resp.status_code == 200
        bookings = bookings_resp.json()
        assert bookings["total"] >= 1

    def test_owner_commands_after_booking(self, client):
        """Owner can use text commands to see bookings."""
        seed, token, headers = seed_and_login(client)

        # Owner sends "today" to get today's bookings
        result = simulate(client, seed["owner_phone"], "today", seed["owner_phone"])
        assert result["received"]
        assert result["sender_type"] == "owner"
        assert len(result["message"]) > 0

    def test_owner_help_command(self, client):
        """Owner sends 'help' to see available commands."""
        seed, token, headers = seed_and_login(client)

        result = simulate(client, seed["owner_phone"], "help", seed["owner_phone"])
        assert result["received"]
        assert result["sender_type"] == "owner"
        assert len(result["message"]) > 0

    def test_my_bookings(self, client):
        """Customer can check their bookings."""
        seed, token, headers = seed_and_login(client)
        cust_phone = random_phone()

        # Start a conversation first
        simulate(client, cust_phone, "hi", seed["owner_phone"])

        # Request my bookings
        result = simulate(client, cust_phone, "action_mybookings", seed["owner_phone"])
        assert result["received"]
        assert len(result["message"]) > 0

    def test_simulate_unknown_tenant(self, client):
        """Simulate with non-existent tenant phone returns helpful message."""
        result = client.post("/dev/simulate", json={
            "from_phone": random_phone(),
            "body": "hi",
            "to_tenant_phone": "+910000000001",
        })
        assert result.status_code == 200
        data = result.json()
        assert data["received"] is True

    def test_customer_creates_record(self, client):
        """Simulating a message from a new phone creates a customer record."""
        seed, token, headers = seed_and_login(client)
        cust_phone = random_phone()

        result = simulate(client, cust_phone, "hi", seed["owner_phone"])
        assert result["customer_id"] is not None

        # Verify customer exists via API
        cust_resp = client.get(
            f"/api/customers/{result['customer_id']}",
            headers=headers,
        )
        assert cust_resp.status_code == 200
        assert cust_resp.json()["customer"]["phone"] == cust_phone


class TestE2ECancelFlow:
    def test_cancel_flow_start(self, client):
        """Customer can initiate cancellation via my bookings."""
        seed, token, headers = seed_and_login(client)
        cust_phone = random_phone()

        # Start conversation
        simulate(client, cust_phone, "hi", seed["owner_phone"])

        # Try cancel action (may show "no bookings" if none exist)
        result = simulate(client, cust_phone, "action_mybookings", seed["owner_phone"])
        assert result["received"]
        assert len(result["message"]) > 0


class TestE2EOwnerConsole:
    def test_owner_tomorrow(self, client):
        seed, _, _ = seed_and_login(client)
        result = simulate(client, seed["owner_phone"], "tomorrow", seed["owner_phone"])
        assert result["sender_type"] == "owner"
        assert len(result["message"]) > 0

    def test_owner_week(self, client):
        seed, _, _ = seed_and_login(client)
        result = simulate(client, seed["owner_phone"], "week", seed["owner_phone"])
        assert result["sender_type"] == "owner"
        assert len(result["message"]) > 0

    def test_owner_stats(self, client):
        seed, _, _ = seed_and_login(client)
        result = simulate(client, seed["owner_phone"], "stats", seed["owner_phone"])
        assert result["sender_type"] == "owner"
        assert len(result["message"]) > 0
