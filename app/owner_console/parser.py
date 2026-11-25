"""Forgiving command parser for owner WhatsApp messages.

Owners type casually: "today", "tmrw bookings", "cancel 5pm", "block tomorrow 4-6".
Parser must handle typos, abbreviations, and natural phrasing.
"""
from __future__ import annotations

import re
import zoneinfo
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum


class OwnerCommand(str, Enum):
    TODAY = "today"
    TOMORROW = "tomorrow"
    WEEK = "week"
    BOOKINGS = "bookings"
    CANCEL = "cancel"
    BLOCK = "block"
    UNBLOCK = "unblock"
    STATS = "stats"
    FREE = "free"
    PAUSE = "pause"
    RESUME = "resume"
    HELP = "help"
    UNKNOWN = "unknown"


@dataclass
class ParsedCommand:
    command: OwnerCommand
    target_date: date | None = None
    time_start: time | None = None
    time_end: time | None = None
    booking_ref: str | None = None
    reason: str | None = None
    raw: str = ""


# Typo-tolerant patterns
TODAY_PATTERNS = re.compile(r"^(today|tday|2day|todays?|today'?s?)(\s|$)", re.I)
TOMORROW_PATTERNS = re.compile(r"^(tomorrow|tmrw|tmr|2mrw|tomorow|tommorow)(\s|$)", re.I)
WEEK_PATTERNS = re.compile(r"^(week|this\s+week|weekly|next\s+7\s+days?)(\s|$)", re.I)
CANCEL_PATTERNS = re.compile(r"^(cancel|cncl|cancl)", re.I)
BLOCK_PATTERNS = re.compile(r"^(block|blck|blok)", re.I)
UNBLOCK_PATTERNS = re.compile(r"^(unblock|unblck|unblok)", re.I)
STATS_PATTERNS = re.compile(r"^(stats|stat|summary|report)", re.I)
FREE_PATTERNS = re.compile(r"^(free|available|slots|openings)", re.I)
HELP_PATTERNS = re.compile(r"^(help|commands|cmds|menu|\?)", re.I)
PAUSE_PATTERNS = re.compile(r"^(pause|stop|off|closed)", re.I)
RESUME_PATTERNS = re.compile(r"^(resume|start|on|open)", re.I)
BOOKINGS_PATTERNS = re.compile(r"^(bookings?|appointments?|schedule)", re.I)

# Time patterns: "4pm", "4:30pm", "16:00", "4-6pm", "4pm-6pm"
TIME_PATTERN = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", re.I)
TIME_RANGE_PATTERN = re.compile(
    r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*[-–to]+\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", re.I
)

# Date patterns: "20th", "may 20", "20 may", "20/5"
DATE_PATTERN = re.compile(r"(\d{1,2})(?:st|nd|rd|th)?(?:\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*)?", re.I)


def parse_command(text: str, tz_name: str = "Asia/Kolkata") -> ParsedCommand:
    """Parse an owner's WhatsApp message into a structured command."""
    text = text.strip()
    raw = text

    if not text:
        return ParsedCommand(command=OwnerCommand.UNKNOWN, raw=raw)

    tz = zoneinfo.ZoneInfo(tz_name)
    today = datetime.now(tz).date()
    tomorrow = today + timedelta(days=1)

    # Help
    if HELP_PATTERNS.match(text):
        return ParsedCommand(command=OwnerCommand.HELP, raw=raw)

    # Pause/Resume
    if PAUSE_PATTERNS.match(text):
        return ParsedCommand(command=OwnerCommand.PAUSE, raw=raw)
    if RESUME_PATTERNS.match(text):
        return ParsedCommand(command=OwnerCommand.RESUME, raw=raw)

    # Stats
    if STATS_PATTERNS.match(text):
        return ParsedCommand(command=OwnerCommand.STATS, raw=raw)

    # Today
    if TODAY_PATTERNS.match(text):
        return ParsedCommand(command=OwnerCommand.TODAY, target_date=today, raw=raw)

    # Tomorrow
    if TOMORROW_PATTERNS.match(text):
        return ParsedCommand(command=OwnerCommand.TOMORROW, target_date=tomorrow, raw=raw)

    # Week
    if WEEK_PATTERNS.match(text):
        return ParsedCommand(command=OwnerCommand.WEEK, target_date=today, raw=raw)

    # Cancel
    if CANCEL_PATTERNS.match(text):
        ref = text.split(maxsplit=1)[1] if len(text.split()) > 1 else None
        return ParsedCommand(command=OwnerCommand.CANCEL, booking_ref=ref, raw=raw)

    # Block with time range
    if BLOCK_PATTERNS.match(text):
        return _parse_block_command(text, today, tomorrow, OwnerCommand.BLOCK, raw)

    # Unblock
    if UNBLOCK_PATTERNS.match(text):
        return _parse_block_command(text, today, tomorrow, OwnerCommand.UNBLOCK, raw)

    # Free slots
    if FREE_PATTERNS.match(text):
        target = _extract_date_from_text(text, today, tomorrow)
        return ParsedCommand(command=OwnerCommand.FREE, target_date=target or today, raw=raw)

    # Bookings for a date
    if BOOKINGS_PATTERNS.match(text):
        target = _extract_date_from_text(text, today, tomorrow)
        return ParsedCommand(command=OwnerCommand.BOOKINGS, target_date=target or today, raw=raw)

    # Fallback: try to detect if it's a date query
    if any(word in text.lower() for word in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")):
        target = _extract_weekday_date(text, today)
        if target:
            return ParsedCommand(command=OwnerCommand.BOOKINGS, target_date=target, raw=raw)

    return ParsedCommand(command=OwnerCommand.UNKNOWN, raw=raw)


def _parse_block_command(text: str, today: date, tomorrow: date, cmd: OwnerCommand, raw: str) -> ParsedCommand:
    """Parse block/unblock commands like 'block tomorrow 4-6pm'."""
    target = _extract_date_from_text(text, today, tomorrow)

    time_range = TIME_RANGE_PATTERN.search(text)
    t_start = t_end = None

    if time_range:
        t_start = _parse_time(time_range.group(1), time_range.group(2), time_range.group(3))
        t_end = _parse_time(time_range.group(4), time_range.group(5), time_range.group(6))
    else:
        single_time = TIME_PATTERN.search(text.split(maxsplit=1)[-1] if " " in text else "")
        if single_time:
            t_start = _parse_time(single_time.group(1), single_time.group(2), single_time.group(3))

    return ParsedCommand(
        command=cmd,
        target_date=target or today,
        time_start=t_start,
        time_end=t_end,
        raw=raw,
    )


def _parse_time(hour_str: str, minute_str: str | None, ampm: str | None) -> time | None:
    """Parse time components into a time object."""
    try:
        hour = int(hour_str)
        minute = int(minute_str) if minute_str else 0

        if ampm:
            if ampm.lower() == "pm" and hour != 12:
                hour += 12
            elif ampm.lower() == "am" and hour == 12:
                hour = 0
        elif hour < 8:
            # Assume PM for small numbers without am/pm (salon context)
            hour += 12

        return time(hour, minute)
    except (ValueError, TypeError):
        return None


def _extract_date_from_text(text: str, today: date, tomorrow: date) -> date | None:
    """Extract a date reference from text."""
    lower = text.lower()
    if "today" in lower or "2day" in lower:
        return today
    if "tomorrow" in lower or "tmrw" in lower or "tmr" in lower:
        return tomorrow
    return _extract_weekday_date(text, today)


def _extract_weekday_date(text: str, today: date) -> date | None:
    """Extract next occurrence of a weekday from text."""
    days = {
        "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
        "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
        "friday": 4, "saturday": 5, "sunday": 6,
    }
    lower = text.lower()
    for name, weekday in days.items():
        if name in lower:
            days_ahead = weekday - today.weekday()
            if days_ahead <= 0:
                days_ahead += 7
            return today + timedelta(days=days_ahead)
    return None
