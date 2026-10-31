"""Walk lifecycle, auto-booked calendar block, HR policy and metric counting."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import db as dbmod
from calendar_provider import MockCalendar
from db import CalendarEventRow, HistoryRow, reset_and_seed, set_demo_clock
from exceptions import PolicyBlockedError
from interventions import (
    accept_intervention,
    apply_activity,
    create_intervention,
    get_intervention,
    verify_open_interventions,
)
from models import Intervention, InterventionStatus, InterventionType
from services import confirm_and_create_order, update_policy

USER = "demo_user"


class _NoLLM:
    def has_credentials(self) -> bool:
        return False


@pytest.fixture()
def session(tmp_path: Path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 't.db'}", connect_args={"check_same_thread": False}, future=True
    )
    old_engine, old_factory = dbmod._engine, dbmod._SessionLocal
    dbmod._engine = engine
    dbmod._SessionLocal = sessionmaker(bind=engine, autoflush=False, future=True)
    reset_and_seed()
    s = dbmod._SessionLocal()
    set_demo_clock(s, datetime.combine(datetime.now().date(), datetime.min.time()).replace(hour=12, minute=20))
    yield s
    s.close()
    dbmod._engine, dbmod._SessionLocal = old_engine, old_factory


def _walk(session, cid: str = "c1", minutes: int = 15):
    return create_intervention(
        session,
        user_id=USER,
        intervention=Intervention(
            type=InterventionType.WALK,
            rationale="test",
            timing="now",
            duration_min=minutes,
            requires_confirmation=False,
        ),
        correlation_id=cid,
        risk_score=80,
    )


def _today(hour: int, minute: int = 0) -> datetime:
    return datetime.combine(datetime.now().date(), datetime.min.time()).replace(hour=hour, minute=minute)


def test_start_walk_books_block_hidden_from_agent(session):
    apply_activity(session, user_id=USER, absolute_steps=1000, source="manual")
    rec = _walk(session)
    session.commit()
    rec = accept_intervention(session, rec.id)

    block = session.get(CalendarEventRow, rec.calendar_event_id)
    assert block is not None and block.is_wellness_block
    assert (block.start, block.end) == (_today(12, 20), _today(12, 35))

    cal = MockCalendar(session)
    agent_view = cal.get_events(USER, _today(12, 20))
    assert all(e.start != block.start for e in agent_view)
    assert any(e.start == block.start for e in cal.get_events(USER, _today(12, 20), include_wellness_blocks=True))


def test_block_ends_before_next_meeting(session):
    set_demo_clock(session, _today(12, 50))
    rec = _walk(session)
    session.commit()
    rec = accept_intervention(session, rec.id)
    block = session.get(CalendarEventRow, rec.calendar_event_id)
    assert block.end == _today(13, 0)


def test_auto_book_policy_off(session):
    update_policy(session, auto_book_walk_break=False)
    rec = _walk(session)
    session.commit()
    rec = accept_intervention(session, rec.id)
    assert rec.calendar_event_id is None


def test_stale_baseline_locked_by_first_sync(session):
    # Seeded step state is not a real reading → start must be locked by a sync
    rec = _walk(session)
    session.commit()
    rec = accept_intervention(session, rec.id)
    assert rec.baseline_pending

    first = apply_activity(session, user_id=USER, absolute_steps=5000)
    assert not first["verified"]
    assert get_intervention(session, rec.id).baseline_steps == 5000

    done = apply_activity(session, user_id=USER, absolute_steps=6500)
    assert done["verified"]
    assert get_intervention(session, rec.id).status == InterventionStatus.COMPLETED


def test_proposed_walk_not_auto_started_by_phone_sync(session):
    rec = _walk(session)
    session.commit()
    result = apply_activity(session, user_id=USER, absolute_steps=9000)
    assert result["intervention"] is None
    assert get_intervention(session, rec.id).status == InterventionStatus.PROPOSED


def test_completed_walk_ignored_on_later_sync(session):
    apply_activity(session, user_id=USER, absolute_steps=1000, source="manual")
    rec = _walk(session)
    session.commit()
    accept_intervention(session, rec.id)
    assert apply_activity(session, user_id=USER, absolute_steps=2600)["verified"]
    again = apply_activity(session, user_id=USER, absolute_steps=3000, intervention_id=rec.id)
    assert again["intervention"] is None


def test_metrics_count_each_stage_once(session):
    apply_activity(session, user_id=USER, absolute_steps=1000, source="manual")
    rec = _walk(session)
    session.commit()
    accept_intervention(session, rec.id)
    apply_activity(session, user_id=USER, absolute_steps=2600)
    row = session.scalars(
        select(HistoryRow).where(HistoryRow.date == _today(0).date(), HistoryRow.department == "Engineering")
    ).first()
    assert (row.interventions_offered, row.interventions_accepted, row.slumps_prevented) == (1, 1, 1)


def test_expired_when_meeting_starts_and_block_released(session):
    rec = _walk(session)
    session.commit()
    verify_open_interventions(session, _today(13, 0) + timedelta(minutes=1))
    assert get_intervention(session, rec.id).status == InterventionStatus.EXPIRED


def test_food_policy_blocks_order(session):
    update_policy(session, allow_food_orders=False)
    with pytest.raises(PolicyBlockedError):
        confirm_and_create_order(session, user_id=USER, meal_id="meal_cottage_cheese", confirmed=True)


def test_price_cap_blocks_order(session):
    update_policy(session, max_order_sar=10)
    with pytest.raises(PolicyBlockedError):
        confirm_and_create_order(session, user_id=USER, meal_id="meal_cottage_cheese", confirmed=True)


def test_operational_stats_peak_hour_and_before_meeting():
    from db import InterventionRow
    from services import _operational_stats

    def row(hour, status="declined", meeting_min=None, done_min=None, dept="Sales"):
        created = _today(hour, 0)
        return InterventionRow(
            id=f"{hour}-{status}-{meeting_min}-{done_min}",
            user_id="u",
            department=dept,
            type="walk",
            status=status,
            correlation_id="c",
            created_at=created,
            updated_at=created,
            meeting_start=created + timedelta(minutes=meeting_min) if meeting_min else None,
            completed_at=created + timedelta(minutes=done_min) if done_min else None,
        )

    ops = _operational_stats(
        [
            row(14, "completed", meeting_min=40, done_min=15),  # mitigated
            row(14, "completed", meeting_min=10, done_min=20),  # finished after meeting started
            row(14),
            row(10, "completed", done_min=12),  # no meeting → not meeting-linked
        ]
    )
    assert ops["peak_hour_display"] == "14:00" and ops["peak_hour_label"] == "Post-lunch"
    assert ops["meeting_linked"] == 2
    assert ops["mitigated_before_meetings"] == 1
    assert ops["mitigation_rate"] == 0.5


def test_monthly_insights_have_no_financial_kpis(session):
    from services import hr_insights

    data = hr_insights(session, "monthly")
    assert "estimated_savings_sar" not in data["kpis"] and "allowance_sar" not in data["kpis"]
    assert data["kpis"]["peak_slump_hour"] != "—"
    assert data["kpis"]["mitigated_before_meetings"] > 0
    assert not any("SAR" in text for text in data["insights"])


def test_food_policy_swaps_proactive_snack_for_walk(session):
    from agent_core import Orchestrator

    decision, _ = Orchestrator(session, llm=_NoLLM()).handle_proactive(USER)
    assert decision.intervention.type == InterventionType.SNACK

    update_policy(session, allow_food_orders=False)
    decision, msg = Orchestrator(session, llm=_NoLLM()).handle_proactive(USER)
    assert decision.intervention.type == InterventionType.WALK
    assert any(b.action_id == "start_walk" for b in msg.buttons)
