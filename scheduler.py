"""Proactive monitor — APScheduler job + on-demand demo trigger."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select

from agent_core import Orchestrator
from channels import Channel, InMemoryChannel, OutboxChannel
from config import get_settings
from db import EmployeeRow, get_session_factory, increment_proactive, init_db, now
from demo_mode import advance_clock, ensure_demo_response_files, in_work_hours
from exceptions import ProactiveCapError, QuietHoursError
from guardrails import assert_not_quiet_hours, assert_proactive_cap
from interventions import latest_open_intervention, verify_open_interventions
from logging_utils import setup_logging
from models import Importance, ProactiveScanResult

logger = logging.getLogger(__name__)

# Shared channel for simulator to poll proactive messages
SIMULATOR_CHANNEL = InMemoryChannel()


def _nutrition_or_movement_gap(session, user_id: str, at: datetime) -> bool:
    from db import minutes_since_last_meal, minutes_since_last_movement

    meal_mins = minutes_since_last_meal(session, user_id, at)
    move_mins = minutes_since_last_movement(session, user_id, at)
    meal_gap = meal_mins is None or meal_mins >= 240
    move_gap = move_mins is None or move_mins >= 180
    return meal_gap or move_gap


def scan_and_notify(
    channel: Channel | None = None,
    *,
    only_user_id: str | None = None,
    force_demo_1220: bool = False,
) -> ProactiveScanResult:
    """
    Scan opted-in employees for high-importance meetings in 60–90 minutes
    with a nutrition/movement gap; send proactive Channel messages.
    """
    ensure_demo_response_files()
    init_db()
    channel = channel or SIMULATOR_CHANNEL
    session = get_session_factory()()
    sent = 0
    scanned = 0
    skipped_quiet = 0
    skipped_cap = 0
    details: list[dict] = []

    try:
        if force_demo_1220:
            # Jump DEMO_CLOCK to today 12:20 for the live demo
            wall = datetime.now()
            target = wall.replace(hour=12, minute=20, second=0, microsecond=0)
            advance_clock(session, target)
            only_user_id = only_user_id or get_settings().live_demo_user_id

        at = now(session)
        verify_open_interventions(session, at)
        try:
            assert_not_quiet_hours(at)
            if not in_work_hours(at):
                raise QuietHoursError("Outside work hours")
        except QuietHoursError:
            if not force_demo_1220:
                return ProactiveScanResult(
                    messages_sent=0,
                    scanned=0,
                    skipped_quiet_hours=1,
                    skipped_cap=0,
                    details=[{"reason": "outside_work_hours"}],
                )

        stmt = select(EmployeeRow).where(EmployeeRow.opted_in.is_(True))
        employees = list(session.scalars(stmt).all())
        if only_user_id:
            employees = [e for e in employees if e.id == only_user_id]

        from calendar_provider import MockCalendar

        cal = MockCalendar(session)
        orch = Orchestrator(session)

        for emp in employees:
            scanned += 1
            try:
                assert_proactive_cap(session, emp.id, at)
            except ProactiveCapError:
                skipped_cap += 1
                details.append({"user": emp.id, "reason": "cap"})
                continue
            # Don't stack nudges: one open suggestion at a time per employee
            if not force_demo_1220 and latest_open_intervention(session, emp.id):
                details.append({"user": emp.id, "reason": "open_suggestion"})
                continue

            events = cal.remaining_events(emp.id, at)
            high = [
                e
                for e in events
                if e.importance == Importance.HIGH
                and 60 <= (e.start - at).total_seconds() / 60 <= 90
            ]
            if not high and not force_demo_1220:
                details.append({"user": emp.id, "reason": "no_high_meeting_window"})
                continue
            if not _nutrition_or_movement_gap(session, emp.id, at) and not force_demo_1220:
                details.append({"user": emp.id, "reason": "no_gap"})
                continue

            decision, message = orch.handle_proactive(
                emp.id, demo_key="proactive_1220"
            )
            mid = channel.send_proactive(emp.id, message)
            increment_proactive(session, emp.id, at.date())
            session.commit()
            sent += 1
            details.append(
                {
                    "user": emp.id if only_user_id else "anonymized",
                    "message_id": mid,
                    "intervention": decision.intervention.type.value
                    if decision.intervention
                    else None,
                    "correlation_id": decision.correlation_id,
                }
            )
            logger.info("Proactive message sent correlation=%s", decision.correlation_id)
    finally:
        session.close()

    return ProactiveScanResult(
        messages_sent=sent,
        scanned=scanned,
        skipped_quiet_hours=skipped_quiet,
        skipped_cap=skipped_cap,
        details=details,
    )


def start_scheduler(
    channel_factory: Callable[[], Channel] | None = None,
) -> BackgroundScheduler:
    """Start APScheduler with a 15-minute proactive scan job."""
    setup_logging()
    init_db()
    scheduler = BackgroundScheduler()

    def job() -> None:
        # Outbox (SQLite) so messages from this process reach the simulator UI
        ch = channel_factory() if channel_factory else OutboxChannel()
        result = scan_and_notify(ch)
        logger.info("Proactive scan: %s", result.model_dump())

    scheduler.add_job(job, "interval", minutes=15, id="proactive_scan", replace_existing=True)
    scheduler.start()
    logger.info("Scheduler started (every 15 minutes)")
    return scheduler


def main() -> None:
    setup_logging()
    start_scheduler()
    logger.info("Scheduler process running — Ctrl+C to stop")
    import time

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        logger.info("Scheduler stopped")


if __name__ == "__main__":
    main()
