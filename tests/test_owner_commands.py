"""Expanded tests for owner console command parser — typos, time formats, edge cases."""
from datetime import time

import pytest

from app.owner_console.parser import OwnerCommand, ParsedCommand, parse_command


class TestTodayCommand:
    def test_today(self):
        result = parse_command("today")
        assert result.command == OwnerCommand.TODAY

    def test_today_uppercase(self):
        result = parse_command("TODAY")
        assert result.command == OwnerCommand.TODAY

    def test_todays(self):
        result = parse_command("todays")
        assert result.command == OwnerCommand.TODAY

    def test_2day(self):
        result = parse_command("2day")
        assert result.command == OwnerCommand.TODAY


class TestTomorrowCommand:
    def test_tomorrow(self):
        result = parse_command("tomorrow")
        assert result.command == OwnerCommand.TOMORROW

    def test_tmrw(self):
        result = parse_command("tmrw")
        assert result.command == OwnerCommand.TOMORROW

    def test_tmr(self):
        result = parse_command("tmr")
        assert result.command == OwnerCommand.TOMORROW

    def test_tomorow_typo(self):
        result = parse_command("tomorow")
        assert result.command == OwnerCommand.TOMORROW

    def test_tommorow_typo(self):
        result = parse_command("tommorow")
        assert result.command == OwnerCommand.TOMORROW


class TestTimeParsing:
    def test_block_4pm(self):
        result = parse_command("block today 4pm")
        assert result.command == OwnerCommand.BLOCK
        assert result.time_start == time(16, 0)

    def test_block_4_30pm(self):
        result = parse_command("block today 4:30pm")
        assert result.command == OwnerCommand.BLOCK
        assert result.time_start == time(16, 30)

    def test_block_4_30_pm_with_space(self):
        result = parse_command("block today 4:30 PM")
        assert result.command == OwnerCommand.BLOCK
        assert result.time_start == time(16, 30)

    def test_block_time_range(self):
        result = parse_command("block today 4pm-6pm")
        assert result.command == OwnerCommand.BLOCK
        assert result.time_start == time(16, 0)
        assert result.time_end == time(18, 0)

    def test_block_time_range_with_dash(self):
        result = parse_command("block today 4-6pm")
        assert result.command == OwnerCommand.BLOCK
        assert result.time_end == time(18, 0)

    def test_block_am_time(self):
        result = parse_command("block today 9am")
        assert result.command == OwnerCommand.BLOCK
        assert result.time_start == time(9, 0)

    def test_block_12pm(self):
        result = parse_command("block today 12pm")
        assert result.command == OwnerCommand.BLOCK
        assert result.time_start == time(12, 0)


class TestDateParsing:
    def test_block_tomorrow(self):
        result = parse_command("block tomorrow 4pm")
        assert result.command == OwnerCommand.BLOCK
        assert result.target_date is not None

    def test_free_today(self):
        result = parse_command("free today")
        assert result.command == OwnerCommand.FREE
        assert result.target_date is not None

    def test_bookings_default_today(self):
        result = parse_command("bookings")
        assert result.command == OwnerCommand.BOOKINGS
        assert result.target_date is not None

    def test_monday_detected(self):
        result = parse_command("monday")
        assert result.command == OwnerCommand.BOOKINGS
        assert result.target_date is not None
        assert result.target_date.weekday() == 0  # Monday

    def test_next_saturday(self):
        result = parse_command("saturday")
        assert result.command == OwnerCommand.BOOKINGS
        assert result.target_date is not None
        assert result.target_date.weekday() == 5  # Saturday


class TestBlockUnblockCommands:
    def test_block_basic(self):
        result = parse_command("block")
        assert result.command == OwnerCommand.BLOCK

    def test_unblock_basic(self):
        result = parse_command("unblock")
        assert result.command == OwnerCommand.UNBLOCK

    def test_unblock_with_time(self):
        result = parse_command("unblock today 4pm-6pm")
        assert result.command == OwnerCommand.UNBLOCK
        assert result.time_start == time(16, 0)
        assert result.time_end == time(18, 0)

    def test_block_typo_blck(self):
        result = parse_command("blck today 4pm")
        assert result.command == OwnerCommand.BLOCK

    def test_unblock_typo_unblck(self):
        result = parse_command("unblck today 4pm")
        assert result.command == OwnerCommand.UNBLOCK


class TestCancelCommand:
    def test_cancel_basic(self):
        result = parse_command("cancel")
        assert result.command == OwnerCommand.CANCEL

    def test_cancel_with_ref(self):
        result = parse_command("cancel ABC123")
        assert result.command == OwnerCommand.CANCEL
        assert result.booking_ref == "ABC123"

    def test_cancel_typo(self):
        result = parse_command("cancl something")
        assert result.command == OwnerCommand.CANCEL


class TestOtherCommands:
    def test_stats(self):
        result = parse_command("stats")
        assert result.command == OwnerCommand.STATS

    def test_summary_maps_to_stats(self):
        result = parse_command("summary")
        assert result.command == OwnerCommand.STATS

    def test_help(self):
        result = parse_command("help")
        assert result.command == OwnerCommand.HELP

    def test_question_mark_is_help(self):
        result = parse_command("?")
        assert result.command == OwnerCommand.HELP

    def test_pause(self):
        result = parse_command("pause")
        assert result.command == OwnerCommand.PAUSE

    def test_resume(self):
        result = parse_command("resume")
        assert result.command == OwnerCommand.RESUME

    def test_free(self):
        result = parse_command("free")
        assert result.command == OwnerCommand.FREE

    def test_appointments_maps_to_bookings(self):
        result = parse_command("appointments")
        assert result.command == OwnerCommand.BOOKINGS


class TestEdgeCases:
    def test_empty_string(self):
        result = parse_command("")
        assert result.command == OwnerCommand.UNKNOWN

    def test_whitespace_only(self):
        result = parse_command("   ")
        assert result.command == OwnerCommand.UNKNOWN

    def test_very_long_input(self):
        result = parse_command("x" * 10000)
        assert result.command == OwnerCommand.UNKNOWN

    def test_special_characters(self):
        result = parse_command("@#$%^&*()")
        assert result.command == OwnerCommand.UNKNOWN

    def test_raw_preserved(self):
        result = parse_command("today bookings")
        assert result.raw == "today bookings"

    def test_leading_trailing_whitespace_stripped(self):
        result = parse_command("  today  ")
        assert result.command == OwnerCommand.TODAY

    def test_numeric_only(self):
        result = parse_command("12345")
        assert result.command == OwnerCommand.UNKNOWN
