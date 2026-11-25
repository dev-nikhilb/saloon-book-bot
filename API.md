# API Reference

Complete API reference for all endpoints in the WhatsApp Booking SaaS platform.

**Base URL:** `https://yourdomain.com`

---

## Table of Contents

1. [Health Check](#health-check)
2. [WhatsApp Webhook](#whatsapp-webhook)
3. [Dev Endpoints](#dev-endpoints)
4. [Dashboard API](#dashboard-api)
5. [Payment Webhook](#payment-webhook)

---

## Authentication

### WhatsApp Webhook

WhatsApp webhook endpoints use Meta's signature-based authentication. Every POST request from Meta includes an `X-Hub-Signature-256` header containing an HMAC-SHA256 signature of the request body, computed using your `META_APP_SECRET`.

In production, requests with invalid signatures are rejected with 403. In development mode, signature validation is skipped.

### Dashboard API

Dashboard endpoints use JWT Bearer token authentication. Obtain a token via the auth endpoints, then include it in all subsequent requests:

```
Authorization: Bearer <your_jwt_token>
```

Tokens contain the `tenant_id` in the `sub` claim and expire after 24 hours.

---

## Health Check

### GET /health

Returns the application health status. No authentication required. No rate limiting.

**Response:**

```json
{
  "status": "ok",
  "environment": "production"
}
```

**Example:**

```bash
curl https://yourdomain.com/health
```

**Response Codes:**

| Code | Meaning |
|---|---|
| 200 | Application is healthy |
| 503 | Application is starting up or unhealthy |

---

## WhatsApp Webhook

### GET /webhooks/whatsapp

Meta webhook verification handshake. Called once when you configure the webhook URL in Meta Developer Dashboard.

**Query Parameters:**

| Parameter | Type | Description |
|---|---|---|
| `hub.mode` | string | Must be `subscribe` |
| `hub.verify_token` | string | Must match `META_WEBHOOK_VERIFY_TOKEN` env var |
| `hub.challenge` | string | Challenge string to echo back |

**Response:**

- **200**: Returns the `hub.challenge` string as plain text (verification successful)
- **403**: Verification failed (token mismatch)

**Example:**

```bash
curl "https://yourdomain.com/webhooks/whatsapp?hub.mode=subscribe&hub.verify_token=your_token&hub.challenge=test123"
```

---

### POST /webhooks/whatsapp

Receives inbound WhatsApp messages and status updates from Meta Cloud API.

**Headers:**

| Header | Description |
|---|---|
| `X-Hub-Signature-256` | HMAC-SHA256 signature: `sha256=<hex_digest>` |
| `Content-Type` | `application/json` |

**Request Body (Meta Webhook Payload):**

```json
{
  "object": "whatsapp_business_account",
  "entry": [
    {
      "id": "<WABA_ID>",
      "changes": [
        {
          "value": {
            "messaging_product": "whatsapp",
            "metadata": {
              "display_phone_number": "919999900000",
              "phone_number_id": "109876543210987"
            },
            "contacts": [
              {
                "profile": { "name": "Rahul Kumar" },
                "wa_id": "919876543210"
              }
            ],
            "messages": [
              {
                "from": "919876543210",
                "id": "wamid.HBgMOTE5ODc2NTQzMjEw",
                "timestamp": "1705312200",
                "type": "text",
                "text": { "body": "hi" }
              }
            ]
          },
          "field": "messages"
        }
      ]
    }
  ]
}
```

**Supported Message Types:**

| Type | Handling |
|---|---|
| `text` | Extracts `text.body` |
| `interactive` (button_reply) | Extracts `interactive.button_reply.title` |
| `interactive` (list_reply) | Extracts `interactive.list_reply.title` |
| Other types | Logged as `[type message]` |

**Response:**

- **200**: Always returns `OK` (Meta expects 200 within 5 seconds)
- **403**: Invalid signature (production only)

**Processing Flow:**

1. Validates webhook signature (production) or skips (development)
2. Extracts messages from the Meta payload
3. Looks up tenant by `phone_number_id`
4. Routes message:
   - If sender is the owner phone: routes to `owner_console`
   - If sender is a customer: routes to `conversation/menu_flow`
5. Sends reply via WhatsApp Cloud API
6. Logs inbound message to `messages` table

**Example (simulating with curl):**

```bash
curl -X POST https://yourdomain.com/webhooks/whatsapp \
  -H "Content-Type: application/json" \
  -H "X-Hub-Signature-256: sha256=abc123..." \
  -d '{
    "object": "whatsapp_business_account",
    "entry": [{
      "id": "12345",
      "changes": [{
        "value": {
          "messaging_product": "whatsapp",
          "metadata": {"phone_number_id": "109876543210987"},
          "contacts": [{"profile": {"name": "Rahul"}, "wa_id": "919876543210"}],
          "messages": [{"from": "919876543210", "id": "wamid.xxx", "timestamp": "1705312200", "type": "text", "text": {"body": "hi"}}]
        },
        "field": "messages"
      }]
    }]
  }'
```

---

## Dev Endpoints

These endpoints are only available when `ENVIRONMENT=development`. They simulate WhatsApp messaging without requiring a Meta account.

### POST /dev/simulate

Simulate an inbound WhatsApp message. The full conversation engine processes the message and returns what the bot would reply.

**Request Body:**

```json
{
  "from_phone": "+919876543210",
  "body": "hi",
  "to_tenant_phone": "+919999900000"
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `from_phone` | string | Yes | Sender phone number (customer or owner) |
| `body` | string | Yes | Message text |
| `to_tenant_phone` | string | No | Owner phone to identify tenant. If omitted, uses first active tenant |

**Response:**

```json
{
  "received": true,
  "sender_type": "new_customer",
  "tenant_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "customer_id": "f1e2d3c4-b5a6-7890-abcd-ef1234567890",
  "message": "Hi Rahul! Welcome to *Style Studio*\n\nWhat would you like to book?\n\n  • Haircut — 30min · INR 500\n  • Beard Trim — 15min · INR 200\n\n[View Services]",
  "debug": {
    "is_new_customer": true,
    "raw_input": "hi"
  }
}
```

| Field | Description |
|---|---|
| `sender_type` | `owner`, `new_customer`, `returning_customer`, or `unknown_tenant` |
| `message` | The formatted text the bot would send via WhatsApp. For interactive messages, buttons/lists are rendered as text |
| `debug` | Additional debug information |

**Example -- Customer booking flow:**

```bash
# Step 1: Customer says hi
curl -s -X POST http://localhost:8000/dev/simulate \
  -H "Content-Type: application/json" \
  -d '{"from_phone": "+919876543210", "body": "hi"}' | python -m json.tool

# Step 2: Select first service
curl -s -X POST http://localhost:8000/dev/simulate \
  -H "Content-Type: application/json" \
  -d '{"from_phone": "+919876543210", "body": "svc_0"}' | python -m json.tool

# Step 3: Select first date
curl -s -X POST http://localhost:8000/dev/simulate \
  -H "Content-Type: application/json" \
  -d '{"from_phone": "+919876543210", "body": "date_0"}' | python -m json.tool

# Step 4: Select first time slot
curl -s -X POST http://localhost:8000/dev/simulate \
  -H "Content-Type: application/json" \
  -d '{"from_phone": "+919876543210", "body": "slot_0"}' | python -m json.tool

# Step 5: Confirm booking
curl -s -X POST http://localhost:8000/dev/simulate \
  -H "Content-Type: application/json" \
  -d '{"from_phone": "+919876543210", "body": "confirm_yes"}' | python -m json.tool
```

**Example -- Owner commands:**

```bash
# Today's bookings (use the owner phone number)
curl -s -X POST http://localhost:8000/dev/simulate \
  -H "Content-Type: application/json" \
  -d '{"from_phone": "+919999900000", "body": "today"}' | python -m json.tool

# Weekly stats
curl -s -X POST http://localhost:8000/dev/simulate \
  -H "Content-Type: application/json" \
  -d '{"from_phone": "+919999900000", "body": "stats"}' | python -m json.tool

# Add a service
curl -s -X POST http://localhost:8000/dev/simulate \
  -H "Content-Type: application/json" \
  -d '{"from_phone": "+919999900000", "body": "add service Shave 20 300"}' | python -m json.tool
```

---

### POST /dev/seed

Creates a complete demo salon tenant with branch, staff, and services for testing. If a demo tenant already exists (owner phone `+919999900000`), it is deleted and recreated.

**Request Body:** None

**Response:**

```json
{
  "tenant_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "owner_phone": "+919999900000",
  "branch_id": "b1c2d3e4-f5a6-7890-abcd-ef1234567890",
  "staff_ids": [
    "c1d2e3f4-a5b6-7890-abcd-ef1234567890",
    "d1e2f3a4-b5c6-7890-abcd-ef1234567890"
  ],
  "service_ids": [
    "e1f2a3b4-c5d6-7890-abcd-ef1234567890",
    "f1a2b3c4-d5e6-7890-abcd-ef1234567890",
    "a2b3c4d5-e6f7-8901-abcd-ef1234567890",
    "b2c3d4e5-f6a7-8901-abcd-ef1234567890",
    "c2d3e4f5-a6b7-8901-abcd-ef1234567890"
  ],
  "message": "Demo salon 'Style Studio' created. Owner: Priya (+919999900000). 2 staff, 5 services."
}
```

**Demo tenant details:**

| Attribute | Value |
|---|---|
| Business Name | Style Studio |
| Business Type | salon |
| Owner | Priya (+919999900000) |
| Plan | growth |
| Branch | Main Branch, 42 Anna Nagar, Chennai |

**Staff:**

| Name | Role | Services |
|---|---|---|
| Ravi | stylist | Haircut, Beard Trim, Hair Color |
| Meena | stylist | All 5 services |

**Services:**

| Service | Duration | Price | Deposit |
|---|---|---|---|
| Haircut | 30 min | INR 500 | No |
| Beard Trim | 15 min | INR 200 | No |
| Hair Color | 90 min | INR 2,000 | INR 500 |
| Facial | 60 min | INR 1,500 | No |
| Head Massage | 30 min | INR 400 | No |

**Example:**

```bash
curl -X POST http://localhost:8000/dev/seed | python -m json.tool
```

---

### POST /dev/onboard/message

Simulate the WhatsApp onboarding conversation flow. Each call represents one message in the onboarding conversation. State is maintained in Redis between calls.

**Request Body:**

```json
{
  "phone": "+919876543210",
  "body": "Priya's Hair Studio"
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `phone` | string | Yes | Owner's phone number (used as conversation key) |
| `body` | string | Yes | Message text (the owner's response) |

**Response:**

```json
{
  "reply": "Great! Priya's Hair Studio it is.\n\nWhat type of business?\n  1. Salon\n  2. Barbershop\n  3. Beauty Parlour\n  4. Other\n\nReply with a number."
}
```

**Onboarding Flow Steps:**

| Step | Bot Asks | Expected Reply |
|---|---|---|
| 1 | "What's your business name?" | Business name text |
| 2 | "What type of business?" | 1, 2, 3, or 4 |
| 3 | "What's your name?" | Owner name text |
| 4 | "What are your business hours?" | 1, 2, 3, or 4 |
| 5 | "What's the name of your first service?" | Service name text |
| 6 | "How long does X take?" | Duration in minutes |
| 7 | "What do you charge for X?" | Price in INR |
| 8 | "Add another service?" | 1 (yes) or 2 (no) |
| 9 | Summary -- "Reply YES to go live" | YES or 0 |

**Example -- Complete onboarding:**

```bash
BASE="http://localhost:8000/dev/onboard/message"
PHONE="+919876543210"

# Start
curl -s -X POST $BASE -H "Content-Type: application/json" \
  -d "{\"phone\": \"$PHONE\", \"body\": \"start\"}"

# Business name
curl -s -X POST $BASE -H "Content-Type: application/json" \
  -d "{\"phone\": \"$PHONE\", \"body\": \"Priya's Salon\"}"

# Business type: salon
curl -s -X POST $BASE -H "Content-Type: application/json" \
  -d "{\"phone\": \"$PHONE\", \"body\": \"1\"}"

# Owner name
curl -s -X POST $BASE -H "Content-Type: application/json" \
  -d "{\"phone\": \"$PHONE\", \"body\": \"Priya\"}"

# Hours: Mon-Sat 9am-8pm
curl -s -X POST $BASE -H "Content-Type: application/json" \
  -d "{\"phone\": \"$PHONE\", \"body\": \"1\"}"

# Service name
curl -s -X POST $BASE -H "Content-Type: application/json" \
  -d "{\"phone\": \"$PHONE\", \"body\": \"Haircut\"}"

# Duration
curl -s -X POST $BASE -H "Content-Type: application/json" \
  -d "{\"phone\": \"$PHONE\", \"body\": \"30\"}"

# Price
curl -s -X POST $BASE -H "Content-Type: application/json" \
  -d "{\"phone\": \"$PHONE\", \"body\": \"500\"}"

# No more services
curl -s -X POST $BASE -H "Content-Type: application/json" \
  -d "{\"phone\": \"$PHONE\", \"body\": \"2\"}"

# Confirm
curl -s -X POST $BASE -H "Content-Type: application/json" \
  -d "{\"phone\": \"$PHONE\", \"body\": \"yes\"}"
```

---

## Dashboard API

Dashboard API endpoints are mounted at `/api/*` and require JWT authentication. These are used by the web-based admin dashboard.

> **Note:** The dashboard API is currently under active development. The following documents the authentication system and planned endpoint structure.

### Authentication

#### POST /api/auth/login

Authenticate a tenant owner and receive a JWT token.

**Request Body:**

```json
{
  "phone": "+919999900000",
  "otp": "123456"
}
```

**Response:**

```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "bearer",
  "tenant_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "expires_in": 86400
}
```

**JWT Token Payload:**

```json
{
  "sub": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "phone": "+919999900000",
  "iat": 1705312200,
  "exp": 1705398600
}
```

- `sub`: Tenant UUID (used for all tenant-scoped queries)
- Token expires after 24 hours
- Secret derived from `ENCRYPTION_KEY` environment variable

### Planned Dashboard Endpoints

The following endpoints are planned for the dashboard API. All require `Authorization: Bearer <token>`.

#### Tenant Management

| Method | Path | Description |
|---|---|---|
| GET | `/api/tenant` | Get current tenant details |
| PUT | `/api/tenant` | Update tenant details |
| GET | `/api/tenant/settings` | Get tenant settings |
| PUT | `/api/tenant/settings` | Update tenant settings |

#### Services

| Method | Path | Description |
|---|---|---|
| GET | `/api/services` | List all services |
| POST | `/api/services` | Create a service |
| GET | `/api/services/:id` | Get service details |
| PUT | `/api/services/:id` | Update a service |
| DELETE | `/api/services/:id` | Deactivate a service |

#### Staff

| Method | Path | Description |
|---|---|---|
| GET | `/api/staff` | List all staff |
| POST | `/api/staff` | Create a staff member |
| GET | `/api/staff/:id` | Get staff details |
| PUT | `/api/staff/:id` | Update a staff member |
| DELETE | `/api/staff/:id` | Deactivate a staff member |

#### Bookings

| Method | Path | Description |
|---|---|---|
| GET | `/api/bookings` | List bookings (filterable by date, status, staff) |
| GET | `/api/bookings/:id` | Get booking details |
| PUT | `/api/bookings/:id/status` | Update booking status |

#### Customers

| Method | Path | Description |
|---|---|---|
| GET | `/api/customers` | List customers |
| GET | `/api/customers/:id` | Get customer details with booking history |

#### Analytics

| Method | Path | Description |
|---|---|---|
| GET | `/api/analytics/summary` | Booking counts, revenue, cancellation rates |
| GET | `/api/analytics/popular-services` | Most booked services |
| GET | `/api/analytics/peak-hours` | Busiest time slots |

#### Branches

| Method | Path | Description |
|---|---|---|
| GET | `/api/branches` | List branches |
| POST | `/api/branches` | Create a branch |
| PUT | `/api/branches/:id` | Update a branch |

---

## Payment Webhook

### POST /payments/webhook

Receives payment status updates from Razorpay.

> **Note:** Payment integration is under active development.

**Headers:**

| Header | Description |
|---|---|
| `X-Razorpay-Signature` | HMAC-SHA256 signature of the request body using `RAZORPAY_WEBHOOK_SECRET` |

**Request Body (Razorpay Webhook Event):**

```json
{
  "entity": "event",
  "event": "payment.captured",
  "payload": {
    "payment": {
      "entity": {
        "id": "pay_XXXXXXXXXX",
        "order_id": "order_XXXXXXXXXX",
        "amount": 50000,
        "currency": "INR",
        "status": "captured",
        "method": "upi"
      }
    }
  }
}
```

**Supported Events:**

| Event | Action |
|---|---|
| `payment.captured` | Confirm the booking (transition from `pending_deposit` to `confirmed`) |
| `payment.failed` | Log failure, notify customer to retry |
| `refund.processed` | Log refund |

**Response:**

- **200**: Event processed
- **400**: Invalid payload
- **403**: Invalid signature

---

## Rate Limits

### Nginx Layer

| Path | Rate | Burst |
|---|---|---|
| `/webhooks/*` | 30 requests/second | 50 |
| All other paths | 10 requests/second | 20 |

### Application Layer (Redis-based)

| Path Prefix | Requests | Window |
|---|---|---|
| `/webhooks/` | 100 | 60 seconds |
| `/dev/` | 30 | 60 seconds |
| Default | 60 | 60 seconds |

Rate limits are per IP address. When exceeded, the API returns:

```
HTTP 429 Too Many Requests
Body: "Rate limited"
```

The `/health` endpoint is exempt from rate limiting.

---

## Error Responses

All error responses follow this general format:

```json
{
  "detail": "Error description"
}
```

### Common HTTP Status Codes

| Code | Meaning |
|---|---|
| 200 | Success |
| 400 | Bad request (invalid input) |
| 401 | Unauthorized (missing or invalid JWT) |
| 403 | Forbidden (invalid webhook signature) |
| 404 | Resource not found |
| 429 | Rate limited |
| 500 | Internal server error |

---

## Message Types in WhatsApp Responses

The conversation engine returns three types of messages, which the webhook handler sends via the WhatsApp Cloud API:

### Text Message

Plain text reply.

```json
{
  "messaging_product": "whatsapp",
  "to": "919876543210",
  "type": "text",
  "text": { "body": "Booking cancelled. Send any message to start over." }
}
```

### Reply Buttons (up to 3)

Tappable button chips below the message.

```json
{
  "messaging_product": "whatsapp",
  "to": "919876543210",
  "type": "interactive",
  "interactive": {
    "type": "button",
    "body": { "text": "Confirm your booking?" },
    "action": {
      "buttons": [
        { "type": "reply", "reply": { "id": "confirm_yes", "title": "Confirm" } },
        { "type": "reply", "reply": { "id": "confirm_no", "title": "Cancel" } }
      ]
    }
  }
}
```

### List Message (up to 10 items)

Expandable list that the customer taps to reveal, then selects from.

```json
{
  "messaging_product": "whatsapp",
  "to": "919876543210",
  "type": "interactive",
  "interactive": {
    "type": "list",
    "body": { "text": "What would you like to book?" },
    "action": {
      "button": "View Services",
      "sections": [
        {
          "title": "Services",
          "rows": [
            { "id": "svc_0", "title": "Haircut", "description": "30min - INR 500" },
            { "id": "svc_1", "title": "Beard Trim", "description": "15min - INR 200" }
          ]
        }
      ]
    }
  }
}
```
