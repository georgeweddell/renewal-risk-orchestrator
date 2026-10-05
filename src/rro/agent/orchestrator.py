"""The agent loop.

A plain tool-use loop over the Messages API: send the conversation, run the
tools Claude asks for, send the results back, repeat until it's done. We own
the loop, rather than using the SDK's tool runner, so every call is routed
through the Tool Gateway and the run's state (scores, briefing, token usage)
is tracked in one place.

The message history is append-only: Claude's responses, including thinking
blocks, are passed back exactly as received.
"""

from __future__ import annotations

import asyncio
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rro.agent.llm import LLM
from rro.agent.local_tools import LocalTools
from rro.agent.prompts import SYSTEM_PROMPT
from rro.db import Store
from rro.governance.approvals import ApprovalService, ApprovalStore
from rro.governance.gateway import ToolGateway
from rro.governance.trifecta import TrifectaError, check
from rro.risk import RiskAssessment, RiskConfig
from rro.settings import Settings

NUDGE = (
    "You haven't saved the briefing yet. Call write_briefing with the complete markdown briefing, "
    "then give your short summary."
)


@dataclass
class AgentEvent:
    kind: str  # progress | text | tool_call | tool_result | fallback | nudge
    data: dict[str, Any]


EventHandler = Callable[[AgentEvent], None]


@dataclass
class RunResult:
    run_id: str
    status: str  # completed | awaiting_approval | failed
    summary: str = ""
    account_slug: str | None = None
    assessment: RiskAssessment | None = None
    briefing_path: Path | None = None
    approval_ids: list[str] = field(default_factory=list)
    error: str | None = None
    turns: int = 0
    usage: dict[str, int] = field(default_factory=dict)


class AgentError(RuntimeError):
    pass


class Orchestrator:
    def __init__(
        self,
        settings: Settings,
        gateway: ToolGateway,
        llm: LLM,
        store: Store,
        approvals: ApprovalStore,
        risk_config: RiskConfig,
        approval_service: ApprovalService | None = None,  # lets the spend policy's approvals be executed mid-run
    ):
        self.approval_service = approval_service
        self.settings = settings
        self.gateway = gateway
        self.llm = llm
        self.store = store
        self.approvals = approvals
        self.risk_config = risk_config

    async def run(self, instruction: str, on_event: EventHandler | None = None, run_id: str | None = None) -> RunResult:
        """Run the agent. Pass a run_id from Store.create_run to know the ID before the run starts (the web UI does)."""
        emit = on_event or (lambda event: None)
        run_id = run_id or self.store.create_run(instruction, self.llm.model)
        local = LocalTools(
            self.settings, self.risk_config, self.store, self.gateway, self.approvals, run_id, self.llm.model,
            service=self.approval_service,
        )  # fmt: skip
        tools = self.gateway.model_tools() + local.definitions
        today = datetime.now(UTC).date()
        # The date goes in the first user turn, not the system prompt, so the cached prefix stays identical.
        messages: list[dict[str, Any]] = [{"role": "user", "content": f"{instruction}\n\n(Today is {today:%A %d %B %Y}.)"}]
        result = RunResult(run_id=run_id, status="failed")
        usage: Counter[str] = Counter()
        nudged = False

        try:
            try:  # before the model reads anything: no path from untrusted text to an unapproved write
                check(self.gateway.inventory(), self.gateway.policy, LocalTools.effects, local.names)
            except TrifectaError as exc:
                raise AgentError(str(exc)) from exc
            for turn in range(1, self.settings.rro_max_agent_turns + 1):
                result.turns = turn
                response = await self.llm.create(system=SYSTEM_PROMPT, tools=tools, messages=messages)
                _add_usage(usage, response.usage)

                if response.stop_reason == "refusal":
                    details = getattr(response, "stop_details", None)
                    raise AgentError(f"Claude declined the request ({getattr(details, 'category', None) or 'no category'}).")

                messages.append({"role": "assistant", "content": response.content})
                self._emit_content(response.content, emit)

                tool_uses = [b for b in response.content if b.type == "tool_use"]
                if tool_uses:
                    # Independent calls in one turn run concurrently; all results go back in one message.
                    results = await asyncio.gather(*(self._execute(b, local, run_id, emit) for b in tool_uses))
                    messages.append({"role": "user", "content": list(results)})
                    continue

                if response.stop_reason == "max_tokens":
                    raise AgentError("Response hit max_tokens; raise ANTHROPIC_MAX_TOKENS.")

                if local.briefing is None and not nudged:
                    # Tool choice can't be forced on this model, so check the briefing happened and ask once.
                    nudged = True
                    emit(AgentEvent("nudge", {"message": NUDGE}))
                    messages.append({"role": "user", "content": NUDGE})
                    continue

                result.summary = "\n".join(b.text for b in response.content if b.type == "text").strip()
                break
            else:
                raise AgentError(f"Stopped after {self.settings.rro_max_agent_turns} turns without finishing.")

            if local.briefing is None:
                raise AgentError("The agent finished without writing a briefing.")
            result.account_slug, result.briefing_path = local.briefing
            result.assessment = local.assessments.get(result.account_slug)
            result.approval_ids = local.proposed
            result.status = "awaiting_approval" if local.proposed else "completed"
        except AgentError as exc:
            result.error = str(exc)
        except BaseException as exc:
            result.error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            result.usage = dict(usage)
            self.store.finish_run(
                run_id,
                status=result.status,
                account_slug=result.account_slug,
                risk_score=result.assessment.score if result.assessment else None,
                risk_band=result.assessment.band if result.assessment else None,
                briefing_path=str(result.briefing_path) if result.briefing_path else None,
                summary=result.summary or None,
                error=result.error,
                usage=result.usage,
            )
        return result

    async def _execute(self, block: Any, local: LocalTools, run_id: str, emit: EventHandler) -> dict[str, Any]:
        emit(AgentEvent("tool_call", {"id": block.id, "name": block.name, "input": block.input}))
        if block.name in local.names:
            text, is_error = await local.call(block.name, block.input)
            denied = False
        else:
            outcome = await self.gateway.call(block.name, block.input, run_id=run_id, actor="agent")
            text, is_error, denied = outcome.text, outcome.is_error, not outcome.decision.allowed
            if not denied:  # what a server returned, as opposed to the gateway's own refusal
                system = block.name.partition("__")[0]
                text = as_data(system, self.gateway.policy.content_of(system), text)
        emit(AgentEvent("tool_result", {"id": block.id, "name": block.name, "is_error": is_error, "denied": denied, "text": text}))
        result: dict[str, Any] = {"type": "tool_result", "tool_use_id": block.id, "content": text}
        if is_error:
            result["is_error"] = True
        return result

    @staticmethod
    def _emit_content(content: list[Any], emit: EventHandler) -> None:
        for block in content:
            if block.type == "thinking" and getattr(block, "thinking", ""):
                emit(AgentEvent("progress", {"text": block.thinking}))
            elif block.type == "text" and block.text.strip():
                emit(AgentEvent("text", {"text": block.text}))
            elif block.type == "fallback":
                emit(AgentEvent("fallback", {"from": block.from_.model, "to": block.to.model}))


_CLOSING_TAG = re.compile(r"<\s*/\s*tool_output", re.IGNORECASE)


def as_data(system: str, trust: str, text: str) -> str:
    """Wrap a tool result so the model reads it as data from a named source, never as instructions.

    The system prompt says what the tags mean. A result can't close the wrapper early and
    continue as if it were outside it: any closing tag inside the text is defused.
    """
    body = _CLOSING_TAG.sub("&lt;/tool_output", text)
    return f'<tool_output source="{system}" trust="{trust}">\n{body}\n</tool_output>'


def _add_usage(total: Counter[str], usage: Any) -> None:
    if usage is None:
        return
    for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
        total[key] += getattr(usage, key, None) or 0
