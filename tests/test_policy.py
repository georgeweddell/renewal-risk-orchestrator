import pytest

from rro.governance.policy import Policy
from rro.settings import PROJECT_ROOT


@pytest.fixture
def policy() -> Policy:
    return Policy.load(PROJECT_ROOT / "config" / "policy.yaml")


def test_read_tools_are_allowed_for_the_agent(policy):
    decision = policy.authorize("crm", "search_crm_objects", {}, actor="agent")
    assert decision.allowed and decision.scope == "read"


def test_unlisted_tools_are_denied_by_default(policy):
    decision = policy.authorize("crm", "delete_everything", {}, actor="agent")
    assert not decision.allowed and decision.scope == "unlisted"


def test_the_agent_can_never_write(policy):
    decision = policy.authorize("crm", "manage_crm_objects", {}, actor="agent", approval_id="anything")
    assert not decision.allowed and decision.scope == "write"


def test_executor_needs_a_matching_approval():
    args = {"objectType": "deals", "objectId": "5014", "properties": {"renewal_risk_level": "critical"}}
    approved = {("appr-1", "crm", "manage_crm_objects")}

    def check(approval_id, system, tool, call_args):
        return (approval_id, system, tool) in approved and call_args == args

    policy = Policy({("crm", "manage_crm_objects"): "write"}, approval_check=check)
    assert not policy.authorize("crm", "manage_crm_objects", args, actor="executor").allowed
    assert not policy.authorize("crm", "manage_crm_objects", args, actor="executor", approval_id="appr-2").allowed
    tampered = args | {"properties": {"renewal_risk_level": "healthy"}}
    assert not policy.authorize("crm", "manage_crm_objects", tampered, actor="executor", approval_id="appr-1").allowed
    assert policy.authorize("crm", "manage_crm_objects", args, actor="executor", approval_id="appr-1").allowed


def test_a_tool_cannot_be_in_two_scopes(tmp_path):
    path = tmp_path / "policy.yaml"
    path.write_text("systems:\n  crm:\n    read: [x]\n    write: [x]\n")
    with pytest.raises(ValueError, match="more than one scope"):
        Policy.load(path)


def test_content_labels_default_to_untrusted(policy, tmp_path):
    assert policy.content_of("tickets") == "untrusted" and policy.content_of("memory") == "trusted"
    assert policy.content_of("a_new_system") == "untrusted"  # unlabelled means untrusted

    path = tmp_path / "policy.yaml"
    path.write_text("systems:\n  tickets:\n    content: mostly-fine\n", encoding="utf-8")
    with pytest.raises(ValueError, match="trusted' or 'untrusted"):
        Policy.load(path)
