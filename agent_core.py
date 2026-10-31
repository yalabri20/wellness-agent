"""Orchestrator + VisionAgent + ContextAgent + DecisionAgent."""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from calendar_provider import MockCalendar
from channels import build_agent_message
from db import (
    MealCatalogRow,
    MealLogRow,
    MovementBreakRow,
    TraceStepRow,
    WalletRow,
    dietary_tags_of,
    get_employee,
    get_policy,
    minutes_since_last_meal,
    minutes_since_last_movement,
    now,
    refresh_wallet,
    write_audit,
)
from demo_mode import (
    detect_demo_image_key,
    effective_agent_now,
    is_demo_mode,
    load_cached_macros,
)
from exceptions import LLMError, NotFoundError
from guardrails import (
    assert_allowance,
    assert_confirmation,
    assert_dietary,
    assert_order_policy,
    filter_catalog,
    meal_matches_dietary,
)
from heuristic import build_assessment
from interventions import (
    create_intervention,
    get_activity_state,
    verify_open_interventions,
)
from llm_client import LLMClient
from logging_utils import get_correlation_id, new_correlation_id
from models import (
    AgentDecision,
    ChannelMessage,
    EnergyAssessment,
    Importance,
    Intervention,
    InterventionType,
    Language,
    MealMacros,
    SlumpRisk,
    TraceStep,
    WalletState,
)

logger = logging.getLogger(__name__)

# Slack-first: short replies only (no essays / tables)
SLACK_REASONING_SYSTEM = (
    "You write ONE short Slack chat reply for a workplace wellness agent. "
    "Hard limits: max 2 sentences, max 280 characters, plain text only. "
    "No markdown headings, no bullet lists, no tables, no emojis overload. "
    "Mention: (1) if the meal looks heavy for work energy, "
    "(2) the next meeting time if provided, "
    "(3) nothing else. Not medical advice."
)

SLACK_VISION_SYSTEM = (
    "Estimate macros for ONE meal only. Return JSON only. "
    "Handle Arabic/English Saudi dishes. Never invent daily calorie budgets. "
    "Use short dish_names (2-4 words each). confidence 0-1."
)

SAUDI_DISH_HINTS = (
    "kabsa", "كبسة", "mandi", "مندي", "jareesh", "جريش", "shawarma", "شاورما",
    "dates", "تمر", "قهوة", "labneh", "لبن", "hummus", "حمص", "mutabbaq", "مطبق",
    "مكرونة", "مكرونه", "ماكرونة", "ماكرونه", "معكرونة", "معكرونه",
    "pasta", "macaroni",
)


def _format_next_meeting(events: list, at: datetime) -> str:
    """Return a short privacy-safe next-meeting phrase (no titles). Always set."""
    upcoming = sorted((e for e in events if e.end > at), key=lambda e: e.start)
    if not upcoming:
        return "none upcoming in work-hours calendar"
    ev = upcoming[0]
    mins = int((ev.start - at).total_seconds() // 60)
    if mins < 0:
        return (
            f"{ev.importance.value}-importance in progress until "
            f"{ev.end.strftime('%H:%M')} ({ev.attendee_count} attendees)"
        )
    return (
        f"{ev.importance.value}-importance at {ev.start.strftime('%H:%M')} "
        f"(in {mins}m, {ev.attendee_count} attendees)"
    )


def _clip_slack_text(text: str, max_chars: int = 280) -> str:
    """Collapse LLM verbosity into a Slack-sized line."""
    cleaned = text.strip()
    # Drop markdown scaffolding the model sometimes invents
    for marker in ("# ", "## ", "|", "*What's Working*", "Key Findings", "Nutrient"):
        if marker in cleaned:
            # take first non-empty paragraph only
            break
    lines = [ln.strip() for ln in cleaned.replace("\r", "").split("\n") if ln.strip()]
    lines = [ln for ln in lines if not ln.startswith("#") and not ln.startswith("|")]
    lines = [ln for ln in lines if not ln.startswith("-") and not ln.startswith("*")]
    compact = " ".join(lines) if lines else cleaned
    compact = " ".join(compact.split())
    if len(compact) > max_chars:
        compact = compact[: max_chars - 1].rstrip() + "…"
    return compact


def _meal_heaviness_label(kcal: float) -> str:
    if kcal >= 700:
        return "heavy"
    if kcal >= 450:
        return "solid"
    return "light"


class VisionAgent:
    """Estimate per-meal macros from image bytes or free text."""

    def __init__(self, llm: LLMClient | None = None) -> None:
        self.llm = llm or LLMClient()

    def analyze(
        self,
        *,
        text: str | None = None,
        image_bytes: bytes | None = None,
        image_mime: str = "image/jpeg",
        language_hint: Language = Language.EN,
    ) -> MealMacros | str:
        """
        Return MealMacros or a clarifying question string if confidence < 0.5.
        """
        # Bundled placeholder images (exact fingerprint) use cached macros in DEMO_MODE;
        # text and real photos go to the LLM first, cache/heuristic only as fallback.
        image_key = detect_demo_image_key(image_bytes, None) if image_bytes else None
        if is_demo_mode() and image_key:
            cached = load_cached_macros(image_key)
            if cached:
                return cached
        text_key = detect_demo_image_key(None, text) if text else None
        demo_key = text_key if text_key != "proactive_1220" else None

        fallback = self._heuristic_text_macros(text or "", language_hint)
        if not self.llm.has_credentials():
            if fallback.confidence < 0.5:
                return self._clarify(text)
            return fallback

        user = text or "Identify the meal in the image and estimate macros."
        try:
            macros = self.llm.complete_json(
                system=SLACK_VISION_SYSTEM,
                user=user,
                schema_model=MealMacros,
                image_bytes=image_bytes,
                image_mime=image_mime,
                fallback=fallback,
            )
        except (LLMError, Exception) as exc:
            logger.warning("VisionAgent LLM failed: %s", exc)
            if is_demo_mode() and demo_key:
                cached = load_cached_macros(demo_key)
                if cached:
                    return cached
            macros = fallback

        if macros.confidence < 0.5:
            return self._clarify(text)
        return macros

    def _clarify(self, text: str | None) -> str:
        if text and any(ord(ch) > 127 for ch in text):
            return "لم أستطع تقدير الوجبة بثقة. هل يمكنك وصف الأطباق والمشروبات بوضوح أكثر؟"
        return (
            "I couldn't estimate this meal confidently. "
            "Could you describe the dishes and drinks more clearly?"
        )

    def _heuristic_text_macros(self, text: str, language_hint: Language) -> MealMacros:
        lowered = text.lower()
        lang = Language.AR if any(ord(c) > 127 for c in text) else language_hint

        has_soda = (
            "pepsi" in lowered
            or "بيبسي" in text
            or "soda" in lowered
            or "cola" in lowered
            or "كولا" in text
        )
        is_pasta = any(
            x in text or x in lowered
            for x in (
                "مكرونة",
                "مكرونه",
                "ماكرونة",
                "ماكرونه",
                "معكرونة",
                "معكرونه",
                "pasta",
                "macaroni",
                "macarona",
                "spaghetti",
                "سباغيتي",
            )
        )

        # Heavy kabsa + soda pattern (demo scenario only when kabsa is named)
        if ("kabsa" in lowered or "كبسة" in text) and has_soda:
            return MealMacros(
                kcal=980, protein_g=42, refined_carbs_g=95, fiber_g=6, fat_g=38,
                sugar_g=42, confidence=0.86, dish_names=["chicken kabsa", "Pepsi"], language=lang,
            )
        if "kabsa" in lowered or "كبسة" in text or "mandi" in lowered or "مندي" in text:
            return MealMacros(
                kcal=850, protein_g=40, refined_carbs_g=85, fiber_g=5, fat_g=32,
                sugar_g=8, confidence=0.8, dish_names=["kabsa/mandi"], language=lang,
            )
        if is_pasta:
            dishes = ["macaroni with chicken"] if ("دجاج" in text or "chicken" in lowered) else ["pasta"]
            if has_soda:
                dishes.append("Pepsi")
            return MealMacros(
                kcal=820 if has_soda else 650,
                protein_g=28,
                refined_carbs_g=85 if has_soda else 70,
                fiber_g=4,
                fat_g=22,
                sugar_g=38 if has_soda else 6,
                confidence=0.82,
                dish_names=dishes,
                language=lang,
            )
        if "shawarma" in lowered or "شاورما" in text:
            return MealMacros(
                kcal=620, protein_g=30, refined_carbs_g=48, fiber_g=4, fat_g=28,
                sugar_g=6, confidence=0.78, dish_names=["shawarma"], language=lang,
            )
        if "salad" in lowered or "سلطة" in text:
            return MealMacros(
                kcal=420, protein_g=28, refined_carbs_g=18, fiber_g=8, fat_g=16,
                sugar_g=6, confidence=0.75, dish_names=["salad"], language=lang,
            )
        if any(h in lowered or h in text for h in SAUDI_DISH_HINTS):
            return MealMacros(
                kcal=550, protein_g=24, refined_carbs_g=50, fiber_g=5, fat_g=20,
                sugar_g=10, confidence=0.62, dish_names=[text[:40] or "saudi dish"], language=lang,
            )
        if not text.strip():
            return MealMacros(
                kcal=0, protein_g=0, refined_carbs_g=0, fiber_g=0, fat_g=0,
                sugar_g=0, confidence=0.2, dish_names=[], language=lang,
            )
        return MealMacros(
            kcal=480, protein_g=18, refined_carbs_g=45, fiber_g=4, fat_g=16,
            sugar_g=12, confidence=0.45, dish_names=[text[:40]], language=lang,
        )


class ContextAgent:
    """Gather calendar / wallet / meal timing context and build assessment."""

    def __init__(self, session: Session, llm: LLMClient | None = None) -> None:
        self.session = session
        self.llm = llm or LLMClient()
        self.calendar = MockCalendar(session)

    def gather(self, user_id: str, macros: MealMacros | None, at: datetime | None = None) -> dict[str, Any]:
        """Collect calendar-first context for decisioning (work-hours clock)."""
        # Calendar is checked first against the effective agent clock (DEMO_CLOCK /
        # work-hours snap), not raw evening wall time which would hide today's meetings.
        clock = at or effective_agent_now(self.session)
        emp = get_employee(self.session, user_id)
        if emp is None:
            raise NotFoundError(user_id)
        today_events = self.calendar.get_events(user_id, clock)
        events = self.calendar.remaining_events(user_id, clock)
        free = self.calendar.find_free_slots(user_id, min_minutes=10, day=clock)
        wallet = refresh_wallet(self.session, user_id)
        return {
            "user_id": user_id,
            "department": emp.department,
            "dietary_tags": dietary_tags_of(emp),
            "language": emp.language,
            "at": clock,
            "today_events": today_events,
            "upcoming_events": events,
            "next_meeting": _format_next_meeting(events, clock),
            "free_slots": free,
            "minutes_since_meal": minutes_since_last_meal(self.session, user_id, clock),
            "minutes_since_movement": minutes_since_last_movement(self.session, user_id, clock),
            "wallet": wallet,
            "macros": macros,
        }

    def assess(self, context: dict[str, Any], demo_key: str | None = None) -> EnergyAssessment:
        """Compute deterministic score; optionally refine reasoning via LLM."""
        macros = context.get("macros")
        at: datetime = context["at"]
        events = context["upcoming_events"]

        if macros is None:
            # Proactive path: nutrition gap framing
            mins = context.get("minutes_since_meal")
            score = 55.0
            factors = ["nutrition_gap"]
            if mins is not None and mins >= 240:
                score = 70.0
                factors.append(f"minutes_since_meal={mins}")
            high_soon = [
                e for e in events
                if e.importance == Importance.HIGH
                and 60 <= (e.start - at).total_seconds() / 60 <= 90
            ]
            if high_soon:
                score = max(score, 72.0)
                factors.append("high_importance_meeting_60_90m")
            reasoning = self._proactive_reasoning(context, score)
            return EnergyAssessment(
                slump_risk=SlumpRisk.HIGH if score >= 65 else SlumpRisk.MEDIUM,
                risk_score=score,
                predicted_slump_window=f"{(at + timedelta(minutes=40)).strftime('%H:%M')}–"
                f"{(at + timedelta(minutes=100)).strftime('%H:%M')}",
                reasoning=reasoning or "Proactive wellness check.",
                key_factors=factors,
            )

        # LLM wording first; deterministic build_assessment text is the fallback.
        # (Cached demo reasoning hardcoded meeting times, so it is no longer used.)
        llm_reasoning = None
        if self.llm.has_credentials():
            try:
                base = build_assessment(macros, events, at)
                next_meeting = context.get("next_meeting") or _format_next_meeting(events, at)
                llm_reasoning = self.llm.complete_text(
                    system=SLACK_REASONING_SYSTEM,
                    user=(
                        f"risk_score={base.risk_score}/100 band={base.slump_risk.value}. "
                        f"meal≈{macros.kcal:.0f}kcal protein={macros.protein_g:.0f}g "
                        f"refined_carbs={macros.refined_carbs_g:.0f}g sugar={macros.sugar_g:.0f}g. "
                        f"agent_clock={at.strftime('%H:%M')}. "
                        f"next_meeting={next_meeting}. "
                        "Lead with whether a nearby meeting matters. "
                        "Reply in the same language as the meal dish names if Arabic, else English."
                    ),
                )
                llm_reasoning = _clip_slack_text(llm_reasoning)
            except LLMError:
                llm_reasoning = None
        return build_assessment(macros, events, at, llm_reasoning=llm_reasoning)

    def _proactive_reasoning(self, context: dict[str, Any], score: float) -> str:
        """Short proactive reason built from the real calendar + meal gap."""
        at: datetime = context["at"]
        next_meeting = context.get("next_meeting") or "none upcoming"
        mins = context.get("minutes_since_meal")
        gap = f"last meal ~{mins} min ago" if mins is not None else "no meal logged today"
        template = (
            f"Next meeting: {next_meeting}; {gap}. "
            "A light protein snack or short walk now keeps energy steady."
        )
        if not self.llm.has_credentials():
            return template
        try:
            text = self.llm.complete_text(
                system=SLACK_REASONING_SYSTEM,
                user=(
                    f"Proactive check, no new meal. risk_score={score:.0f}/100. "
                    f"agent_clock={at.strftime('%H:%M')}. next_meeting={next_meeting}. "
                    f"{gap}. Suggest steadying energy before the meeting."
                ),
            )
            return _clip_slack_text(text) or template
        except LLMError:
            return template


class DecisionAgent:
    """Pick ONE primary intervention using tools + deterministic fallback."""

    def __init__(self, session: Session, llm: LLMClient | None = None) -> None:
        self.session = session
        self.llm = llm or LLMClient()
        self.calendar = MockCalendar(session)
        self.tools_called: list[str] = []

    def decide(
        self,
        context: dict[str, Any],
        assessment: EnergyAssessment,
        *,
        proactive: bool = False,
        demo_key: str | None = None,
        user_confirmed: bool = False,
    ) -> Intervention:
        """Return a single intervention; never spends without confirmation."""
        self.tools_called = []
        # Deterministic, calendar-aware policy is the source of truth (demo_key no longer
        # short-circuits it, so decisions always reflect the real calendar + HR policy).
        intervention = self._deterministic_policy(context, assessment, proactive=proactive)
        if self.llm.has_credentials() and not is_demo_mode():
            try:
                refined = self._llm_decide(context, assessment, intervention)
                if refined is not None and self._llm_choice_valid(refined, context):
                    intervention = refined
            except (LLMError, ValueError) as exc:
                logger.warning("DecisionAgent LLM failed: %s", exc)

        if intervention.type == InterventionType.SNACK and intervention.meal_id:
            intervention.requires_confirmation = True
            if user_confirmed:
                self.queue_order(context["user_id"], intervention.meal_id, confirmed=True)
        return intervention

    def _snack_allowed(self, meal_id: str | None, context: dict[str, Any]) -> bool:
        """Catalog item exists, fits diet, wallet and HR policy."""
        if not meal_id:
            return False
        meal = self.session.get(MealCatalogRow, meal_id)
        if meal is None:
            return False
        if not meal_matches_dietary(json.loads(meal.tags_json or "[]"), context.get("dietary_tags") or []):
            return False
        wallet: WalletRow | None = context.get("wallet")
        if wallet is None or meal.price_sar > wallet.daily_remaining_sar + 1e-6:
            return False
        try:
            assert_order_policy(self.session, meal.price_sar)
        except Exception:
            return False
        return True

    def _llm_choice_valid(self, refined: Intervention, context: dict[str, Any]) -> bool:
        if refined.type == InterventionType.SNACK:
            return self._snack_allowed(refined.meal_id, context)
        return True

    def _deterministic_policy(
        self,
        context: dict[str, Any],
        assessment: EnergyAssessment,
        *,
        proactive: bool,
    ) -> Intervention:
        macros: MealMacros | None = context.get("macros")
        at: datetime = context["at"]
        events = context["upcoming_events"]

        # Just ate a heavy meal with imminent high meeting -> walk, not more food
        if macros and assessment.slump_risk == SlumpRisk.HIGH and macros.kcal >= 700:
            high = [e for e in events if e.importance == Importance.HIGH and e.start > at]
            minutes_to = (high[0].start - at).total_seconds() / 60 if high else None
            if minutes_to is not None and minutes_to <= 120:
                # Walk must finish (with a few minutes spare) before the meeting
                duration = int(max(5, min(15, minutes_to - 5)))
                return Intervention(
                    type=InterventionType.WALK,
                    rationale=f"High risk before a key meeting — {duration}-min walk beats more food.",
                    timing="now",
                    duration_min=duration,
                    requires_confirmation=False,
                )

        if macros and assessment.slump_risk == SlumpRisk.LOW:
            return Intervention(
                type=InterventionType.NONE,
                rationale="Low work-hours slump risk — no intervention needed.",
                timing="n/a",
                requires_confirmation=False,
            )

        if macros and assessment.slump_risk == SlumpRisk.MEDIUM:
            return Intervention(
                type=InterventionType.HYDRATION,
                rationale="Moderate risk — drink water or unsweetened tea, then a short stretch.",
                timing="within 15 minutes",
                duration_min=3,
                requires_confirmation=False,
            )

        # Proactive nutrition gap -> snack from catalog (if HR policy allows food)
        if proactive or macros is None:
            if not get_policy(self.session).allow_food_orders:
                return Intervention(
                    type=InterventionType.WALK,
                    rationale="Food orders are off by company policy — a 10-min walk steadies energy instead.",
                    timing="now",
                    duration_min=10,
                    requires_confirmation=False,
                )
            meal_id = self._pick_protein_snack(context)
            if meal_id:
                return Intervention(
                    type=InterventionType.SNACK,
                    rationale=(
                        "Nutrition gap before an important meeting. "
                        "Protein snack selected from curated catalog to arrive before the meeting."
                    ),
                    timing="order now for arrival before meeting",
                    meal_id=meal_id,
                    requires_confirmation=True,
                )
            return Intervention(
                type=InterventionType.MOVEMENT_BREAK,
                rationale="No snack fits the allowance, policy cap or prep window — short movement break.",
                timing="now",
                duration_min=5,
                requires_confirmation=False,
            )

        # High risk without ultra-heavy meal
        return Intervention(
            type=InterventionType.MOVEMENT_BREAK,
            rationale="Elevated risk — short movement break recommended.",
            timing="now",
            duration_min=5,
            requires_confirmation=False,
        )

    def _pick_protein_snack(self, context: dict[str, Any]) -> str | None:
        self.tools_called.append("search_meal_catalog")
        self.tools_called.append("check_wallet")
        self.tools_called.append("get_schedule")
        at: datetime = context["at"]
        events = context["upcoming_events"]
        wallet: WalletRow | None = context.get("wallet")
        policy = get_policy(self.session)
        if not policy.allow_food_orders:
            return None
        max_price = min(wallet.daily_remaining_sar if wallet else 0, policy.max_order_sar)
        # Must arrive before the next important meeting that hasn't started yet
        next_important = next(
            (
                e
                for e in sorted(events, key=lambda e: e.start)
                if e.start > at and e.importance in (Importance.HIGH, Importance.MEDIUM)
            ),
            None,
        )
        max_prep = 25
        if next_important:
            minutes_to = int((next_important.start - at).total_seconds() // 60)
            max_prep = max(5, min(max_prep, minutes_to - 15))

        meals = list(self.session.scalars(select(MealCatalogRow)).all())
        filtered = filter_catalog(
            meals,
            tags=["high_protein"],
            max_prep_minutes=max_prep,
            max_price=max_price,
            dietary_tags=context.get("dietary_tags") or [],
        )
        if not filtered:
            filtered = filter_catalog(
                meals,
                tags=["pre_meeting"],
                max_prep_minutes=max_prep,
                max_price=max_price,
                dietary_tags=context.get("dietary_tags") or [],
            )
        if not filtered:
            return None
        # Prefer light + high_protein, cheapest effective
        filtered.sort(key=lambda m: (m.price_sar, m.prep_minutes))
        return filtered[0].id

    def _llm_decide(
        self,
        context: dict[str, Any],
        assessment: EnergyAssessment,
        baseline: Intervention,
    ) -> Intervention | None:
        def handler(name: str, args: dict[str, Any]) -> Any:
            return self.dispatch_tool(name, args, context)

        tools = [
            {"name": "get_schedule", "parameters": {"user_id": "str"}},
            {"name": "find_free_slots", "parameters": {"user_id": "str", "min_minutes": "int"}},
            {
                "name": "search_meal_catalog",
                "parameters": {
                    "tags": "list",
                    "max_prep_minutes": "int",
                    "max_price": "float",
                    "dietary_tags": "list",
                },
            },
            {"name": "check_wallet", "parameters": {"user_id": "str"}},
            {"name": "book_wellness_break", "parameters": {"user_id": "str", "start": "str", "duration": "int"}},
            {"name": "log_decision", "parameters": {"summary": "str"}},
        ]
        final_raw, called = self.llm.tool_call_loop(
            system=(
                "Pick ONE primary intervention: snack|walk|movement_break|hydration|none. "
                "Prefer least costly effective option. Never invent meals. "
                "Snacks must come from search_meal_catalog. Never spend without confirmation."
            ),
            user=json.dumps(
                {
                    "assessment": assessment.model_dump(mode="json"),
                    "baseline": baseline.model_dump(mode="json"),
                    "dietary_tags": context.get("dietary_tags"),
                },
                default=str,
            ),
            tools=tools,
            tool_handler=handler,
        )
        self.tools_called.extend(called)
        data = json.loads(final_raw) if isinstance(final_raw, str) else final_raw
        if isinstance(data, str):
            data = json.loads(data)
        return Intervention.model_validate(data)

    def dispatch_tool(self, name: str, args: dict[str, Any], context: dict[str, Any]) -> Any:
        """Execute a DecisionAgent tool by name."""
        self.tools_called.append(name)
        user_id = args.get("user_id") or context["user_id"]
        if name == "get_schedule":
            return [e.model_dump(mode="json") for e in self.calendar.remaining_events(user_id)]
        if name == "find_free_slots":
            slots = self.calendar.find_free_slots(user_id, int(args.get("min_minutes", 10)))
            return [s.model_dump(mode="json") for s in slots]
        if name == "search_meal_catalog":
            meals = list(self.session.scalars(select(MealCatalogRow)).all())
            filtered = filter_catalog(
                meals,
                tags=args.get("tags"),
                max_prep_minutes=args.get("max_prep_minutes"),
                max_price=args.get("max_price"),
                dietary_tags=args.get("dietary_tags") or context.get("dietary_tags"),
            )
            return [
                {
                    "id": m.id,
                    "name_en": m.name_en,
                    "price_sar": m.price_sar,
                    "prep_minutes": m.prep_minutes,
                    "tags": json.loads(m.tags_json),
                }
                for m in filtered
            ]
        if name == "check_wallet":
            w = self.session.get(WalletRow, user_id)
            if not w:
                return None
            emp = get_employee(self.session, user_id)
            return WalletState(
                user_id=user_id,
                daily_allowance_sar=emp.daily_allowance_sar if emp else 0,
                daily_remaining_sar=w.daily_remaining_sar,
                weekly_remaining_sar=w.weekly_remaining_sar,
            ).model_dump()
        if name == "queue_order":
            # Hard block without confirmation flag in args
            return {"error": "queue_order only allowed after user confirmation"}
        if name == "book_wellness_break":
            start = datetime.fromisoformat(args["start"])
            duration = int(args.get("duration", 5))
            row = MovementBreakRow(
                id=str(uuid.uuid4()),
                user_id=user_id,
                start=start,
                duration_min=duration,
            )
            self.session.add(row)
            return {"booked": True, "start": start.isoformat(), "duration_min": duration}
        if name == "log_decision":
            write_audit(
                self.session,
                "log_decision",
                str(args.get("summary", "")),
                department=context.get("department"),
                correlation_id=get_correlation_id(),
            )
            return {"ok": True}
        return {"error": f"unknown tool {name}"}

    def queue_order(self, user_id: str, meal_id: str, *, confirmed: bool) -> str:
        """Queue an order only after explicit confirmation (used by API path)."""
        assert_confirmation(confirmed)
        meal = assert_dietary(self.session, user_id, meal_id)
        assert_allowance(self.session, user_id, meal.price_sar)
        # Actual order creation happens in services.orders — here we only validate
        return meal_id


class Orchestrator:
    """Runs Vision -> Context -> Decision and persists TraceSteps."""

    def __init__(self, session: Session, llm: LLMClient | None = None) -> None:
        self.session = session
        self.llm = llm or LLMClient()
        self.vision = VisionAgent(self.llm)
        self.context_agent = ContextAgent(session, self.llm)
        self.decision_agent = DecisionAgent(session, self.llm)

    def handle_meal(
        self,
        user_id: str,
        *,
        text: str | None = None,
        image_bytes: bytes | None = None,
        image_mime: str = "image/jpeg",
        thread_id: str | None = None,
        image_key_hint: str | None = None,
    ) -> tuple[AgentDecision, ChannelMessage]:
        """Process a meal log and return decision + channel message."""
        cid = new_correlation_id()
        trace: list[TraceStep] = []
        demo_key = image_key_hint or detect_demo_image_key(image_bytes, text)
        verify_open_interventions(self.session, effective_agent_now(self.session))

        # Vision
        t0 = time.time()
        vision_out = self.vision.analyze(text=text, image_bytes=image_bytes, image_mime=image_mime)
        self._persist_trace(
            trace,
            "VisionAgent",
            input_summary=(text or f"image:{len(image_bytes or b'')}B")[:200],
            output_summary=str(vision_out)[:500],
            tools=[],
            duration_ms=int((time.time() - t0) * 1000),
            user_id=user_id,
            correlation_id=cid,
        )
        if isinstance(vision_out, str):
            decision = AgentDecision(
                clarifying_question=vision_out,
                trace=trace,
                correlation_id=cid,
            )
            msg = ChannelMessage(text=vision_out, thread_id=thread_id, metadata={"correlation_id": cid})
            return decision, msg

        macros = vision_out
        # Context first (calendar-aware clock), then persist meal on that clock
        t1 = time.time()
        context = self.context_agent.gather(user_id, macros)
        self.session.add(
            MealLogRow(
                id=str(uuid.uuid4()),
                user_id=user_id,
                logged_at=context["at"],
                macros_json=macros.model_dump_json(),
                source="image" if image_bytes else "text",
            )
        )
        assessment = self.context_agent.assess(context, demo_key=demo_key)
        self._persist_trace(
            trace,
            "ContextAgent",
            input_summary=f"events={len(context['upcoming_events'])}",
            output_summary=f"risk={assessment.slump_risk} score={assessment.risk_score}",
            tools=["get_schedule", "find_free_slots", "check_wallet"],
            duration_ms=int((time.time() - t1) * 1000),
            user_id=user_id,
            correlation_id=cid,
        )

        # Decision
        t2 = time.time()
        intervention = self.decision_agent.decide(
            context, assessment, proactive=False, demo_key=demo_key
        )
        self._persist_trace(
            trace,
            "DecisionAgent",
            input_summary=f"risk={assessment.risk_score}",
            output_summary=f"{intervention.type}:{intervention.rationale[:120]}",
            tools=self.decision_agent.tools_called,
            duration_ms=int((time.time() - t2) * 1000),
            user_id=user_id,
            correlation_id=cid,
        )

        emp = get_employee(self.session, user_id)
        record = create_intervention(
            self.session,
            user_id=user_id,
            intervention=intervention,
            correlation_id=cid,
            risk_score=assessment.risk_score,
            proactive=False,
        )
        write_audit(
            self.session,
            "meal_decision",
            f"{intervention.type.value} for work-hours energy",
            department=emp.department if emp else None,
            correlation_id=cid,
        )
        self.session.commit()

        msg = self._to_message(
            macros, assessment, intervention, thread_id, cid, context, record=record
        )
        decision = AgentDecision(
            assessment=assessment,
            intervention=intervention,
            trace=trace,
            correlation_id=cid,
            macros=macros,
        )
        return decision, msg

    def handle_proactive(
        self,
        user_id: str,
        *,
        thread_id: str | None = None,
        demo_key: str = "proactive_1220",
    ) -> tuple[AgentDecision, ChannelMessage]:
        """Build a proactive pre-meeting suggestion."""
        cid = new_correlation_id()
        trace: list[TraceStep] = []
        verify_open_interventions(self.session, effective_agent_now(self.session))
        t0 = time.time()
        context = self.context_agent.gather(user_id, macros=None)
        assessment = self.context_agent.assess(context, demo_key=demo_key)
        self._persist_trace(
            trace,
            "ContextAgent",
            input_summary="proactive_scan",
            output_summary=f"risk={assessment.risk_score}",
            tools=["get_schedule", "check_wallet"],
            duration_ms=int((time.time() - t0) * 1000),
            user_id=user_id,
            correlation_id=cid,
        )
        t1 = time.time()
        intervention = self.decision_agent.decide(
            context, assessment, proactive=True, demo_key=demo_key
        )
        self._persist_trace(
            trace,
            "DecisionAgent",
            input_summary="proactive",
            output_summary=f"{intervention.type}",
            tools=self.decision_agent.tools_called,
            duration_ms=int((time.time() - t1) * 1000),
            user_id=user_id,
            correlation_id=cid,
        )
        emp = get_employee(self.session, user_id)
        record = create_intervention(
            self.session,
            user_id=user_id,
            intervention=intervention,
            correlation_id=cid,
            risk_score=assessment.risk_score,
            proactive=True,
        )
        write_audit(
            self.session,
            "proactive_decision",
            intervention.rationale[:200],
            department=emp.department if emp else None,
            correlation_id=cid,
        )
        self.session.commit()

        msg = self._to_message(
            None,
            assessment,
            intervention,
            thread_id,
            cid,
            context,
            proactive=True,
            record=record,
        )
        return AgentDecision(
            assessment=assessment,
            intervention=intervention,
            trace=trace,
            correlation_id=cid,
        ), msg

    def _to_message(
        self,
        macros: MealMacros | None,
        assessment: EnergyAssessment,
        intervention: Intervention,
        thread_id: str | None,
        cid: str,
        context: dict[str, Any],
        proactive: bool = False,
        record: Any | None = None,
    ) -> ChannelMessage:
        """Build a short Slack-style reply (meeting → meal load → one action)."""
        from models import InterventionRecord

        at: datetime = context["at"]
        events = context.get("upcoming_events") or []
        next_meeting = context.get("next_meeting") or _format_next_meeting(events, at)
        reason = _clip_slack_text(assessment.reasoning or "", 220)
        activity = get_activity_state(self.session, context["user_id"])

        # Calendar-first Slack layout
        lines: list[str] = [
            f"📅 Next meeting: {next_meeting}",
            f"🕒 Agent clock: {at.strftime('%H:%M')} (work-hours view)",
        ]
        if proactive:
            mins = context.get("minutes_since_meal")
            lines.append(f"⏰ Last meal ~{mins or '?'} min ago")
        if macros:
            dishes = ", ".join(macros.dish_names[:3]) or "meal"
            heavy = _meal_heaviness_label(macros.kcal)
            lines.append(
                f"🍽️ *{dishes}* · ~{macros.kcal:.0f} kcal ({heavy}, "
                f"est. {macros.confidence:.0%})"
            )
        lines.append(
            f"⚡ Risk *{assessment.slump_risk.value.upper()}* "
            f"({assessment.risk_score:.0f}/100)"
        )

        walk_id = None
        if isinstance(record, InterventionRecord) and intervention.type in (
            InterventionType.WALK,
            InterventionType.MOVEMENT_BREAK,
        ):
            goal = (record.baseline_steps or 0) + (record.target_steps or 0)
            lines.append(
                f"🚶 Steps now *{activity.steps}* → goal *{goal}* "
                f"(+{record.target_steps or 0} in ~{record.duration_min or 15} min)"
            )
            walk_id = record.id

        action = _format_action_line(intervention, self.session, record=record)
        lines.append(f"👉 {action}")
        if reason:
            lines.append(f"_{reason}_")
        lines.append("_Wellness only — not medical advice._")

        meal_url = None
        if intervention.meal_id:
            meal = self.session.get(MealCatalogRow, intervention.meal_id)
            if meal:
                meal_url = meal.url

        return build_agent_message(
            text="\n".join(lines),
            thread_id=thread_id,
            include_confirm=intervention.type == InterventionType.SNACK and bool(intervention.meal_id),
            meal_id=intervention.meal_id,
            meal_url=meal_url,
            correlation_id=cid,
            why_text=reason or intervention.rationale,
            start_walk_id=walk_id,
            intervention_id=record.id if isinstance(record, InterventionRecord) else None,
            done_id=(
                record.id
                if isinstance(record, InterventionRecord)
                and intervention.type == InterventionType.HYDRATION
                else None
            ),
        )

    def _persist_trace(
        self,
        trace: list[TraceStep],
        agent_name: str,
        *,
        input_summary: str,
        output_summary: str,
        tools: list[str],
        duration_ms: int,
        user_id: str,
        correlation_id: str,
    ) -> None:
        """Persist one pipeline TraceStep to memory and SQLite."""
        ts = TraceStep(
            agent_name=agent_name,
            input_summary=input_summary,
            output_summary=output_summary,
            tools_called=tools,
            timestamp=now(self.session),
            duration_ms=duration_ms,
            correlation_id=correlation_id,
        )
        trace.append(ts)
        emp = get_employee(self.session, user_id)
        self.session.add(
            TraceStepRow(
                id=str(uuid.uuid4()),
                correlation_id=correlation_id,
                user_id=user_id,
                department=emp.department if emp else None,
                agent_name=agent_name,
                input_summary=input_summary,
                output_summary=output_summary,
                tools_called_json=json.dumps(tools),
                timestamp=ts.timestamp,
                duration_ms=duration_ms,
            )
        )


def _format_action_line(
    intervention: Intervention,
    session: Session,
    record: Any | None = None,
) -> str:
    """One-line primary action for Slack."""
    t = intervention.type
    if t == InterventionType.WALK:
        mins = intervention.duration_min or 15
        if record is not None and getattr(record, "target_steps", None):
            goal = (record.baseline_steps or 0) + record.target_steps
            return (
                f"{mins}-min walk — reach {goal} steps "
                f"(now {record.baseline_steps})"
            )
        return f"{mins}-min walk now — best before your next meeting"
    if t == InterventionType.MOVEMENT_BREAK:
        mins = intervention.duration_min or 5
        return f"{mins}-min movement break now"
    if t == InterventionType.HYDRATION:
        return "Water or unsweetened tea + a short stretch"
    if t == InterventionType.NONE:
        return "You're fine — no intervention needed"
    if t == InterventionType.SNACK:
        if intervention.meal_id:
            meal = session.get(MealCatalogRow, intervention.meal_id)
            if meal:
                return (
                    f"Protein snack: {meal.name_en} ({meal.price_sar:.0f} SAR, "
                    f"~{meal.prep_minutes} min) — confirm to order"
                )
        return "Light protein snack before the meeting — confirm to order"
    return intervention.rationale
