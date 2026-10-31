"""Intervention lifecycle — central closed-loop entity for the demo."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from calendar_provider import MockCalendar
from db import (
    ActivityLogRow,
    ActivityStateRow,
    CalendarEventRow,
    DecisionRow,
    InterventionRow,
    MovementBreakRow,
    bump_today_metrics,
    get_employee,
    get_policy,
    now,
    write_audit,
)
from exceptions import GuardrailError, NotFoundError
from models import (
    Importance,
    Intervention,
    InterventionRecord,
    InterventionStatus,
    InterventionType,
)

logger = logging.getLogger(__name__)

OPEN_STATUSES = {
    InterventionStatus.PROPOSED.value,
    InterventionStatus.ACCEPTED.value,
    InterventionStatus.IN_PROGRESS.value,
}
# Only walks the employee actually started are step-tracked
TRACKING_STATUSES = {
    InterventionStatus.ACCEPTED.value,
    InterventionStatus.IN_PROGRESS.value,
}
STEP_TRACKED_TYPES = {
    InterventionType.WALK.value,
    InterventionType.MOVEMENT_BREAK.value,
}
# A phone sync older than this can't be trusted as the walk's starting step count
STALE_SYNC = timedelta(minutes=10)
MIN_BLOCK = timedelta(minutes=5)

# Allowed transitions
_TRANSITIONS: dict[str, set[str]] = {
    InterventionStatus.PROPOSED.value: {
        InterventionStatus.ACCEPTED.value,
        InterventionStatus.DECLINED.value,
        InterventionStatus.EXPIRED.value,
        InterventionStatus.IN_PROGRESS.value,
        InterventionStatus.COMPLETED.value,
    },
    InterventionStatus.ACCEPTED.value: {
        InterventionStatus.IN_PROGRESS.value,
        InterventionStatus.COMPLETED.value,
        InterventionStatus.EXPIRED.value,
        InterventionStatus.DECLINED.value,
    },
    InterventionStatus.IN_PROGRESS.value: {
        InterventionStatus.COMPLETED.value,
        InterventionStatus.EXPIRED.value,
        InterventionStatus.DECLINED.value,
    },
}


def _wall_now() -> datetime:
    """Real time — phone syncs happen in real time even when DEMO_CLOCK is frozen."""
    return datetime.now().replace(microsecond=0)


def _row_to_record(row: InterventionRow) -> InterventionRecord:
    return InterventionRecord(
        id=row.id,
        user_id=row.user_id,
        department=row.department,
        type=InterventionType(row.type),
        status=InterventionStatus(row.status),
        rationale=row.rationale,
        correlation_id=row.correlation_id,
        meal_id=row.meal_id,
        duration_min=row.duration_min,
        target_steps=row.target_steps,
        baseline_steps=row.baseline_steps or 0,
        current_steps=row.current_steps or 0,
        meeting_start=row.meeting_start,
        created_at=row.created_at,
        updated_at=row.updated_at,
        completed_at=row.completed_at,
        proactive=row.proactive,
        baseline_pending=bool(row.baseline_pending),
        calendar_event_id=row.calendar_event_id,
    )


def get_activity_state(session: Session, user_id: str) -> ActivityStateRow:
    """Return or create activity state for a user."""
    row = session.get(ActivityStateRow, user_id)
    if row is None:
        row = ActivityStateRow(
            user_id=user_id,
            steps=0,
            updated_at=_wall_now(),
            source="init",
        )
        session.add(row)
        session.flush()
    return row


def _sync_is_fresh(state: ActivityStateRow) -> bool:
    """True if the stored step total is a recent real reading (not seed/init/stale)."""
    if state.source in ("seed", "init") or state.updated_at is None:
        return False
    return _wall_now() - state.updated_at <= STALE_SYNC


def _next_high_or_any_meeting(session: Session, user_id: str, at: datetime) -> datetime | None:
    """Start of the next meeting that hasn't begun yet (prefer high importance)."""
    cal = MockCalendar(session)
    events = [e for e in cal.get_events(user_id, at) if e.start > at]
    high = [e for e in events if e.importance == Importance.HIGH]
    pool = high or events
    if not pool:
        return None
    return sorted(pool, key=lambda e: e.start)[0].start


def create_intervention(
    session: Session,
    *,
    user_id: str,
    intervention: Intervention,
    correlation_id: str,
    risk_score: float | None,
    proactive: bool = False,
) -> InterventionRecord | None:
    """
    Persist a PROPOSED intervention (skip type=none).

    Idempotent per correlation_id: repeating the same decision returns the existing row.
    """
    if intervention.type == InterventionType.NONE:
        return None

    existing = session.scalars(
        select(InterventionRow).where(InterventionRow.correlation_id == correlation_id)
    ).first()
    if existing:
        return _row_to_record(existing)

    emp = get_employee(session, user_id)
    department = emp.department if emp else "Unknown"
    at = now(session)
    meeting_start = _next_high_or_any_meeting(session, user_id, at)

    # A new suggestion replaces any older one the employee never acted on
    for old in session.scalars(
        select(InterventionRow).where(
            InterventionRow.user_id == user_id,
            InterventionRow.status == InterventionStatus.PROPOSED.value,
        )
    ).all():
        old.status = InterventionStatus.EXPIRED.value
        old.updated_at = at
        write_audit(
            session,
            "intervention_superseded",
            f"{old.type} replaced by a newer suggestion",
            department=old.department,
            correlation_id=old.correlation_id,
        )

    # Steps come only from phone API / manual updates — no seeded fake totals.
    activity = get_activity_state(session, user_id)
    live_steps = activity.steps or 0
    target_steps = None
    duration = intervention.duration_min
    if intervention.type == InterventionType.WALK:
        duration = duration or 15
        target_steps = duration * 100  # ~100 steps/min
    elif intervention.type == InterventionType.MOVEMENT_BREAK:
        duration = duration or 5
        target_steps = duration * 100

    row = InterventionRow(
        id=str(uuid.uuid4()),
        user_id=user_id,
        department=department,
        type=intervention.type.value,
        status=InterventionStatus.PROPOSED.value,
        rationale=intervention.rationale,
        correlation_id=correlation_id,
        meal_id=intervention.meal_id,
        duration_min=duration,
        target_steps=target_steps,
        baseline_steps=live_steps,
        current_steps=0,
        meeting_start=meeting_start,
        created_at=at,
        updated_at=at,
        completed_at=None,
        proactive=proactive,
        baseline_pending=False,
    )
    session.add(row)

    # Keep legacy DecisionRow in sync for older dashboard queries
    session.add(
        DecisionRow(
            id=str(uuid.uuid4()),
            correlation_id=correlation_id,
            user_id=user_id,
            department=department,
            intervention_type=intervention.type.value,
            accepted=None,
            risk_score=risk_score,
            rationale=intervention.rationale,
            meal_id=intervention.meal_id,
            created_at=at,
            proactive=proactive,
            intervention_id=row.id,
        )
    )
    bump_today_metrics(session, department, offered=True)
    write_audit(
        session,
        "intervention_proposed",
        f"{intervention.type.value} → proposed",
        department=department,
        correlation_id=correlation_id,
    )
    logger.info("Intervention %s proposed (%s)", row.id[:8], intervention.type.value)
    return _row_to_record(row)


def get_intervention(session: Session, intervention_id: str) -> InterventionRecord:
    row = session.get(InterventionRow, intervention_id)
    if row is None:
        raise NotFoundError(intervention_id)
    return _row_to_record(row)


def get_calendar_block(session: Session, record: InterventionRecord) -> CalendarEventRow | None:
    if not record.calendar_event_id:
        return None
    return session.get(CalendarEventRow, record.calendar_event_id)


def _transition(session: Session, row: InterventionRow, new_status: InterventionStatus) -> InterventionRow:
    allowed = _TRANSITIONS.get(row.status, set())
    if new_status.value not in allowed:
        raise GuardrailError(f"Cannot transition {row.status} → {new_status.value}")
    row.status = new_status.value
    row.updated_at = now(session)
    if new_status == InterventionStatus.COMPLETED:
        row.completed_at = row.updated_at
    return row


def _sync_decisions(session: Session, intervention_id: str, accepted: bool) -> None:
    decisions = session.scalars(
        select(DecisionRow).where(DecisionRow.intervention_id == intervention_id)
    ).all()
    for d in decisions:
        d.accepted = accepted


def _book_walk_block(session: Session, row: InterventionRow, at: datetime) -> CalendarEventRow | None:
    """Reserve the walk in the employee's calendar, ending before the next meeting."""
    if not get_policy(session).auto_book_walk_break:
        return None
    if row.calendar_event_id and session.get(CalendarEventRow, row.calendar_event_id):
        return session.get(CalendarEventRow, row.calendar_event_id)

    duration = row.duration_min or (15 if row.type == InterventionType.WALK.value else 5)
    start = at.replace(second=0, microsecond=0)
    end = start + timedelta(minutes=duration)
    upcoming = [
        e.start for e in MockCalendar(session).get_events(row.user_id, at) if e.start > at
    ]
    next_start = min(upcoming) if upcoming else None
    if next_start and next_start < end:
        end = next_start
    if end - start < MIN_BLOCK:
        write_audit(
            session,
            "calendar_block_skipped",
            "Not enough free time before the next meeting to book a walk block",
            department=row.department,
            correlation_id=row.correlation_id,
        )
        return None

    minutes = int((end - start).total_seconds() // 60)
    kind = "walk" if row.type == InterventionType.WALK.value else "movement break"
    block = CalendarEventRow(
        id=str(uuid.uuid4()),
        user_id=row.user_id,
        start=start,
        end=end,
        importance=Importance.LOW.value,
        attendee_count=1,
        label=f"Wellness {kind} ({minutes} min) — auto-booked",
        is_wellness_block=True,
        intervention_id=row.id,
    )
    session.add(block)
    row.calendar_event_id = block.id
    write_audit(
        session,
        "calendar_block_booked",
        f"{kind} {start.strftime('%H:%M')}–{end.strftime('%H:%M')}",
        department=row.department,
        correlation_id=row.correlation_id,
    )
    return block


def _release_walk_block(session: Session, row: InterventionRow, at: datetime) -> None:
    """Free the calendar when a walk is declined/expired: drop it if unstarted, else trim."""
    if not row.calendar_event_id:
        return
    block = session.get(CalendarEventRow, row.calendar_event_id)
    if block is None:
        return
    if block.start >= at:
        session.delete(block)
        row.calendar_event_id = None
    elif block.end > at:
        block.end = at


def _record_movement(session: Session, row: InterventionRow, at: datetime) -> None:
    """Verified walk counts as movement so the proactive gap check sees it."""
    session.add(
        MovementBreakRow(
            id=str(uuid.uuid4()),
            user_id=row.user_id,
            start=at,
            duration_min=row.duration_min or 5,
        )
    )


def accept_intervention(
    session: Session,
    intervention_id: str,
    *,
    start_progress: bool = True,
) -> InterventionRecord:
    """User accepts proposal (Start walk). Walks also get an auto-booked calendar block."""
    row = session.get(InterventionRow, intervention_id)
    if row is None:
        raise NotFoundError(intervention_id)
    if row.status in (InterventionStatus.IN_PROGRESS.value, InterventionStatus.COMPLETED.value):
        return _row_to_record(row)
    if row.status not in OPEN_STATUSES:
        raise GuardrailError(f"This suggestion is no longer active ({row.status})")

    at = now(session)
    if row.status == InterventionStatus.PROPOSED.value:
        _transition(session, row, InterventionStatus.ACCEPTED)
    if row.type in STEP_TRACKED_TYPES:
        state = get_activity_state(session, row.user_id)
        row.baseline_steps = state.steps or 0
        row.current_steps = 0
        # Stale/seed reading would let steps walked earlier count toward this walk
        row.baseline_pending = not _sync_is_fresh(state)
        _book_walk_block(session, row, at)
    if start_progress:
        _transition(session, row, InterventionStatus.IN_PROGRESS)

    _sync_decisions(session, intervention_id, True)
    bump_today_metrics(session, row.department, accepted=True)
    write_audit(
        session,
        "intervention_accepted",
        f"{row.type} → {row.status}",
        department=row.department,
        correlation_id=row.correlation_id,
    )
    session.commit()
    return _row_to_record(row)


def decline_intervention(session: Session, intervention_id: str) -> InterventionRecord:
    row = session.get(InterventionRow, intervention_id)
    if row is None:
        raise NotFoundError(intervention_id)
    if row.status not in OPEN_STATUSES:
        return _row_to_record(row)
    _transition(session, row, InterventionStatus.DECLINED)
    _release_walk_block(session, row, now(session))
    _sync_decisions(session, intervention_id, False)
    write_audit(
        session,
        "intervention_declined",
        f"{row.type} declined",
        department=row.department,
        correlation_id=row.correlation_id,
    )
    session.commit()
    return _row_to_record(row)


def latest_open_intervention(
    session: Session, user_id: str, correlation_id: str | None = None
) -> InterventionRow | None:
    stmt = select(InterventionRow).where(
        InterventionRow.user_id == user_id,
        InterventionRow.status.in_(list(OPEN_STATUSES)),
    )
    if correlation_id:
        stmt = stmt.where(InterventionRow.correlation_id == correlation_id)
    return session.scalars(stmt.order_by(InterventionRow.created_at.desc())).first()


def complete_intervention(
    session: Session,
    intervention_id: str,
    *,
    reason: str = "completed",
) -> InterventionRecord:
    """Employee marks a non-tracked suggestion done (e.g. hydration)."""
    row = session.get(InterventionRow, intervention_id)
    if row is None:
        raise NotFoundError(intervention_id)
    if row.status == InterventionStatus.COMPLETED.value:
        return _row_to_record(row)
    if row.status not in OPEN_STATUSES:
        raise GuardrailError(f"This suggestion is no longer active ({row.status})")
    was_unaccepted = row.status == InterventionStatus.PROPOSED.value
    if row.status == InterventionStatus.PROPOSED.value:
        _transition(session, row, InterventionStatus.ACCEPTED)
    if row.status == InterventionStatus.ACCEPTED.value:
        _transition(session, row, InterventionStatus.IN_PROGRESS)
    _transition(session, row, InterventionStatus.COMPLETED)
    if row.type in STEP_TRACKED_TYPES:
        _record_movement(session, row, row.completed_at or now(session))

    _sync_decisions(session, intervention_id, True)
    bump_today_metrics(
        session, row.department, accepted=was_unaccepted, slump_prevented=True
    )
    write_audit(
        session,
        "intervention_completed",
        f"{row.type} completed ({reason})",
        department=row.department,
        correlation_id=row.correlation_id,
    )
    session.commit()
    return _row_to_record(row)


def _find_tracked_walk(
    session: Session, user_id: str, intervention_id: str | None
) -> InterventionRow | None:
    if intervention_id:
        row = session.get(InterventionRow, intervention_id)
        if row is None or row.user_id != user_id:
            raise NotFoundError("intervention not found for user")
        if row.type in STEP_TRACKED_TYPES and row.status in TRACKING_STATUSES:
            return row
        return None
    return session.scalars(
        select(InterventionRow)
        .where(
            InterventionRow.user_id == user_id,
            InterventionRow.status.in_(list(TRACKING_STATUSES)),
            InterventionRow.type.in_(list(STEP_TRACKED_TYPES)),
        )
        .order_by(InterventionRow.updated_at.desc())
    ).first()


def apply_activity(
    session: Session,
    *,
    user_id: str,
    steps_delta: int | None = None,
    absolute_steps: int | None = None,
    source: str = "shortcut",
    intervention_id: str | None = None,
) -> dict:
    """
    Ingest a step ping (Apple Shortcut absolute total, or manual delta/total).

    Tracks only walks the employee started (Start walk). Completes when
    absolute steps >= steps_at_start + target.
    """
    if absolute_steps is None and steps_delta is None:
        raise GuardrailError("Provide steps (absolute) or steps_delta")

    at = now(session)
    state = get_activity_state(session, user_id)
    prev = state.steps or 0

    if absolute_steps is not None:
        delta = max(0, absolute_steps - prev)
        state.steps = absolute_steps
    else:
        delta = max(0, int(steps_delta or 0))
        state.steps = prev + delta
    state.updated_at = _wall_now()
    state.source = source

    row = _find_tracked_walk(session, user_id, intervention_id)

    session.add(
        ActivityLogRow(
            id=str(uuid.uuid4()),
            user_id=user_id,
            intervention_id=row.id if row else None,
            steps_delta=delta,
            absolute_steps=state.steps,
            source=source,
            created_at=_wall_now(),
        )
    )

    result: dict = {
        "steps": state.steps,
        "steps_delta": delta,
        "intervention": None,
        "verified": False,
        "message": None,
    }
    if row is None:
        session.commit()
        result["message"] = "Steps saved — no walk in progress"
        return result

    if row.status == InterventionStatus.ACCEPTED.value:
        _transition(session, row, InterventionStatus.IN_PROGRESS)

    verified = False
    if row.baseline_pending:
        row.baseline_steps = state.steps
        row.current_steps = 0
        row.baseline_pending = False
        row.updated_at = at
        goal = row.baseline_steps + (row.target_steps or 0)
        result["message"] = (
            f"Start locked at {state.steps} steps — goal {goal}. Walk, then sync again."
        )
    else:
        baseline = row.baseline_steps or 0
        goal = baseline + (row.target_steps or 0)
        row.current_steps = max(0, state.steps - baseline)
        row.updated_at = at
        if row.target_steps and state.steps >= goal:
            _transition(session, row, InterventionStatus.COMPLETED)
            _record_movement(session, row, at)
            _sync_decisions(session, row.id, True)
            bump_today_metrics(session, row.department, slump_prevented=True)
            write_audit(
                session,
                "intervention_verified",
                f"steps {state.steps}/{goal} via {source}",
                department=row.department,
                correlation_id=row.correlation_id,
            )
            verified = True
            result["message"] = (
                f"✅ Walk complete — {state.steps}/{goal} steps. Intervention COMPLETED."
            )
        else:
            result["message"] = f"Progress {state.steps}/{goal} steps — keep going"

    session.commit()
    result["intervention"] = _row_to_record(row).model_dump(mode="json")
    result["verified"] = verified
    result["goal_steps"] = goal
    result["baseline_steps"] = row.baseline_steps or 0
    return result


def verify_open_interventions(session: Session, at: datetime | None = None) -> list[InterventionRecord]:
    """Expire open interventions whose meeting started or that are from a previous day."""
    clock = at or now(session)
    rows = session.scalars(
        select(InterventionRow).where(InterventionRow.status.in_(list(OPEN_STATUSES)))
    ).all()
    changed: list[InterventionRecord] = []
    for row in rows:
        meeting_started = row.meeting_start is not None and clock >= row.meeting_start
        stale_day = row.created_at.date() < clock.date()
        if not (meeting_started or stale_day):
            continue
        row.status = InterventionStatus.EXPIRED.value
        row.updated_at = clock
        _release_walk_block(session, row, clock)
        write_audit(
            session,
            "intervention_expired",
            f"{row.type} expired — "
            + ("meeting started" if meeting_started else "previous day"),
            department=row.department,
            correlation_id=row.correlation_id,
        )
        changed.append(_row_to_record(row))
    if changed:
        session.commit()
    return changed


def list_interventions(session: Session, limit: int = 40) -> list[InterventionRecord]:
    rows = session.scalars(
        select(InterventionRow).order_by(InterventionRow.updated_at.desc()).limit(limit)
    ).all()
    return [_row_to_record(r) for r in rows]


def mark_snack_in_progress(session: Session, *, user_id: str, meal_id: str, correlation_id: str | None) -> None:
    """When an order is confirmed, advance matching snack intervention."""
    stmt = select(InterventionRow).where(
        InterventionRow.user_id == user_id,
        InterventionRow.type == InterventionType.SNACK.value,
        InterventionRow.status.in_(list(OPEN_STATUSES)),
    )
    if correlation_id:
        stmt = stmt.where(InterventionRow.correlation_id == correlation_id)
    else:
        stmt = stmt.where(InterventionRow.meal_id == meal_id)
    row = session.scalars(stmt.order_by(InterventionRow.created_at.desc())).first()
    if not row:
        return
    if row.status == InterventionStatus.PROPOSED.value:
        _transition(session, row, InterventionStatus.ACCEPTED)
    if row.status == InterventionStatus.ACCEPTED.value:
        _transition(session, row, InterventionStatus.IN_PROGRESS)
    # Demo: snack order counts as completed (metrics bumped by order confirm)
    if row.status != InterventionStatus.COMPLETED.value:
        _transition(session, row, InterventionStatus.COMPLETED)
