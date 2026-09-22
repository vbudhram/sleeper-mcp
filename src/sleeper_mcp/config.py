import json
import os
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator


class League(BaseModel):
    model_config = ConfigDict(extra="forbid")
    league_id: str = Field(pattern=r"^\d+$")
    label: str = ""
    roster_id: int | None = Field(default=None, ge=1)
    chat_enabled: bool = False


class CacheSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["normal", "cache_only", "refresh"] = "normal"
    league_settings_seconds: int = Field(default=3600, ge=0)
    league_rosters_seconds: int = Field(default=60, ge=0)
    chat_seconds: int = Field(default=300, ge=0)
    chat_history_seconds: int = Field(default=86400, ge=0)
    projections_seconds: int = Field(default=300, ge=0)


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cache: CacheSettings = Field(default_factory=CacheSettings)
    user_id: str = Field(pattern=r"^\d+$")
    timezone: str = "America/New_York"
    leagues: list[League] = Field(default_factory=list)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        ZoneInfo(value)
        return value

    @field_validator("leagues")
    @classmethod
    def unique_leagues(cls, value: list[League]) -> list[League]:
        if len({x.league_id for x in value}) != len(value):
            raise ValueError("League IDs must be unique")
        return value

    def league(self, league_id: str) -> League:
        for league in self.leagues:
            if league.league_id == league_id:
                return league
        raise ValueError("League is not configured")


def load_config() -> Config:
    return Config.model_validate(
        json.loads(Path(os.getenv("SLEEPER_CONFIG", "config.json")).read_text())
    )


def token() -> str | None:
    value = os.getenv("SLEEPER_SESSION_TOKEN", "").strip()
    return value if value and value != "REPLACE_WITH_YOUR_SESSION_TOKEN" else None
