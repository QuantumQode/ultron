"""Settings for Ultron, loaded from environment variables / `.env`.

Telegram and Anthropic are required: without them Ultron can't do anything.
Notion, Yahoo and iCloud are optional so the bot can run before every account
is set up; features that need them stay off until their keys are present.
"""

from datetime import date, datetime, time, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DEFAULT_ZONE = ZoneInfo("Europe/London")


def local_today(zone: ZoneInfo = DEFAULT_ZONE) -> date:
    """Today in the owner's timezone, whatever the server's clock is set to."""
    return datetime.now(zone).date()


def week_ahead(today: date) -> str:
    """'Sat 24 Oct (tomorrow), Sun 25 Oct, Mon 26 Oct, …': so "Monday" maps to a date."""
    days = [today + timedelta(days=n) for n in range(1, 8)]
    return ", ".join(
        f"{d:%a %d %b}" + (" (tomorrow)" if n == 0 else "") for n, d in enumerate(days)
    )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", env_ignore_empty=True, extra="ignore"
    )

    # --- Telegram (required) ---
    telegram_token: SecretStr
    telegram_owner_id: int  # your Telegram user ID; everyone else is ignored
    telegram_group_id: int | None = None  # the private group with Topics
    telegram_topic_chat: int | None = None  # message_thread_id of 💬 Chat
    telegram_topic_reflect: int | None = None  # message_thread_id of 🧠 Reflect
    telegram_topic_briefs: int | None = None  # message_thread_id of 📋 Briefs

    # --- Claude (required) ---
    anthropic_api_key: SecretStr
    model_fast: str = "claude-haiku-5-5"  # triage, extraction, everyday chat
    model_smart: str = "claude-sonnet-5-5"  # planning, reviews, Reflect
    monthly_cost_cap_usd: float = Field(default=20.0, gt=0)

    # --- Notion (optional until phase 2) ---
    notion_token: SecretStr | None = None
    notion_root_page_id: str | None = None

    # --- Yahoo Mail, read-only (optional until phase 3) ---
    yahoo_user: str | None = None
    yahoo_app_password: SecretStr | None = None
    email_blocklist: Annotated[list[str], NoDecode] = []  # senders/domains never fetched

    # --- iCloud Calendar (optional until phase 4) ---
    icloud_user: str | None = None
    icloud_app_password: SecretStr | None = None
    icloud_calendar_name: str = "Ultron"

    # --- Schedule ---
    tz: str = "Europe/London"
    morning_brief_time: time = time(7, 30)
    evening_review_time: time = time(21, 30)
    nightly_time: time = time(21, 0)  # "what moves the needle" + priorities for tomorrow
    nightly_nudge_minutes: int = Field(default=45, ge=5, le=120)  # one reminder, this much later
    midday_checkin: bool = True  # "how's the day going?" with Replan
    midday_checkin_time: time = time(13, 0)
    weekly_review_day: str = "sun"
    weekly_review_time: time = time(19, 0)
    quiet_hours_start: time = time(22, 30)
    quiet_hours_end: time = time(7, 0)

    # --- Storage ---
    db_path: Path = Path("data/ultron.db")
    backup_dir: Path = Path("backups")  # one dated folder per night
    backup_time: time = time(3, 30)  # before the server's 04:00 update restarts
    backup_keep_days: int = Field(default=14, ge=1)

    @field_validator("email_blocklist", mode="before")
    @classmethod
    def _split_blocklist(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip().lower() for item in value.split(",") if item.strip()]
        return value

    @field_validator("tz")
    @classmethod
    def _check_tz(cls, value: str) -> str:
        ZoneInfo(value)  # raises if the timezone name is unknown
        return value

    @field_validator("weekly_review_day")
    @classmethod
    def _check_weekday(cls, value: str) -> str:
        value = value.strip().lower()[:3]
        if value not in WEEKDAYS:
            raise ValueError(f"must be one of {', '.join(WEEKDAYS)}")
        return value

    def secret_values(self) -> list[str]:
        """Every secret set, so logs can scrub them."""
        return [
            v.get_secret_value()
            for v in self.__dict__.values()
            if isinstance(v, SecretStr) and v.get_secret_value()
        ]

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    @property
    def notion_enabled(self) -> bool:
        return bool(self.notion_token and self.notion_root_page_id)

    @property
    def mail_enabled(self) -> bool:
        return bool(self.yahoo_user and self.yahoo_app_password)

    @property
    def calendar_enabled(self) -> bool:
        return bool(self.icloud_user and self.icloud_app_password)


@lru_cache
def get_settings() -> Settings:
    """Load settings once. Raises a readable error listing any missing/invalid values."""
    return Settings()
