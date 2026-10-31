"""Demo mode helpers: cached LLM responses and virtual clock."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from config import DEMO_RESPONSES_DIR, get_settings
from db import get_demo_clock, set_demo_clock
from models import MealMacros

logger = logging.getLogger(__name__)


def is_demo_mode() -> bool:
    """Return whether DEMO_MODE is enabled."""
    return get_settings().demo_mode


def get_virtual_now(session: Session | None = None) -> datetime:
    """Return DEMO_CLOCK if set, else wall clock."""
    if session is not None:
        clock = get_demo_clock(session)
        if clock is not None:
            return clock
    settings = get_settings()
    if settings.demo_clock:
        return datetime.fromisoformat(settings.demo_clock)
    return datetime.now().replace(microsecond=0)


def _parse_hhmm(value: str) -> tuple[int, int]:
    hour, minute = value.split(":")
    return int(hour), int(minute)


def in_work_hours(at: datetime) -> bool:
    """True if `at` falls inside configured work hours (same calendar day)."""
    settings = get_settings()
    sh, sm = _parse_hhmm(settings.work_hours_start)
    eh, em = _parse_hhmm(settings.work_hours_end)
    start = at.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = at.replace(hour=eh, minute=em, second=0, microsecond=0)
    return start <= at < end


def effective_agent_now(session: Session, *, persist: bool = True) -> datetime:
    """
    Clock used for calendar-aware agent decisions.

    If DEMO_MODE and there is no DEMO_CLOCK, and wall time is outside work hours,
    snap to today 12:20 so today's seeded meetings are still visible for the demo.
    """
    clock = get_demo_clock(session)
    if clock is not None:
        return clock
    settings = get_settings()
    if settings.demo_clock:
        return datetime.fromisoformat(settings.demo_clock)

    wall = datetime.now().replace(microsecond=0)
    if is_demo_mode() and not in_work_hours(wall):
        snapped = wall.replace(hour=12, minute=20, second=0, microsecond=0)
        if persist:
            set_demo_clock(session, snapped)
            logger.info(
                "DEMO_MODE outside work hours — snapped DEMO_CLOCK to %s for calendar context",
                snapped.isoformat(),
            )
        return snapped
    return wall


def advance_clock(session: Session, when: datetime) -> datetime:
    """Set virtual DEMO_CLOCK and return it."""
    set_demo_clock(session, when)
    logger.info("DEMO_CLOCK advanced to %s", when.isoformat())
    return when


def clear_clock(session: Session) -> None:
    """Clear virtual clock back to wall time."""
    set_demo_clock(session, None)


def _image_fingerprint(image_bytes: bytes) -> str:
    return hashlib.sha256(image_bytes).hexdigest()[:16]


def detect_demo_image_key(image_bytes: bytes | None, text: str | None = None) -> str | None:
    """
    Map known demo inputs to cached response keys.

    Text: Arabic/English keywords. Image: exact fingerprint of the three
    bundled placeholder JPEGs only.
    """
    if text:
        lowered = text.lower()
        # Only the explicit demo kabsa scenario — never match on Pepsi alone
        if (
            "heavy" in lowered
            or "كبسة" in text
            or "kabsa" in lowered
            or ("demo image heavy" in lowered)
        ):
            return "heavy"
        if "balanced" in lowered or (
            ("سلطة" in text or "salad" in lowered)
            and "فواكه" not in text
            and "fruit" not in lowered
        ):
            return "balanced"
        if "light" in lowered or "سلطة فواكه" in text or (
            "fruit" in lowered and "salad" not in lowered
        ):
            return "light"
        if "proactive" in lowered or "protein snack" in lowered:
            return "proactive_1220"

    if image_bytes:
        # Only the bundled placeholder JPEGs (exact fingerprint) map to cached keys;
        # any real photo returns None so it is analyzed for what it actually shows.
        meta_path = DEMO_RESPONSES_DIR / "image_keys.json"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            fp = _image_fingerprint(image_bytes)
            for key, info in meta.items():
                if info.get("sha16") == fp:
                    return key
    return None


def load_cached_macros(key: str) -> MealMacros | None:
    """Load cached MealMacros for a demo key."""
    path = DEMO_RESPONSES_DIR / f"{key}_macros.json"
    if not path.exists():
        return None
    return MealMacros.model_validate_json(path.read_text(encoding="utf-8"))


def load_cached_decision(key: str) -> dict[str, Any] | None:
    """Load cached decision payload (assessment + intervention)."""
    path = DEMO_RESPONSES_DIR / f"{key}_decision.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_cached_reasoning(key: str) -> str | None:
    path = DEMO_RESPONSES_DIR / f"{key}_reasoning.txt"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return None


def ensure_demo_response_files() -> None:
    """Write default cached demo responses if missing."""
    import hashlib

    from config import DEMO_IMAGES_DIR

    DEMO_RESPONSES_DIR.mkdir(parents=True, exist_ok=True)
    # Keep image fingerprints in sync with placeholder JPEGs
    meta_path = DEMO_RESPONSES_DIR / "image_keys.json"
    meta: dict[str, Any] = {}
    for key in ("heavy", "balanced", "light"):
        img = DEMO_IMAGES_DIR / f"{key}.jpg"
        if img.exists():
            data = img.read_bytes()
            meta[key] = {
                "sha16": hashlib.sha256(data).hexdigest()[:16],
                "size": len(data),
                "path": str(img),
            }
    if meta:
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    heavy_macros = {
        "kcal": 980,
        "protein_g": 42,
        "refined_carbs_g": 95,
        "fiber_g": 6,
        "fat_g": 38,
        "sugar_g": 42,
        "confidence": 0.88,
        "dish_names": ["chicken kabsa", "Pepsi"],
        "language": "en",
    }
    balanced_macros = {
        "kcal": 520,
        "protein_g": 34,
        "refined_carbs_g": 28,
        "fiber_g": 9,
        "fat_g": 18,
        "sugar_g": 8,
        "confidence": 0.84,
        "dish_names": ["grilled chicken salad", "water"],
        "language": "en",
    }
    light_macros = {
        "kcal": 280,
        "protein_g": 12,
        "refined_carbs_g": 18,
        "fiber_g": 5,
        "fat_g": 8,
        "sugar_g": 14,
        "confidence": 0.81,
        "dish_names": ["fruit plate", "labneh"],
        "language": "en",
    }

    configs: dict[str, dict[str, Any]] = {
        "heavy": {
            "macros": heavy_macros,
            "reasoning": (
                "Heavy kabsa with sugary soda after a protein-light breakfast raises "
                "post-meal sleepiness risk ahead of your 13:00 high-importance meeting. "
                "A short walk is a lower-cost intervention than more food."
            ),
            "decision": {
                "intervention_type": "walk",
                "rationale": "High slump risk before a high-importance meeting — 10-minute walk preferred over more food.",
                "timing": "now",
                "duration_min": 10,
                "meal_id": None,
                "requires_confirmation": False,
            },
        },
        "balanced": {
            "macros": balanced_macros,
            "reasoning": (
                "Balanced protein and fiber with moderate refined carbs. Risk is moderate; "
                "hydration and a brief stretch are enough unless the next meeting is high-stakes soon."
            ),
            "decision": {
                "intervention_type": "hydration",
                "rationale": "Balanced meal — prioritize water and light movement.",
                "timing": "within 15 minutes",
                "duration_min": 3,
                "meal_id": None,
                "requires_confirmation": False,
            },
        },
        "light": {
            "macros": light_macros,
            "reasoning": (
                "Light meal with modest protein. Slump risk is low for work-hours energy."
            ),
            "decision": {
                "intervention_type": "none",
                "rationale": "Light meal, low risk — no intervention needed.",
                "timing": "n/a",
                "duration_min": None,
                "meal_id": None,
                "requires_confirmation": False,
            },
        },
        "proactive_1220": {
            "macros": None,
            "reasoning": (
                "Important meeting around 13:00; last meal was breakfast (~07:30). "
                "A high-protein snack that can arrive by ~12:45 supports steady energy."
            ),
            "decision": {
                "intervention_type": "snack",
                "rationale": "Nutrition gap before high-importance meeting — protein snack with prep<=25 min.",
                "timing": "order now for arrival by 12:45",
                "duration_min": None,
                "meal_id": "meal_cottage_cheese",
                "requires_confirmation": True,
            },
        },
    }

    for key, cfg in configs.items():
        if cfg["macros"] is not None:
            (DEMO_RESPONSES_DIR / f"{key}_macros.json").write_text(
                json.dumps(cfg["macros"], indent=2), encoding="utf-8"
            )
        (DEMO_RESPONSES_DIR / f"{key}_decision.json").write_text(
            json.dumps(cfg["decision"], indent=2), encoding="utf-8"
        )
        (DEMO_RESPONSES_DIR / f"{key}_reasoning.txt").write_text(
            cfg["reasoning"], encoding="utf-8"
        )
