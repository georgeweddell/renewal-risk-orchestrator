"""The trifecta gate: refuse to run an agent that could be talked into leaking data.

An agent is exposed when it has all three of: access to private data, exposure
to text an outsider can write, and a way to send things out. Here the first two
are the job (CRM data, customer tickets), so the gate checks the third: no tool
the model can call may write outside this app unless a human approves it first.

It inspects the tools the model would actually be given, before every run:

  - an MCP tool the model can see must be read-only by the policy AND by the
    server's own annotation. A server saying "this writes", or saying nothing,
    makes the gate stricter. (A server can't make it looser: a tool only reaches
    the model if config/policy.yaml lists it as read.)
  - a local tool must declare its effect: none, approval (queues a write for a
    human), or local (a file in this app). Anything else, or no declaration,
    counts as an unapproved write.

This catches configuration mistakes, such as a write tool listed under `read`
in the policy, or a new local tool that posts somewhere directly, before any
untrusted text is read.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from rro.governance.gateway import ToolSpec
from rro.governance.policy import Policy

SAFE_LOCAL_EFFECTS = {"none", "approval", "local"}


class TrifectaError(RuntimeError):
    pass


@dataclass(frozen=True)
class TrifectaReport:
    private: list[str]  # model-visible tools that read company data
    untrusted: list[str]  # model-visible tools that return text outsiders can write
    unapproved_writes: list[str]  # model-visible tools that could write out with no human step

    @property
    def safe(self) -> bool:
        return not (self.untrusted and self.unapproved_writes)

    def explain(self) -> str:
        if self.safe:
            return (
                f"Trifecta gate: pass. The agent reads private data ({len(self.private)} tools) and untrusted text "
                f"({len(self.untrusted)} tools), but nothing it can call writes outside this app without a human."
            )
        return (
            "Trifecta gate: refusing to run. The agent can read text outsiders write "
            f"({', '.join(self.untrusted)}) and can write outside this app with no human approval "
            f"({', '.join(self.unapproved_writes)}). List those tools under `write` in config/policy.yaml, "
            "or route them through an approval step."
        )


def assess(specs: Iterable[ToolSpec], policy: Policy, local_effects: dict[str, str], local_names: Iterable[str]) -> TrifectaReport:
    visible = [s for s in specs if s.exposed_to_model]
    private = [s.qualified_name for s in visible]  # every connected system holds company data
    untrusted = [s.qualified_name for s in visible if policy.content_of(s.system) == "untrusted"]
    writes = [s.qualified_name for s in visible if s.scope != "read" or s.read_only_hint is not True]
    # "policy" (a payment the spend policy may approve alone) is only safe when there are spend rules:
    # then it can only pay allowlisted sellers, within budget, and sends only a CRM-sourced domain.
    safe = SAFE_LOCAL_EFFECTS | ({"policy"} if policy.spend else set())
    writes += [name for name in local_names if local_effects.get(name) not in safe]
    return TrifectaReport(private, untrusted, writes)


def check(specs: Iterable[ToolSpec], policy: Policy, local_effects: dict[str, str], local_names: Iterable[str]) -> TrifectaReport:
    report = assess(specs, policy, local_effects, local_names)
    if not report.safe:
        raise TrifectaError(report.explain())
    return report
