# Configuration Reference

Complete reference for all configurable options in the WhatsApp Booking SaaS platform.

---

## Table of Contents

1. [Environment Variables](#environment-variables)
2. [Tenant Settings](#tenant-settings)
3. [Booking Rules](#booking-rules)
4. [Operating Hours](#operating-hours)
5. [Service Configuration](#service-configuration)
6. [Staff Configuration](#staff-configuration)
7. [Notification Templates](#notification-templates)
8. [Plan Tiers](#plan-tiers)

---

## Environment Variables

All environment variables are loaded via Pydantic Settings from the `.env` file (development) or `.env.prod` file (production). Defined in `app/config.py`.

### Database

| Variable | Description | Default | Required |
|---|---|---|---|
| `DATABASE_URL` | PostgreSQL connection string. Must use the `psycopg` async driver. | `postgresql+psycopg://booking:booking@localhost:5432/booking` | Yes |
| `REDIS_URL` | Redis connection string for caching, conversation state, and rate limiting. | `redis://localhost:6379/0` | Yes |
| `CELERY_BROKER_URL` | Redis connection string for Celery task broker. Should use a different Redis DB number than `REDIS_URL` to isolate task queues from application state. | `redis://localhost:6379/1` | Yes |

### Security

| Variable | Description | Default | Required |
|---|---|---|---|
| `ENCRYPTION_KEY` | Fernet symmetric encryption key for encrypting sensitive data at rest (WABA tokens, OAuth tokens). Generate with: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` | `""` | Yes (production) |
| `META_APP_SECRET` | Meta App Secret from the Meta Developer Dashboard. Used for HMAC-SHA256 webhook signature validation and as fallback WABA access token. | `""` | Yes (production) |
| `META_WEBHOOK_VERIFY_TOKEN` | Arbitrary string used during Meta webhook URL verification handshake. You choose this value and enter it in both Meta Dashboard and your environment. | `changeme` | Yes |
| `META_APP_ID` | Meta App ID from the Meta Developer Dashboard. Used for embedded signup and API calls. | `""` | No |

### Google OAuth (Calendar Sync)

| Variable | Description | Default | Required |
|---|---|---|---|
| `GOOGLE_OAUTH_CLIENT_ID` | Google OAuth 2.0 client ID for Google Calendar integration. Create in Google Cloud Console. | `""` | No |
| `GOOGLE_OAUTH_CLIENT_SECRET` | Google OAuth 2.0 client secret. | `""` | No |
| `GOOGLE_OAUTH_REDIRECT_URI` | OAuth redirect URI. Must match what is configured in Google Cloud Console. | `http://localhost:8000/auth/google/callback` | No |

### Payments (Razorpay)

| Variable | Description | Default | Required |
|---|---|---|---|
| `RAZORPAY_KEY_ID` | Razorpay API Key ID. Get from Razorpay Dashboard > Settings > API Keys. Use `rzp_test_` prefix for test mode, `rzp_live_` for production. | `""` | No |
| `RAZORPAY_KEY_SECRET` | Razorpay API Key Secret. Shown once when generating keys. | `""` | No |
| `RAZORPAY_WEBHOOK_SECRET` | Secret for validating Razorpay webhook signatures. Set in Razorpay Dashboard > Webhooks. | `""` | No |

### AI (Gemini)

| Variable | Description | Default | Required |
|---|---|---|---|
| `GEMINI_API_KEY` | Google Gemini API key for AI-powered conversation features. Get from Google AI Studio. | `""` | No |
| `GEMINI_MODEL` | Gemini model identifier. | `gemini-2.5-flash` | No |

### Observability

| Variable | Description | Default | Required |
|---|---|---|---|
| `SENTRY_DSN` | Sentry Data Source Name for error tracking. Get from your Sentry project settings. When set, Sentry SDK is initialized automatically. | `""` | No |
| `LOG_LEVEL` | Application logging level. Controls structlog filtering. | `INFO` | No |
| `ENVIRONMENT` | Runtime environment. Affects: logging format (JSON in production, console in development), webhook signature validation (skipped in development), dev endpoint availability. | `development` | Yes |

**Valid `LOG_LEVEL` values:** `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`

**Valid `ENVIRONMENT` values:** `development`, `production`

### Docker-Only Variables

These are used in `docker-compose.prod.yml` for provisioning PostgreSQL and Redis containers:

| Variable | Description | Default | Required |
|---|---|---|---|
| `DB_USER` | PostgreSQL username for the container. | `booking` | Yes (Docker) |
| `DB_PASSWORD` | PostgreSQL password for the container. | None | Yes (Docker) |
| `DB_NAME` | PostgreSQL database name for the container. | `booking` | Yes (Docker) |
| `REDIS_PASSWORD` | Redis password for the container. | None | Yes (Docker) |

### Example .env (Development)

```env
DATABASE_URL=postgresql+psycopg://booking:booking@localhost:5432/booking
REDIS_URL=redis://localhost:6379/0
CELERY_BROKER_URL=redis://localhost:6379/1
ENCRYPTION_KEY=
META_APP_ID=
META_APP_SECRET=
META_WEBHOOK_VERIFY_TOKEN=changeme
GOOGLE_OAUTH_CLIENT_ID=
GOOGLE_OAUTH_CLIENT_SECRET=
GOOGLE_OAUTH_REDIRECT_URI=http://localhost:8000/auth/google/callback
RAZORPAY_KEY_ID=
RAZORPAY_KEY_SECRET=
RAZORPAY_WEBHOOK_SECRET=
GEMINI_API_KEY=
GEMINI_MODEL=gemini-2.5-flash
SENTRY_DSN=
LOG_LEVEL=INFO
ENVIRONMENT=development
```

### Example .env.prod (Production)

```env
DATABASE_URL=postgresql+psycopg://booking:STRONG_PASSWORD@db:5432/booking
REDIS_URL=redis://:REDIS_PASSWORD@redis:6379/0
CELERY_BROKER_URL=redis://:REDIS_PASSWORD@redis:6379/1
DB_USER=booking
DB_PASSWORD=STRONG_PASSWORD
DB_NAME=booking
REDIS_PASSWORD=REDIS_PASSWORD
ENCRYPTION_KEY=<generate-fernet-key>
META_APP_ID=your_app_id
META_APP_SECRET=your_app_secret
META_WEBHOOK_VERIFY_TOKEN=your_random_verify_token
RAZORPAY_KEY_ID=rzp_live_xxxxxxxxxx
RAZORPAY_KEY_SECRET=your_razorpay_secret
RAZORPAY_WEBHOOK_SECRET=your_webhook_secret
SENTRY_DSN=https://xxx@xxx.ingest.sentry.io/xxx
LOG_LEVEL=INFO
ENVIRONMENT=production
```

---

## Tenant Settings

Per-tenant configurable options stored in the `tenant_settings` table. Each tenant has exactly one settings row (one-to-one relationship via `tenant_id` foreign key).

### Booking Rules

| Setting | Column | Type | Default | Description |
|---|---|---|---|---|
| Advance booking window | `advance_booking_days` | integer | 14 | Maximum number of days into the future a customer can book. The slot calculator checks up to this many days ahead. |
| Cancellation policy | `min_cancel_hours` | integer | 2 | Minimum hours before an appointment that a customer can cancel. Cancellation requests within this window are rejected. |
| Slot increment | `slot_increment_minutes` | integer | 15 | Time gap between consecutive slot options. A 15-minute increment means slots at 9:00, 9:15, 9:30, etc. Smaller values show more options but make lists longer. |
| Max bookings per customer | `max_bookings_per_customer` | integer | 5 | Maximum number of active (confirmed/pending_deposit) bookings a single customer can have simultaneously. |

### Notification Settings

| Setting | Column | Type | Default | Description |
|---|---|---|---|---|
| Send reminders | `send_reminders` | boolean | true | Whether to send appointment reminder messages to customers before their booking. |
| Reminder timing | `reminder_hours_before` | integer | 24 | How many hours before the appointment to send the reminder. The Celery beat task scans for bookings in a window around this time. |
| Send feedback | `send_feedback_request` | boolean | true | Whether to send a feedback request message after the appointment is completed. |
| Feedback timing | `feedback_delay_hours` | integer | 2 | How many hours after the appointment ends to send the feedback request. |

### Display Settings

| Setting | Column | Type | Default | Description |
|---|---|---|---|---|
| Welcome message | `welcome_message` | text | null | Custom welcome message sent to new customers on their first interaction. When null, the default template is used: "Welcome to {business_name}!" |
| Confirmation message | `booking_confirmed_message` | text | null | Custom message sent after a booking is confirmed. When null, the default template is used. |
| Google Review URL | `google_review_url` | text | null | Google Maps review URL. When set, customers who give high ratings in feedback are prompted to leave a Google review. Format: `https://g.page/r/YOUR_PLACE_ID/review` |

### Integration Settings

| Setting | Column | Type | Default | Description |
|---|---|---|---|---|
| Google Calendar | `google_calendar_enabled` | boolean | false | Whether Google Calendar sync is active for this tenant. When enabled, bookings create Google Calendar events for the assigned staff member. |
| Razorpay payments | `razorpay_enabled` | boolean | false | Whether Razorpay payment collection is active. When enabled, services marked with `requires_deposit = true` will trigger a payment link before confirming. |

---

## Booking Rules

Detailed explanation of each booking rule and its edge cases.

### advance_booking_days

**What it does:** Limits how far into the future customers can see and book slots.

**Default:** 14 days

**How it works:** The slot calculator (`get_available_dates`) scans from today up to `advance_booking_days` into the future, looking for dates with at least one available slot.

**Edge cases:**
- If set to 0: only same-day bookings are possible
- If set to 1: today and tomorrow
- The scan actually checks up to `num_days + 14` calendar days to find enough dates with availability (in case some days are fully booked or closed)

**Example:** Set to 7 for businesses that do not want bookings more than a week ahead.

### min_cancel_hours

**What it does:** Prevents last-minute cancellations.

**Default:** 2 hours

**How it works:** When a customer attempts to cancel, the system checks if the booking's `starts_at` is more than `min_cancel_hours` from now. If not, the cancellation is rejected.

**Edge cases:**
- Set to 0 to allow cancellation at any time (even during the appointment)
- Owner cancellations bypass this check -- owners can always cancel
- This does not apply to no-show marking (done after the appointment)

**Example:** A salon with expensive color treatments might set this to 24 hours.

### slot_increment_minutes

**What it does:** Controls the granularity of time slot options.

**Default:** 15 minutes

**How it works:** The slot generator advances by this increment through each working window. For example, with 15-minute increments: 9:00, 9:15, 9:30, 9:45, 10:00...

**Edge cases:**
- Must divide evenly into service durations for optimal results
- Very small increments (5 min) generate many slots -- WhatsApp list messages max out at 10 items, so excess slots are paginated or truncated
- Very large increments (60 min) may miss valid bookable times

**Recommended values:** 15 (most common), 30 (for longer services like 1-hour treatments), 10 (for quick services like 10-minute trims).

### max_bookings_per_customer

**What it does:** Prevents a single customer from hoarding appointment slots.

**Default:** 5

**How it works:** Before creating a booking, the system counts the customer's active bookings (status in `confirmed`, `pending_deposit`).

**Edge cases:**
- Completed and cancelled bookings do not count against the limit
- Owner-created bookings (manual) also count
- If a customer has exactly the max, they must cancel one before booking another

---

## Operating Hours

### JSON Format

Operating hours are stored as JSONB in both `branches.operating_hours` and `staff.working_hours`.

```json
{
  "mon": [{"open": "09:00", "close": "20:00"}],
  "tue": [{"open": "09:00", "close": "20:00"}],
  "wed": [{"open": "09:00", "close": "13:00"}, {"open": "14:00", "close": "20:00"}],
  "thu": [{"open": "09:00", "close": "20:00"}],
  "fri": [{"open": "09:00", "close": "21:00"}],
  "sat": [{"open": "10:00", "close": "21:00"}],
  "sun": []
}
```

### Key Rules

**Day names:** Lowercase 3-letter abbreviations: `mon`, `tue`, `wed`, `thu`, `fri`, `sat`, `sun`.

**Time format:** 24-hour `HH:MM` strings. Examples: `"09:00"`, `"13:30"`, `"21:00"`.

**Multiple windows per day:** Each day is an array of `{open, close}` objects. This supports:
- **Split shifts:** `[{"open": "09:00", "close": "13:00"}, {"open": "15:00", "close": "20:00"}]` (closed 1pm-3pm for lunch)
- **Extended evening:** Normal hours plus late evening for special services

**Closed days:** Set the day to an empty array `[]` or omit the day entirely. Both mean no slots are generated.

### Priority: Staff vs Branch Hours

The slot calculator checks hours in this order:
1. **Staff-level hours** (`staff.working_hours`) -- checked first
2. **Branch-level hours** (`branches.operating_hours`) -- fallback if staff hours are null or empty

This allows individual staff members to have different schedules from the branch default. For example, a senior stylist might work only afternoons.

### Holiday Handling

Currently, holidays are handled by:
1. **Owner blocks:** Use the `block` command to block time off (`block tomorrow 9am-8pm`)
2. **Manual hours update:** Set the day's hours to `[]` in the database

A formal holiday calendar feature is planned for future development.

### Common Presets

These presets are offered during WhatsApp onboarding:

| Preset | Days | Hours |
|---|---|---|
| 1 | Mon-Sat | 9:00 AM - 8:00 PM |
| 2 | Mon-Sat | 10:00 AM - 9:00 PM |
| 3 | All days (Mon-Sun) | 10:00 AM - 8:00 PM |

### Example: Typical Indian Salon Hours

```json
{
  "mon": [{"open": "09:30", "close": "13:00"}, {"open": "14:00", "close": "20:30"}],
  "tue": [{"open": "09:30", "close": "13:00"}, {"open": "14:00", "close": "20:30"}],
  "wed": [],
  "thu": [{"open": "09:30", "close": "13:00"}, {"open": "14:00", "close": "20:30"}],
  "fri": [{"open": "09:30", "close": "13:00"}, {"open": "14:00", "close": "21:00"}],
  "sat": [{"open": "09:00", "close": "21:00"}],
  "sun": [{"open": "10:00", "close": "18:00"}]
}
```

This represents a salon closed on Wednesdays, with a lunch break, and extended Friday/Saturday hours.

---

## Service Configuration

### Service Fields

| Field | Type | Description | Required | Default |
|---|---|---|---|---|
| `name` | text | Display name of the service. Shown in WhatsApp list messages (max 24 characters visible). | Yes | -- |
| `description` | text | Detailed description. Shown in dashboard and WhatsApp list row descriptions (max 72 characters visible). | No | null |
| `image_url` | text | URL to a service image. Used in dashboard. | No | null |
| `duration_minutes` | integer | How long the service takes in minutes. Used by the slot calculator to determine booking end time. | Yes | -- |
| `buffer_minutes` | integer | Gap time after each booking in minutes. Prevents back-to-back bookings. Used for cleanup, transition time, or sanitization between customers. | No | 0 |
| `price_paise` | integer | Price in paise (INR x 100). ₹500 = 50000 paise. Displayed as ₹500 in WhatsApp messages. | No | null |
| `requires_deposit` | boolean | Whether a deposit payment is required before the booking is confirmed. When true, booking starts in `pending_deposit` status. | No | false |
| `deposit_amount_paise` | integer | Deposit amount in paise. Only used when `requires_deposit` is true. | No | null |
| `is_active` | boolean | Whether the service is visible and bookable. Deactivated services are hidden but not deleted (preserves booking history). | No | true |
| `sort_order` | integer | Display order in the service menu. Lower numbers appear first. | No | 0 |
| `parent_service_id` | UUID | Foreign key to parent service. When set, this service is a child of the parent category. When null, this is a top-level item. | No | null |

### Service Hierarchy

Services support a two-level parent/child hierarchy:

```
Parent Service (category)
  |-- Child Service 1 (bookable)
  |-- Child Service 2 (bookable)
  |-- Child Service 3 (bookable)

Standalone Service (bookable, no parent)
```

**Rules:**
- Parent services (those with children) are not directly bookable
- Only leaf services (no children) can be booked
- Maximum 2 levels deep (parent -> child, no grandchildren)
- `parent_service_id` references `services.id` (self-referencing FK)
- Deleting a parent cascades to children (`cascade="all, delete-orphan"`)

### Buffer Time

Buffer time is added after each booking to prevent back-to-back scheduling:

```
Booking:   |--- duration (30min) ---|- buffer (10min) -|
Timeline:  9:00                   9:30                 9:40
Next slot                                              9:40 (earliest)
```

The slot calculator uses `total_block = duration + buffer` for conflict checking, but the actual booking `ends_at` is `starts_at + duration` (buffer is not part of the booking record).

**Use cases:**
- Salon cleanup time between clients: 5-10 min
- COVID sanitization protocol: 15 min
- Equipment preparation: 10 min
- Stylist break time: 15 min

Set buffer for all services at once via owner WhatsApp: `set buffer 10`

### Pricing in Paise

All monetary values are stored in **paise** (1/100 of a Rupee) to avoid floating-point precision issues:

| Display Price | Stored Value (paise) |
|---|---|
| ₹200 | 20000 |
| ₹500 | 50000 |
| ₹1,500 | 150000 |
| ₹2,000 | 200000 |
| ₹99.50 | 9950 |

**Conversion:** `display_price = price_paise / 100`

### Deposit Configuration

For high-value services where no-shows are costly:

1. Set `requires_deposit = true` on the service
2. Set `deposit_amount_paise` (e.g., 50000 for ₹500 deposit)
3. Enable `razorpay_enabled = true` in tenant settings
4. When a customer books this service:
   - Booking is created with status `pending_deposit`
   - A Razorpay payment link is sent via WhatsApp
   - On successful payment, booking transitions to `confirmed`
   - If payment is not completed within the timeout, booking is cancelled

---

## Staff Configuration

### Staff Fields

| Field | Type | Description | Required | Default |
|---|---|---|---|---|
| `name` | text | Staff member's display name. Shown to customers in slot selection. | Yes | -- |
| `role` | text | Role or title (e.g., "stylist", "senior stylist", "therapist"). | No | null |
| `bio` | text | Short biography or description (e.g., "5 yrs experience, color specialist"). Shown in WhatsApp slot descriptions for multi-staff businesses. | No | null |
| `specialty` | text | Specialty areas (e.g., "Hair Color, Bridal Makeup"). Shown alongside bio in slot selection. | No | null |
| `skills` | JSONB | Array of service UUIDs this staff member can perform. If null, they can do all services. | No | null |
| `working_hours` | JSONB | Per-day schedule in the same format as branch operating hours. Overrides branch hours when set. | No | null |
| `branch_id` | UUID | Which branch this staff member belongs to. | No | null |
| `google_calendar_id` | text | Google Calendar ID for calendar sync (e.g., "primary" or "abc@group.calendar.google.com"). | No | null |
| `google_oauth_refresh_token_encrypted` | bytes | Encrypted Google OAuth refresh token for calendar access. | No | null |
| `is_active` | boolean | Whether the staff member is active and available for bookings. | No | true |

### Skills Mapping

The `skills` field is a JSON array of service UUID strings:

```json
["550e8400-e29b-41d4-a716-446655440001", "550e8400-e29b-41d4-a716-446655440002"]
```

**How skills affect slot availability:**

1. When a customer selects a service, the slot calculator finds all active staff where:
   - `skills IS NULL` (can do everything), OR
   - `skills @> '["<service_id>"]'` (skills array contains the service ID)
2. Only eligible staff's working hours are checked for available slots

**When adding staff via WhatsApp** (`add staff Meena stylist`):
- Skills are automatically set to all active service IDs
- Working hours are copied from the first existing staff member

### Working Hours

Staff working hours use the same JSON format as branch operating hours:

```json
{
  "mon": [{"open": "09:00", "close": "20:00"}],
  "tue": [{"open": "09:00", "close": "20:00"}],
  "wed": [{"open": "09:00", "close": "20:00"}],
  "thu": [{"open": "09:00", "close": "20:00"}],
  "fri": [{"open": "09:00", "close": "21:00"}],
  "sat": [{"open": "10:00", "close": "21:00"}],
  "sun": [{"open": "10:00", "close": "18:00"}]
}
```

**Priority:**
- Staff hours override branch hours
- If staff has no working hours, the system falls back to their branch's hours
- If neither has hours for a day, no slots are generated

### Multi-Branch Staff

Staff can be assigned to a specific branch via `branch_id`. This affects:
- Which branch's operating hours are used as fallback
- Booking assignment (bookings record both `staff_id` and `branch_id`)

Currently, each staff member belongs to one branch. Multi-branch scheduling (same staff at different locations on different days) is planned for future development.

### Solo vs Multi-Staff Display

The conversation engine adapts the UI based on staff count:

**Solo business (1 staff member):**
- Time slots are shown without staff names
- Duplicate times (same time, same staff) are deduplicated
- Confirmation does not show staff name (it is implied)

**Multi-staff business (2+ staff members):**
- Time slots show the staff member's name: "10:00 AM -- Ravi"
- Staff bio and specialty are shown in the description
- Customer can see who they will be booked with
- Confirmation includes the staff name

---

## Notification Templates

### Template System

Messages are defined in YAML files under `app/messaging/templates/`:

- `en.yaml` -- English (default)
- `hi.yaml` -- Hindi
- `ta.yaml` -- Tamil

The `i18n.py` module loads templates and performs variable substitution.

### Template Format

Templates use Python string formatting with named variables:

```yaml
welcome_new: "Welcome to {business_name}!"
booking_confirmed: |
  Booking confirmed!
    Service: {service_name}
    Date: {date}
    Time: {time}
    Staff: {staff}
    Booking ID: {booking_id}
```

### Available Variables

| Variable | Description | Available In |
|---|---|---|
| `{business_name}` | Tenant's business name | All templates |
| `{service_name}` | Selected service name | Service, booking, reminder templates |
| `{date}` | Formatted date (e.g., "Monday, 20 Jan") | Booking, reminder templates |
| `{time}` | Formatted time (e.g., "10:30 AM") | Booking, reminder templates |
| `{staff}` | Staff member name | Booking, reminder templates |
| `{booking_id}` | Short booking reference (first 8 chars of UUID) | Booking, confirmation templates |
| `{number}` | Item number in a list | Menu templates |
| `{duration}` | Service duration in minutes | Service menu templates |
| `{price}` | Service price in INR | Service menu templates |
| `{max}` | Maximum valid number for a choice | Invalid choice template |

### Key Templates

| Template Key | When Used | Description |
|---|---|---|
| `welcome_new` | First message from new customer | Greeting with business name |
| `welcome_back` | First message from returning customer | Personalized return greeting |
| `service_menu_header` | Service selection step | "What would you like to book?" |
| `service_menu_item` | Each service in the list | "1. Haircut (30 min -- ₹500)" |
| `date_picker_header` | Date selection step | Date list header |
| `slot_picker_header` | Time slot selection step | Time slot list header |
| `confirm_header` | Booking confirmation step | Summary header |
| `booking_confirmed` | After successful booking | Full confirmation message |
| `booking_cancelled` | After cancellation | Cancellation acknowledgment |
| `slot_taken` | When selected slot was taken by another customer | Conflict message |
| `reminder` | 2 hours before appointment | Reminder with appointment details |
| `human_handoff` | When customer requests human help | "I've notified the owner" |
| `business_paused` | When tenant has paused bookings | Unavailability message |
| `no_services` | When no active services exist | No services message |
| `invalid_choice` | When customer enters invalid number | Prompt to try again |

### Adding a New Language

1. Create a new YAML file: `app/messaging/templates/<lang_code>.yaml`
2. Copy all keys from `en.yaml`
3. Translate all values
4. The system falls back to English for any missing keys

```yaml
# app/messaging/templates/hi.yaml
welcome_new: "{business_name} में आपका स्वागत है!"
booking_confirmed: |
  बुकिंग कन्फर्म!
    सेवा: {service_name}
    तारीख: {date}
    समय: {time}
    स्टाफ: {staff}
    बुकिंग ID: {booking_id}
```

### Reloading Templates

Templates are cached in memory. To reload after editing YAML files:

```python
from app.messaging.i18n import reload_templates
reload_templates()
```

In production, a restart of the app process is needed to pick up template changes.

---

## Plan Tiers

### Tier Definitions

The `plan_tier` field on the tenant record determines feature limits. Stored in `tenants.plan_tier`.

| Tier | Staff Limit | Services Limit | Branches | Intended For |
|---|---|---|---|---|
| `solo` | 1 | 10 | 1 | Single-person businesses (freelance stylist, solo barber) |
| `team` | 5 | 25 | 1 | Small teams (salon with 2-5 staff) |
| `business` | 15 | 50 | 3 | Established businesses (multi-chair salon, clinic with multiple doctors) |

### Feature Matrix

| Feature | Solo | Team | Business |
|---|---|---|---|
| WhatsApp booking | Yes | Yes | Yes |
| Customer management | Yes | Yes | Yes |
| Owner WhatsApp commands | Yes | Yes | Yes |
| Appointment reminders | Yes | Yes | Yes |
| Staff members | 1 | Up to 5 | Up to 15 |
| Services | Up to 10 | Up to 25 | Up to 50 |
| Branches | 1 | 1 | Up to 3 |
| Service categories (hierarchy) | No | Yes | Yes |
| Feedback collection | No | Yes | Yes |
| Google Calendar sync | No | Yes | Yes |
| Razorpay payments | No | Yes | Yes |
| Analytics dashboard | Basic | Full | Full |
| Custom welcome message | No | Yes | Yes |
| API access | No | No | Yes |
| Priority support | No | No | Yes |

### Plan Enforcement

Plan limits are enforced by the `PlanLimitExceededError` exception:

```python
class PlanLimitExceededError(BookingSaaSError):
    def __init__(self, limit_name: str, current: int, maximum: int):
        # e.g., "staff: 5/5"
```

When a limit is exceeded, the operation is rejected with a clear message indicating the current count and the maximum allowed.

### Subscription Billing

Subscription status is tracked in the `subscriptions` table:

| Field | Description |
|---|---|
| `plan_tier` | Current plan: solo, team, business |
| `billing_cycle` | monthly or annual |
| `current_period_start` | Start of current billing period |
| `current_period_end` | End of current billing period |
| `status` | trial, active, past_due, cancelled |
| `trial_ends_at` | When the free trial expires |

### Message Credits

WhatsApp messages have costs. The `message_credit_ledger` table tracks usage:

| Field | Description |
|---|---|
| `category` | Message category: service, utility, marketing, authentication |
| `quantity` | Number of messages |
| `amount_paise` | Cost in paise |
| `reference_message_id` | Link to the messages table |

WhatsApp Cloud API pricing (approximate, varies by category):
- **Utility messages** (reminders, confirmations): ~₹0.35/message
- **Service messages** (within 24-hour window): Free
- **Marketing messages:** ~₹0.85/message
- **Authentication messages:** ~₹0.30/message
