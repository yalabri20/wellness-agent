"""Application settings loaded from environment / .env."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parent
SEED_DIR = ROOT_DIR / "seed"
ASSETS_DIR = ROOT_DIR / "assets"
DEMO_IMAGES_DIR = ASSETS_DIR / "demo_images"
DEMO_RESPONSES_DIR = SEED_DIR / "demo_responses"


class Settings(BaseSettings):
    """Runtime configuration for the wellness agent stack."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    demo_mode: bool = True
    demo_clock: str | None = None
    database_url: str = "sqlite:///./wellness.db"
    api_base_url: str = "http://127.0.0.1:8000"
    api_latency_ms: int = 50
    api_failure_rate: float = 0.0

    quiet_hours_start: str = "18:00"
    quiet_hours_end: str = "08:00"
    work_hours_start: str = "08:00"
    work_hours_end: str = "16:00"
    max_proactive_per_day: int = 3
    k_anonymity: int = 5
    llm_timeout_seconds: float = 8.0

    llm_provider: str = "anthropic"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-haiku-4-5-20251001"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"

    slack_bot_token: str = ""
    slack_app_token: str = ""
    slack_signing_secret: str = ""
    live_demo_slack_user_id: str = ""
    live_demo_user_id: str = "demo_user"

    # Shared secret for Apple Shortcut → POST /activity/steps (leave empty to allow local demo)
    activity_api_token: str = "wellness-demo-steps"

    simulator_port: int = 8501
    dashboard_port: int = 8502

    weekly_allowance_multiplier: float = Field(
        default=5.0,
        description="Weekly wallet cap = daily_allowance * multiplier",
    )


@lru_cache
def get_settings() -> Settings:
    """Return cached settings singleton."""
    return Settings()
