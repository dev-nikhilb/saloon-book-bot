"""Tests for feedback handler — pattern matching and regex validation.

The actual handle_feedback_response function requires a DB session,
so we test the regex patterns and flow logic with mocks.
"""
import re
import uuid

import pytest

from app.feedback.handler import BUTTON_PATTERN, RATING_PATTERN


class TestButtonPattern:
    def test_matches_valid_button_callback(self):
        booking_id = str(uuid.uuid4())
        body = f"fb_{booking_id}_5"
        match = BUTTON_PATTERN.match(body)
        assert match is not None
        assert match.group(1) == booking_id
        assert match.group(2) == "5"

    def test_extracts_rating_1(self):
        booking_id = str(uuid.uuid4())
        match = BUTTON_PATTERN.match(f"fb_{booking_id}_1")
        assert match.group(2) == "1"

    def test_extracts_rating_3(self):
        booking_id = str(uuid.uuid4())
        match = BUTTON_PATTERN.match(f"fb_{booking_id}_3")
        assert match.group(2) == "3"

    def test_case_insensitive(self):
        booking_id = str(uuid.uuid4()).upper()
        match = BUTTON_PATTERN.match(f"fb_{booking_id}_4")
        assert match is not None

    def test_no_match_without_fb_prefix(self):
        booking_id = str(uuid.uuid4())
        match = BUTTON_PATTERN.match(f"{booking_id}_5")
        assert match is None

    def test_no_match_without_rating(self):
        booking_id = str(uuid.uuid4())
        match = BUTTON_PATTERN.match(f"fb_{booking_id}")
        assert match is None

    def test_no_match_double_digit_rating(self):
        booking_id = str(uuid.uuid4())
        match = BUTTON_PATTERN.match(f"fb_{booking_id}_10")
        assert match is None

    def test_no_match_text_rating(self):
        booking_id = str(uuid.uuid4())
        match = BUTTON_PATTERN.match(f"fb_{booking_id}_great")
        assert match is None

    def test_no_match_empty_string(self):
        assert BUTTON_PATTERN.match("") is None

    def test_no_match_random_text(self):
        assert BUTTON_PATTERN.match("hello world") is None


class TestRatingPattern:
    def test_matches_1(self):
        assert RATING_PATTERN.match("1") is not None

    def test_matches_2(self):
        assert RATING_PATTERN.match("2") is not None

    def test_matches_3(self):
        assert RATING_PATTERN.match("3") is not None

    def test_matches_4(self):
        assert RATING_PATTERN.match("4") is not None

    def test_matches_5(self):
        assert RATING_PATTERN.match("5") is not None

    def test_no_match_0(self):
        assert RATING_PATTERN.match("0") is None

    def test_no_match_6(self):
        assert RATING_PATTERN.match("6") is None

    def test_no_match_negative(self):
        assert RATING_PATTERN.match("-1") is None

    def test_no_match_text(self):
        assert RATING_PATTERN.match("great") is None

    def test_no_match_multi_digit(self):
        assert RATING_PATTERN.match("12") is None

    def test_no_match_digit_with_text(self):
        assert RATING_PATTERN.match("3 stars") is None

    def test_no_match_empty(self):
        assert RATING_PATTERN.match("") is None


class TestFeedbackHandlerFlow:
    """Test the handler function with mocked DB session."""

    @pytest.mark.asyncio
    async def test_button_callback_saves_rating(self):
        """Button callback should update feedback and return a reply."""
        from unittest.mock import AsyncMock, MagicMock

        from app.feedback.handler import handle_feedback_response

        booking_id = str(uuid.uuid4())
        body = f"fb_{booking_id}_5"

        db = AsyncMock()
        # First execute: UPDATE feedback ... RETURNING id
        update_result = MagicMock()
        update_result.first.return_value = MagicMock(id=uuid.uuid4())
        db.execute.return_value = update_result

        reply = await handle_feedback_response(db, "tenant1", "+919999999999", body)
        assert reply is not None
        assert isinstance(reply, str)

    @pytest.mark.asyncio
    async def test_non_feedback_message_returns_none(self):
        """Random text that isn't a feedback pattern should return None."""
        from unittest.mock import AsyncMock, MagicMock

        from app.feedback.handler import handle_feedback_response

        db = AsyncMock()
        # For the comment follow-up check
        comment_result = MagicMock()
        comment_result.first.return_value = None
        db.execute.return_value = comment_result

        reply = await handle_feedback_response(db, "tenant1", "+919999999999", "hello there")
        assert reply is None

    @pytest.mark.asyncio
    async def test_plain_digit_without_pending_returns_none(self):
        """A digit 1-5 but no pending feedback row should return None."""
        from unittest.mock import AsyncMock, MagicMock

        from app.feedback.handler import handle_feedback_response

        db = AsyncMock()
        result = MagicMock()
        result.first.return_value = None
        db.execute.return_value = result

        reply = await handle_feedback_response(db, "tenant1", "+919999999999", "4")
        assert reply is None

    @pytest.mark.asyncio
    async def test_comment_followup_saves(self):
        """After rating, a text message should be saved as comment."""
        from unittest.mock import AsyncMock, MagicMock

        from app.feedback.handler import handle_feedback_response

        db = AsyncMock()

        # The handler checks button pattern first (no match for "Great service"),
        # then digit pattern (no match), then comment follow-up
        comment_row = MagicMock()
        comment_row.id = uuid.uuid4()
        comment_row.booking_id = uuid.uuid4()
        comment_row.rating = 5

        result = MagicMock()
        result.first.return_value = comment_row
        db.execute.return_value = result

        reply = await handle_feedback_response(db, "tenant1", "+919999999999", "Great service!")
        assert reply is not None
        assert "thank" in reply.lower()
