"""Governance guardrails enforced in code before any action."""

from __future__ import annotations

import json
from datetime import datetime, time

from sqlalchemy.orm import Session

from config import get_settings
from db import (
    MealCatalogRow,
    WalletRow,
    dietary_tags_of,
    get_employee,
    get_policy,
    proactive_count_today,
    refresh_wallet,
)
from exceptions import (
    AllowanceExceededError,
    ConfirmationRequiredError,
    DietaryFilterError,
    NotFoundError,
    PolicyBlockedError,
    ProactiveCapError,
    QuietHoursError,
)


def _parse_hhmm(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def in_quiet_hours(at: datetime) -> bool:
    """Return True if `at` falls in configured quiet hours (may wrap midnight)."""
    settings = get_settings()
    start = _parse_hhmm(settings.quiet_hours_start)
    end = _parse_hhmm(settings.quiet_hours_end)
    t = at.time()
    if start <= end:
        return start <= t < end
    # wraps midnight, e.g. 18:00–08:00
    return t >= start or t < end


def assert_not_quiet_hours(at: datetime) -> None:
    """Raise if proactive messaging is blocked by quiet hours."""
    if in_quiet_hours(at):
        raise QuietHoursError("Quiet hours — proactive messaging paused")


def assert_proactive_cap(session: Session, user_id: str, at: datetime) -> None:
    """Raise if employee hit the daily proactive message cap (HR policy)."""
    cap = get_policy(session).max_proactive_per_day
    count = proactive_count_today(session, user_id, at.date())
    if count >= cap:
        raise ProactiveCapError(f"Proactive cap reached ({cap}/day) for {user_id}")


def assert_order_policy(session: Session, price_sar: float) -> None:
    """Enforce HR policy on food orders (kill switch + per-order price cap)."""
    policy = get_policy(session)
    if not policy.allow_food_orders:
        raise PolicyBlockedError("Food orders are disabled by company policy")
    if price_sar > policy.max_order_sar + 1e-6:
        raise PolicyBlockedError(
            f"Order {price_sar:.0f} SAR exceeds policy cap {policy.max_order_sar:.0f} SAR"
        )


def order_allowed_by_policy(session: Session, price_sar: float) -> bool:
    try:
        assert_order_policy(session, price_sar)
    except PolicyBlockedError:
        return False
    return True


def assert_confirmation(confirmed: bool) -> None:
    """Spending always requires explicit human confirmation."""
    if not confirmed:
        raise ConfirmationRequiredError("Ordering requires explicit user confirmation")


def meal_matches_dietary(meal_tags: list[str], dietary_tags: list[str]) -> bool:
    """
    Return True if meal is compatible with employee dietary tags.

    Rules:
    - If employee has 'vegetarian', meal must include 'vegetarian'
    - If employee has 'no_nuts', meal must include 'no_nuts' (catalog marks nut-free items)
    - Other tags are soft preferences (not hard blocks)
    """
    hard = {"vegetarian", "no_nuts"}
    for tag in dietary_tags:
        if tag in hard and tag not in meal_tags:
            return False
    return True


def assert_dietary(session: Session, user_id: str, meal_id: str) -> MealCatalogRow:
    """Load meal and enforce dietary-tag filtering."""
    emp = get_employee(session, user_id)
    if emp is None:
        raise NotFoundError(f"Employee {user_id} not found")
    meal = session.get(MealCatalogRow, meal_id)
    if meal is None:
        raise NotFoundError(f"Meal {meal_id} not found")
    meal_tags = json.loads(meal.tags_json or "[]")
    if not meal_matches_dietary(meal_tags, dietary_tags_of(emp)):
        raise DietaryFilterError(
            f"Meal {meal_id} incompatible with dietary tags {dietary_tags_of(emp)}"
        )
    return meal


def assert_allowance(session: Session, user_id: str, price_sar: float) -> WalletRow:
    """Enforce daily and weekly allowance caps."""
    wallet = refresh_wallet(session, user_id)
    if wallet is None:
        raise NotFoundError(f"Wallet for {user_id} not found")
    if price_sar > wallet.daily_remaining_sar + 1e-6:
        raise AllowanceExceededError(
            f"Daily allowance exceeded: need {price_sar}, have {wallet.daily_remaining_sar}"
        )
    if price_sar > wallet.weekly_remaining_sar + 1e-6:
        raise AllowanceExceededError(
            f"Weekly allowance exceeded: need {price_sar}, have {wallet.weekly_remaining_sar}"
        )
    return wallet


def filter_catalog(
    meals: list[MealCatalogRow],
    *,
    tags: list[str] | None = None,
    max_prep_minutes: int | None = None,
    max_price: float | None = None,
    dietary_tags: list[str] | None = None,
) -> list[MealCatalogRow]:
    """Filter catalog rows by prep time, price, tags, and dietary constraints."""
    results: list[MealCatalogRow] = []
    for meal in meals:
        meal_tags = json.loads(meal.tags_json or "[]")
        if max_prep_minutes is not None and meal.prep_minutes > max_prep_minutes:
            continue
        if max_price is not None and meal.price_sar > max_price:
            continue
        if tags:
            if not all(t in meal_tags for t in tags):
                continue
        if dietary_tags and not meal_matches_dietary(meal_tags, dietary_tags):
            continue
        results.append(meal)
    return results


def k_anonymize_groups(
    groups: list[dict],
    count_key: str = "employee_count",
    k: int | None = None,
) -> list[dict]:
    """Hide any aggregated group with fewer than k employees."""
    threshold = k if k is not None else get_settings().k_anonymity
    return [g for g in groups if int(g.get(count_key, 0)) >= threshold]
