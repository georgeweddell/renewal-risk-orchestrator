"""All configuration comes from the environment / .env. Nothing secret lives in code."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]

Backend = Literal["mock", "live"]
SYSTEMS = ("crm", "tickets", "usage")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", extra="ignore")

    # Claude
    anthropic_api_key: SecretStr | None = None
    anthropic_model: str = "claude-opus-5-5"
    anthropic_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    anthropic_max_tokens: int = 16000
    anthropic_fallbacks: bool = True
    anthropic_progress_updates: bool = True

    # Mode
    rro_mode: Backend = "mock"
    crm_backend: Backend | None = None
    tickets_backend: Backend | None = None
    usage_backend: Backend | None = None
    rro_max_agent_turns: int = 25

    # Locations (override for tests)
    rro_home: Path = PROJECT_ROOT

    def backend_for(self, system: str) -> Backend:
        return getattr(self, f"{system}_backend", None) or self.rro_mode

    @property
    def config_dir(self) -> Path:
        return self.rro_home / "config"

    @property
    def data_dir(self) -> Path:
        return self.rro_home / "data"

    @property
    def output_dir(self) -> Path:
        return self.rro_home / "output"

    @property
    def seed_file(self) -> Path:
        return self.rro_home / "seed" / "accounts.yaml"

    @property
    def rro_db(self) -> Path:
        """Orchestrator state: runs and the audit log."""
        return self.data_dir / "rro.db"

    @property
    def mock_db(self) -> Path:
        """The fake systems of record used in mock mode."""
        return self.data_dir / "mock_systems.db"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"


@lru_cache
def get_settings() -> Settings:
    return Settings()
