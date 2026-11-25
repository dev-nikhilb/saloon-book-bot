"""Tests for owner command parser."""
from datetime import date, timedelta

from app.owner_console.parser import OwnerCommand, parse_command


class TestOwnerParser:
    def test_today_variants(self):
        for text in ["today", "Today", "TODAY", "todays", "today's", "2day"]:
            cmd = parse_command(text)
            assert cmd.command == OwnerCommand.TODAY
            assert cmd.target_date == date.today()

    def test_tomorrow_variants(self):
        for text in ["tomorrow", "tmrw", "tmr", "Tomorrow"]:
            cmd = parse_command(text)
            assert cmd.command == OwnerCommand.TOMORROW
            assert cmd.target_date == date.today() + timedelta(days=1)

    def test_help(self):
        for text in ["help", "Help", "commands", "?"]:
            assert parse_command(text).command == OwnerCommand.HELP

    def test_stats(self):
        for text in ["stats", "Stats", "summary", "report"]:
            assert parse_command(text).command == OwnerCommand.STATS

    def test_cancel_with_ref(self):
        cmd = parse_command("cancel abc123")
        assert cmd.command == OwnerCommand.CANCEL
        assert cmd.booking_ref == "abc123"

    def test_cancel_without_ref(self):
        cmd = parse_command("cancel")
        assert cmd.command == OwnerCommand.CANCEL
        assert cmd.booking_ref is None

    def test_block_with_time_range(self):
        cmd = parse_command("block tomorrow 4-6pm")
        assert cmd.command == OwnerCommand.BLOCK
        assert cmd.target_date == date.today() + timedelta(days=1)
        assert cmd.time_start is not None
        assert cmd.time_start.hour == 16
        assert cmd.time_end is not None
        assert cmd.time_end.hour == 18

    def test_pause_resume(self):
        assert parse_command("pause").command == OwnerCommand.PAUSE
        assert parse_command("resume").command == OwnerCommand.RESUME

    def test_free_slots(self):
        cmd = parse_command("free")
        assert cmd.command == OwnerCommand.FREE

    def test_unknown_command(self):
        assert parse_command("asdfghjkl").command == OwnerCommand.UNKNOWN

    def test_bookings_command(self):
        cmd = parse_command("bookings")
        assert cmd.command == OwnerCommand.BOOKINGS

    def test_time_parsing_pm_assumption(self):
        """Small numbers without am/pm should assume PM (salon context)."""
        cmd = parse_command("block today 4-6")
        assert cmd.time_start.hour == 16
        assert cmd.time_end.hour == 18
