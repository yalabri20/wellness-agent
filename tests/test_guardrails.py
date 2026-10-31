"""Tests for guardrails: k-anonymity, caps, dietary, confirmation, catalog prep."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import db as dbmod
from db import Base, EmployeeRow, MealCatalogRow, WalletRow
from exceptions import (
    AllowanceExceededError,
    ConfirmationRequiredError,
    DietaryFilterError,
)
from guardrails import (
    assert_allowance,
    assert_confirmation,
    assert_dietary,
    filter_catalog,
    k_anonymize_groups,
    meal_matches_dietary,
)
from services import confirm_and_create_order


@pytest.fixture()
def session(tmp_path: Path) -> Session:
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}", future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, future=True)
    s = SessionLocal()
    s.add(
        EmployeeRow(
            id="u1",
            department="Engineering",
            daily_allowance_sar=30,
            dietary_tags_json=json.dumps(["vegetarian", "no_nuts"]),
            opted_in=True,
            language="en",
        )
    )
    s.add(WalletRow(user_id="u1", daily_remaining_sar=30, weekly_remaining_sar=150))
    s.add(
        MealCatalogRow(
            id="veg_ok",
            name_ar="خ",
            name_en="Veg",
            restaurant="R",
            price_sar=12,
            est_macros_json="{}",
            tags_json=json.dumps(["vegetarian", "no_nuts", "high_protein", "light"]),
            prep_minutes=15,
            url="https://example.com/REPLACE_ME",
        )
    )
    s.add(
        MealCatalogRow(
            id="nuts_meal",
            name_ar="م",
            name_en="Nuts",
            restaurant="R",
            price_sar=12,
            est_macros_json="{}",
            tags_json=json.dumps(["vegetarian", "high_protein"]),
            prep_minutes=10,
            url="https://example.com/REPLACE_ME",
        )
    )
    s.add(
        MealCatalogRow(
            id="slow_meal",
            name_ar="ب",
            name_en="Slow",
            restaurant="R",
            price_sar=12,
            est_macros_json="{}",
            tags_json=json.dumps(["vegetarian", "no_nuts", "high_protein"]),
            prep_minutes=40,
            url="https://example.com/REPLACE_ME",
        )
    )
    s.add(
        MealCatalogRow(
            id="meat",
            name_ar="ل",
            name_en="Meat",
            restaurant="R",
            price_sar=20,
            est_macros_json="{}",
            tags_json=json.dumps(["high_protein", "no_nuts"]),
            prep_minutes=15,
            url="https://example.com/REPLACE_ME",
        )
    )
    s.commit()
    # patch now() to be stable
    yield s
    s.close()


def test_k_anonymity_hides_small_groups():
    groups = [
        {"department": "A", "employee_count": 8, "energy_index": 70},
        {"department": "B", "employee_count": 3, "energy_index": 60},
        {"department": "C", "employee_count": 5, "energy_index": 65},
    ]
    out = k_anonymize_groups(groups, k=5)
    assert len(out) == 2
    assert all(g["employee_count"] >= 5 for g in out)


def test_confirmation_required():
    with pytest.raises(ConfirmationRequiredError):
        assert_confirmation(False)
    assert_confirmation(True)


def test_dietary_filtering(session: Session):
    assert meal_matches_dietary(["vegetarian", "no_nuts"], ["vegetarian", "no_nuts"])
    assert not meal_matches_dietary(["vegetarian"], ["vegetarian", "no_nuts"])
    assert_dietary(session, "u1", "veg_ok")
    with pytest.raises(DietaryFilterError):
        assert_dietary(session, "u1", "nuts_meal")
    with pytest.raises(DietaryFilterError):
        assert_dietary(session, "u1", "meat")


def test_allowance_caps(session: Session):
    assert_allowance(session, "u1", 20)
    with pytest.raises(AllowanceExceededError):
        assert_allowance(session, "u1", 50)


def test_catalog_prep_time_filter(session: Session):
    from sqlalchemy import select

    meals = list(session.scalars(select(MealCatalogRow)).all())
    filtered = filter_catalog(
        meals,
        tags=["high_protein"],
        max_prep_minutes=20,
        max_price=30,
        dietary_tags=["vegetarian", "no_nuts"],
    )
    ids = {m.id for m in filtered}
    assert "veg_ok" in ids
    assert "slow_meal" not in ids
    assert "meat" not in ids
    assert "nuts_meal" not in ids


def test_idempotent_confirm(session: Session, monkeypatch):
    monkeypatch.setattr(dbmod, "now", lambda s=None: datetime(2026, 10, 30, 12, 30))
    token = "tok-idem-1"
    o1 = confirm_and_create_order(
        session, user_id="u1", meal_id="veg_ok", confirmation_token=token, confirmed=True
    )
    bal_after = session.get(WalletRow, "u1").daily_remaining_sar
    o2 = confirm_and_create_order(
        session, user_id="u1", meal_id="veg_ok", confirmation_token=token, confirmed=True
    )
    assert o1.id == o2.id
    assert session.get(WalletRow, "u1").daily_remaining_sar == bal_after
