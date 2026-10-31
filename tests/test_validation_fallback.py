"""Pydantic validation fallbacks and VisionAgent clarifying questions."""

from __future__ import annotations

from agent_core import VisionAgent
from models import Language, MealMacros


def test_meal_macros_validation():
    m = MealMacros(
        kcal=100,
        protein_g=10,
        refined_carbs_g=10,
        fiber_g=2,
        fat_g=3,
        sugar_g=1,
        confidence=0.9,
        dish_names="kabsa",
        language=Language.EN,
    )
    assert m.dish_names == ["kabsa"]


def test_vision_clarify_on_low_confidence():
    agent = VisionAgent()
    out = agent.analyze(text="asdf qwer zxcv")
    # heuristic confidence for unknown text is 0.45 -> clarifying question
    assert isinstance(out, str)


def test_vision_saudi_kabsa():
    agent = VisionAgent()
    out = agent.analyze(text="كبسة دجاج مع بيبسي")
    assert isinstance(out, MealMacros)
    assert out.confidence >= 0.5
    assert out.kcal >= 700
