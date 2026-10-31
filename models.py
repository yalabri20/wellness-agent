"""Pydantic v2 domain models and enums for the wellness agent."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class Importance(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class SlumpRisk(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class InterventionType(str, Enum):
    SNACK = "snack"
    WALK = "walk"
    MOVEMENT_BREAK = "movement_break"
    HYDRATION = "hydration"
    NONE = "none"


class InterventionStatus(str, Enum):
    """Closed-loop lifecycle for a single intervention."""

    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    DECLINED = "declined"
    EXPIRED = "expired"


class OrderStatus(str, Enum):
    PENDING = "pending"
    PREPARING = "preparing"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


class Language(str, Enum):
    EN = "en"
    AR = "ar"


class MealMacros(BaseModel):
    """Estimated macros for a single logged meal (not a daily budget)."""

    kcal: float = Field(ge=0)
    protein_g: float = Field(ge=0)
    refined_carbs_g: float = Field(ge=0)
    fiber_g: float = Field(ge=0)
    fat_g: float = Field(ge=0)
    sugar_g: float = Field(ge=0)
    confidence: float = Field(ge=0, le=1)
    dish_names: list[str] = Field(default_factory=list)
    language: Language = Language.EN

    @field_validator("dish_names", mode="before")
    @classmethod
    def _coerce_dishes(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return list(value)


class CalendarEvent(BaseModel):
    """Privacy-safe calendar event — no meeting titles."""

    start: datetime
    end: datetime
    importance: Importance
    attendee_count: int = Field(ge=0)


class EnergyAssessment(BaseModel):
    """Work-hours energy / slump assessment for one meal context."""

    slump_risk: SlumpRisk
    risk_score: float = Field(ge=0, le=100)
    predicted_slump_window: str
    reasoning: str
    key_factors: list[str] = Field(default_factory=list)


class Intervention(BaseModel):
    """Single primary intervention recommendation."""

    type: InterventionType
    rationale: str
    timing: str
    meal_id: str | None = None
    duration_min: int | None = None
    requires_confirmation: bool = False


class InterventionRecord(BaseModel):
    """Persisted intervention with lifecycle + walk progress."""

    id: str
    user_id: str
    department: str
    type: InterventionType
    status: InterventionStatus
    rationale: str
    correlation_id: str
    meal_id: str | None = None
    duration_min: int | None = None
    target_steps: int | None = None
    baseline_steps: int = 0
    current_steps: int = 0
    meeting_start: datetime | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    proactive: bool = False
    baseline_pending: bool = False
    calendar_event_id: str | None = None

    @property
    def goal_steps(self) -> int | None:
        if self.target_steps is None:
            return None
        return self.baseline_steps + self.target_steps


class ActivityState(BaseModel):
    """Current step count for an employee (Shortcut / manual)."""

    user_id: str
    steps: int = 0
    updated_at: datetime
    source: str = "manual"


class CalendarEventCreate(BaseModel):
    """Add a privacy-safe meeting in the demo calendar UI."""

    start: datetime
    end: datetime
    importance: Importance = Importance.MEDIUM
    attendee_count: int = Field(default=2, ge=0)
    label: str | None = Field(
        default=None,
        description="Employee-facing label only — never sent to LLM or HR",
    )


class StepsUpdateRequest(BaseModel):
    """Ingest steps from Apple Shortcut — Shortcut may send ONLY {\"steps\": N}."""

    steps: int = Field(ge=0, description="Absolute step total from Apple Health")
    # Optional extras (defaults used when Shortcut sends steps only)
    user_id: str = "demo_user"
    steps_delta: int | None = Field(default=None, ge=0)
    source: str = "shortcut"
    intervention_id: str | None = None
    token: str | None = None


class TraceStep(BaseModel):
    """One persisted agent pipeline step."""

    agent_name: str
    input_summary: str
    output_summary: str
    tools_called: list[str] = Field(default_factory=list)
    timestamp: datetime
    duration_ms: int = Field(ge=0)
    correlation_id: str | None = None


class AgentDecision(BaseModel):
    """Full orchestrator output for one request."""

    assessment: EnergyAssessment | None = None
    intervention: Intervention | None = None
    trace: list[TraceStep] = Field(default_factory=list)
    clarifying_question: str | None = None
    correlation_id: str | None = None
    macros: MealMacros | None = None


class Order(BaseModel):
    id: str
    user_id: str
    meal_id: str
    status: OrderStatus
    price_sar: float
    created_at: datetime
    eta_minutes: int | None = None
    confirmed: bool = False


class WalletState(BaseModel):
    user_id: str
    daily_allowance_sar: float
    daily_remaining_sar: float
    weekly_remaining_sar: float
    currency: str = "SAR"


class AuditLogEntry(BaseModel):
    id: str
    action: str
    detail: str
    timestamp: datetime
    department: str | None = None
    correlation_id: str | None = None


class MessageButton(BaseModel):
    """Block-like interactive button for channel rendering."""

    action_id: str
    label: str
    style: str = "default"  # default | primary | danger
    url: str | None = None
    value: str | None = None


class ChannelMessage(BaseModel):
    """Channel-agnostic agent reply."""

    text: str
    buttons: list[MessageButton] = Field(default_factory=list)
    thread_id: str | None = None
    blocks: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class FreeSlot(BaseModel):
    start: datetime
    end: datetime
    duration_min: int


class CatalogMeal(BaseModel):
    id: str
    name_ar: str
    name_en: str
    restaurant: str
    price_sar: float
    est_macros: dict[str, float]
    tags: list[str]
    prep_minutes: int
    url: str


class Employee(BaseModel):
    id: str
    department: str
    daily_allowance_sar: float
    dietary_tags: list[str] = Field(default_factory=list)
    opted_in: bool = True
    language: Language = Language.EN
    live_demo_user: bool = False


class ConfirmOrderRequest(BaseModel):
    user_id: str
    meal_id: str
    confirmation_token: str | None = None
    correlation_id: str | None = None


class ProactiveScanResult(BaseModel):
    messages_sent: int
    scanned: int
    skipped_quiet_hours: int
    skipped_cap: int
    details: list[dict[str, Any]] = Field(default_factory=list)
