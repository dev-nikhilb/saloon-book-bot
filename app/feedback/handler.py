"""Feedback handler — processes customer rating responses from WhatsApp.

Called from the conversation router when a message matches a feedback pattern
(button tap like "fb_<booking_id>_<rating>" or a plain digit 1-5 during feedback flow).
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()

# Matches button callback: fb_<uuid>_<rating>
BUTTON_PATTERN = re.compile(r"^fb_([0-9a-f-]+)_(\d)$", re.IGNORECASE)

# Matches plain digit rating (1-5)
RATING_PATTERN = re.compile(r"^([1-5])$")


async def handle_feedback_response(
    db: AsyncSession,
    tenant_id: str,
    customer_phone: str,
    body: str,
) -> str | None:
    """Process a customer's feedback response.

    Returns a reply message string, or None if the body is not a feedback response.
    """
    body = body.strip()

    booking_id: str | None = None
    rating: int | None = None

    # Try button callback format first
    btn_match = BUTTON_PATTERN.match(body)
    if btn_match:
        booking_id = btn_match.group(1)
        rating = int(btn_match.group(2))
    else:
        # Try plain digit (look up most recent unanswered feedback for this customer)
        digit_match = RATING_PATTERN.match(body)
        if digit_match:
            rating = int(digit_match.group(1))
            # Find the most recent feedback row without a rating for this customer
            result = await db.execute(
                text("""
                    SELECT f.id, f.booking_id
                    FROM feedback f
                    JOIN bookings b ON f.booking_id = b.id
                    JOIN customers c ON b.customer_id = c.id
                    WHERE f.tenant_id = :tid
                      AND c.phone = :phone
                      AND f.rating IS NULL
                    ORDER BY f.created_at DESC
                    LIMIT 1
                """),
                {"tid": tenant_id, "phone": customer_phone},
            )
            row = result.first()
            if row:
                booking_id = str(row.booking_id)
            else:
                return None  # No pending feedback request
        else:
            # Check if this is a comment follow-up (customer already gave a rating,
            # we asked for optional comment)
            comment_result = await db.execute(
                text("""
                    SELECT f.id, f.booking_id, f.rating
                    FROM feedback f
                    JOIN bookings b ON f.booking_id = b.id
                    JOIN customers c ON b.customer_id = c.id
                    WHERE f.tenant_id = :tid
                      AND c.phone = :phone
                      AND f.rating IS NOT NULL
                      AND f.comment IS NULL
                    ORDER BY f.created_at DESC
                    LIMIT 1
                """),
                {"tid": tenant_id, "phone": customer_phone},
            )
            comment_row = comment_result.first()
            if comment_row and len(body) > 1:
                # Store the comment
                await db.execute(
                    text("""
                        UPDATE feedback SET comment = :comment
                        WHERE id = :fid AND tenant_id = :tid
                    """),
                    {"comment": body, "fid": comment_row.id, "tid": tenant_id},
                )
                await db.commit()
                logger.info(
                    "feedback_comment_saved",
                    feedback_id=str(comment_row.id),
                    tenant_id=tenant_id,
                )
                return "Thank you for your feedback! We truly appreciate it."
            return None

    if not booking_id or rating is None:
        return None

    # Update feedback with rating
    result = await db.execute(
        text("""
            UPDATE feedback
            SET rating = :rating
            WHERE booking_id = :bid AND tenant_id = :tid AND rating IS NULL
            RETURNING id
        """),
        {"rating": rating, "bid": booking_id, "tid": tenant_id},
    )
    updated = result.first()

    if not updated:
        # No pending feedback row -- might be a duplicate; insert fresh
        await db.execute(
            text("""
                INSERT INTO feedback (id, booking_id, tenant_id, rating, created_at)
                VALUES (:id, :bid, :tid, :rating, :now)
                ON CONFLICT DO NOTHING
            """),
            {
                "id": uuid.uuid4(),
                "bid": booking_id,
                "tid": tenant_id,
                "rating": rating,
                "now": datetime.now(timezone.utc),
            },
        )

    await db.commit()

    logger.info(
        "feedback_rating_saved",
        booking_id=booking_id,
        tenant_id=tenant_id,
        rating=rating,
    )

    # Build reply
    if rating >= 4:
        # Check if tenant has a Google review URL
        reply = await _maybe_prompt_google_review(db, tenant_id, booking_id, rating)
        if reply:
            return reply
        return (
            "Thank you for the wonderful rating! "
            "We're glad you had a great experience. "
            "Feel free to share any additional comments."
        )
    elif rating == 3:
        return (
            "Thank you for your feedback! "
            "We'd love to know how we can improve. "
            "Feel free to share any comments."
        )
    else:
        return (
            "We're sorry to hear that your experience wasn't great. "
            "We take your feedback seriously and will work to improve. "
            "Please share any specific concerns so we can address them."
        )


async def _maybe_prompt_google_review(
    db: AsyncSession,
    tenant_id: str,
    booking_id: str,
    rating: int,
) -> str | None:
    """If tenant has google_review_url and rating >= 4, prompt for Google review."""
    result = await db.execute(
        text("""
            SELECT ts.google_review_url
            FROM tenant_settings ts
            WHERE ts.tenant_id = :tid AND ts.google_review_url IS NOT NULL
            LIMIT 1
        """),
        {"tid": tenant_id},
    )
    row = result.first()
    if not row or not row.google_review_url:
        return None

    # Mark feedback as google_review_prompted
    await db.execute(
        text("""
            UPDATE feedback
            SET google_review_prompted = true
            WHERE booking_id = :bid AND tenant_id = :tid
        """),
        {"bid": booking_id, "tid": tenant_id},
    )
    await db.commit()

    logger.info(
        "google_review_prompted",
        booking_id=booking_id,
        tenant_id=tenant_id,
    )

    return (
        f"Thank you so much for the {rating}-star rating! "
        f"We'd really appreciate it if you could share your experience on Google too.\n\n"
        f"{row.google_review_url}\n\n"
        f"Feel free to also share any additional comments here."
    )
