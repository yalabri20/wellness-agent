"""SQLAlchemy models, session factory, and seed loader."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any, Generator

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    create_engine,
    inspect,
    select,
    text,
)
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from config import SEED_DIR, get_settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """SQLAlchemy declarative base."""


class EmployeeRow(Base):
    __tablename__ = "employees"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    department: Mapped[str] = mapped_column(String(64))
    daily_allowance_sar: Mapped[float] = mapped_column(Float)
    dietary_tags_json: Mapped[str] = mapped_column(Text, default="[]")
    opted_in: Mapped[bool] = mapped_column(Boolean, default=True)
    language: Mapped[str] = mapped_column(String(8), default="en")
    live_demo_user: Mapped[bool] = mapped_column(Boolean, default=False)


class WalletRow(Base):
    __tablename__ = "wallets"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    daily_remaining_sar: Mapped[float] = mapped_column(Float)
    weekly_remaining_sar: Mapped[float] = mapped_column(Float)
    # Day the daily balance applies to; a new day (or ISO week) refills the balance
    balance_day: Mapped[date | None] = mapped_column(Date, nullable=True)


class CalendarEventRow(Base):
    __tablename__ = "calendar_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    start: Mapped[datetime] = mapped_column(DateTime)
    end: Mapped[datetime] = mapped_column(DateTime)
    importance: Mapped[str] = mapped_column(String(16))
    attendee_count: Mapped[int] = mapped_column(Integer, default=0)
    label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Agent-booked walk blocks: shown to the employee, ignored when the agent reads meetings
    is_wellness_block: Mapped[bool] = mapped_column(Boolean, default=False)
    intervention_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class MealCatalogRow(Base):
    __tablename__ = "meals_catalog"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name_ar: Mapped[str] = mapped_column(String(128))
    name_en: Mapped[str] = mapped_column(String(128))
    restaurant: Mapped[str] = mapped_column(String(128))
    price_sar: Mapped[float] = mapped_column(Float)
    est_macros_json: Mapped[str] = mapped_column(Text)
    tags_json: Mapped[str] = mapped_column(Text)
    prep_minutes: Mapped[int] = mapped_column(Integer)
    url: Mapped[str] = mapped_column(String(512))


class HistoryRow(Base):
    __tablename__ = "history_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    department: Mapped[str] = mapped_column(String(64), index=True)
    energy_index: Mapped[float] = mapped_column(Float)
    slumps_prevented: Mapped[int] = mapped_column(Integer)
    interventions_offered: Mapped[int] = mapped_column(Integer)
    interventions_accepted: Mapped[int] = mapped_column(Integer)
    allowance_used_sar: Mapped[float] = mapped_column(Float)
    employee_count: Mapped[int] = mapped_column(Integer)


class MealLogRow(Base):
    __tablename__ = "meal_logs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    logged_at: Mapped[datetime] = mapped_column(DateTime)
    macros_json: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(32), default="text")


class MovementBreakRow(Base):
    __tablename__ = "movement_breaks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    start: Mapped[datetime] = mapped_column(DateTime)
    duration_min: Mapped[int] = mapped_column(Integer)


class OrderRow(Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    meal_id: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    price_sar: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    eta_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    confirmation_token: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)


class TraceStepRow(Base):
    __tablename__ = "trace_steps"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    correlation_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    department: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agent_name: Mapped[str] = mapped_column(String(64))
    input_summary: Mapped[str] = mapped_column(Text)
    output_summary: Mapped[str] = mapped_column(Text)
    tools_called_json: Mapped[str] = mapped_column(Text, default="[]")
    timestamp: Mapped[datetime] = mapped_column(DateTime)
    duration_ms: Mapped[int] = mapped_column(Integer)


class DecisionRow(Base):
    __tablename__ = "decisions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    correlation_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    department: Mapped[str] = mapped_column(String(64))
    intervention_type: Mapped[str] = mapped_column(String(32))
    accepted: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    risk_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    rationale: Mapped[str] = mapped_column(Text, default="")
    meal_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    proactive: Mapped[bool] = mapped_column(Boolean, default=False)
    intervention_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)


class InterventionRow(Base):
    __tablename__ = "interventions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    department: Mapped[str] = mapped_column(String(64))
    type: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), index=True)
    rationale: Mapped[str] = mapped_column(Text, default="")
    correlation_id: Mapped[str] = mapped_column(String(64), index=True)
    meal_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    duration_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_steps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    baseline_steps: Mapped[int] = mapped_column(Integer, default=0)
    current_steps: Mapped[int] = mapped_column(Integer, default=0)
    meeting_start: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    proactive: Mapped[bool] = mapped_column(Boolean, default=False)
    # True when the last phone sync was stale at Start walk; first sync after start sets baseline
    baseline_pending: Mapped[bool] = mapped_column(Boolean, default=False)
    calendar_event_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class PolicyRow(Base):
    """Company wellness policy (single row, id=1) editable by HR."""

    __tablename__ = "policy"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    allow_food_orders: Mapped[bool] = mapped_column(Boolean, default=True)
    max_order_sar: Mapped[float] = mapped_column(Float, default=35.0)
    max_proactive_per_day: Mapped[int] = mapped_column(Integer, default=3)
    auto_book_walk_break: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class OutboxMessageRow(Base):
    """Cross-process channel: scheduler writes, simulator pulls."""

    __tablename__ = "outbox_messages"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    payload_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    delivered: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class ActivityStateRow(Base):
    __tablename__ = "activity_state"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    steps: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime)
    source: Mapped[str] = mapped_column(String(32), default="manual")


class ActivityLogRow(Base):
    __tablename__ = "activity_logs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    intervention_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    steps_delta: Mapped[int] = mapped_column(Integer, default=0)
    absolute_steps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="manual")
    created_at: Mapped[datetime] = mapped_column(DateTime)


class AuditLogRow(Base):
    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    action: Mapped[str] = mapped_column(String(64))
    detail: Mapped[str] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(DateTime)
    department: Mapped[str | None] = mapped_column(String(64), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ProactiveCounterRow(Base):
    __tablename__ = "proactive_counters"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    count: Mapped[int] = mapped_column(Integer, default=0)


class DemoClockRow(Base):
    __tablename__ = "demo_clock"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    clock_iso: Mapped[str | None] = mapped_column(String(64), nullable=True)


_engine = None
_SessionLocal: sessionmaker[Session] | None = None


def get_engine():
    """Create or return the shared SQLAlchemy engine."""
    global _engine, _SessionLocal
    if _engine is None:
        settings = get_settings()
        connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
        _engine = create_engine(
            settings.database_url,
            connect_args=connect_args,
            future=True,
        )
        _SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False, future=True)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    get_engine()
    assert _SessionLocal is not None
    return _SessionLocal


def get_db() -> Generator[Session, None, None]:
    """FastAPI-compatible session dependency."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


_COLUMN_MIGRATIONS: list[tuple[str, str, str]] = [
    ("calendar_events", "is_wellness_block", "BOOLEAN NOT NULL DEFAULT 0"),
    ("calendar_events", "intervention_id", "VARCHAR(64)"),
    ("interventions", "baseline_pending", "BOOLEAN NOT NULL DEFAULT 0"),
    ("interventions", "calendar_event_id", "VARCHAR(64)"),
    ("wallets", "balance_day", "DATE"),
]


def _migrate_columns(engine) -> None:
    """Add columns introduced after a DB was created (SQLite has no auto-migrate)."""
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    for table, column, ddl in _COLUMN_MIGRATIONS:
        if table not in tables:
            continue
        if column in {c["name"] for c in insp.get_columns(table)}:
            continue
        try:
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
            logger.info("Migrated: added %s.%s", table, column)
        except OperationalError:
            # Another process added it concurrently
            pass


def init_db() -> None:
    """Create all tables and add any missing columns."""
    engine = get_engine()
    Base.metadata.create_all(bind=engine)
    _migrate_columns(engine)


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


# Simulated hour-of-day profile for historical slump flags (work hours 09–15)
_HISTORY_HOUR_WEIGHTS = {9: 3, 10: 6, 11: 9, 12: 12, 13: 22, 14: 30, 15: 14}
_HISTORY_TYPE_WEIGHTS = {"walk": 45, "snack": 25, "hydration": 20, "movement_break": 10}


def _seed_history_interventions(session: Session, history: list[dict]) -> None:
    """
    Expand each daily history row into individual past interventions so hour-of-day
    and before-meeting metrics can be computed from the same table as live data.
    Counts match the row: completed = slumps_prevented, accepted-but-unfinished =
    expired, the rest declined. Deterministic (fixed seed) for repeatable demos.
    """
    import random
    from datetime import timedelta

    rng = random.Random(42)
    hours, hour_w = zip(*_HISTORY_HOUR_WEIGHTS.items(), strict=True)
    types, type_w = zip(*_HISTORY_TYPE_WEIGHTS.items(), strict=True)
    today = date.today()
    rows: list[InterventionRow] = []
    for h in history:
        day = date.fromisoformat(h["date"])
        if day >= today:
            continue
        offered = int(h["interventions_offered"])
        completed = min(int(h["slumps_prevented"]), offered)
        accepted = min(max(int(h["interventions_accepted"]), completed), offered)
        for i in range(offered):
            created = datetime.combine(day, datetime.min.time()).replace(
                hour=rng.choices(hours, hour_w)[0], minute=rng.randrange(60)
            )
            meeting = (
                created + timedelta(minutes=rng.randint(25, 110)) if rng.random() < 0.75 else None
            )
            if i < completed:
                status = "completed"
                done_at = created + timedelta(minutes=rng.randint(8, 22))
            elif i < accepted:
                status, done_at = "expired", None
            else:
                status, done_at = "declined", None
            rows.append(
                InterventionRow(
                    id=str(uuid.uuid4()),
                    user_id=f"hist-{h['department'].lower()}",
                    department=h["department"],
                    type=rng.choices(types, type_w)[0],
                    status=status,
                    rationale="historical (simulated)",
                    correlation_id=str(uuid.uuid4()),
                    meal_id=None,
                    duration_min=None,
                    target_steps=None,
                    baseline_steps=0,
                    current_steps=0,
                    meeting_start=meeting,
                    created_at=created,
                    updated_at=done_at or created,
                    completed_at=done_at,
                    proactive=rng.random() < 0.5,
                    baseline_pending=False,
                )
            )
    session.add_all(rows)


def reset_and_seed(seed_dir: Path | None = None) -> None:
    """Recreate schema and reload seed JSON into SQLite."""
    seed_dir = seed_dir or SEED_DIR
    engine = get_engine()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    session = get_session_factory()()
    try:

        employees = _load_json(seed_dir / "employees.json")
        calendars = _load_json(seed_dir / "calendars.json")
        meals = _load_json(seed_dir / "meals_catalog.json")
        history = _load_json(seed_dir / "history.json")
        settings = get_settings()

        for emp in employees:
            session.add(
                EmployeeRow(
                    id=emp["id"],
                    department=emp["department"],
                    daily_allowance_sar=emp["daily_allowance_sar"],
                    dietary_tags_json=json.dumps(emp.get("dietary_tags", [])),
                    opted_in=emp.get("opted_in", True),
                    language=emp.get("language", "en"),
                    live_demo_user=emp.get("live_demo_user", False),
                )
            )
            session.add(
                WalletRow(
                    user_id=emp["id"],
                    daily_remaining_sar=emp["daily_allowance_sar"],
                    weekly_remaining_sar=emp["daily_allowance_sar"]
                    * settings.weekly_allowance_multiplier,
                    balance_day=date.today(),
                )
            )
            session.add(
                ActivityStateRow(
                    user_id=emp["id"],
                    steps=0,
                    updated_at=datetime.now().replace(microsecond=0),
                    source="seed",
                )
            )

        today = date.today()
        for user_id, events in calendars.items():
            for ev in events:
                start = datetime.fromisoformat(ev["start"])
                end = datetime.fromisoformat(ev["end"])
                # Always map seed times onto "today" so DEMO_CLOCK / work-hours work
                start = datetime.combine(today, start.time())
                end = datetime.combine(today, end.time())
                session.add(
                    CalendarEventRow(
                        id=str(uuid.uuid4()),
                        user_id=user_id,
                        start=start,
                        end=end,
                        importance=ev["importance"],
                        attendee_count=ev.get("attendee_count", 0),
                        label=ev.get("label"),
                    )
                )

        for meal in meals:
            session.add(
                MealCatalogRow(
                    id=meal["id"],
                    name_ar=meal["name_ar"],
                    name_en=meal["name_en"],
                    restaurant=meal["restaurant"],
                    price_sar=meal["price_sar"],
                    est_macros_json=json.dumps(meal["est_macros"]),
                    tags_json=json.dumps(meal["tags"]),
                    prep_minutes=meal["prep_minutes"],
                    url=meal["url"],
                )
            )

        _seed_history_interventions(session, history)

        for row in history:
            session.add(
                HistoryRow(
                    date=date.fromisoformat(row["date"]),
                    department=row["department"],
                    energy_index=row["energy_index"],
                    slumps_prevented=row["slumps_prevented"],
                    interventions_offered=row["interventions_offered"],
                    interventions_accepted=row["interventions_accepted"],
                    allowance_used_sar=row["allowance_used_sar"],
                    employee_count=row["employee_count"],
                )
            )

        # Demo user: breakfast at 07:30 so proactive gap is realistic
        breakfast_macros = {
            "kcal": 420,
            "protein_g": 18,
            "refined_carbs_g": 45,
            "fiber_g": 3,
            "fat_g": 12,
            "sugar_g": 10,
            "confidence": 0.9,
            "dish_names": ["eggs", "toast", "Arabic coffee"],
            "language": "en",
        }
        session.add(
            MealLogRow(
                id=str(uuid.uuid4()),
                user_id="demo_user",
                logged_at=datetime.combine(today, datetime.min.time()).replace(hour=7, minute=30),
                macros_json=json.dumps(breakfast_macros),
                source="seed",
            )
        )

        session.add(DemoClockRow(id=1, clock_iso=None))
        session.add(_default_policy())
        session.add(
            AuditLogRow(
                id=str(uuid.uuid4()),
                action="seed_reset",
                detail="Database reset to known demo state",
                timestamp=datetime.now().replace(microsecond=0),
                department=None,
                correlation_id=None,
            )
        )
        session.commit()
        logger.info("Database seeded from %s", seed_dir)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_demo_clock(session: Session) -> datetime | None:
    """Return virtual DEMO_CLOCK if set."""
    row = session.get(DemoClockRow, 1)
    if row and row.clock_iso:
        return datetime.fromisoformat(row.clock_iso)
    settings = get_settings()
    if settings.demo_clock:
        return datetime.fromisoformat(settings.demo_clock)
    return None


def set_demo_clock(session: Session, when: datetime | None) -> None:
    """Persist virtual clock (None clears to wall time)."""
    row = session.get(DemoClockRow, 1)
    if row is None:
        row = DemoClockRow(id=1, clock_iso=None)
        session.add(row)
    row.clock_iso = when.isoformat() if when else None
    session.commit()


def now(session: Session | None = None) -> datetime:
    """Return DEMO_CLOCK if set, else wall-clock UTC-naive local-ish now."""
    if session is not None:
        clock = get_demo_clock(session)
        if clock is not None:
            return clock
    settings = get_settings()
    if settings.demo_clock:
        return datetime.fromisoformat(settings.demo_clock)
    return datetime.now().replace(microsecond=0)


def write_audit(
    session: Session,
    action: str,
    detail: str,
    department: str | None = None,
    correlation_id: str | None = None,
) -> None:
    """Append an audit log entry."""
    session.add(
        AuditLogRow(
            id=str(uuid.uuid4()),
            action=action,
            detail=detail,
            timestamp=now(session),
            department=department,
            correlation_id=correlation_id,
        )
    )


def get_employee(session: Session, user_id: str) -> EmployeeRow | None:
    return session.get(EmployeeRow, user_id)


def dietary_tags_of(emp: EmployeeRow) -> list[str]:
    return json.loads(emp.dietary_tags_json or "[]")


def minutes_since_last_meal(session: Session, user_id: str, at: datetime) -> int | None:
    stmt = (
        select(MealLogRow)
        .where(MealLogRow.user_id == user_id)
        .order_by(MealLogRow.logged_at.desc())
        .limit(1)
    )
    row = session.scalars(stmt).first()
    if not row:
        return None
    return int((at - row.logged_at).total_seconds() // 60)


def minutes_since_last_movement(session: Session, user_id: str, at: datetime) -> int | None:
    stmt = (
        select(MovementBreakRow)
        .where(MovementBreakRow.user_id == user_id)
        .order_by(MovementBreakRow.start.desc())
        .limit(1)
    )
    row = session.scalars(stmt).first()
    if not row:
        return None
    return int((at - row.start).total_seconds() // 60)


def proactive_count_today(session: Session, user_id: str, day: date) -> int:
    row = session.get(ProactiveCounterRow, (user_id, day))
    return row.count if row else 0


def increment_proactive(session: Session, user_id: str, day: date) -> int:
    row = session.get(ProactiveCounterRow, (user_id, day))
    if row is None:
        row = ProactiveCounterRow(user_id=user_id, day=day, count=0)
        session.add(row)
    row.count += 1
    return row.count


def _default_policy() -> PolicyRow:
    settings = get_settings()
    return PolicyRow(
        id=1,
        allow_food_orders=True,
        max_order_sar=35.0,
        max_proactive_per_day=settings.max_proactive_per_day,
        auto_book_walk_break=True,
        updated_at=None,
    )


def refresh_wallet(session: Session, user_id: str) -> WalletRow | None:
    """Refill daily (and weekly on a new ISO week) allowance when the day rolls over."""
    wallet = session.get(WalletRow, user_id)
    emp = session.get(EmployeeRow, user_id)
    if wallet is None or emp is None:
        return wallet
    today = now(session).date()
    if wallet.balance_day is None:
        wallet.balance_day = today
        return wallet
    if wallet.balance_day == today:
        return wallet
    if wallet.balance_day.isocalendar()[:2] != today.isocalendar()[:2]:
        wallet.weekly_remaining_sar = emp.daily_allowance_sar * get_settings().weekly_allowance_multiplier
    wallet.daily_remaining_sar = min(emp.daily_allowance_sar, wallet.weekly_remaining_sar)
    wallet.balance_day = today
    return wallet


def get_policy(session: Session) -> PolicyRow:
    """Return the company policy row, creating defaults if missing."""
    row = session.get(PolicyRow, 1)
    if row is None:
        row = _default_policy()
        session.add(row)
        session.flush()
    return row


def bump_today_metrics(
    session: Session,
    department: str,
    *,
    offered: bool = False,
    accepted: bool = False,
    slump_prevented: bool = False,
    allowance_delta: float = 0.0,
) -> None:
    """
    Update today's aggregated history row for live dashboard demos.

    Each lifecycle event bumps only its own counter: propose → offered,
    accept → accepted, verified completion → slump_prevented.
    """
    today = now(session).date()
    stmt = select(HistoryRow).where(
        HistoryRow.date == today,
        HistoryRow.department == department,
    )
    row = session.scalars(stmt).first()
    if row is None:
        opted = session.scalars(
            select(EmployeeRow).where(EmployeeRow.department == department)
        ).all()
        row = HistoryRow(
            date=today,
            department=department,
            energy_index=72.0,
            slumps_prevented=0,
            interventions_offered=0,
            interventions_accepted=0,
            allowance_used_sar=0.0,
            employee_count=max(5, len(opted)),
        )
        session.add(row)
    if offered:
        row.interventions_offered += 1
    if accepted:
        row.interventions_accepted += 1
        row.energy_index = min(95.0, row.energy_index + 0.8)
    if slump_prevented:
        row.slumps_prevented += 1
    row.allowance_used_sar += allowance_delta
