"""The Claude call, in one place. The orchestrator depends on the small `LLM`
protocol, so tests can drive the loop with a scripted fake instead."""

from __future__ import annotations

from typing import Any, Protocol

import anthropic

from rro.settings import Settings

FALLBACK_BETA = "server-side-fallback-2026-07-01"
PROGRESS_UPDATES_BETA = "thinking-display-updates-2026-08-18"


class LLM(Protocol):
    model: str

    async def create(self, *, system: str, tools: list[dict[str, Any]], messages: list[dict[str, Any]]) -> Any: ...


class ClaudeLLM:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.model = settings.anthropic_model
        key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else None
        # With no key in .env the SDK falls back to its own credential chain (env var, `ant auth login`).
        self.client = anthropic.AsyncAnthropic(api_key=key) if key else anthropic.AsyncAnthropic()

    async def create(self, *, system: str, tools: list[dict[str, Any]], messages: list[dict[str, Any]]) -> Any:
        s = self.settings
        betas: list[str] = []
        extra: dict[str, Any] = {}
        # Thinking is always on for this model family; effort sets how much.
        thinking: dict[str, Any] = {"type": "adaptive"}
        if s.anthropic_progress_updates:
            # Short progress notes between tool calls, shown in the run trace.
            thinking["display"] = "updates"
            betas.append(PROGRESS_UPDATES_BETA)
        if s.anthropic_fallbacks:
            # If a safety classifier declines, the API retries on its recommended fallback model.
            betas.append(FALLBACK_BETA)
            extra["fallbacks"] = "default"

        return await self.client.beta.messages.create(
            model=self.model,
            max_tokens=s.anthropic_max_tokens,
            system=system,
            tools=tools,
            messages=messages,
            thinking=thinking,
            output_config={"effort": s.anthropic_effort},
            # Cache the stable prefix (tools + system prompt) across the turns of a run.
            cache_control={"type": "ephemeral"},
            betas=betas,
            **extra,
        )
