"""Unit tests for the deterministic slump heuristic."""

from __future__ import annotations

from datetime import datetime, timedelta

from heuristic import build_assessment, compute_slump_score, risk_band
from models import CalendarEvent, Importance, Language, MealMacros, SlumpRisk


def _macros(**kwargs) -> MealMacros:
    base = dict(
        kcal=400,
        protein_g=20,
        refined_carbs_g=30,
        fiber_g=5,
        fat_g=12,
        sugar_g=8,
        confidence=0.8,
        dish_names=["test"],
        language=Language.EN,
    )
    base.update(kwargs)
    return MealMacros(**base)


def test_heavy_refined_raises_score():
    light = _macros(kcal=250, protein_g=25, refined_carbs_g=10, sugar_g=2, fat_g=5, fiber_g=8)
    heavy = _macros(kcal=900, protein_g=20, refined_carbs_g=100, sugar_g=40, fat_g=40, fiber_g=3)
    at = datetime(2026, 10, 30, 12, 45)
    s_light, _ = compute_slump_score(light, [], at)
    s_heavy, _ = compute_slump_score(heavy, [], at)
    assert s_heavy > s_light
    assert risk_band(s_heavy) in (SlumpRisk.MEDIUM, SlumpRisk.HIGH)


def test_high_importance_meeting_bump():
    macros = _macros(kcal=750, protein_g=25, refined_carbs_g=80, sugar_g=30, fat_g=30)
    at = datetime(2026, 10, 30, 12, 20)
    events = [
        CalendarEvent(
            start=at + timedelta(minutes=50),
            end=at + timedelta(minutes=110),
            importance=Importance.HIGH,
            attendee_count=4,
        )
    ]
    score_with, factors = compute_slump_score(macros, events, at)
    score_without, _ = compute_slump_score(macros, [], at)
    assert score_with > score_without
    assert any("high_importance" in f for f in factors)


def test_reproducible():
    macros = _macros(kcal=980, protein_g=42, refined_carbs_g=95, sugar_g=42, fat_g=38)
    at = datetime(2026, 10, 30, 12, 30)
    a1 = build_assessment(macros, [], at)
    a2 = build_assessment(macros, [], at)
    assert a1.risk_score == a2.risk_score
    assert a1.key_factors == a2.key_factors


def test_score_clamped():
    macros = _macros(kcal=2000, protein_g=1, refined_carbs_g=200, sugar_g=100, fat_g=100, fiber_g=0)
    at = datetime(2026, 10, 30, 13, 0)
    score, _ = compute_slump_score(macros, [], at)
    assert 0 <= score <= 100
