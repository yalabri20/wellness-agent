"""Shared enterprise service helpers (orders, HR metrics, confirm)."""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from config import get_settings
from db import (
    AuditLogRow,
    DecisionRow,
    EmployeeRow,
    HistoryRow,
    InterventionRow,
    MealCatalogRow,
    OrderRow,
    TraceStepRow,
    bump_today_metrics,
    get_employee,
    get_policy,
    now,
    refresh_wallet,
    write_audit,
)
from exceptions import GuardrailError, IdempotencyError, NotFoundError
from guardrails import (
    assert_allowance,
    assert_confirmation,
    assert_dietary,
    assert_order_policy,
    k_anonymize_groups,
)
from models import Order, OrderStatus, WalletState

logger = logging.getLogger(__name__)

# In-process order progression timers
_order_timers: dict[str, threading.Thread] = {}


def get_wallet(session: Session, user_id: str) -> WalletState:
    """Return wallet state for a user."""
    emp = get_employee(session, user_id)
    wallet = refresh_wallet(session, user_id)
    if emp is None or wallet is None:
        raise NotFoundError(f"Wallet/user {user_id} not found")
    return WalletState(
        user_id=user_id,
        daily_allowance_sar=emp.daily_allowance_sar,
        daily_remaining_sar=wallet.daily_remaining_sar,
        weekly_remaining_sar=wallet.weekly_remaining_sar,
    )


def get_order(session: Session, order_id: str) -> Order:
    row = session.get(OrderRow, order_id)
    if row is None:
        raise NotFoundError(order_id)
    return Order(
        id=row.id,
        user_id=row.user_id,
        meal_id=row.meal_id,
        status=OrderStatus(row.status),
        price_sar=row.price_sar,
        created_at=row.created_at,
        eta_minutes=row.eta_minutes,
        confirmed=row.confirmed,
    )


def _progress_order(order_id: str, database_url: str) -> None:
    """Background: pending -> preparing -> delivered."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(
        database_url,
        connect_args={"check_same_thread": False} if database_url.startswith("sqlite") else {},
    )
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    time.sleep(2)
    with SessionLocal() as session:
        row = session.get(OrderRow, order_id)
        if row and row.status == OrderStatus.PENDING.value:
            row.status = OrderStatus.PREPARING.value
            row.eta_minutes = 8
            session.commit()
    time.sleep(3)
    with SessionLocal() as session:
        row = session.get(OrderRow, order_id)
        if row and row.status in (
            OrderStatus.PREPARING.value,
            OrderStatus.PENDING.value,
        ):
            row.status = OrderStatus.DELIVERED.value
            row.eta_minutes = 0
            session.commit()


def confirm_and_create_order(
    session: Session,
    *,
    user_id: str,
    meal_id: str,
    confirmation_token: str | None = None,
    correlation_id: str | None = None,
    confirmed: bool = True,
) -> Order:
    """
    Create an order after human confirmation.

    Idempotent on confirmation_token: repeating the same token returns the
    existing order without double-charging.
    """
    assert_confirmation(confirmed)

    if confirmation_token:
        existing = session.scalars(
            select(OrderRow).where(OrderRow.confirmation_token == confirmation_token)
        ).first()
        if existing:
            return get_order(session, existing.id)

    meal = assert_dietary(session, user_id, meal_id)
    assert_order_policy(session, meal.price_sar)
    wallet = assert_allowance(session, user_id, meal.price_sar)

    order_id = str(uuid.uuid4())
    token = confirmation_token or str(uuid.uuid4())
    # Second check for race on generated token
    dup = session.scalars(
        select(OrderRow).where(OrderRow.confirmation_token == token)
    ).first()
    if dup:
        raise IdempotencyError("Duplicate confirmation token")

    wallet.daily_remaining_sar = round(wallet.daily_remaining_sar - meal.price_sar, 2)
    wallet.weekly_remaining_sar = round(wallet.weekly_remaining_sar - meal.price_sar, 2)

    row = OrderRow(
        id=order_id,
        user_id=user_id,
        meal_id=meal_id,
        status=OrderStatus.PENDING.value,
        price_sar=meal.price_sar,
        created_at=now(session),
        eta_minutes=meal.prep_minutes,
        confirmed=True,
        confirmation_token=token,
    )
    session.add(row)

    emp = get_employee(session, user_id)
    dept = emp.department if emp else "Unknown"

    # Mark latest matching decision accepted
    stmt = (
        select(DecisionRow)
        .where(DecisionRow.user_id == user_id, DecisionRow.meal_id == meal_id)
        .order_by(DecisionRow.created_at.desc())
        .limit(1)
    )
    decision = session.scalars(stmt).first()
    if decision:
        decision.accepted = True

    from interventions import mark_snack_in_progress

    mark_snack_in_progress(
        session,
        user_id=user_id,
        meal_id=meal_id,
        correlation_id=correlation_id,
    )

    bump_today_metrics(
        session,
        dept,
        accepted=True,
        slump_prevented=True,
        allowance_delta=meal.price_sar,
    )
    write_audit(
        session,
        "order_confirmed",
        f"Ordered {meal_id} for {meal.price_sar} SAR (token={token[:8]}…)",
        department=dept,
        correlation_id=correlation_id,
    )
    session.commit()

    settings = get_settings()
    thread = threading.Thread(
        target=_progress_order,
        args=(order_id, settings.database_url),
        daemon=True,
    )
    _order_timers[order_id] = thread
    thread.start()

    return get_order(session, order_id)


def mark_decision_skipped(
    session: Session,
    user_id: str,
    correlation_id: str | None = None,
) -> None:
    """Decline the open suggestion (lifecycle) and mark its decision skipped."""
    from interventions import decline_intervention, latest_open_intervention

    open_row = latest_open_intervention(session, user_id, correlation_id)
    if open_row is None and correlation_id:
        open_row = latest_open_intervention(session, user_id)
    if open_row is not None:
        decline_intervention(session, open_row.id)
        return

    stmt = (
        select(DecisionRow)
        .where(DecisionRow.user_id == user_id, DecisionRow.accepted.is_(None))
        .order_by(DecisionRow.created_at.desc())
        .limit(1)
    )
    decision = session.scalars(stmt).first()
    if decision:
        decision.accepted = False
        write_audit(
            session,
            "intervention_skipped",
            f"Skipped {decision.intervention_type}",
            department=decision.department,
            correlation_id=correlation_id or decision.correlation_id,
        )
        session.commit()


def hr_metrics(session: Session) -> dict[str, Any]:
    """Aggregated HR metrics with k-anonymity."""
    settings = get_settings()
    rows = session.scalars(select(HistoryRow)).all()
    by_dept: dict[str, list[HistoryRow]] = {}
    for r in rows:
        by_dept.setdefault(r.department, []).append(r)

    # Department headcounts from employees
    counts = dict(
        session.execute(
            select(EmployeeRow.department, func.count())
            .where(EmployeeRow.opted_in.is_(True))
            .group_by(EmployeeRow.department)
        ).all()
    )

    series: list[dict[str, Any]] = []
    for dept, items in by_dept.items():
        emp_count = int(counts.get(dept, 0))
        for item in items:
            series.append(
                {
                    "date": item.date.isoformat(),
                    "department": dept,
                    "energy_index": item.energy_index,
                    "slumps_prevented": item.slumps_prevented,
                    "interventions_offered": item.interventions_offered,
                    "interventions_accepted": item.interventions_accepted,
                    "allowance_used_sar": item.allowance_used_sar,
                    "employee_count": max(item.employee_count, emp_count),
                }
            )

    series = k_anonymize_groups(series, k=settings.k_anonymity)

    totals = {
        "slumps_prevented": sum(x["slumps_prevented"] for x in series),
        "interventions_offered": sum(x["interventions_offered"] for x in series),
        "interventions_accepted": sum(x["interventions_accepted"] for x in series),
        "allowance_used_sar": round(sum(x["allowance_used_sar"] for x in series), 1),
        "avg_energy_index": round(
            (sum(x["energy_index"] for x in series) / len(series)) if series else 0, 1
        ),
    }

    opted = session.scalars(select(EmployeeRow)).all()
    opt_in_rate = (
        sum(1 for e in opted if e.opted_in) / len(opted) if opted else 0.0
    )

    return {
        "series": series,
        "totals": totals,
        "opt_in_rate": round(opt_in_rate, 3),
        "k_anonymity": settings.k_anonymity,
        "simulated": True,
        "badge": "Simulated data",
        "before_after": {
            "before_energy_index": 61.0,
            "after_energy_index": totals["avg_energy_index"] or 72.0,
            "badge": "Simulated data",
        },
    }


def hr_trace(session: Session, limit: int = 40) -> list[dict[str, Any]]:
    """Anonymized recent agent trace steps (no employee ids)."""
    rows = session.scalars(
        select(TraceStepRow).order_by(TraceStepRow.timestamp.desc()).limit(limit)
    ).all()
    return [
        {
            "department": r.department,
            "agent_name": r.agent_name,
            "input_summary": r.input_summary,
            "output_summary": r.output_summary,
            "tools_called": json.loads(r.tools_called_json or "[]"),
            "timestamp": r.timestamp.isoformat(),
            "duration_ms": r.duration_ms,
            "correlation_id": r.correlation_id,
        }
        for r in rows
    ]


def hr_live_feed(session: Session, limit: int = 30) -> list[dict[str, Any]]:
    """Anonymized intervention lifecycle feed for the Live Feed tab."""
    from interventions import list_interventions

    records = list_interventions(session, limit=limit)
    if records:
        return [
            {
                "department": r.department,
                "intervention_type": r.type.value,
                "status": r.status.value,
                "accepted": r.status.value
                in ("accepted", "in_progress", "completed"),
                "time": r.updated_at.isoformat(),
                "proactive": r.proactive,
                "risk_score": None,
                "steps": (
                    "waiting for first phone sync"
                    if r.target_steps and r.baseline_pending
                    else f"+{r.current_steps}/{r.target_steps}"
                    if r.target_steps
                    else None
                ),
            }
            for r in records
        ]
    rows = session.scalars(
        select(DecisionRow).order_by(DecisionRow.created_at.desc()).limit(limit)
    ).all()
    return [
        {
            "department": r.department,
            "intervention_type": r.intervention_type,
            "status": (
                "completed"
                if r.accepted is True
                else ("declined" if r.accepted is False else "proposed")
            ),
            "accepted": r.accepted,
            "time": r.created_at.isoformat(),
            "proactive": r.proactive,
            "risk_score": r.risk_score,
            "steps": None,
        }
        for r in rows
    ]


def list_meal_logs(session: Session, user_id: str, *, day: datetime | None = None) -> list[dict[str, Any]]:
    """Employee-only view of meals logged on a day (never exposed to HR)."""
    from datetime import timedelta

    from db import MealLogRow

    at = day or now(session)
    start = at.replace(hour=0, minute=0, second=0, microsecond=0)
    rows = session.scalars(
        select(MealLogRow)
        .where(
            MealLogRow.user_id == user_id,
            MealLogRow.logged_at >= start,
            MealLogRow.logged_at < start + timedelta(days=1),
        )
        .order_by(MealLogRow.logged_at)
    ).all()
    out = []
    for r in rows:
        m = json.loads(r.macros_json or "{}")
        out.append(
            {
                "time": r.logged_at.strftime("%H:%M"),
                "dishes": ", ".join(m.get("dish_names") or []) or "meal",
                "kcal": m.get("kcal"),
                "protein_g": m.get("protein_g"),
                "confidence": m.get("confidence"),
                "source": r.source,
            }
        )
    return out


def list_calendar_events(session: Session, user_id: str) -> list[dict[str, Any]]:
    """Employee-facing calendar list (labels ok here; never sent to LLM)."""
    from db import CalendarEventRow

    rows = session.scalars(
        select(CalendarEventRow)
        .where(CalendarEventRow.user_id == user_id)
        .order_by(CalendarEventRow.start)
    ).all()
    return [
        {
            "id": r.id,
            "start": r.start.isoformat(),
            "end": r.end.isoformat(),
            "importance": r.importance,
            "attendee_count": r.attendee_count,
            "label": r.label,
            "is_wellness_block": bool(r.is_wellness_block),
        }
        for r in rows
    ]


def add_calendar_event(
    session: Session,
    user_id: str,
    *,
    start: datetime,
    end: datetime,
    importance: str,
    attendee_count: int,
    label: str | None,
) -> dict[str, Any]:
    import uuid

    from db import CalendarEventRow

    row = CalendarEventRow(
        id=str(uuid.uuid4()),
        user_id=user_id,
        start=start,
        end=end,
        importance=importance,
        attendee_count=attendee_count,
        label=label,
    )
    session.add(row)
    write_audit(
        session,
        "calendar_event_added",
        f"{importance} {start.isoformat()}–{end.isoformat()}",
        department=None,
    )
    session.commit()
    return {
        "id": row.id,
        "start": row.start.isoformat(),
        "end": row.end.isoformat(),
        "importance": row.importance,
        "attendee_count": row.attendee_count,
        "label": row.label,
    }


def delete_calendar_event(session: Session, user_id: str, event_id: str) -> None:
    from db import CalendarEventRow

    row = session.get(CalendarEventRow, event_id)
    if row is None or row.user_id != user_id:
        raise NotFoundError(event_id)
    session.delete(row)
    session.commit()


def hr_audit(session: Session, limit: int = 50) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(AuditLogRow).order_by(AuditLogRow.timestamp.desc()).limit(limit)
    ).all()
    return [
        {
            "id": r.id,
            "action": r.action,
            "detail": r.detail,
            "timestamp": r.timestamp.isoformat(),
            "department": r.department,
            "correlation_id": r.correlation_id,
        }
        for r in rows
    ]


POLICY_FIELDS = ("allow_food_orders", "max_order_sar", "max_proactive_per_day", "auto_book_walk_break")


def policy_dict(session: Session) -> dict[str, Any]:
    p = get_policy(session)
    return {
        "allow_food_orders": bool(p.allow_food_orders),
        "max_order_sar": float(p.max_order_sar),
        "max_proactive_per_day": int(p.max_proactive_per_day),
        "auto_book_walk_break": bool(p.auto_book_walk_break),
        # Locked: spending always needs the employee's explicit confirmation
        "require_confirmation": True,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


def update_policy(session: Session, **changes: Any) -> dict[str, Any]:
    """Apply HR policy changes; every change is audited."""
    if "require_confirmation" in changes and not changes["require_confirmation"]:
        raise GuardrailError("Human confirmation for spending cannot be disabled")
    p = get_policy(session)
    diffs: list[str] = []
    for key in POLICY_FIELDS:
        if key not in changes or changes[key] is None:
            continue
        value = changes[key]
        if key == "max_order_sar":
            value = float(value)
            if value < 0:
                raise GuardrailError("max_order_sar must be >= 0")
        elif key == "max_proactive_per_day":
            value = int(value)
            if not 0 <= value <= 10:
                raise GuardrailError("max_proactive_per_day must be 0–10")
        else:
            value = bool(value)
        if getattr(p, key) != value:
            diffs.append(f"{key}: {getattr(p, key)} → {value}")
            setattr(p, key, value)
    if diffs:
        p.updated_at = datetime.now().replace(microsecond=0)
        write_audit(session, "policy_updated", "; ".join(diffs))
    session.commit()
    return policy_dict(session)


def _opted_in_headcount(session: Session) -> dict[str, int]:
    return {
        dept: int(n)
        for dept, n in session.execute(
            select(EmployeeRow.department, func.count())
            .where(EmployeeRow.opted_in.is_(True))
            .group_by(EmployeeRow.department)
        ).all()
    }


def _rate(num: int, den: int) -> float:
    return round(num / den, 3) if den else 0.0


_ACCEPTED_STATES = ("accepted", "in_progress", "completed")


def _hour_label(hour: int) -> str:
    if hour < 11:
        return "Morning"
    if hour == 11:
        return "Pre-lunch"
    if hour == 12:
        return "Lunch"
    if hour <= 14:
        return "Post-lunch"
    return "Late afternoon"


def _operational_stats(rows: list[InterventionRow]) -> dict[str, Any]:
    """
    Peak slump hour (hour with the most risk-triggered interventions) and slumps
    mitigated before meetings (meeting-linked interventions completed before the
    meeting started).
    """
    hour_counts: dict[int, int] = {}
    meeting_linked = 0
    mitigated = 0
    mitigated_by_dept: dict[str, int] = {}
    for r in rows:
        hour_counts[r.created_at.hour] = hour_counts.get(r.created_at.hour, 0) + 1
        if r.meeting_start is None:
            continue
        meeting_linked += 1
        if r.status == "completed" and r.completed_at and r.completed_at <= r.meeting_start:
            mitigated += 1
            mitigated_by_dept[r.department] = mitigated_by_dept.get(r.department, 0) + 1
    peak = max(hour_counts, key=lambda h: (hour_counts[h], -h)) if hour_counts else None
    return {
        "peak_hour": peak,
        "peak_hour_display": f"{peak:02d}:00" if peak is not None else "—",
        "peak_hour_label": _hour_label(peak) if peak is not None else None,
        "peak_hour_share": _rate(hour_counts[peak], len(rows)) if peak is not None else 0.0,
        "hourly": [{"hour": f"{h:02d}:00", "suggestions": hour_counts[h]} for h in sorted(hour_counts)],
        "meeting_linked": meeting_linked,
        "mitigated_before_meetings": mitigated,
        "mitigation_rate": _rate(mitigated, meeting_linked),
        "mitigated_by_dept": mitigated_by_dept,
    }


def _peak_hour_insight(ops: dict[str, Any]) -> str:
    h = ops["peak_hour"]
    return (
        f"Peak slump hour is {h:02d}:00–{h + 1:02d}:00 ({ops['peak_hour_label'].lower()}), "
        f"accounting for {ops['peak_hour_share']:.0%} of risk flags — schedule critical "
        "meetings outside this window where possible."
    )


def _mitigation_insight(ops: dict[str, Any]) -> str:
    return (
        f"{ops['mitigated_before_meetings']:,} slumps were mitigated before an imminent meeting "
        f"— {ops['mitigation_rate']:.0%} of {ops['meeting_linked']:,} meeting-linked nudges "
        "were completed before the meeting started."
    )


def hr_insights(session: Session, period: str = "daily") -> dict[str, Any]:
    """Daily (live, today) or monthly (last 30 days) HR insights, k-anonymous."""
    if period == "monthly":
        return _monthly_insights(session)
    return _daily_insights(session)


def _daily_insights(session: Session) -> dict[str, Any]:
    from datetime import timedelta

    settings = get_settings()
    k = settings.k_anonymity
    at = now(session)
    day_start = at.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    headcount = _opted_in_headcount(session)
    visible = {d for d, n in headcount.items() if n >= k}

    rows = [
        r
        for r in session.scalars(
            select(InterventionRow).where(
                InterventionRow.created_at >= day_start,
                InterventionRow.created_at < day_end,
            )
        ).all()
        if r.department in visible
    ]
    offered = len(rows)
    status_counts: dict[str, int] = {}
    type_counts: dict[str, int] = {}
    for r in rows:
        status_counts[r.status] = status_counts.get(r.status, 0) + 1
        type_counts[r.type] = type_counts.get(r.type, 0) + 1
    ops = _operational_stats(rows)
    accepted = sum(status_counts.get(s, 0) for s in _ACCEPTED_STATES)
    completed = status_counts.get("completed", 0)
    expired = status_counts.get("expired", 0)
    declined = status_counts.get("declined", 0)
    verified_steps = sum(
        r.current_steps or 0
        for r in rows
        if r.status == "completed" and r.type in ("walk", "movement_break")
    )
    walks_verified = sum(
        1 for r in rows if r.status == "completed" and r.type in ("walk", "movement_break")
    )

    risk_scores = [
        d.risk_score
        for d in session.scalars(
            select(DecisionRow).where(
                DecisionRow.created_at >= day_start, DecisionRow.created_at < day_end
            )
        ).all()
        if d.risk_score is not None and d.department in visible
    ]
    avg_risk = round(sum(risk_scores) / len(risk_scores), 1) if risk_scores else None

    top_type = max(type_counts, key=type_counts.get) if type_counts else None

    by_dept: dict[str, dict[str, int]] = {}
    for r in rows:
        d = by_dept.setdefault(r.department, {"offered": 0, "accepted": 0, "completed": 0})
        d["offered"] += 1
        if r.status in _ACCEPTED_STATES:
            d["accepted"] += 1
        if r.status == "completed":
            d["completed"] += 1
    departments = [
        {"department": dept, **vals, "acceptance_rate": _rate(vals["accepted"], vals["offered"])}
        for dept, vals in sorted(by_dept.items())
    ]

    insights: list[str] = []
    if not offered:
        insights.append("No suggestions yet today — the agent nudges only around meals and key meetings.")
    else:
        insights.append(
            f"{offered} suggestion(s) today; {accepted} accepted "
            f"({_rate(accepted, offered):.0%}) and {completed} completed."
        )
        if ops["peak_hour"] is not None:
            insights.append(_peak_hour_insight(ops))
        if ops["meeting_linked"]:
            insights.append(_mitigation_insight(ops))
        if top_type:
            insights.append(f"Most common intervention: {top_type.replace('_', ' ')}.")
        if walks_verified:
            insights.append(
                f"{walks_verified} walk(s) verified by phone/manual steps (+{verified_steps} steps)."
            )
        if expired:
            insights.append(
                f"{expired} suggestion(s) expired unanswered — nudges may be arriving too close to meetings."
            )
        if declined > accepted:
            insights.append("More declines than accepts — review timing or the proactive cap in Governance.")

    return {
        "period": "daily",
        "date": at.date().isoformat(),
        "kpis": {
            "offered": offered,
            "accepted": accepted,
            "completed": completed,
            "declined": declined,
            "expired": expired,
            "acceptance_rate": _rate(accepted, offered),
            "completion_rate": _rate(completed, accepted),
            "verified_steps": verified_steps,
            "avg_risk_score": avg_risk,
            "peak_slump_hour": ops["peak_hour_display"],
            "peak_slump_label": ops["peak_hour_label"],
            "mitigated_before_meetings": ops["mitigated_before_meetings"],
            "meeting_linked": ops["meeting_linked"],
            "mitigation_rate": ops["mitigation_rate"],
        },
        "status_counts": status_counts,
        "type_counts": type_counts,
        "hourly": ops["hourly"],
        "departments": departments,
        "insights": insights,
        "k_anonymity": k,
        "badge": "Live demo data",
    }


def _monthly_insights(session: Session) -> dict[str, Any]:
    from datetime import timedelta

    settings = get_settings()
    k = settings.k_anonymity
    today = now(session).date()
    start = today - timedelta(days=29)
    headcount = _opted_in_headcount(session)
    rows = [
        r
        for r in session.scalars(select(HistoryRow).where(HistoryRow.date >= start)).all()
        if max(r.employee_count, headcount.get(r.department, 0)) >= k
    ]
    if not rows:
        return {"period": "monthly", "insights": ["Not enough k-anonymous data for this period."],
                "daily": [], "departments": [], "weekday": [], "kpis": {}, "badge": "Simulated data",
                "k_anonymity": k}

    by_date: dict[str, list[HistoryRow]] = {}
    by_dept: dict[str, list[HistoryRow]] = {}
    by_weekday: dict[int, list[float]] = {}
    for r in rows:
        by_date.setdefault(r.date.isoformat(), []).append(r)
        by_dept.setdefault(r.department, []).append(r)
        by_weekday.setdefault(r.date.weekday(), []).append(r.energy_index)

    daily = [
        {
            "date": d,
            "energy_index": round(sum(x.energy_index for x in items) / len(items), 1),
            "offered": sum(x.interventions_offered for x in items),
            "accepted": sum(x.interventions_accepted for x in items),
            "slumps_prevented": sum(x.slumps_prevented for x in items),
        }
        for d, items in sorted(by_date.items())
    ]
    first_week = daily[:7]
    last_week = daily[-7:]
    e_first = sum(x["energy_index"] for x in first_week) / len(first_week)
    e_last = sum(x["energy_index"] for x in last_week) / len(last_week)

    visible = set(by_dept)
    window_start = datetime.combine(start, datetime.min.time())
    window_end = datetime.combine(today + timedelta(days=1), datetime.min.time())
    interventions = [
        r
        for r in session.scalars(
            select(InterventionRow).where(
                InterventionRow.created_at >= window_start,
                InterventionRow.created_at < window_end,
            )
        ).all()
        if r.department in visible
    ]
    ops = _operational_stats(interventions)

    departments = []
    for dept, items in sorted(by_dept.items()):
        off = sum(x.interventions_offered for x in items)
        acc = sum(x.interventions_accepted for x in items)
        departments.append(
            {
                "department": dept,
                "avg_energy": round(sum(x.energy_index for x in items) / len(items), 1),
                "offered": off,
                "accepted": acc,
                "acceptance_rate": _rate(acc, off),
                "slumps_prevented": sum(x.slumps_prevented for x in items),
                "mitigated_before_meetings": ops["mitigated_by_dept"].get(dept, 0),
            }
        )
    best = max(departments, key=lambda d: d["avg_energy"])
    worst = min(departments, key=lambda d: d["avg_energy"])

    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    weekday = [
        {"weekday": names[w], "avg_energy": round(sum(v) / len(v), 1)}
        for w, v in sorted(by_weekday.items())
    ]
    low_day = min(weekday, key=lambda w: w["avg_energy"])

    offered = sum(x["offered"] for x in daily)
    accepted = sum(x["accepted"] for x in daily)
    prevented = sum(x["slumps_prevented"] for x in daily)

    trend = e_last - e_first
    insights = [
        f"Team energy {'rose' if trend >= 0 else 'fell'} {abs(trend):.1f} pts "
        f"(first week {e_first:.1f} → last week {e_last:.1f}).",
        f"Lowest-energy weekday: {low_day['weekday']} ({low_day['avg_energy']}) "
        "— a good day for lighter meeting loads or proactive snacks.",
        f"Strongest department: {best['department']} ({best['avg_energy']}); "
        f"needs attention: {worst['department']} ({worst['avg_energy']}).",
        f"Acceptance rate {_rate(accepted, offered):.0%} across {offered} suggestions; "
        f"{prevented} slumps prevented.",
    ]
    if ops["peak_hour"] is not None:
        insights.append(_peak_hour_insight(ops))
    if ops["meeting_linked"]:
        insights.append(_mitigation_insight(ops))
    return {
        "period": "monthly",
        "from": start.isoformat(),
        "to": today.isoformat(),
        "kpis": {
            "offered": offered,
            "accepted": accepted,
            "acceptance_rate": _rate(accepted, offered),
            "slumps_prevented": prevented,
            "peak_slump_hour": ops["peak_hour_display"],
            "peak_slump_label": ops["peak_hour_label"],
            "peak_slump_share": ops["peak_hour_share"],
            "mitigated_before_meetings": ops["mitigated_before_meetings"],
            "meeting_linked": ops["meeting_linked"],
            "mitigation_rate": ops["mitigation_rate"],
            "energy_first_week": round(e_first, 1),
            "energy_last_week": round(e_last, 1),
        },
        "daily": daily,
        "departments": departments,
        "weekday": weekday,
        "insights": insights,
        "k_anonymity": k,
        "badge": "Simulated data",
    }


def list_catalog(session: Session) -> list[dict[str, Any]]:
    rows = session.scalars(select(MealCatalogRow)).all()
    return [
        {
            "id": m.id,
            "name_en": m.name_en,
            "name_ar": m.name_ar,
            "restaurant": m.restaurant,
            "price_sar": m.price_sar,
            "est_macros": json.loads(m.est_macros_json),
            "tags": json.loads(m.tags_json),
            "prep_minutes": m.prep_minutes,
            "url": m.url,
        }
        for m in rows
    ]
