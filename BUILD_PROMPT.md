# BUILD: WhatsApp-First Appointment Booking SaaS for Indian Service Businesses

You are building a production-grade SaaS application. Read this entire document before writing any code. Then build it incrementally with tests. Do not skip the testing — calendar bugs lose customers permanently.

## What we're building (one paragraph)

A multi-tenant SaaS where Indian service businesses (solo tutors, single-doctor clinics, salons with 1–10 staff, multi-branch chains) let their customers book appointments on WhatsApp. The owner never needs a dashboard for core operations — they manage everything via WhatsApp commands to a control number, with an optional web dashboard for analytics. The system handles bookings, reminders, reschedules, cancellations, deposits (UPI), multi-staff routing, multi-branch, multi-language conversation (English + Tamil + Hindi to start), rebooking nudges, feedback collection, and Google review prompts.

## Non-negotiable principles

1. **Calendar is the product.** Double-bookings are the worst failure mode. Engineer with paranoia: row-level locks, idempotency keys, transactional booking, conflict detection at every layer.
2. **Multi-tenant from day one.** Every table has `tenant_id`. Every query is scoped. No shortcuts.
3. **WhatsApp is the primary interface for both customer AND owner.** The dashboard is secondary.
4. **Modular by feature.** Each feature lives in its own module (`bookings/`, `payments/`, `messaging/`, etc.) so future engineers can work in parallel.
5. **Webhook-driven, async-first.** No long-running HTTP requests. All WhatsApp messages processed via Celery tasks.
6. **Test the critical paths.** Bookings, reminders, payments, calendar sync — unit + integration tests required. UI tests not required.
7. **Vernacular support means real i18n.** No hardcoded English strings in user-facing messages. All templates externalised.
8. **Observability from day one.** Structured logs, request IDs, error tracking hooks (Sentry-compatible).

## Tech stack (locked — do not propose alternatives)

- Python 3.11
- FastAPI (web framework)
- PostgreSQL 15 (primary database)
- Redis 7 (caching + Celery broker)
- Celery (async task processing)
- SQLAlchemy 2.0 + Alembic (ORM + migrations)
- Pydantic v2 (validation)
- httpx (HTTP client)
- pytest + pytest-asyncio (testing)
- Anthropic SDK (`anthropic` Python package) for conversation understanding
- Google API Python client (Calendar API)
- Razorpay Python SDK
- structlog (logging)
- Docker + docker-compose

## Repository structure

```
booking-saas/
├── README.md
├── pyproject.toml
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── alembic.ini
├── alembic/
│   └── versions/
├── app/
│   ├── __init__.py
│   ├── main.py                    # FastAPI entry
│   ├── config.py                  # Pydantic settings
│   ├── database.py                # SQLAlchemy session
│   ├── celery_app.py              # Celery config
│   ├── exceptions.py              # Custom exceptions
│   ├── middleware.py              # Request ID, logging
│   │
│   ├── tenants/                   # Tenant (business) management
│   │   ├── models.py              # Tenant, Branch, Staff, Service
│   │   ├── schemas.py
│   │   ├── repository.py
│   │   ├── service.py
│   │   └── router.py
│   │
│   ├── customers/                 # End-customer (the salon's customers)
│   │   ├── models.py              # Customer, CustomerNote
│   │   ├── schemas.py
│   │   ├── repository.py
│   │   └── service.py
│   │
│   ├── bookings/                  # The core
│   │   ├── models.py              # Booking, BookingStatus, BookingHistory
│   │   ├── schemas.py
│   │   ├── repository.py
│   │   ├── service.py             # Slot calculation, conflict detection
│   │   ├── state_machine.py       # PENDING -> CONFIRMED -> COMPLETED/CANCELLED/NO_SHOW
│   │   └── router.py
│   │
│   ├── calendar_sync/             # Google Calendar 2-way sync
│   │   ├── google_client.py
│   │   ├── sync_service.py
│   │   └── tasks.py               # Celery: push to GCal, pull from GCal
│   │
│   ├── messaging/                 # WhatsApp + future SMS
│   │   ├── whatsapp/
│   │   │   ├── client.py          # Meta Cloud API wrapper
│   │   │   ├── webhook.py         # Inbound message handler
│   │   │   ├── templates.py       # Template message senders
│   │   │   └── webhook_router.py
│   │   ├── templates/             # Message templates per language
│   │   │   ├── en.yaml
│   │   │   ├── ta.yaml            # Tamil
│   │   │   └── hi.yaml            # Hindi
│   │   ├── i18n.py                # Template loader + variable substitution
│   │   └── tasks.py               # Celery: send messages, retry on failure
│   │
│   ├── conversation/              # AI-driven conversation handling
│   │   ├── intents.py             # BOOK, RESCHEDULE, CANCEL, ASK_AVAILABILITY, etc.
│   │   ├── menu_flow.py           # Menu-driven fallback (reliable)
│   │   ├── ai_flow.py             # Claude-driven NLU
│   │   ├── router.py              # Decides menu vs AI based on tenant config
│   │   ├── state.py               # Per-customer conversation state (Redis)
│   │   └── handlers/              # One handler per intent
│   │       ├── booking_handler.py
│   │       ├── reschedule_handler.py
│   │       ├── cancel_handler.py
│   │       └── owner_command_handler.py
│   │
│   ├── owner_console/             # Owner manages business via WhatsApp
│   │   ├── commands.py            # "Block tomorrow 4-6pm", "Today's bookings", etc.
│   │   ├── parser.py
│   │   └── handler.py
│   │
│   ├── reminders/                 # Configurable reminder system
│   │   ├── models.py              # ReminderRule, ReminderLog
│   │   ├── scheduler.py           # Celery beat: scan for upcoming appointments
│   │   ├── service.py
│   │   └── tasks.py
│   │
│   ├── payments/                  # Razorpay UPI deposits
│   │   ├── razorpay_client.py
│   │   ├── models.py              # Payment, Refund
│   │   ├── service.py
│   │   ├── webhook.py
│   │   └── tasks.py
│   │
│   ├── rebooking/                 # Rebooking nudges + churn prevention
│   │   ├── service.py             # Detects customers due for rebooking
│   │   ├── tasks.py               # Celery: send rebooking nudges
│   │   └── rules.py               # Per-tenant rebooking interval rules
│   │
│   ├── feedback/                  # Post-visit feedback + Google review
│   │   ├── models.py
│   │   ├── service.py
│   │   └── tasks.py
│   │
│   ├── analytics/                 # Owner-facing reports
│   │   ├── service.py             # No-show rate, booking trends, revenue
│   │   ├── daily_digest.py        # Daily WhatsApp summary to owner
│   │   └── router.py              # Dashboard endpoints
│   │
│   ├── billing/                   # Our SaaS billing (Solo/Growth/Pro tiers)
│   │   ├── models.py              # Plan, Subscription, Invoice, MessageCredit
│   │   ├── service.py
│   │   ├── tasks.py               # Monthly invoice generation
│   │   └── router.py
│   │
│   ├── auth/                      # WhatsApp OTP-based auth
│   │   ├── otp.py
│   │   ├── jwt.py
│   │   └── router.py
│   │
│   └── dashboard/                 # Minimal web dashboard (HTMX-served from FastAPI)
│       ├── templates/
│       ├── static/
│       └── router.py
│
└── tests/
    ├── conftest.py
    ├── test_bookings.py           # Slot calculation, conflict detection, state transitions
    ├── test_calendar_sync.py
    ├── test_messaging.py
    ├── test_conversation.py
    ├── test_reminders.py
    ├── test_payments.py
    └── test_multi_tenant.py       # Critical: isolation tests
```

## Data model — get this right or everything else fails

### Core tables (PostgreSQL)

```sql
-- Tenants are the businesses paying us
CREATE TABLE tenants (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_name TEXT NOT NULL,
    business_type TEXT NOT NULL,  -- 'salon', 'clinic', 'tutor', 'trainer', etc.
    owner_phone TEXT NOT NULL UNIQUE,
    owner_name TEXT,
    primary_language TEXT NOT NULL DEFAULT 'en',  -- 'en', 'ta', 'hi'
    timezone TEXT NOT NULL DEFAULT 'Asia/Kolkata',
    waba_phone_number_id TEXT,    -- Meta WABA phone ID
    waba_access_token_encrypted BYTEA,
    plan_tier TEXT NOT NULL DEFAULT 'solo',  -- 'solo', 'growth', 'pro'
    is_active BOOLEAN NOT NULL DEFAULT true,
    onboarded_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE branches (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    address TEXT,
    is_primary BOOLEAN NOT NULL DEFAULT false,
    operating_hours JSONB NOT NULL,  -- {mon: [{open:"09:00",close:"20:00"}], ...}
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE staff (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    branch_id UUID REFERENCES branches(id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    role TEXT,  -- 'stylist', 'doctor', 'tutor'
    skills JSONB,  -- service IDs they can perform
    working_hours JSONB,  -- overrides branch hours
    google_calendar_id TEXT,
    google_oauth_refresh_token_encrypted BYTEA,
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE services (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    duration_minutes INT NOT NULL,
    price_paise INT,  -- store paise to avoid float errors
    requires_deposit BOOLEAN NOT NULL DEFAULT false,
    deposit_amount_paise INT,
    is_active BOOLEAN NOT NULL DEFAULT true,
    sort_order INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE customers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    phone TEXT NOT NULL,
    name TEXT,
    preferred_language TEXT,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_visit_at TIMESTAMPTZ,
    total_bookings INT NOT NULL DEFAULT 0,
    total_no_shows INT NOT NULL DEFAULT 0,
    notes TEXT,
    UNIQUE(tenant_id, phone)
);

CREATE TABLE bookings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    branch_id UUID NOT NULL REFERENCES branches(id),
    staff_id UUID REFERENCES staff(id),
    customer_id UUID NOT NULL REFERENCES customers(id),
    service_id UUID NOT NULL REFERENCES services(id),
    starts_at TIMESTAMPTZ NOT NULL,
    ends_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL,  -- 'pending_deposit', 'confirmed', 'completed', 'cancelled', 'no_show'
    deposit_payment_id UUID,
    idempotency_key TEXT UNIQUE,  -- prevents double-booking from retries
    cancelled_at TIMESTAMPTZ,
    cancellation_reason TEXT,
    google_event_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Critical: exclusion constraint prevents overlapping bookings for same staff
-- Applied via Alembic migration with raw SQL

CREATE INDEX idx_bookings_tenant_starts ON bookings(tenant_id, starts_at);
CREATE INDEX idx_bookings_staff_starts ON bookings(staff_id, starts_at) WHERE status IN ('pending_deposit', 'confirmed');

-- Message log: every WhatsApp message in/out, for debugging and billing
CREATE TABLE messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    customer_id UUID REFERENCES customers(id),
    direction TEXT NOT NULL,  -- 'inbound', 'outbound'
    category TEXT,            -- 'service', 'utility', 'marketing', 'authentication'
    waba_message_id TEXT,
    template_name TEXT,
    body TEXT,
    status TEXT,              -- 'queued', 'sent', 'delivered', 'read', 'failed'
    error TEXT,
    cost_paise INT,           -- Meta's charge for billing reconciliation
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_messages_tenant_created ON messages(tenant_id, created_at);

-- Reminder rules per tenant
CREATE TABLE reminder_rules (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    service_id UUID REFERENCES services(id),  -- null = applies to all services
    hours_before INT NOT NULL,
    template_key TEXT NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT true
);

CREATE TABLE reminder_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    booking_id UUID NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
    rule_id UUID NOT NULL REFERENCES reminder_rules(id),
    sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(booking_id, rule_id)  -- never send same reminder twice
);

CREATE TABLE payments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    booking_id UUID REFERENCES bookings(id),
    razorpay_order_id TEXT,
    razorpay_payment_id TEXT,
    amount_paise INT NOT NULL,
    status TEXT NOT NULL,  -- 'created', 'authorized', 'captured', 'failed', 'refunded'
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE feedback (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    booking_id UUID NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    rating INT,  -- 1-5
    comment TEXT,
    google_review_prompted BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Our SaaS billing
CREATE TABLE subscriptions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    plan_tier TEXT NOT NULL,
    billing_cycle TEXT NOT NULL,  -- 'monthly', 'annual'
    current_period_start TIMESTAMPTZ NOT NULL,
    current_period_end TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL,  -- 'trial', 'active', 'past_due', 'cancelled'
    trial_ends_at TIMESTAMPTZ
);

CREATE TABLE message_credit_ledger (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    category TEXT NOT NULL,
    quantity INT NOT NULL,        -- positive = used, negative = topped up
    amount_paise INT NOT NULL,
    reference_message_id UUID REFERENCES messages(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

## Conversation flow specifications

### Customer-facing flow (the booking)

A customer messages the tenant's WhatsApp number. The bot:

1. Identifies if customer is new or returning (lookup by phone within tenant scope)
2. Detects intent: BOOK / RESCHEDULE / CANCEL / ASK_AVAILABILITY / ASK_SERVICES / ASK_PRICE / HUMAN_HELP / GENERIC
3. For BOOK: collect service → preferred date → preferred staff (if multi-staff) → show available slots → confirm → request deposit if required → finalise
4. For RESCHEDULE: identify booking → offer alternatives → confirm → release old slot atomically
5. For CANCEL: identify booking → confirm → release slot
6. At any point, customer can type "human" or equivalent → bot pauses, alerts owner

**The conversation router decides between menu-driven (reliable) and AI-driven (natural) based on tenant config.** New tenants default to menu; AI is enabled per-tenant after they've confirmed they want it. The AI flow uses Claude with a strict system prompt that limits it to booking-related actions — never let Claude invent booking logic, only let it extract structured data.

### Owner-facing flow (the operations)

The owner sends messages to the same WhatsApp number (system recognises owner by phone). Commands include:

- `today` → list today's bookings
- `tomorrow` → list tomorrow's bookings
- `bookings <date>` → list specific date
- `block <date> <time-range>` → mark unavailable
- `unblock <date> <time-range>` → restore availability
- `add staff <name> <phone>` → onboard staff
- `mark <booking-id> done` → mark complete
- `mark <booking-id> no-show` → mark no-show
- `cancel <booking-id> [reason]` → cancel
- `pause` → temporarily pause new bookings (e.g., illness)
- `resume` → re-enable bookings
- `stats` → no-show rate, booking count, revenue this week
- `help` → command list

Implement a forgiving parser (Levenshtein/regex hybrid) — owners will type casually.

### Multi-language handling

- Each tenant has a `primary_language`
- Each customer has `preferred_language` (auto-detected from first message, can be changed)
- All outbound templates exist in `app/messaging/templates/{lang}.yaml`
- AI conversation: pass language code to Claude system prompt; instruct Claude to respond in that language
- Owner commands: parse English + Tamil + Hindi
- Meta templates: must be pre-approved in all supported languages

## Slot calculation algorithm (the most important code in the system)

When a customer asks for available slots:

```
INPUT: tenant_id, service_id, preferred_staff_id (optional), date_range
OUTPUT: list of available time slots

1. Get service.duration_minutes
2. Get eligible staff:
   - If preferred_staff_id given, use that one
   - Otherwise, all active staff whose `skills` include this service_id
3. For each staff member:
   a. Get their working_hours for each date in range
   b. Subtract any blocked time (from "block" command)
   c. Subtract any existing bookings (status IN pending_deposit, confirmed)
   d. Generate possible start times in 15-minute increments
   e. For each candidate start time, check: start + duration fits within working hours AND doesn't overlap any existing booking
4. Merge available slots across all eligible staff
5. Return sorted list, deduplicated, with staff_id attached

CRITICAL: Wrap the actual booking insert in:
  BEGIN;
  SELECT ... FOR UPDATE on the staff's bookings table for the day;
  INSERT booking;
  COMMIT;

The PostgreSQL exclusion constraint is the final defence.
```

## Reminder system

Celery Beat job runs every 5 minutes:
1. Query all confirmed bookings where `starts_at` is within the next 48 hours AND has reminder rules with `hours_before` matching now-ish
2. For each, check `reminder_log` — skip if already sent
3. Enqueue `send_reminder` task per (booking, rule)
4. Task sends WhatsApp utility template, logs to `reminder_log`, logs to `messages`

Default rules per tenant (created at onboarding, editable):
- 24 hours before
- 3 hours before
- 1 hour before (premium plans only)

## Deposit flow (no-show killer)

If `service.requires_deposit` is true:

1. After customer confirms slot, create `booking` with status `pending_deposit` (this still reserves the slot)
2. Create Razorpay order; send UPI payment link via WhatsApp template
3. Wait for Razorpay webhook → on capture, update booking to `confirmed`
4. Timeout (configurable, default 15 min): if no payment, cancel booking, release slot, notify customer
5. Refund logic: if booking cancelled by customer >= X hours before (configurable, default 24), auto-refund. Otherwise, forfeit.

## Rebooking nudges

Per service, tenant configures a rebooking interval. Daily Celery job:
1. Finds customers whose `last_visit_at` for a given service is approaching the interval
2. Sends a marketing template
3. Logs to `messages` for billing

Respect quiet hours (no messages 9pm–9am IST by default).

## Multi-tenant safety rules

- **Every repository method takes `tenant_id` as first argument.** No exceptions.
- **Every query filters by `tenant_id`.** Write a SQLAlchemy event listener that raises if a query against a tenant-scoped table doesn't filter by tenant_id.
- **Cross-tenant data leak is the worst possible bug.** Write integration tests that spin up 2 tenants and verify isolation.
- **Owner can only command on their own tenant.** Phone-to-tenant mapping is checked on every owner message.

## Plan tier enforcement

- **Solo (₹499):** 1 staff, 1 branch, 300 bookings/month, English + 1 vernacular
- **Growth (₹1,499):** Up to 3 staff, 1 branch, 1,500 bookings/month, all languages, deposits, rebooking, feedback
- **Pro (₹2,999):** Unlimited staff, multi-branch, unlimited bookings, analytics dashboard, priority support

## Environment variables (`.env.example`)

```
DATABASE_URL=postgresql+psycopg://booking:booking@localhost:5432/booking
REDIS_URL=redis://localhost:6379/0
CELERY_BROKER_URL=redis://localhost:6379/1
ENCRYPTION_KEY=

META_APP_ID=
META_APP_SECRET=
META_WEBHOOK_VERIFY_TOKEN=

GOOGLE_OAUTH_CLIENT_ID=
GOOGLE_OAUTH_CLIENT_SECRET=
GOOGLE_OAUTH_REDIRECT_URI=

RAZORPAY_KEY_ID=
RAZORPAY_KEY_SECRET=
RAZORPAY_WEBHOOK_SECRET=

ANTHROPIC_API_KEY=
CLAUDE_MODEL=claude-sonnet-4-5

SENTRY_DSN=
LOG_LEVEL=INFO
ENVIRONMENT=development
```

## Build order (phases)

1. Project scaffold, Docker Compose, database, Alembic, basic FastAPI health check
2. Multi-tenant models with exclusion constraint and tenant isolation tests
3. Slot calculation service + thorough tests
4. WhatsApp Cloud API client, webhook receiver, message log, outbound templates with i18n
5. Conversation router + menu-driven booking flow end-to-end
6. Owner command parser + handlers
7. Reminder system
8. Google Calendar 2-way sync
9. Razorpay deposits
10. Claude-based conversation flow
11. Multi-language templates (Tamil, Hindi)
12. Rebooking nudges
13. Feedback + Google review prompt
14. Minimal dashboard
15. Plan tier enforcement + billing
16. Onboarding flow
17. Analytics + daily owner digest
18. Hardening, observability, deploy
