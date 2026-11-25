"""Optional Gemini-powered natural language intent extractor.

Safely extracts structured booking hints (intent, service name, date, time) from
free-text WhatsApp messages (supporting English, Hindi, Tamil, Hinglish, Tanglish).
Never bypasses deterministic slot calculation or database constraints; gracefully
returns None when GEMINI_API_KEY is not configured or on any API failure.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date

import structlog

from app.config import settings

logger = structlog.get_logger()


@dataclass
class ExtractedIntent:
    intent: str = "unknown"  # book, my_bookings, cancel, reschedule, human, greeting, unknown
    service_name: str | None = None
    target_date: str | None = None  # YYYY-MM-DD
    preferred_time: str | None = None  # HH:MM (24h)
    confidence: float = 0.0


async def extract_booking_intent(
    message: str,
    available_services: list[str],
    today: date,
    business_type: str = "salon",
) -> ExtractedIntent | None:
    """Use Gemini to parse a free-text customer message into structured hints.

    Returns None if Gemini is unconfigured, message is too short, or parsing fails.
    """
    if not settings.gemini_api_key or len(message.strip()) < 4:
        return None

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=settings.gemini_api_key)
        prompt = (
            f"You are an intent extractor for a {business_type} appointment booking bot.\n"
            f"Today's date is {today.isoformat()} ({today.strftime('%A')}).\n"
            f"Available services: {json.dumps(available_services)}\n"
            f"Customer message (may be English, Hindi, Tamil, Hinglish, or Tanglish): {json.dumps(message)}\n\n"
            "Return ONLY valid JSON with keys:\n"
            '- "intent": one of ["book", "my_bookings", "cancel", "reschedule", "human", "greeting", "unknown"]\n'
            '- "service_name": exact match from Available services or null\n'
            '- "target_date": "YYYY-MM-DD" or null\n'
            '- "preferred_time": "HH:MM" (24-hour) or null\n'
            '- "confidence": float between 0.0 and 1.0\n'
        )
        response = await client.aio.models.generate_content(
            model=settings.gemini_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.0,
            ),
        )
        raw_text = (response.text or "").strip()
        if not raw_text:
            return None
        data = json.loads(raw_text)
        return ExtractedIntent(
            intent=str(data.get("intent", "unknown")),
            service_name=data.get("service_name"),
            target_date=data.get("target_date"),
            preferred_time=data.get("preferred_time"),
            confidence=float(data.get("confidence", 0.0) or 0.0),
        )
    except Exception as exc:
        logger.debug("gemini_nlu_fallback", error=str(exc))
        return None
