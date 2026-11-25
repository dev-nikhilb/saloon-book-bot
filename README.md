# Booking SaaS — WhatsApp-First Appointment Booking for Indian Service Businesses

A multi-tenant SaaS that lets salons, clinics, tutors, and other service businesses in India manage appointments entirely through WhatsApp. Customers book by chatting. Owners manage their schedule by texting commands. A web dashboard exists but is secondary.

## Architecture

```
Customer (WhatsApp) ──→ Meta Cloud API ──→ Webhook ──→ Celery ──→ Conversation Engine
                                                                        │
Owner (WhatsApp) ──→ Meta Cloud API ──→ Webhook ──→ Celery ──→ Owner Console
                                                                        │
                                                          ┌─────────────┼─────────────┐
                                                          ▼             ▼             ▼
                                                     Bookings     Calendar Sync   Payments
                                                     (PostgreSQL)  (Google Cal)    (Razorpay)
```

### Core idea

The calendar is the product. Every design decision optimises for zero double-bookings. PostgreSQL exclusion constraints prevent overlapping staff bookings at the database level. Application-layer slot calculation provides the UX. Both layers must agree — if app logic has a bug, the DB rejects the insert.

### Multi-tenancy

Every table has `tenant_id`. Every query is scoped. Tenant isolation is tested explicitly — two tenants are created in tests and we verify that neither can see the other's data through any code path.

### Conversation handling

Two modes per tenant:
- **Menu-driven** (default): Reliable numbered-option flows. "Reply 1 for Haircut, 2 for Beard Trim."
- **AI-driven** (opt-in): Claude extracts structured booking data from natural language, including Tamil and Hindi.

The conversation router picks the mode. New tenants start with menu. AI mode is enabled after tenant confirms.

### Owner console

Owners text their WhatsApp number with commands like `today`, `block tomorrow 4-6pm`, `stats`. A forgiving parser handles casual typing. No app or dashboard needed for daily operations.

## Tech stack

| Component | Tech |
|-----------|------|
| Web framework | FastAPI |
| Database | PostgreSQL 15 |
| Cache / broker | Redis 7 |
| Task queue | Celery |
| ORM | SQLAlchemy 2.0 + Alembic |
| Validation | Pydantic v2 |
| WhatsApp | Meta Cloud API (direct) |
| Calendar | Google Calendar API (2-way sync) |
| Payments | Razorpay (UPI + cards) |
| AI/NLU | Anthropic Claude |
| Logging | structlog |
| Deployment | Docker + docker-compose |

## Running locally

```bash
# Copy env and fill in secrets
cp .env.example .env

# Start infrastructure
docker compose up -d db redis

# Install Python deps
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Run migrations
alembic upgrade head

# Start app
uvicorn app.main:app --reload

# Start worker (separate terminal)
celery -A app.celery_app:celery worker --loglevel=info

# Start beat (separate terminal)
celery -A app.celery_app:celery beat --loglevel=info
```

Or run everything via Docker:

```bash
docker compose up
```

## Running tests

```bash
pytest -v
```

## Project structure

- `app/tenants/` — Tenant, Branch, Staff, Service models and management
- `app/customers/` — End-customer (the business's customers) data
- `app/bookings/` — Core booking engine: slot calculation, conflict detection, state machine
- `app/messaging/` — WhatsApp Cloud API client, webhook handler, i18n templates
- `app/conversation/` — Intent detection, menu flows, AI flows, conversation state
- `app/owner_console/` — Owner WhatsApp command parser and handlers
- `app/reminders/` — Configurable reminder rules and Celery-based sending
- `app/calendar_sync/` — Google Calendar 2-way sync
- `app/payments/` — Razorpay deposit collection and refunds
- `app/rebooking/` — Churn prevention via rebooking nudges
- `app/feedback/` — Post-visit feedback and Google review prompts
- `app/analytics/` — Reports and daily owner digest
- `app/billing/` — SaaS plan tiers (Solo/Growth/Pro) and message credit tracking
- `app/auth/` — WhatsApp OTP-based authentication
- `app/dashboard/` — Minimal HTMX web dashboard

## Plan tiers

| Feature | Solo (₹499) | Growth (₹1,499) | Pro (₹2,999) |
|---------|-------------|------------------|---------------|
| Staff | 1 | 3 | Unlimited |
| Branches | 1 | 1 | Multi-branch |
| Bookings/month | 300 | 1,500 | Unlimited |
| Languages | EN + 1 | All | All |
| Deposits | No | Yes | Yes |
| Rebooking | No | Yes | Yes |
| Dashboard | No | No | Yes |
