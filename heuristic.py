"""Deterministic work-hours energy / slump heuristic (not medical advice)."""

from __future__ import annotations

from datetime import datetime, timedelta

from models import CalendarEvent, EnergyAssessment, Importance, MealMacros, SlumpRisk


def compute_slump_score(
    macros: MealMacros,
    upcoming_events: list[CalendarEvent],
    at: datetime,
) -> tuple[float, list[str]]:
    """
    Compute a reproducible 0–100 slump risk score.

    Higher refined carbs + sugar + fat relative to protein + fiber raises the
    score; meal heaviness and time of day adjust it; a high-importance meeting
    30–120 minutes after a heavy meal raises urgency.
    """
    factors: list[str] = []

    protein = max(macros.protein_g, 0.0)
    fiber = max(macros.fiber_g, 0.0)
    refined = max(macros.refined_carbs_g, 0.0)
    sugar = max(macros.sugar_g, 0.0)
    fat = max(macros.fat_g, 0.0)
    kcal = max(macros.kcal, 1.0)

    protective = protein + fiber * 1.5
    load = refined * 1.2 + sugar * 1.4 + fat * 0.6
    ratio = load / max(protective, 1.0)

    # Base from macro ratio (cap contribution)
    score = min(55.0, ratio * 12.0)
    factors.append(f"macro_ratio={ratio:.2f}")

    # Meal heaviness (kcal) — lunch-size heavy meals push risk up
    if kcal >= 700:
        score += 20
        factors.append("heavy_meal_kcal>=700")
    elif kcal >= 500:
        score += 12
        factors.append("moderate_heavy_meal_kcal>=500")
    elif kcal >= 350:
        score += 5
        factors.append("solid_meal_kcal>=350")
    else:
        score -= 5
        factors.append("light_meal")

    # Time-of-day: classic post-lunch window 12:30–15:00
    minutes = at.hour * 60 + at.minute
    if 12 * 60 + 30 <= minutes <= 15 * 60:
        score += 10
        factors.append("afternoon_window")
    elif minutes < 10 * 60:
        score -= 5
        factors.append("morning_buffer")

    # High-importance meeting 30–120 min after meal elevates urgency
    heavy = kcal >= 500 or ratio >= 3.0
    for ev in upcoming_events:
        delta_min = (ev.start - at).total_seconds() / 60.0
        if 30 <= delta_min <= 120 and ev.importance == Importance.HIGH:
            bump = 18 if heavy else 8
            score += bump
            factors.append(f"high_importance_meeting_in_{int(delta_min)}m")
            break
        if 30 <= delta_min <= 120 and ev.importance == Importance.MEDIUM and heavy:
            score += 6
            factors.append(f"medium_meeting_after_heavy_in_{int(delta_min)}m")
            break

    score = max(0.0, min(100.0, score))
    return score, factors


def risk_band(score: float) -> SlumpRisk:
    """Map numeric score to low/medium/high band."""
    if score >= 65:
        return SlumpRisk.HIGH
    if score >= 40:
        return SlumpRisk.MEDIUM
    return SlumpRisk.LOW


def predicted_window(at: datetime, score: float) -> str:
    """Return a human-readable predicted slump window string."""
    start = at + timedelta(minutes=45 if score >= 50 else 70)
    end = start + timedelta(minutes=60)
    return f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')}"


def build_assessment(
    macros: MealMacros,
    upcoming_events: list[CalendarEvent],
    at: datetime,
    llm_reasoning: str | None = None,
) -> EnergyAssessment:
    """Build EnergyAssessment from the deterministic score (+ optional LLM text)."""
    score, factors = compute_slump_score(macros, upcoming_events, at)
    band = risk_band(score)
    window = predicted_window(at, score)
    default_reason = (
        f"Estimated work-hours slump risk is {band.value} (score {score:.0f}/100) "
        f"based on meal composition and upcoming meetings. "
        f"This is a wellness heuristic, not medical advice."
    )
    return EnergyAssessment(
        slump_risk=band,
        risk_score=round(score, 1),
        predicted_slump_window=window,
        reasoning=llm_reasoning or default_reason,
        key_factors=factors,
    )
