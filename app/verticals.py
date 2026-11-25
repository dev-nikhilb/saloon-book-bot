"""Industry vertical presets & one-click starter templates for BookBot MicroSaaS.

Enables salons, clinics, dental practices, spas, gyms, tutors, consultants,
diagnostics labs, vet clinics, and home service providers to go live in seconds.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.labels import get_labels

VERTICALS: dict[str, dict] = {
    "salon": {
        "key": "salon",
        "display_name": "Salon & Hair Studio",
        "tagline": "Haircuts, coloring, styling, and beauty care",
        "collect_reason": False,
        "reason_placeholder": "Any style notes or preferences (optional)",
        "default_rebooking_days": 28,
        "starter_services": [
            {"name": "Haircut & Styling", "duration_minutes": 30, "buffer_minutes": 5, "price_paise": 50000, "description": "Wash, precision cut, and blow dry"},
            {"name": "Beard Trim & Grooming", "duration_minutes": 15, "buffer_minutes": 5, "price_paise": 25000, "description": "Shape-up and hot towel finish"},
            {"name": "Hair Color (Global)", "duration_minutes": 90, "buffer_minutes": 15, "price_paise": 250000, "requires_deposit": True, "deposit_amount_paise": 50000, "description": "Ammonia-free global color"},
            {"name": "Relaxing Facial", "duration_minutes": 60, "buffer_minutes": 10, "price_paise": 150000, "description": "Deep cleansing and glow facial"},
            {"name": "Head Massage", "duration_minutes": 30, "buffer_minutes": 5, "price_paise": 40000, "description": "Aromatic oil scalp massage"},
        ],
    },
    "barbershop": {
        "key": "barbershop",
        "display_name": "Barbershop",
        "tagline": "Classic cuts, fades, shaves, and beard grooming",
        "collect_reason": False,
        "reason_placeholder": "Preferred fade / style notes (optional)",
        "default_rebooking_days": 21,
        "starter_services": [
            {"name": "Classic Haircut", "duration_minutes": 30, "buffer_minutes": 5, "price_paise": 30000, "description": "Clipper or scissor cut with wash"},
            {"name": "Skin Fade", "duration_minutes": 30, "buffer_minutes": 5, "price_paise": 40000, "description": "Sharp taper or zero fade"},
            {"name": "Beard Trim & Line-up", "duration_minutes": 15, "buffer_minutes": 5, "price_paise": 15000, "description": "Razor line-up and beard oil"},
            {"name": "Haircut + Beard Combo", "duration_minutes": 45, "buffer_minutes": 5, "price_paise": 45000, "description": "Complete grooming package"},
        ],
    },
    "beauty_parlour": {
        "key": "beauty_parlour",
        "display_name": "Beauty Parlour",
        "tagline": "Facials, threading, waxing, manicure & bridal care",
        "collect_reason": False,
        "reason_placeholder": "Any skin sensitivity or notes (optional)",
        "default_rebooking_days": 30,
        "starter_services": [
            {"name": "Eyebrow Threading & Upper Lip", "duration_minutes": 15, "buffer_minutes": 0, "price_paise": 10000, "description": "Precision shaping"},
            {"name": "Gold Glow Facial", "duration_minutes": 60, "buffer_minutes": 10, "price_paise": 120000, "description": "Deep clean-up and radiance pack"},
            {"name": "Full Arms & Legs Waxing", "duration_minutes": 45, "buffer_minutes": 10, "price_paise": 80000, "description": "Rica / chocolate wax"},
            {"name": "Manicure & Pedicure", "duration_minutes": 60, "buffer_minutes": 10, "price_paise": 100000, "description": "Spa nail care and polish"},
            {"name": "Party Makeup", "duration_minutes": 90, "buffer_minutes": 15, "price_paise": 350000, "requires_deposit": True, "deposit_amount_paise": 100000, "description": "HD makeup with hairstyling"},
        ],
    },
    "clinic": {
        "key": "clinic",
        "display_name": "Medical Clinic / Doctor",
        "tagline": "OPD consultations, follow-ups, and specialist visits",
        "collect_reason": True,
        "reason_placeholder": "Brief symptoms or reason for visit (e.g. fever, follow-up)",
        "default_rebooking_days": 90,
        "starter_services": [
            {"name": "General Consultation", "duration_minutes": 15, "buffer_minutes": 5, "price_paise": 50000, "description": "New patient / general OPD checkup"},
            {"name": "Follow-up Visit", "duration_minutes": 15, "buffer_minutes": 0, "price_paise": 30000, "description": "Report review or post-treatment check"},
            {"name": "Detailed Specialist Consultation", "duration_minutes": 30, "buffer_minutes": 5, "price_paise": 100000, "description": "Comprehensive assessment & care plan"},
            {"name": "Vaccination / Immunization", "duration_minutes": 15, "buffer_minutes": 5, "price_paise": 40000, "description": "Scheduled vaccine administration"},
        ],
    },
    "dental": {
        "key": "dental",
        "display_name": "Dental Clinic",
        "tagline": "Checkups, scaling, root canals, and orthodontics",
        "collect_reason": True,
        "reason_placeholder": "Tooth concern or symptoms (e.g. sensitivity, routine cleaning)",
        "default_rebooking_days": 180,
        "starter_services": [
            {"name": "Dental Checkup & X-Ray", "duration_minutes": 30, "buffer_minutes": 5, "price_paise": 50000, "description": "Full oral exam and treatment plan"},
            {"name": "Scaling & Polishing", "duration_minutes": 45, "buffer_minutes": 10, "price_paise": 150000, "description": "Ultrasonic plaque removal and polish"},
            {"name": "Tooth Filling", "duration_minutes": 45, "buffer_minutes": 10, "price_paise": 120000, "description": "Tooth-colored composite restoration"},
            {"name": "Root Canal Consultation", "duration_minutes": 60, "buffer_minutes": 15, "price_paise": 350000, "requires_deposit": True, "deposit_amount_paise": 50000, "description": "Endodontic assessment and first sitting"},
        ],
    },
    "spa": {
        "key": "spa",
        "display_name": "Spa & Wellness Center",
        "tagline": "Massages, aromatherapy, Ayurveda, and body therapies",
        "collect_reason": True,
        "reason_placeholder": "Pressure preference or focus areas (optional)",
        "default_rebooking_days": 30,
        "starter_services": [
            {"name": "Swedish Relaxation Massage", "duration_minutes": 60, "buffer_minutes": 15, "price_paise": 250000, "description": "Full body gentle soothing therapy"},
            {"name": "Deep Tissue Massage", "duration_minutes": 60, "buffer_minutes": 15, "price_paise": 300000, "description": "Targeted muscle tension relief"},
            {"name": "Ayurvedic Abhyanga", "duration_minutes": 75, "buffer_minutes": 15, "price_paise": 320000, "description": "Warm herbal oil full-body massage"},
            {"name": "Foot Reflexology", "duration_minutes": 30, "buffer_minutes": 10, "price_paise": 100000, "description": "Pressure-point foot & calf therapy"},
        ],
    },
    "fitness": {
        "key": "fitness",
        "display_name": "Fitness / Yoga / Pilates Studio",
        "tagline": "Personal training, yoga sessions, and fitness assessments",
        "collect_reason": True,
        "reason_placeholder": "Fitness goals or any injuries to note (optional)",
        "default_rebooking_days": 14,
        "starter_services": [
            {"name": "1-on-1 Personal Training", "duration_minutes": 60, "buffer_minutes": 10, "price_paise": 100000, "description": "Customized strength & conditioning session"},
            {"name": "Private Yoga Session", "duration_minutes": 60, "buffer_minutes": 10, "price_paise": 80000, "description": "Hatha / Vinyasa guided practice"},
            {"name": "Fitness & Body Assessment", "duration_minutes": 30, "buffer_minutes": 5, "price_paise": 50000, "description": "BMI, mobility check & workout plan"},
            {"name": "Trial Class", "duration_minutes": 45, "buffer_minutes": 15, "price_paise": 20000, "description": "Introductory studio session"},
        ],
    },
    "tutor": {
        "key": "tutor",
        "display_name": "Tutoring & Coaching",
        "tagline": "1-on-1 academic tutoring, music, language, or exam prep",
        "collect_reason": True,
        "reason_placeholder": "Subject, grade, or topics to focus on",
        "default_rebooking_days": 14,
        "starter_services": [
            {"name": "1-on-1 Subject Session", "duration_minutes": 60, "buffer_minutes": 10, "price_paise": 70000, "description": "Focused individual coaching"},
            {"name": "Exam Prep Intensive", "duration_minutes": 90, "buffer_minutes": 15, "price_paise": 120000, "description": "Mock paper review & problem solving"},
            {"name": "Free Demo / Assessment", "duration_minutes": 30, "buffer_minutes": 5, "price_paise": 0, "description": "Diagnostic evaluation for new students"},
        ],
    },
    "consulting": {
        "key": "consulting",
        "display_name": "Consulting / CA / Legal",
        "tagline": "Advisory calls, tax planning, legal & career consultations",
        "collect_reason": True,
        "reason_placeholder": "Briefly describe what you'd like to discuss",
        "default_rebooking_days": 90,
        "starter_services": [
            {"name": "Discovery Call", "duration_minutes": 30, "buffer_minutes": 10, "price_paise": 50000, "description": "Initial case or requirement review"},
            {"name": "Deep-Dive Consultation", "duration_minutes": 60, "buffer_minutes": 15, "price_paise": 200000, "requires_deposit": True, "deposit_amount_paise": 50000, "description": "Detailed advisory & action plan"},
            {"name": "Document / Filing Review", "duration_minutes": 45, "buffer_minutes": 10, "price_paise": 150000, "description": "Audit and compliance walkthrough"},
        ],
    },
    "diagnostics": {
        "key": "diagnostics",
        "display_name": "Diagnostics & Pathology Lab",
        "tagline": "Blood tests, health packages, ECG, and home sample collection",
        "collect_reason": True,
        "reason_placeholder": "Doctor's prescription or fasting notes (optional)",
        "default_rebooking_days": 180,
        "starter_services": [
            {"name": "Complete Blood Count (CBC)", "duration_minutes": 15, "buffer_minutes": 5, "price_paise": 40000, "description": "Quick sample draw — reports same day"},
            {"name": "Full Body Health Checkup", "duration_minutes": 30, "buffer_minutes": 10, "price_paise": 199900, "description": "60+ parameters (fasting required)"},
            {"name": "Thyroid & Diabetes Profile", "duration_minutes": 15, "buffer_minutes": 5, "price_paise": 85000, "description": "TSH, T3, T4, HbA1c & fasting glucose"},
            {"name": "ECG Test", "duration_minutes": 20, "buffer_minutes": 5, "price_paise": 50000, "description": "12-lead resting ECG"},
        ],
    },
    "veterinary": {
        "key": "veterinary",
        "display_name": "Veterinary & Pet Care",
        "tagline": "Pet checkups, vaccinations, and grooming appointments",
        "collect_reason": True,
        "reason_placeholder": "Pet name, breed, and symptoms / request",
        "default_rebooking_days": 45,
        "starter_services": [
            {"name": "Pet Wellness Checkup", "duration_minutes": 30, "buffer_minutes": 10, "price_paise": 60000, "description": "Physical examination by vet"},
            {"name": "Vaccination Shot", "duration_minutes": 20, "buffer_minutes": 5, "price_paise": 80000, "description": "Annual booster or puppy/kitten vaccine"},
            {"name": "Full Pet Grooming", "duration_minutes": 90, "buffer_minutes": 15, "price_paise": 180000, "description": "Bath, haircut, nail clip & ear clean"},
        ],
    },
    "home_services": {
        "key": "home_services",
        "display_name": "Home Services & Repairs",
        "tagline": "AC servicing, appliance repair, plumbing, and home cleaning",
        "collect_reason": True,
        "reason_placeholder": "Full address + brief description of the issue",
        "default_rebooking_days": 90,
        "starter_services": [
            {"name": "AC Servicing & Jet Clean", "duration_minutes": 60, "buffer_minutes": 30, "price_paise": 59900, "description": "Filter & coil wash with gas check"},
            {"name": "Appliance Inspection Visit", "duration_minutes": 45, "buffer_minutes": 30, "price_paise": 29900, "description": "Diagnosis adjusted against final bill"},
            {"name": "Bathroom Deep Cleaning", "duration_minutes": 90, "buffer_minutes": 30, "price_paise": 99900, "description": "Descaling, grout & sanitization"},
        ],
    },
}


def get_vertical(business_type: str) -> dict:
    """Return vertical metadata + labels for a business_type (falls back to salon/default)."""
    base = VERTICALS.get(business_type) or {
        "key": business_type or "other",
        "display_name": "Service Business",
        "tagline": "Appointments and client bookings",
        "collect_reason": True,
        "reason_placeholder": "Notes or special requests (optional)",
        "default_rebooking_days": 30,
        "starter_services": [
            {"name": "Standard Appointment", "duration_minutes": 30, "buffer_minutes": 5, "price_paise": 50000, "description": "Standard booking slot"},
            {"name": "Extended Session", "duration_minutes": 60, "buffer_minutes": 10, "price_paise": 100000, "description": "In-depth 1-hour session"},
        ],
    }
    return {**base, "labels": get_labels(business_type)}


def list_verticals() -> list[dict]:
    """Return all supported business verticals with labels and starter templates."""
    return [get_vertical(k) for k in VERTICALS]


async def apply_vertical_template(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    business_type: str,
) -> list[dict]:
    """Insert starter services for a vertical (skipping names that already exist)
    and assign them to existing active staff so the business is immediately bookable.
    """
    vertical = get_vertical(business_type)
    starter = vertical["starter_services"]

    existing_res = await db.execute(
        text("SELECT LOWER(name) AS lname FROM services WHERE tenant_id = :tid AND is_active = true"),
        {"tid": tenant_id},
    )
    existing_names = {r.lname for r in existing_res.fetchall()}

    created: list[dict] = []
    now = datetime.now(timezone.utc)

    for idx, svc in enumerate(starter):
        if svc["name"].lower() in existing_names:
            continue
        sid = uuid.uuid4()
        await db.execute(
            text("""
                INSERT INTO services (
                    id, tenant_id, name, description, duration_minutes, buffer_minutes,
                    price_paise, requires_deposit, deposit_amount_paise, is_active, sort_order, created_at
                ) VALUES (
                    :id, :tid, :name, :desc, :dur, :buf,
                    :price, :req_dep, :dep_amt, true, :sort, :now
                )
            """),
            {
                "id": sid,
                "tid": tenant_id,
                "name": svc["name"],
                "desc": svc.get("description"),
                "dur": svc["duration_minutes"],
                "buf": svc.get("buffer_minutes", 0),
                "price": svc.get("price_paise"),
                "req_dep": bool(svc.get("requires_deposit", False)),
                "dep_amt": svc.get("deposit_amount_paise"),
                "sort": idx + 1,
                "now": now,
            },
        )
        created.append({"id": str(sid), **svc})

    # Ensure staff with explicit skills list also get the new starter services
    if created:
        for item in created:
            await db.execute(
                text("""
                    UPDATE staff
                    SET skills = skills || to_jsonb(CAST(:sid AS text))
                    WHERE tenant_id = :tid AND is_active = true AND skills IS NOT NULL
                      AND NOT (skills @> to_jsonb(CAST(:sid AS text)))
                """),
                {"sid": item["id"], "tid": tenant_id},
            )

    await db.commit()
    return created
