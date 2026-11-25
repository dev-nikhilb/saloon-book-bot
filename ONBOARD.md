# Customer Onboarding Guide

Step-by-step guide for onboarding a new salon or business customer onto the platform. This guide is written for **you, the SaaS operator** running the platform.

---

## Table of Contents

1. [Pre-requisites](#pre-requisites)
2. [Step 1: Meta/WhatsApp Setup](#step-1-metawhatsapp-setup)
3. [Step 2: Platform Registration](#step-2-platform-registration)
4. [Step 3: Service Configuration](#step-3-service-configuration)
5. [Step 4: Settings Configuration](#step-4-settings-configuration)
6. [Step 5: Testing](#step-5-testing)
7. [Step 6: Go Live Checklist](#step-6-go-live-checklist)
8. [Troubleshooting](#troubleshooting)
9. [Customer Training](#customer-training)

---

## Pre-requisites

Before onboarding a new business customer, you need:

| Requirement | Details |
|---|---|
| **Meta Business Account** | The business (or you on their behalf) must have a verified Meta Business Account at [business.facebook.com](https://business.facebook.com) |
| **WhatsApp Business API Access** | Approved access to the WhatsApp Business Platform (Cloud API) |
| **Dedicated Phone Number** | A phone number for the business's WhatsApp. Cannot already be registered on WhatsApp/WhatsApp Business app. Indian mobile (+91) or landline numbers both work |
| **Business Details** | Business name, type (salon/barbershop/beauty parlour), owner name, service list with durations and prices |
| **Platform Running** | Your deployment must be live with webhook URL accessible over HTTPS |

**What you collect from the business owner:**

- Business name (e.g., "Style Studio")
- Business type (salon, barbershop, beauty parlour, clinic, etc.)
- Owner/manager name and phone number (e.g., +919876543210)
- List of services with duration (minutes) and price (INR)
- Operating hours (or accept defaults: Mon-Sat 9am-8pm)
- Number of staff members and their names/roles

---

## Step 1: Meta/WhatsApp Setup

### 1.1 Create Meta Business Account

If the business does not already have one:

1. Go to [business.facebook.com](https://business.facebook.com)
2. Click "Create Account"
3. Enter business name, owner name, business email
4. Verify the email address
5. Complete business verification (PAN card or GST registration for Indian businesses)

> **Note:** Business verification can take 1-5 business days. Start this early.

### 1.2 Apply for WhatsApp Business API Access

1. Go to [developers.facebook.com](https://developers.facebook.com)
2. Create a new App (type: Business)
3. Add the "WhatsApp" product to the app
4. In the WhatsApp section, go to "Getting Started"
5. Connect the Meta Business Account from step 1.1
6. Add a phone number (the business's dedicated number)
7. Verify the phone number via SMS or voice call

### 1.3 Get Phone Number ID and Access Token

After verification, from the Meta Developer Dashboard:

1. Navigate to WhatsApp > Getting Started
2. Note the **Phone Number ID** (e.g., `109876543210987`)
3. Generate a **Permanent Access Token**:
   - Go to Business Settings > System Users
   - Create a system user with admin access
   - Generate a token with `whatsapp_business_messaging` permission
   - Copy the token (it is shown only once)

### 1.4 Set Webhook URL

1. In the Meta Developer Dashboard, go to WhatsApp > Configuration
2. Set the **Callback URL** to:
   ```
   https://yourdomain.com/webhooks/whatsapp
   ```
3. Set the **Verify Token** to the value of your `META_WEBHOOK_VERIFY_TOKEN` environment variable
4. Click "Verify and Save" -- Meta will send a GET request to your webhook to confirm

### 1.5 Subscribe to Messages Webhook

1. On the same Configuration page, under "Webhook Fields"
2. Subscribe to the **messages** field
3. This ensures your platform receives all inbound messages and status updates

### 1.6 Store Credentials in the Platform

You will need to store the following in the tenant record:

- `waba_phone_number_id` -- the Phone Number ID from step 1.3
- `waba_access_token_encrypted` -- the permanent access token (encrypted at rest)

---

## Step 2: Platform Registration

There are two ways to register a new business on the platform.

### Option A: WhatsApp Onboarding (Guided Flow)

This is the recommended approach for non-technical business owners. The owner texts the platform's central onboarding number, and a guided conversation collects all required information.

**How it works:**

1. The business owner sends any message to the onboarding WhatsApp number
2. The bot responds with a welcome message and asks for the business name
3. The flow collects:
   - Business name
   - Business type (salon / barbershop / beauty parlour / other)
   - Owner name
   - Operating hours (choose from presets or custom)
   - Services (name, duration in minutes, price in INR) -- one at a time
4. A summary is shown for confirmation
5. On confirmation, the tenant is created with:
   - A primary branch with the selected operating hours
   - The owner as the sole staff member
   - All listed services assigned to the owner

**Example conversation:**

```
Owner: Hi
Bot:   Welcome! Let's set up your business on WhatsApp.
       What's your business name?

Owner: Priya's Hair Studio
Bot:   Great! Priya's Hair Studio it is.
       What type of business?
         1. Salon
         2. Barbershop
         3. Beauty Parlour
         4. Other
       Reply with a number.

Owner: 1
Bot:   What's your name? (The owner/manager)

Owner: Priya
Bot:   Hi Priya!
       What are your business hours?
         1. Mon-Sat 9am-8pm (default)
         2. Mon-Sat 10am-9pm
         3. All days 10am-8pm
         4. Custom
       Reply with a number.

Owner: 1
Bot:   Now let's add your services.
       What's the name of your first service?

Owner: Haircut
Bot:   How long does Haircut take? (in minutes)

Owner: 30
Bot:   What do you charge for Haircut? (in INR)

Owner: 500
Bot:   Added! (1 service so far)
       Add another service?
         1. Yes, add more
         2. No, that's all

...continues until owner says "done"...

Bot:   Setup Summary
       Business: Priya's Hair Studio
       Type: salon
       Owner: Priya
       Services:
         - Haircut -- 30min -- INR 500
         - Beard Trim -- 15min -- INR 200
       Reply YES to go live or 0 to restart.

Owner: YES
Bot:   Priya's Hair Studio is live!
       Your customers can now book by messaging this WhatsApp number.
       Owner commands:
         today -- see today's bookings
         help -- all commands
```

**For dev/testing**, use the onboarding API endpoint:

```bash
# Step 1: Start onboarding
curl -X POST http://localhost:8000/dev/onboard/message \
  -H "Content-Type: application/json" \
  -d '{"phone": "+919876543210", "body": "start"}'

# Step 2: Provide business name
curl -X POST http://localhost:8000/dev/onboard/message \
  -H "Content-Type: application/json" \
  -d '{"phone": "+919876543210", "body": "Priya Hair Studio"}'

# ...continue the conversation...
```

### Option B: Dashboard/API Signup

For bulk onboarding or technical integrations, create the tenant directly via the seed endpoint (dev) or future dashboard API.

**Dev seed (creates a complete demo salon):**

```bash
curl -X POST http://localhost:8000/dev/seed
```

This creates "Style Studio" with:
- Owner: Priya (+919999900000)
- 2 staff: Ravi, Meena
- 5 services: Haircut (INR 500), Beard Trim (INR 200), Hair Color (INR 2000), Facial (INR 1500), Head Massage (INR 400)
- Operating hours: Mon-Thu 9am-8pm, Fri-Sat 9am/10am-9pm, Sun 10am-6pm

**What info you need for manual creation:**

| Field | Required | Example |
|---|---|---|
| `business_name` | Yes | "Priya's Hair Studio" |
| `business_type` | Yes | "salon" |
| `owner_phone` | Yes | "+919876543210" |
| `owner_name` | Yes | "Priya" |
| `primary_language` | No (default: en) | "en", "hi", "ta" |
| `timezone` | No (default: Asia/Kolkata) | "Asia/Kolkata" |
| `plan_tier` | No (default: solo) | "solo", "team", "business" |
| Services list | Yes | Name, duration (min), price (INR) |
| Operating hours | No (default: Mon-Sat 9am-8pm) | JSON format |

---

## Step 3: Service Configuration

### 3.1 Service Categories (Parent Services)

Services can be organized hierarchically. A **parent service** is a category that groups related services. Parent services are not directly bookable -- customers select the category, then pick a specific service.

**Example:**

```
Hair Services (parent)
  |- Haircut         30min  INR 500
  |- Hair Color      90min  INR 2000
  |- Hair Smoothing  120min INR 5000

Skin Care (parent)
  |- Facial          60min  INR 1500
  |- Cleanup         30min  INR 800
```

In WhatsApp, parent categories appear with a folder icon and show "3 options" as the description. Standalone services (no parent) appear directly in the services list.

### 3.2 Adding Individual Services

Each service has:

| Field | Description | Example |
|---|---|---|
| `name` | Display name | "Haircut" |
| `duration_minutes` | How long the service takes | 30 |
| `price_paise` | Price in paise (INR x 100) | 50000 (= INR 500) |
| `buffer_minutes` | Gap time after each booking | 10 |
| `requires_deposit` | Whether a deposit is needed to confirm | false |
| `deposit_amount_paise` | Deposit amount in paise | 50000 (= INR 500) |
| `sort_order` | Display order in the menu | 1 |
| `parent_service_id` | UUID of parent category (null for top-level) | null |

**Via owner WhatsApp commands:**

```
add service Haircut 30 500
add service "Hair Color" 90 2000
set buffer 10
```

### 3.3 Setting Up Staff Members

Each staff member has:

| Field | Description | Example |
|---|---|---|
| `name` | Staff name | "Ravi" |
| `role` | Role/title | "stylist" |
| `bio` | Short description | "5 yrs experience" |
| `specialty` | Specialty areas | "Hair Color, Bridal" |
| `skills` | JSON array of service IDs they can perform | `["uuid1", "uuid2"]` |
| `working_hours` | Per-day schedule | See format below |
| `branch_id` | Which branch they belong to | UUID |

**Via owner WhatsApp:**

```
add staff Ravi stylist
add staff Meena stylist "5 yrs experience, color specialist"
```

When adding staff via WhatsApp, the system automatically:
- Copies working hours from existing staff
- Assigns all active services to the new staff member

### 3.4 Configuring Operating Hours

Operating hours are stored as JSON. Each day can have multiple windows (e.g., split shifts with a lunch break).

**Format:**

```json
{
  "mon": [{"open": "09:00", "close": "13:00"}, {"open": "14:00", "close": "20:00"}],
  "tue": [{"open": "09:00", "close": "20:00"}],
  "wed": [{"open": "09:00", "close": "20:00"}],
  "thu": [{"open": "09:00", "close": "20:00"}],
  "fri": [{"open": "09:00", "close": "21:00"}],
  "sat": [{"open": "10:00", "close": "21:00"}],
  "sun": []
}
```

- Days with an empty array `[]` or missing entirely mean the business is closed
- Times are in 24-hour format
- Multiple windows per day support lunch breaks or split shifts
- Hours are set at both the branch level and individual staff level
- Staff hours override branch hours when present

---

## Step 4: Settings Configuration

### 4.1 Booking Rules

Configure via the `tenant_settings` table:

| Setting | Description | Default |
|---|---|---|
| `advance_booking_days` | How many days ahead customers can book | 14 |
| `min_cancel_hours` | Minimum hours before appointment that cancellation is allowed | 2 |
| `slot_increment_minutes` | Gap between available time slot options | 15 |
| `max_bookings_per_customer` | Maximum active bookings per customer | 5 |

### 4.2 Notification Preferences

| Setting | Description | Default |
|---|---|---|
| `send_reminders` | Whether to send appointment reminders | true |
| `reminder_hours_before` | How many hours before the appointment to send reminder | 24 |
| `send_feedback_request` | Whether to send feedback request after appointment | true |
| `feedback_delay_hours` | Hours after appointment to send feedback request | 2 |

### 4.3 Custom Messages

| Setting | Description | Default |
|---|---|---|
| `welcome_message` | Custom welcome message for new customers | null (uses template) |
| `booking_confirmed_message` | Custom confirmation message | null (uses template) |
| `google_review_url` | Google Maps review URL for feedback redirect | null |

### 4.4 Payment Integration (Razorpay)

If the business needs deposit collection:

1. Create a Razorpay account at [razorpay.com](https://razorpay.com)
2. Get the Key ID and Key Secret from the Razorpay Dashboard
3. Set up a webhook in Razorpay pointing to `https://yourdomain.com/payments/webhook`
4. Configure the webhook secret
5. Enable deposits on specific services by setting `requires_deposit = true` and `deposit_amount_paise`

Platform-level environment variables:

```
RAZORPAY_KEY_ID=rzp_live_xxxxxxxxxx
RAZORPAY_KEY_SECRET=xxxxxxxxxxxxxxxx
RAZORPAY_WEBHOOK_SECRET=xxxxxxxxxxxxxxxx
```

Per-tenant: set `razorpay_enabled = true` in `tenant_settings`.

### 4.5 Google Calendar Integration

For businesses that want Google Calendar sync:

1. Set up Google OAuth credentials in Google Cloud Console
2. Configure redirect URI to `https://yourdomain.com/auth/google/callback`
3. Enable `google_calendar_enabled = true` in tenant settings
4. Staff members connect their Google Calendar through an OAuth flow

---

## Step 5: Testing

Before going live, complete these tests:

### 5.1 Send Test Message as Customer

```bash
curl -X POST http://localhost:8000/dev/simulate \
  -H "Content-Type: application/json" \
  -d '{
    "from_phone": "+919876543210",
    "body": "hi"
  }'
```

Expected: Welcome message with service menu.

### 5.2 Book a Test Appointment

Walk through the full booking flow:

```bash
# 1. Start conversation
curl -X POST http://localhost:8000/dev/simulate \
  -d '{"from_phone": "+919876543210", "body": "hi"}'

# 2. Select service (first one)
curl -X POST http://localhost:8000/dev/simulate \
  -d '{"from_phone": "+919876543210", "body": "svc_0"}'

# 3. Select date (first available)
curl -X POST http://localhost:8000/dev/simulate \
  -d '{"from_phone": "+919876543210", "body": "date_0"}'

# 4. Select time slot (first available)
curl -X POST http://localhost:8000/dev/simulate \
  -d '{"from_phone": "+919876543210", "body": "slot_0"}'

# 5. Confirm booking
curl -X POST http://localhost:8000/dev/simulate \
  -d '{"from_phone": "+919876543210", "body": "confirm_yes"}'
```

### 5.3 Test Owner Commands

```bash
# See today's bookings
curl -X POST http://localhost:8000/dev/simulate \
  -d '{"from_phone": "+919999900000", "body": "today"}'

# Weekly stats
curl -X POST http://localhost:8000/dev/simulate \
  -d '{"from_phone": "+919999900000", "body": "stats"}'

# List all commands
curl -X POST http://localhost:8000/dev/simulate \
  -d '{"from_phone": "+919999900000", "body": "help"}'
```

### 5.4 Verify Reminders

1. Create a booking for ~2 hours from now
2. Check that the Celery beat schedule is running (`scan-reminders-every-5min`)
3. Verify the reminder message is sent (check logs or message table)

---

## Step 6: Go Live Checklist

Before considering a customer fully onboarded, verify all items:

- [ ] WABA phone number connected (`waba_phone_number_id` and `waba_access_token_encrypted` set)
- [ ] Webhook URL configured in Meta Developer Dashboard
- [ ] Webhook verification successful (GET /webhooks/whatsapp returns challenge)
- [ ] Messages webhook field subscribed
- [ ] Services configured (at least 1 active service)
- [ ] Operating hours set on branch and/or staff
- [ ] Staff added with correct skills (service assignments)
- [ ] Test booking completed end-to-end via WhatsApp
- [ ] Owner trained on commands (today, stats, help, cancel, block, pause/resume)
- [ ] Welcome message customized (if desired)
- [ ] Reminder settings configured (send_reminders, reminder_hours_before)
- [ ] Feedback settings configured (if desired)
- [ ] Payment integration set up (if using deposits)
- [ ] Google Review URL added (if desired)
- [ ] Tenant `is_active` is set to `true`

---

## Troubleshooting

### "No tenant found" when testing

**Cause:** No tenant record matches the `waba_phone_number_id` from the webhook, or `is_active` is false.

**Fix:** Verify the `waba_phone_number_id` in the tenants table matches what Meta sends. Check `is_active = true`.

### Webhook verification fails (403)

**Cause:** The verify token in Meta Dashboard does not match `META_WEBHOOK_VERIFY_TOKEN` in your environment.

**Fix:** Ensure the verify token in Meta Developer Dashboard exactly matches the `META_WEBHOOK_VERIFY_TOKEN` environment variable on your server.

### Messages not being received

**Cause:** Webhook not subscribed to `messages` field, or signature validation failing.

**Fix:**
1. Check Meta Developer Dashboard -- is the `messages` webhook field subscribed?
2. Check logs for `webhook_signature_invalid` errors
3. Verify `META_APP_SECRET` matches the App Secret in Meta Dashboard
4. In development mode, signature validation is skipped

### "No services available" message

**Cause:** No active services for the tenant, or services not properly linked to staff skills.

**Fix:**
1. Check `services` table: `is_active = true` and correct `tenant_id`
2. Check `staff` table: `skills` JSON array contains the service UUIDs
3. Check `staff` table: `working_hours` JSON is properly formatted

### Slots showing as unavailable when they should be free

**Cause:** Staff working hours not configured, or existing bookings blocking slots.

**Fix:**
1. Check staff `working_hours` JSON -- day names must be lowercase 3-letter abbreviations: `mon`, `tue`, `wed`, `thu`, `fri`, `sat`, `sun`
2. Check for blocked bookings (`cancellation_reason = 'BLOCKED'`)
3. Verify the timezone is correct (default: `Asia/Kolkata`)
4. Check `buffer_minutes` on services -- large buffers reduce available slots

### Reminders not being sent

**Cause:** Celery beat not running, or reminder already logged.

**Fix:**
1. Verify Celery beat is running: `celery -A app.celery_app:celery beat --loglevel=info`
2. Check `reminder_log` table -- if a record exists for the booking, it will not be reminded again
3. Check tenant has `waba_phone_number_id` set (needed to send messages)

### Owner commands not recognized

**Cause:** The owner phone number in the tenant record does not match the phone sending messages.

**Fix:** Phone numbers must include the country code with `+` prefix. Verify `owner_phone` in tenants table matches exactly (e.g., `+919876543210`).

---

## Customer Training

Give this quick reference card to the business owner after onboarding:

### Owner Quick Reference Card

```
============================================
   YOUR WHATSAPP BOOKING COMMANDS
============================================

SCHEDULE
  today          -- See today's bookings
  tomorrow       -- See tomorrow's bookings
  free           -- View available slots
  stats          -- Weekly summary

MANAGE SERVICES
  services       -- List your services
  add service Haircut 30 500
  remove service Haircut
  set buffer 10  -- 10min gap between bookings

MANAGE TEAM
  staff          -- List your team
  add staff Meena stylist

ACTIONS
  cancel <id>    -- Cancel a booking (get ID from "today")
  block tomorrow 4-6pm  -- Block time off
  unblock tomorrow      -- Remove blocks
  pause          -- Stop taking bookings
  resume         -- Start taking bookings again

HELP
  help           -- Show all commands

============================================
  Tip: Typos are OK! "tmrw", "2day", "cncl"
  all work.
============================================
```

### What to Tell Customers

Tell your customers:

> "You can now book appointments by messaging us on WhatsApp! Just send 'hi' to [your WhatsApp number] and follow the menu."

Customers can:
- Send "hi", "hello", or "book" to start booking
- View their upcoming bookings
- Cancel bookings
- Type "cancel", "exit", or "0" at any time to start over
- Type "human" or "help me" to request a callback from the owner
