"""All configuration comes from the environment / .env. Nothing secret lives in code."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]

Backend = Literal["mock", "live"]
SYSTEMS = ("crm", "tickets", "usage", "memory")


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
    # The CRM also has "hubspot_mcp": HubSpot's own remote MCP server instead of ours.
    crm_backend: Literal["mock", "live", "hubspot_mcp"] | None = None
    tickets_backend: Backend | None = None
    usage_backend: Backend | None = None
    rro_max_agent_turns: int = 25

    # Paid evidence over x402 (testnet only). Off by default: with it off, the evidence
    # server isn't started and nothing about a run changes.
    rro_payments_enabled: bool = False
    evidence_backend: Literal["mock", "x402"] | None = None  # default: mock in mock mode, x402 in live mode
    x402_buyer_private_key: SecretStr | None = None  # passed only to the evidence server, never logged
    # Name recorded against approvals made from the web UI or CLI (Slack records the real Slack user).
    rro_approver_name: str = "Demo approver"

    # Live systems. Passed only to the MCP server that needs each one; see config/servers.yaml.
    hubspot_access_token: SecretStr | None = None
    # HubSpot's own remote MCP server: an "MCP connector" in the developer account (see `rro hubspot-login`).
    hubspot_mcp_client_id: str | None = None
    hubspot_mcp_client_secret: SecretStr | None = None
    github_tickets_repo: str | None = None
    github_token: SecretStr | None = None
    # Seeding only (`rro seed-live`): broader than a run needs, so kept apart from the runtime tokens above.
    hubspot_seed_access_token: SecretStr | None = None
    github_seed_token: SecretStr | None = None
    posthog_host: str = "https://us.posthog.com"
    posthog_project_id: str | None = None
    posthog_project_api_key: SecretStr | None = None
    posthog_personal_api_key: SecretStr | None = None

    # Slack approvals: requests are posted to a channel; decisions come back over Socket Mode.
    slack_bot_token: SecretStr | None = None
    slack_app_token: SecretStr | None = None
    slack_approvals_channel: str | None = None
    slack_approvers: str = ""  # comma-separated Slack member IDs allowed to decide; required for Slack decisions in the channel
    # Where the web UI is reachable, for "read the briefing" links in Slack.
    rro_base_url: str = "http://127.0.0.1:8000"

    # Locations (override for tests)
    rro_home: Path = PROJECT_ROOT

    @property
    def systems(self) -> tuple[str, ...]:
        """The MCP servers to start: the four core systems, plus evidence when payments are on."""
        return (*SYSTEMS, "evidence") if self.rro_payments_enabled else SYSTEMS

    def backend_for(self, system: str) -> str:
        if system == "evidence":
            return self.evidence_backend or ("x402" if self.rro_mode == "live" else "mock")
        return getattr(self, f"{system}_backend", None) or self.rro_mode

    def value(self, name: str) -> str | None:
        """A setting by its .env name (e.g. "GITHUB_TOKEN"), with secrets unwrapped."""
        value = getattr(self, name.lower(), None)
        if isinstance(value, SecretStr):
            value = value.get_secret_value()
        return str(value) if value not in (None, "") else None

    @property
    def slack_enabled(self) -> bool:
        return bool(self.slack_bot_token and self.slack_approvals_channel)

    @property
    def slack_approver_ids(self) -> set[str]:
        return {u.strip() for u in self.slack_approvers.split(",") if u.strip()}

    @property
    def posthog_ingest_host(self) -> str:
        """PostHog receives events on a separate host: us.posthog.com -> us.i.posthog.com."""
        return self.posthog_host.rstrip("/").replace("://us.posthog.com", "://us.i.posthog.com").replace(
            "://eu.posthog.com", "://eu.i.posthog.com"
        )

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
    def memory_seed_file(self) -> Path:
        return self.rro_home / "seed" / "memory.yaml"

    @property
    def rro_db(self) -> Path:
        """Orchestrator state: runs, approvals and the audit log."""
        return self.data_dir / "rro.db"

    @property
    def mock_db(self) -> Path:
        """The fake systems of record used in mock mode."""
        return self.data_dir / "mock_systems.db"

    @property
    def memory_db(self) -> Path:
        """Past renewal decisions and their outcomes."""
        return self.data_dir / "memory.db"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"


@lru_cache
def get_settings() -> Settings:
    return Settings()
