# WhatsApp-First Booking SaaS for Indian SMBs

## Vision

Build the default WhatsApp-native booking and scheduling operating layer for Indian service businesses that do not use CRMs, dashboards, or traditional SaaS tools.

The product should feel like:
- a receptionist
- not software

Core philosophy:
- WhatsApp is the UI
- reliability > AI
- operational simplicity > feature count
- low friction > configurability

---

## Primary Target Market

Initial focus:
- salons
- barbershops
- beauty parlours

Future expansion:
- clinics, tutors, fitness studios, consultants, diagnostic centers, home services

---

## Core Product Principles

1. **WhatsApp is the primary interface** — customers book, owners manage, all via WhatsApp
2. **Deterministic flows first** — structured menus > AI. AI assists, doesn't control
3. **Zero double-bookings** — DB constraints are mandatory. This is the moat
4. **Message minimization** — every outbound message costs money and affects quality score

---

## What We're Building (Phase 1 — MVP)

- Booking engine (slot calc, conflict detection, exclusion constraints)
- Menu-driven conversation flow (service → date → time → confirm)
- Owner console via WhatsApp (today, cancel, block, stats)
- Reminder engine (2h before, utility template)
- Tenant onboarding via WhatsApp
- Webhook signature validation + security
- Single VPS deployment (Docker + Nginx)

## What We're NOT Building Yet

- Payments/deposits (Razorpay)
- Rebooking nudges
- Feedback/Google reviews
- Analytics dashboard
- Multi-branch
- AI conversation mode
- Complex billing tiers
- Staff management UI

---

## Pricing

Single plan: ₹999–₹1499/month
Includes: WhatsApp booking, reminders, owner commands, unlimited bookings

---

## Stack

Python, FastAPI, PostgreSQL 15, Redis 7, Celery, SQLAlchemy 2.0, Pydantic v2, structlog, Docker, Nginx

---

## Deployment

Single VPS (Hetzner/DigitalOcean/AWS Lightsail). Modular monolith. No K8s, no microservices.
