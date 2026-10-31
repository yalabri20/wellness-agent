"""Mock enterprise FastAPI service (calendar, wallet, orders, HR, demo clock)."""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from calendar_provider import MockCalendar
from config import get_settings
from db import get_db, get_session_factory, init_db, now, reset_and_seed
from demo_mode import advance_clock, ensure_demo_response_files
from exceptions import (
    AllowanceExceededError,
    ConfirmationRequiredError,
    DietaryFilterError,
    IdempotencyError,
    NotFoundError,
    PolicyBlockedError,
    WellnessAgentError,
)
from logging_utils import new_correlation_id, setup_logging
from models import CalendarEventCreate, ConfirmOrderRequest, StepsUpdateRequest
from services import (
    add_calendar_event,
    confirm_and_create_order,
    delete_calendar_event,
    get_order,
    get_wallet,
    hr_audit,
    hr_insights,
    hr_live_feed,
    hr_metrics,
    hr_trace,
    list_calendar_events,
    list_catalog,
    mark_decision_skipped,
    policy_dict,
    update_policy,
)

setup_logging()
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Wellness Agent Mock Enterprise API",
    description="Calendar, wallet, orders, HR metrics — hackathon demo backend.",
    version="0.1.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class AdvanceClockRequest(BaseModel):
    clock_iso: str = Field(description="ISO datetime for DEMO_CLOCK, e.g. todayT12:20:00")


class SkipRequest(BaseModel):
    user_id: str
    correlation_id: str | None = None


class FailureToggle(BaseModel):
    failure_rate: float = Field(ge=0, le=1)


_runtime_failure_rate: float | None = None


async def _simulate_latency_and_failures() -> None:
    settings = get_settings()
    rate = _runtime_failure_rate if _runtime_failure_rate is not None else settings.api_failure_rate
    if settings.api_latency_ms:
        await asyncio.sleep(settings.api_latency_ms / 1000.0)
    if rate and random.random() < rate:
        raise HTTPException(status_code=503, detail="Simulated API failure")


@app.on_event("startup")
def _startup() -> None:
    ensure_demo_response_files()
    init_db()
    # Seed if empty
    session = get_session_factory()()
    try:
        from sqlalchemy import select

        from db import EmployeeRow

        count = len(session.scalars(select(EmployeeRow).limit(1)).all())
        if count == 0:
            session.close()
            reset_and_seed()
        else:
            session.close()
    except Exception:
        session.close()
        reset_and_seed()
    logger.info("Mock enterprise API ready")


@app.middleware("http")
async def correlation_middleware(request, call_next):
    cid = request.headers.get("x-correlation-id") or new_correlation_id()
    response = await call_next(request)
    response.headers["x-correlation-id"] = cid
    return response


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/calendar/{user_id}")
async def calendar(user_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    await _simulate_latency_and_failures()
    # Full list for simulator UI (includes optional employee-only labels)
    return {
        "user_id": user_id,
        "events": list_calendar_events(db, user_id),
        "note": "Labels are employee-UI only; LLM/HR never receive meeting titles",
        "privacy_safe": [
            e.model_dump(mode="json")
            for e in MockCalendar(db).get_events(user_id, now(db))
        ],
    }


@app.post("/calendar/{user_id}")
async def calendar_add(
    user_id: str, body: CalendarEventCreate, db: Session = Depends(get_db)
) -> dict[str, Any]:
    if body.end <= body.start:
        raise HTTPException(400, "end must be after start")
    return add_calendar_event(
        db,
        user_id,
        start=body.start,
        end=body.end,
        importance=body.importance.value,
        attendee_count=body.attendee_count,
        label=body.label,
    )


@app.delete("/calendar/{user_id}/{event_id}")
async def calendar_delete(
    user_id: str, event_id: str, db: Session = Depends(get_db)
) -> dict[str, str]:
    try:
        delete_calendar_event(db, user_id, event_id)
    except NotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"status": "deleted"}


@app.get("/wallet/{user_id}")
async def wallet(user_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    await _simulate_latency_and_failures()
    try:
        return get_wallet(db, user_id).model_dump()
    except NotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/orders")
async def create_order(body: ConfirmOrderRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    await _simulate_latency_and_failures()
    try:
        order = confirm_and_create_order(
            db,
            user_id=body.user_id,
            meal_id=body.meal_id,
            confirmation_token=body.confirmation_token,
            correlation_id=body.correlation_id,
            confirmed=True,
        )
        return order.model_dump(mode="json")
    except ConfirmationRequiredError as exc:
        raise HTTPException(400, str(exc)) from exc
    except (AllowanceExceededError, DietaryFilterError, PolicyBlockedError) as exc:
        raise HTTPException(403, str(exc)) from exc
    except IdempotencyError as exc:
        raise HTTPException(409, str(exc)) from exc
    except NotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except WellnessAgentError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/orders/{order_id}")
async def order_status(order_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    await _simulate_latency_and_failures()
    try:
        return get_order(db, order_id).model_dump(mode="json")
    except NotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/interventions/skip")
async def skip_intervention(body: SkipRequest, db: Session = Depends(get_db)) -> dict[str, str]:
    mark_decision_skipped(db, body.user_id, body.correlation_id)
    return {"status": "skipped"}


class InterventionIdBody(BaseModel):
    intervention_id: str


@app.post("/interventions/accept")
async def intervention_accept(
    body: InterventionIdBody, db: Session = Depends(get_db)
) -> dict[str, Any]:
    from interventions import accept_intervention

    try:
        return accept_intervention(db, body.intervention_id).model_dump(mode="json")
    except NotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except WellnessAgentError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/interventions/decline")
async def intervention_decline(
    body: InterventionIdBody, db: Session = Depends(get_db)
) -> dict[str, Any]:
    from interventions import decline_intervention

    try:
        return decline_intervention(db, body.intervention_id).model_dump(mode="json")
    except NotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except WellnessAgentError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/interventions/done")
async def intervention_done(
    body: InterventionIdBody, db: Session = Depends(get_db)
) -> dict[str, Any]:
    from interventions import complete_intervention

    try:
        return complete_intervention(db, body.intervention_id, reason="employee_done").model_dump(
            mode="json"
        )
    except NotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except WellnessAgentError as exc:
        raise HTTPException(400, str(exc)) from exc


class PolicyUpdate(BaseModel):
    allow_food_orders: bool | None = None
    max_order_sar: float | None = Field(default=None, ge=0)
    max_proactive_per_day: int | None = Field(default=None, ge=0, le=10)
    auto_book_walk_break: bool | None = None
    require_confirmation: bool | None = None


@app.get("/policy")
async def policy_get(db: Session = Depends(get_db)) -> dict[str, Any]:
    return policy_dict(db)


@app.put("/policy")
async def policy_put(body: PolicyUpdate, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        return update_policy(db, **body.model_dump(exclude_none=True))
    except WellnessAgentError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/hr/insights")
async def insights(
    period: str = Query("daily", pattern="^(daily|monthly)$"), db: Session = Depends(get_db)
) -> dict[str, Any]:
    return hr_insights(db, period)


@app.get("/interventions")
async def interventions_list(
    limit: int = Query(40, ge=1, le=200), db: Session = Depends(get_db)
) -> list[dict]:
    from interventions import list_interventions

    return [r.model_dump(mode="json") for r in list_interventions(db, limit=limit)]


@app.get("/activity/{user_id}")
async def activity_get(user_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    from interventions import get_activity_state

    state = get_activity_state(db, user_id)
    return {
        "user_id": user_id,
        "steps": state.steps,
        "updated_at": state.updated_at.isoformat(),
        "source": state.source,
    }


@app.post("/activity/steps")
async def activity_steps(
    body: StepsUpdateRequest, db: Session = Depends(get_db)
) -> dict[str, Any]:
    """
    Apple Shortcut step ingest.

    Minimal body (recommended for Shortcuts):
        {"steps": 3842}

    Defaults: user_id=demo_user, source=shortcut. Token is optional.
    """
    from interventions import apply_activity

    settings = get_settings()
    expected = settings.activity_api_token
    # Only enforce token if the client sends one (Shortcut can omit it)
    if body.token is not None and expected and body.token != expected:
        raise HTTPException(401, "Invalid activity token")

    try:
        return apply_activity(
            db,
            user_id=body.user_id or settings.live_demo_user_id,
            absolute_steps=body.steps,
            steps_delta=body.steps_delta,
            source=body.source or "shortcut",
            intervention_id=body.intervention_id,
        )
    except NotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except WellnessAgentError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/catalog")
async def catalog(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return list_catalog(db)


@app.get("/hr/metrics")
async def metrics(db: Session = Depends(get_db)) -> dict[str, Any]:
    await _simulate_latency_and_failures()
    return hr_metrics(db)


@app.get("/hr/trace")
async def trace(limit: int = Query(40, ge=1, le=200), db: Session = Depends(get_db)) -> list[dict]:
    return hr_trace(db, limit=limit)


@app.get("/hr/live")
async def live(limit: int = Query(30, ge=1, le=200), db: Session = Depends(get_db)) -> list[dict]:
    return hr_live_feed(db, limit=limit)


@app.get("/hr/audit")
async def audit(limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_db)) -> list[dict]:
    return hr_audit(db, limit=limit)


@app.post("/demo/advance-clock")
async def demo_advance(body: AdvanceClockRequest, db: Session = Depends(get_db)) -> dict[str, str]:
    when = datetime.fromisoformat(body.clock_iso)
    advance_clock(db, when)
    return {"demo_clock": when.isoformat()}


@app.post("/demo/reset")
async def demo_reset() -> dict[str, str]:
    reset_and_seed()
    ensure_demo_response_files()
    return {"status": "reset"}


@app.post("/demo/failure-toggle")
async def failure_toggle(body: FailureToggle) -> dict[str, float]:
    global _runtime_failure_rate
    _runtime_failure_rate = body.failure_rate
    return {"failure_rate": body.failure_rate}


@app.get("/demo/clock")
async def get_clock(db: Session = Depends(get_db)) -> dict[str, str | None]:
    return {"now": now(db).isoformat()}


def main() -> None:
    import uvicorn

    uvicorn.run("mock_enterprise_api:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    main()
