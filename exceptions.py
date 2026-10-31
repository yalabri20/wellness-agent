"""Custom exceptions for the wellness agent."""

from __future__ import annotations


class WellnessAgentError(Exception):
    """Base error for the wellness agent stack."""


class ConfigurationError(WellnessAgentError):
    """Invalid or missing configuration."""


class LLMError(WellnessAgentError):
    """LLM provider failure or timeout."""


class ValidationFallbackError(WellnessAgentError):
    """Pydantic validation failed after retries; fallback applied."""


class GuardrailError(WellnessAgentError):
    """An action was blocked by a governance guardrail."""


class ConfirmationRequiredError(GuardrailError):
    """Spending actions require explicit human confirmation."""


class AllowanceExceededError(GuardrailError):
    """Daily or weekly allowance cap would be exceeded."""


class DietaryFilterError(GuardrailError):
    """Meal violates employee dietary tags."""


class QuietHoursError(GuardrailError):
    """Action blocked during quiet hours."""


class ProactiveCapError(GuardrailError):
    """Employee has reached the daily proactive message cap."""


class PolicyBlockedError(GuardrailError):
    """Action disabled or capped by the HR wellness policy."""


class IdempotencyError(WellnessAgentError):
    """Duplicate confirmation or order attempt."""


class NotFoundError(WellnessAgentError):
    """Requested entity was not found."""


class CatalogError(WellnessAgentError):
    """Meal catalog lookup or filter failure."""
