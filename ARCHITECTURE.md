# System Architecture

Technical architecture of the WhatsApp Booking SaaS platform.

---

## Table of Contents

1. [System Overview](#system-overview)
2. [Data Model](#data-model)
3. [Request Flow](#request-flow)
4. [Booking Engine](#booking-engine)
5. [Multi-Tenancy](#multi-tenancy)
6. [State Management](#state-management)
7. [Background Tasks](#background-tasks)
8. [Security](#security)
9. [Service Hierarchy](#service-hierarchy)

---

## System Overview

### High-Level Architecture

```
                                    +------------------+
                                    |   Meta Cloud API  |
                                    | (WhatsApp Biz API)|
                                    +--------+---------+
                                             |
                              HTTPS webhooks | Outbound messages
                                             |
                                    +--------v---------+
                                    |      Nginx       |
                                    | (SSL + rate limit)|
                                    +--------+---------+
                                             |
                           +-----------------+------------------+
                           |                                    |
                  +--------v---------+               +----------v----------+
                  |   FastAPI App    |               |   Dashboard Web UI  |
                  | (uvicorn, 2 wkr) |               |    (planned)        |
                  +--------+---------+               +---------------------+
                           |
          +----------------+----------------+
          |                |                |
 +--------v------+ +------v------+ +-------v-------+
 |  PostgreSQL   | |    Redis    | | Celery Workers |
 |  (data store) | | (state/cache| | (reminders,    |
 |               | |  rate limit,| |  feedback,     |
 |               | |  Celery     | |  analytics)    |
 |               | |  broker)    | |                |
 +---------------+ +-------------+ +----------------+
```

### Component Descriptions

| Component | Technology | Purpose |
|---|---|---|
| **FastAPI App** | Python 3.11, FastAPI, uvicorn | HTTP server handling webhooks, API requests, conversation engine |
| **PostgreSQL** | PostgreSQL 15 | Primary data store: tenants, bookings, services, staff, customers, messages |
| **Redis** | Redis 7 | Conversation state (TTL 30min), rate limiting counters, Celery broker |
| **Celery Workers** | Celery 5.4+ | Background tasks: reminders, feedback requests, analytics |
| **Celery Beat** | Celery Beat | Periodic task scheduler (reminder scan every 5 minutes) |
| **Nginx** | Nginx (Alpine) | Reverse proxy, SSL termination, request rate limiting |
| **WhatsApp Cloud API** | Meta Graph API v21.0 | Sending/receiving WhatsApp messages |

### Directory Structure

```
app/
  main.py              # FastAPI app creation, router registration, middleware
  config.py            # Pydantic settings (env vars)
  database.py          # SQLAlchemy async engine, session factory
  middleware.py         # Request ID middleware
  rate_limit.py        # Redis-based rate limiter middleware
  exceptions.py        # Custom exception hierarchy
  celery_app.py        # Celery config, beat schedule, autodiscover
  dev_simulate.py      # Dev-only simulate/seed endpoints

  messaging/
    whatsapp/
      webhook_router.py  # GET/POST /webhooks/whatsapp
      webhook.py         # Payload processing, message routing
      client.py          # Outbound: text, buttons, lists, templates
      security.py        # HMAC-SHA256 signature validation
    models.py            # Message table
    i18n.py              # Multi-language template system
    templates/           # YAML message templates (en, hi, ta)

  conversation/
    state.py             # Redis conversation state (ConversationStep enum, TTL)
    menu_flow.py         # Booking conversation: service -> date -> slot -> confirm

  bookings/
    models.py            # Booking table, BookingStatus enum
    slots.py             # Slot calculation engine (the most critical code)
    repository.py        # Booking CRUD with row-level locking
    state_machine.py     # Valid status transitions

  tenants/
    models.py            # Tenant, Branch, Staff, Service, TenantSettings tables

  customers/
    models.py            # Customer table

  owner_console/
    handler.py           # Owner message dispatcher
    parser.py            # Typo-tolerant command parser
    commands.py          # Extended commands (add service, add staff, etc.)

  onboarding/
    flow.py              # WhatsApp onboarding conversation state machine
    router.py            # Dev endpoint for onboarding simulation

  reminders/
    tasks.py             # Celery task: scan for upcoming bookings, send reminders
    models.py            # ReminderRule, ReminderLog tables

  payments/
    models.py            # Payment table

  billing/
    models.py            # Subscription, MessageCreditLedger tables

  feedback/
    models.py            # Feedback table

  dashboard/
    auth.py              # JWT creation, validation, FastAPI dependency

  analytics/             # Analytics module (planned)
  calendar_sync/         # Google Calendar sync (planned)
  rebooking/             # Rebooking nudges (planned)
```

---

## Data Model

### Entity Relationship Diagram

```
+------------------+       +-------------------+       +------------------+
|     tenants      |       |     branches      |       |      staff       |
+------------------+       +-------------------+       +------------------+
| id (PK, UUID)   |<------| tenant_id (FK)    |       | id (PK, UUID)   |
| business_name    |  1:N  | id (PK, UUID)     |  1:N  | tenant_id (FK)  |----+
| business_type    |       | name              |<------| branch_id       |    |
| owner_phone (UQ) |       | address           |       | name            |    |
| owner_name       |       | is_primary        |       | role            |    |
| primary_language |       | operating_hours{} |       | bio             |    |
| timezone         |       | created_at        |       | specialty       |    |
| waba_phone_id   |       +-------------------+       | skills {}       |    |
| waba_token (enc) |                                   | working_hours{} |    |
| plan_tier        |       +-------------------+       | is_active       |    |
| is_active        |       |     services      |       | created_at      |    |
| onboarded_at     |       +-------------------+       +--------+--------+    |
| created_at       |<------| tenant_id (FK)    |                |             |
| updated_at       |  1:N  | id (PK, UUID)     |                |             |
+--------+---------+       | parent_service_id |--+             |             |
         |                 | name              |  | self-ref     |             |
         |                 | description       |  | (parent/     |             |
         |                 | image_url         |  |  child)      |             |
         |                 | duration_minutes  |  |              |             |
         |                 | buffer_minutes    |<-+              |             |
         |                 | price_paise       |                 |             |
         |                 | requires_deposit  |                 |             |
         |                 | deposit_amt_paise |                 |             |
         |                 | is_active         |                 |             |
         |                 | sort_order        |                 |             |
         |                 | created_at        |                 |             |
         |                 +---------+---------+                 |             |
         |                           |                           |             |
         |    +----------------------+---------------------------+             |
         |    |                      |                                         |
         |    |             +--------v---------+                               |
         |    |             |     bookings     |                               |
         |    |             +------------------+                               |
         |    +------------>| tenant_id        |                               |
         |                  | id (PK, UUID)    |                               |
         |                  | branch_id        |<------------------------------+
         |                  | staff_id         |
         |                  | customer_id      |----------+
         |                  | service_id       |          |
         |                  | starts_at        |          |
         |                  | ends_at          |          |
         |                  | status           |   +------v----------+
         |                  | idempotency_key  |   |    customers    |
         |                  | deposit_pay_id   |   +-----------------+
         |                  | cancelled_at     |   | id (PK, UUID)  |
         |                  | cancel_reason    |   | tenant_id (FK) |
         |                  | google_event_id  |   | phone          |
         |                  | created_at       |   | name           |
         |                  | updated_at       |   | pref_language  |
         |                  +------------------+   | first_seen_at  |
         |                                         | last_visit_at  |
         |                                         | total_bookings |
         |                                         | total_no_shows |
         |                                         | notes          |
         |                                         +----------------+
         |
         |    +-------------------+     +-------------------+
         +--->| tenant_settings   |     |     messages      |
         |    +-------------------+     +-------------------+
         |    | id (PK, UUID)    |     | id (PK, UUID)     |
         |    | tenant_id (FK,UQ)|     | tenant_id         |
         |    | advance_book_days|     | customer_id       |
         |    | min_cancel_hours |     | direction          |
         |    | slot_increment   |     | category           |
         |    | max_bookings/cust|     | waba_message_id    |
         |    | send_reminders   |     | template_name      |
         |    | reminder_hrs     |     | body               |
         |    | send_feedback    |     | status             |
         |    | feedback_delay   |     | error              |
         |    | welcome_message  |     | cost_paise         |
         |    | confirm_message  |     | created_at         |
         |    | google_review_url|     +-------------------+
         |    | gcal_enabled     |
         |    | razorpay_enabled |     +-------------------+
         |    | created_at       |     |    payments       |
         |    | updated_at       |     +-------------------+
         |    +-------------------+     | id (PK, UUID)    |
         |                              | tenant_id        |
         |    +-------------------+     | booking_id       |
         +--->|  subscriptions    |     | rp_order_id      |
         |    +-------------------+     | rp_payment_id    |
         |    | id (PK, UUID)    |     | amount_paise      |
         |    | tenant_id        |     | status             |
         |    | plan_tier        |     | created_at         |
         |    | billing_cycle    |     +-------------------+
         |    | period_start     |
         |    | period_end       |     +-------------------+
         |    | status           |     |    feedback       |
         |    | trial_ends_at    |     +-------------------+
         |    +-------------------+     | id (PK, UUID)    |
         |                              | booking_id       |
         |    +---------------------+   | tenant_id        |
         +--->| msg_credit_ledger  |   | rating            |
              +---------------------+   | comment           |
              | id (PK, UUID)      |   | google_prompted   |
              | tenant_id          |   | created_at         |
              | category           |   +-------------------+
              | quantity           |
              | amount_paise       |   +-------------------+
              | ref_message_id     |   |  reminder_rules   |
              | created_at         |   +-------------------+
              +---------------------+   | id (PK, UUID)    |
                                        | tenant_id        |
              +---------------------+   | service_id       |
              |   reminder_log      |   | hours_before      |
              +---------------------+   | template_key      |
              | id (PK, UUID)      |   | is_active          |
              | booking_id         |   +-------------------+
              | rule_id            |
              | sent_at            |
              | UQ(booking,rule)   |
              +---------------------+
```

### Key Indexes

| Table | Index | Purpose |
|---|---|---|
| `bookings` | `idx_bookings_tenant_starts (tenant_id, starts_at)` | Fast date-range queries per tenant |
| `bookings` | `idx_bookings_staff_starts (staff_id, starts_at) WHERE status IN ('pending_deposit', 'confirmed')` | Conflict detection during slot calculation |
| `messages` | `idx_messages_tenant_created (tenant_id, created_at)` | Message history queries |
| `customers` | `uq_customers_tenant_phone (tenant_id, phone)` | Unique customer per tenant per phone |
| `tenants` | `uq_tenants_owner_phone (owner_phone)` | One tenant per owner phone |

### Booking Status State Machine

```
                    +-------------------+
                    | pending_deposit   |
                    +--------+----------+
                             |
                    payment  |  timeout/cancel
                    received |
                +------------+----------+
                |                       |
       +--------v--------+    +--------v--------+
       |    confirmed     |    |    cancelled    |
       +--------+---------+    +-----------------+
                |                  (terminal)
       +--------+--------+
       |        |        |
  +----v---+ +--v----+ +-v---------+
  |completed| |no_show| | cancelled |
  +--------+ +-------+ +-----------+
  (terminal) (terminal)  (terminal)
```

Valid transitions:
- `pending_deposit` -> `confirmed` (payment received)
- `pending_deposit` -> `cancelled` (timeout or customer cancel)
- `confirmed` -> `completed` (service delivered)
- `confirmed` -> `cancelled` (customer/owner cancel)
- `confirmed` -> `no_show` (customer did not show up)

---

## Request Flow

### Inbound WhatsApp Message Flow

```
Customer sends         Meta Cloud API         Your Server
"hi" on WhatsApp  -->  POST /webhooks/  -->  webhook_router.py
                       whatsapp                    |
                                             (1) Validate signature
                                                   |
                                             (2) webhook.py: extract message
                                                   |
                                             (3) Lookup tenant by phone_number_id
                                                   |
                                    +--------------+---------------+
                                    |                              |
                              sender == owner?              sender is customer
                                    |                              |
                           owner_console/              (4) Find/create customer
                           handler.py                        |
                              |                        (5) Get Redis state
                              |                              |
                        parse_command()               menu_flow.py
                              |                        handle_customer_message()
                        execute command                     |
                              |                   +--------+--------+
                              |                   |        |        |
                              |              IDLE  AWAIT   AWAIT  AWAIT
                              |              show  SERVICE  DATE   CONFIRM
                              |              menu  pick    pick    ...
                              |                   |        |        |
                              +---+               +--------+--------+
                                  |                        |
                            (6) Build reply          (6) Build reply
                                  |                        |
                            (7) Send via WhatsApp Cloud API
                                  |
                            (8) Log to messages table
```

### Outbound Message Types

The WhatsApp client (`client.py`) supports:

| Type | Function | WhatsApp Limits |
|---|---|---|
| Text | `send_text_message()` | Max 4096 characters |
| Reply Buttons | `send_reply_buttons()` | Max 3 buttons, 20 chars per title |
| List | `send_list_message()` | Max 10 rows total, 24 chars per title |
| Template | `send_template_message()` | Must be pre-approved by Meta |

---

## Booking Engine

The slot calculation engine (`bookings/slots.py`) is the most critical code in the system.

### Slot Calculation Algorithm

```
get_available_slots(tenant_id, service_id, target_date)
    |
    +-- 1. Get service (duration_minutes, buffer_minutes)
    |
    +-- 2. Get eligible staff
    |       - If preferred_staff_id: just that staff
    |       - Otherwise: all active staff with matching skills
    |       - Skills matching: staff.skills JSON array contains service_id
    |
    +-- 3. For each staff member:
    |       |
    |       +-- 3a. Get working hours for this day
    |       |       - Check staff.working_hours[day_name] first
    |       |       - Fallback to branch.operating_hours[day_name]
    |       |       - day_name = lowercase 3-letter: mon, tue, wed...
    |       |       - Returns list of WorkingWindow(open_time, close_time)
    |       |
    |       +-- 3b. Get existing bookings for this staff on this date
    |       |       - Query bookings WHERE staff_id AND date AND status
    |       |         IN ('pending_deposit', 'confirmed')
    |       |       - Returns list of (starts_at, ends_at) tuples
    |       |
    |       +-- 3c. Generate slots for each working window
    |               - Start at window open_time
    |               - Advance by SLOT_INCREMENT (15 minutes)
    |               - For each candidate slot:
    |                   - total_block = duration + buffer
    |                   - Check: does [slot_start, slot_start + total_block]
    |                     overlap with any booked range?
    |                   - If no conflict: add TimeSlot to results
    |               - Stop when slot + duration > window close_time
    |
    +-- 4. Merge all slots, sort by (start_time, staff_name)
    |
    +-- 5. Filter out slots in the past (start > now)
    |
    +-- Return list of TimeSlot objects
```

### Conflict Prevention (3-Layer Defence)

Double-booking prevention is the system's primary reliability guarantee. Three layers work together:

```
Layer 1: Slot Calculator (app/bookings/slots.py)
  - Only shows available slots to customers
  - Checks against existing bookings before presenting options
  - Accounts for duration + buffer time

Layer 2: Row-Level Locking (app/bookings/repository.py)
  - SELECT FOR UPDATE on staff's bookings for the day
  - Locks rows before checking for conflicts
  - Application-level overlap check before INSERT
  - Prevents race conditions between concurrent requests

Layer 3: PostgreSQL Exclusion Constraint
  - Database-level constraint: no_overlapping_staff_bookings
  - Uses tstzrange exclusion to prevent overlaps
  - Final safety net if layers 1 and 2 are somehow bypassed
  - Returns error that is caught and converted to SlotConflictError
```

### Booking Creation Flow

```
create_booking(tenant_id, branch_id, staff_id, customer_id, service_id, starts_at, ends_at)
    |
    +-- 1. SELECT FOR UPDATE: lock staff's bookings for this day
    |      (prevents concurrent inserts from racing)
    |
    +-- 2. Check overlap: any existing booking where
    |      starts_at < new_ends_at AND ends_at > new_starts_at?
    |      If yes: raise SlotConflictError
    |
    +-- 3. INSERT booking with status:
    |      - requires_deposit=true  -> status='pending_deposit'
    |      - requires_deposit=false -> status='confirmed'
    |
    +-- 4. UPDATE customer stats (total_bookings + 1)
    |
    +-- 5. COMMIT transaction
    |
    +-- If exclusion constraint violation: raise SlotConflictError
```

---

## Multi-Tenancy

### Tenant Isolation Model

The system uses **shared database, shared schema** multi-tenancy. Every table that contains tenant-specific data has a `tenant_id` column.

### Every-Query-Filters Pattern

**Critical rule:** Every database query that touches tenant data MUST filter by `tenant_id`. This is enforced by convention, not by automatic filters.

```python
# CORRECT: always filter by tenant_id
result = await db.execute(
    text("SELECT * FROM bookings WHERE tenant_id = :tid AND ..."),
    {"tid": tenant_id},
)

# WRONG: missing tenant_id filter (security vulnerability)
result = await db.execute(
    text("SELECT * FROM bookings WHERE id = :bid"),
    {"bid": booking_id},
)
```

The repository layer (`bookings/repository.py`) enforces this:

> "CRITICAL: Every method takes tenant_id. Every query filters by tenant_id."

### Tenant Identification

- **WhatsApp webhook:** Tenant is identified by `phone_number_id` from the Meta payload. Each tenant has a unique `waba_phone_number_id` in the tenants table.
- **Dashboard API:** Tenant is identified by `tenant_id` in the JWT token (`sub` claim).
- **Owner console:** Owner is identified by matching `sender_phone == tenant.owner_phone`.

### Data Isolation Points

| Data | Isolation Mechanism |
|---|---|
| Bookings | `tenant_id` column, filtered in all queries |
| Customers | `tenant_id` column + unique constraint on `(tenant_id, phone)` |
| Services | `tenant_id` column |
| Staff | `tenant_id` column |
| Messages | `tenant_id` column |
| Conversation state | Redis key: `conv:{tenant_id}:{customer_phone}` |
| Settings | `tenant_id` foreign key (one-to-one) |

### Cross-Tenant Access Prevention

The `CrossTenantAccessError` exception exists for cases where cross-tenant access is detected. This is a critical security error that should trigger alerts.

---

## State Management

### Redis Conversation State

Each customer's booking conversation is tracked in Redis with a per-tenant, per-phone key.

**Key format:** `conv:{tenant_id}:{customer_phone}`

**TTL:** 1800 seconds (30 minutes). If the customer does not respond within 30 minutes, the conversation resets.

### Conversation State Machine

```
                    +--------+
                    |  IDLE  |
                    +---+----+
                        |
                   any message
                        |
              +---------v---------+
              | AWAITING_SERVICE  |
              | (show service     |
              |  list/menu)       |
              +--------+----------+
                       |
              select service (or parent category)
                       |
          +------------+------------+
          |                         |
  parent category           standalone service
          |                         |
+---------v-----------+    +--------v----------+
| AWAITING_SUBCATEGORY|    | AWAITING_DATE     |
| (show child         |    | (show date list)  |
|  services)          |    +--------+----------+
+---------+-----------+             |
          |                    select date
     select child                   |
          |                +--------v----------+
          +--------------->| AWAITING_SLOT     |
                           | (show time slots) |
                           +--------+----------+
                                    |
                               select slot
                                    |
                           +--------v----------+
                           | AWAITING_CONFIRM  |
                           | (show summary     |
                           |  with Yes/No)     |
                           +--------+----------+
                                    |
                         confirm_yes / confirm_no
                                    |
                     +--------------+--------------+
                     |                             |
                +----v----+                  +-----v-----+
                | BOOKING |                  | CANCELLED |
                | CREATED |                  | (reset)   |
                +---------+                  +-----------+
```

**Global commands** (work from any state):
- `cancel`, `exit`, `quit`, `back`, `0` -- reset to IDLE
- `human`, `agent`, `help me` -- request owner callback
- `hi`, `hello`, `hey`, `book`, `start`, `menu` -- restart from main menu

### ConversationState Data Structure

```python
@dataclass
class ConversationState:
    step: str                    # Current ConversationStep
    tenant_id: str               # Tenant UUID
    customer_id: str             # Customer UUID
    selected_service_id: str     # Chosen service UUID
    selected_service_name: str   # Display name
    selected_date: str           # ISO date (e.g., "2025-01-20")
    selected_slot_index: int     # Index in the displayed list
    selected_staff_id: str       # Chosen staff UUID
    selected_staff_name: str     # Staff display name
    selected_start: str          # ISO datetime for booking start
    selected_end: str            # ISO datetime for booking end
    selected_parent_id: str      # Parent category UUID (if hierarchical)
    selected_parent_name: str    # Parent category name
    booking_id_to_cancel: str    # Booking UUID (for cancel flow)
    available_slots_json: str    # Cached slots/dates JSON
    services_json: str           # Cached services list JSON
```

### Onboarding State

Separate from the booking conversation, the onboarding flow uses its own Redis state:

**Key format:** `onboard:{phone}`

**TTL:** 3600 seconds (1 hour)

**Steps:** WELCOME -> BUSINESS_NAME -> BUSINESS_TYPE -> OWNER_NAME -> HOURS -> ADD_SERVICE -> SERVICE_DURATION -> SERVICE_PRICE -> MORE_SERVICES -> CONFIRM -> DONE

---

## Background Tasks

### Celery Configuration

```python
celery.conf.update(
    timezone="Asia/Kolkata",
    enable_utc=True,
    task_acks_late=True,              # Acknowledge after completion
    worker_prefetch_multiplier=1,     # Fair scheduling
    task_default_retry_delay=60,      # 1 minute between retries
    task_max_retries=5,               # Max 5 retries
)
```

### Beat Schedule

| Task | Schedule | Description |
|---|---|---|
| `reminders.scan_and_send` | Every 5 minutes | Scan for bookings starting in ~2 hours, send reminders |

### Reminder Task Flow

```
scan_and_send_reminders() [every 5 min]
    |
    +-- Calculate reminder window:
    |     now + 2 hours +/- 10 minutes
    |
    +-- Query bookings:
    |     WHERE status = 'confirmed'
    |     AND starts_at BETWEEN window_start AND window_end
    |     AND NOT blocked (cancellation_reason != 'BLOCKED')
    |     AND NOT already reminded (NOT EXISTS in reminder_log)
    |
    +-- For each booking:
    |     +-- Build reminder message (IST timezone)
    |     +-- Send via WhatsApp (if WABA configured)
    |     +-- Insert into reminder_log (prevents duplicates)
    |
    +-- reminder_log unique constraint: (booking_id, rule_id)
        prevents the same reminder from being sent twice
```

### Task Auto-Discovery

Celery auto-discovers tasks from these modules:

- `app.messaging` -- message sending tasks
- `app.calendar_sync` -- Google Calendar sync
- `app.reminders` -- appointment reminders
- `app.payments` -- payment processing
- `app.rebooking` -- rebooking nudges
- `app.feedback` -- feedback collection
- `app.billing` -- billing and metering
- `app.analytics` -- analytics computation

---

## Security

### Webhook Signature Verification

Meta signs every webhook payload with HMAC-SHA256 using the App Secret.

```
Verification process:
1. Read X-Hub-Signature-256 header (format: "sha256=<hex>")
2. Compute HMAC-SHA256(META_APP_SECRET, request_body)
3. Constant-time comparison (hmac.compare_digest)
4. Reject if mismatch (production only; skipped in development)
```

**Implementation:** `app/messaging/whatsapp/security.py`

### Rate Limiting

Two-layer rate limiting:

**Layer 1 -- Nginx:**

```nginx
limit_req_zone $binary_remote_addr zone=webhook:10m rate=30r/s;
limit_req_zone $binary_remote_addr zone=api:10m rate=10r/s;
```

**Layer 2 -- Application (Redis):**

| Path | Limit | Window |
|---|---|---|
| `/webhooks/` | 100 requests | 60 seconds |
| `/dev/` | 30 requests | 60 seconds |
| Default | 60 requests | 60 seconds |

Rate limit keys: `rl:{client_ip}:{path_prefix}`

Health checks (`/health`) are exempt from rate limiting.

### Encryption

- WABA access tokens are stored encrypted in `waba_access_token_encrypted` (LargeBinary column)
- Google OAuth refresh tokens are encrypted similarly
- Encryption key: Fernet symmetric key from `ENCRYPTION_KEY` env var

### JWT Authentication

- Dashboard API uses HS256 JWTs
- Secret derived from `ENCRYPTION_KEY`
- Tokens expire after 24 hours
- Contains: `sub` (tenant_id), `phone` (owner_phone), `iat`, `exp`

### Request Tracing

- Every request gets a UUID via `RequestIDMiddleware`
- Propagated via `X-Request-ID` header
- Bound to all structlog entries for correlation

### Attack Surface Mitigation

Nginx blocks common attack paths:

```nginx
location ~ /\. { deny all; }
location ~ ^/(wp-admin|wp-login|xmlrpc|phpmyadmin) { return 444; }
```

---

## Service Hierarchy

### Parent/Child Services

Services support a two-level hierarchy via the `parent_service_id` self-referencing foreign key.

```
Services Table:
  id                  -- UUID primary key
  parent_service_id   -- FK to services.id (null for top-level)
  name, duration, price, etc.

Relationships:
  parent: Service | None  (self-referential, remote_side=[id])
  children: list[Service] (cascade delete-orphan)
```

### How Hierarchy Works

**Top-level items** (parent_service_id IS NULL) can be:
1. **Standalone services** -- directly bookable, have duration and price
2. **Parent categories** -- grouping containers, have children

The distinction is determined by whether the service has children (child_count > 0).

### Display in WhatsApp

```
Service List (WhatsApp List Message):
  +----------------------------+
  | [folder icon] Hair Services |   <-- parent category (3 options)
  | Beard Trim -- 15min INR 200 |   <-- standalone service
  | Head Massage -- 30min INR 400| <-- standalone service
  +----------------------------+

When parent category is selected:
  +----------------------------+
  | Haircut -- 30min INR 500    |   <-- child service
  | Hair Color -- 90min INR 2000|   <-- child service
  | Hair Smoothing -- 120min    |   <-- child service
  +----------------------------+
```

### Flow with Categories

1. Customer sees main menu: mix of parent categories and standalone services
2. If customer selects a parent category: show child services as a sub-list
3. Customer selects a child service: proceed to date selection
4. If customer selects a standalone service: proceed directly to date selection

### Dashboard Display

In the dashboard (planned), services show as a tree structure with drag-and-drop reordering. Parent categories group their children visually, and the `sort_order` field controls display order within each level.
