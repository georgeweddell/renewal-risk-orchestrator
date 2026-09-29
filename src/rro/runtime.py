"""Wires the pieces together, the same way for the CLI, the web app and tests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mcp_servers.memory.store import MemoryStore
from rro.agent.llm import LLM
from rro.agent.orchestrator import Orchestrator
from rro.db import Store
from rro.governance.approvals import ApprovalService, ApprovalStore
from rro.governance.gateway import ToolGateway
from rro.governance.policy import Policy
from rro.risk import RiskConfig
from rro.settings import Settings

if TYPE_CHECKING:
    from rro.notify.slack import SlackListener, SlackNotifier


@dataclass
class Runtime:
    settings: Settings
    store: Store
    approvals: ApprovalStore
    memory: MemoryStore
    policy: Policy
    gateway: ToolGateway  # enter with `async with runtime.gateway:` before use
    risk_config: RiskConfig
    notifier: SlackNotifier | None = None  # set when Slack approvals are configured

    def orchestrator(self, llm: LLM) -> Orchestrator:
        return Orchestrator(self.settings, self.gateway, llm, self.store, self.approvals, self.risk_config)

    def approval_service(self) -> ApprovalService:
        return ApprovalService(self.store, self.approvals, self.memory, self.gateway)

    async def announce(self, run_id: str) -> list[str]:
        """Post the run's pending approvals to Slack, if Slack is configured."""
        return await self.notifier.post_pending_for_run(run_id) if self.notifier else []

    async def reflect_decision(self, approval_id: str) -> None:
        """Update the approval's Slack card after a decision made anywhere else."""
        if self.notifier:
            await self.notifier.update_card(self.approvals.get(approval_id))

    def slack_listener(self) -> SlackListener | None:
        """Receives Approve/Reject clicks from Slack. Needs the app-level token for Socket Mode."""
        if not (self.notifier and self.settings.slack_app_token):
            return None
        from rro.notify.slack import SlackApprovalHandlers, SlackListener

        handlers = SlackApprovalHandlers(self.notifier, self.approvals, self.approval_service, self.settings.slack_approver_ids)
        return SlackListener(self.settings, handlers)


def build_runtime(settings: Settings) -> Runtime:
    store = Store.open(settings.rro_db)
    approvals = ApprovalStore(store)
    # The policy asks the approval store whether a write was approved, so the check can't be skipped.
    policy = Policy.load(settings.config_dir / "policy.yaml", approval_check=approvals.check)
    notifier = None
    if settings.slack_enabled:
        from rro.notify.slack import SlackNotifier

        notifier = SlackNotifier(settings, approvals, store)
    return Runtime(
        settings=settings,
        store=store,
        approvals=approvals,
        memory=MemoryStore(settings.memory_db),
        policy=policy,
        gateway=ToolGateway(settings, policy, store),
        risk_config=RiskConfig.load(settings.config_dir / "risk.yaml"),
        notifier=notifier,
    )
