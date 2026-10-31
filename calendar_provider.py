"""Calendar provider protocol and JSON/DB-backed mock implementation."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from config import SEED_DIR
from db import CalendarEventRow, now
from models import CalendarEvent, FreeSlot, Importance


class CalendarProvider(Protocol):
    """Protocol for work-hours calendar access (no meeting titles)."""

    def get_events(self, user_id: str, day: datetime | None = None) -> list[CalendarEvent]:
        """Return privacy-safe events for the user on the given day."""

    def find_free_slots(
        self,
        user_id: str,
        min_minutes: int,
        day: datetime | None = None,
        work_start_hour: int = 8,
        work_end_hour: int = 16,
    ) -> list[FreeSlot]:
        """Return free slots of at least min_minutes within work hours."""


class MockCalendar:
    """Calendar backed by SQLite (seeded from seed/calendars.json)."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get_events(
        self,
        user_id: str,
        day: datetime | None = None,
        *,
        include_wellness_blocks: bool = False,
    ) -> list[CalendarEvent]:
        """Return privacy-safe meetings for the user on the given day."""
        at = day or now(self.session)
        day_start = at.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        stmt = select(CalendarEventRow).where(
            CalendarEventRow.user_id == user_id,
            CalendarEventRow.start >= day_start,
            CalendarEventRow.start < day_end,
        )
        if not include_wellness_blocks:
            stmt = stmt.where(CalendarEventRow.is_wellness_block.is_not(True))
        rows = self.session.scalars(stmt.order_by(CalendarEventRow.start)).all()
        return [
            CalendarEvent(
                start=r.start,
                end=r.end,
                importance=Importance(r.importance),
                attendee_count=r.attendee_count,
            )
            for r in rows
        ]

    def remaining_events(self, user_id: str, at: datetime | None = None) -> list[CalendarEvent]:
        """Events that end after `at` (upcoming / in progress)."""
        clock = at or now(self.session)
        return [e for e in self.get_events(user_id, clock) if e.end > clock]

    def find_free_slots(
        self,
        user_id: str,
        min_minutes: int,
        day: datetime | None = None,
        work_start_hour: int = 8,
        work_end_hour: int = 16,
    ) -> list[FreeSlot]:
        """Return free slots of at least min_minutes within work hours."""
        at = day or now(self.session)
        work_start = at.replace(hour=work_start_hour, minute=0, second=0, microsecond=0)
        work_end = at.replace(hour=work_end_hour, minute=0, second=0, microsecond=0)
        events = sorted(
            self.get_events(user_id, at, include_wellness_blocks=True),
            key=lambda e: e.start,
        )

        cursor = max(work_start, at)
        slots: list[FreeSlot] = []
        for ev in events:
            if ev.start > cursor:
                gap_end = min(ev.start, work_end)
                duration = int((gap_end - cursor).total_seconds() // 60)
                if duration >= min_minutes:
                    slots.append(FreeSlot(start=cursor, end=gap_end, duration_min=duration))
            cursor = max(cursor, ev.end)
        if cursor < work_end:
            duration = int((work_end - cursor).total_seconds() // 60)
            if duration >= min_minutes:
                slots.append(FreeSlot(start=cursor, end=work_end, duration_min=duration))
        return slots


class JsonCalendar:
    """Read-only calendar from seed/calendars.json (used in unit tests)."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (SEED_DIR / "calendars.json")
        self._data = json.loads(self.path.read_text(encoding="utf-8"))

    def get_events(self, user_id: str, day: datetime | None = None) -> list[CalendarEvent]:
        raw = self._data.get(user_id, [])
        events = [
            CalendarEvent(
                start=datetime.fromisoformat(e["start"]),
                end=datetime.fromisoformat(e["end"]),
                importance=Importance(e["importance"]),
                attendee_count=e.get("attendee_count", 0),
            )
            for e in raw
        ]
        if day is None:
            return events
        return [e for e in events if e.start.date() == day.date()]

    def find_free_slots(
        self,
        user_id: str,
        min_minutes: int,
        day: datetime | None = None,
        work_start_hour: int = 8,
        work_end_hour: int = 16,
    ) -> list[FreeSlot]:
        at = day or datetime.now()
        work_start = at.replace(hour=work_start_hour, minute=0, second=0, microsecond=0)
        work_end = at.replace(hour=work_end_hour, minute=0, second=0, microsecond=0)
        events = sorted(self.get_events(user_id, at), key=lambda e: e.start)
        cursor = max(work_start, at)
        slots: list[FreeSlot] = []
        for ev in events:
            if ev.start > cursor:
                gap_end = min(ev.start, work_end)
                duration = int((gap_end - cursor).total_seconds() // 60)
                if duration >= min_minutes:
                    slots.append(FreeSlot(start=cursor, end=gap_end, duration_min=duration))
            cursor = max(cursor, ev.end)
        if cursor < work_end:
            duration = int((work_end - cursor).total_seconds() // 60)
            if duration >= min_minutes:
                slots.append(FreeSlot(start=cursor, end=work_end, duration_min=duration))
        return slots
