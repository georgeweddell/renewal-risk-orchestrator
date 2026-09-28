"""Wires the pieces together, the same way for the CLI, the web app and tests."""

from __future__ import annotations

from dataclasses import dataclass

from mcp_servers.memory.store import MemoryStore
from rro.agent.llm import LLM
from rro.agent.orchestrator import Orchestrator
from rro.db import Store
from rro.governance.approvals import ApprovalService, ApprovalStore
from rro.governance.gateway import ToolGateway
from rro.governance.policy import Policy
from rro.risk import RiskConfig
from rro.settings import Settings


@dataclass
class Runtime:
    settings: Settings
    store: Store
    approvals: ApprovalStore
    memory: MemoryStore
    policy: Policy
    gateway: ToolGateway  # enter with `async with runtime.gateway:` before use
    risk_config: RiskConfig

    def orchestrator(self, llm: LLM) -> Orchestrator:
        return Orchestrator(self.settings, self.gateway, llm, self.store, self.approvals, self.risk_config)

    def approval_service(self) -> ApprovalService:
        return ApprovalService(self.store, self.approvals, self.memory, self.gateway)


def build_runtime(settings: Settings) -> Runtime:
    store = Store.open(settings.rro_db)
    approvals = ApprovalStore(store)
    # The policy asks the approval store whether a write was approved, so the check can't be skipped.
    policy = Policy.load(settings.config_dir / "policy.yaml", approval_check=approvals.check)
    return Runtime(
        settings=settings,
        store=store,
        approvals=approvals,
        memory=MemoryStore(settings.memory_db),
        policy=policy,
        gateway=ToolGateway(settings, policy, store),
        risk_config=RiskConfig.load(settings.config_dir / "risk.yaml"),
    )
