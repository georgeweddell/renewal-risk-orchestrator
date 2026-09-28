"""Integration tests: real MCP servers over stdio, seeded mock data."""

import json
import sqlite3

import pytest

from conftest import make_gateway
from rro.risk import RiskConfig, score
from rro.seeding import load_seed
from rro.signals import collect_signals


async def test_write_tools_are_never_shown_to_the_model(settings, store):
    async with make_gateway(settings, store) as gateway:
        shown = {t["name"] for t in gateway.model_tools()}
        offered = {s.qualified_name for s in gateway.inventory()}
    assert "crm__manage_crm_objects" in offered
    assert "crm__manage_crm_objects" not in shown
    assert shown == {
        "crm__search_crm_objects", "crm__get_crm_objects", "crm__search_owners",
        "tickets__list_issues", "tickets__get_issue", "usage__get_usage_trend",
    }  # fmt: skip


async def test_denied_calls_are_refused_and_audited(settings, store):
    write_args = {"objectType": "deals", "objectId": "5014", "properties": {"renewal_risk_level": "critical"}}
    async with make_gateway(settings, store) as gateway:
        write = await gateway.call("crm__manage_crm_objects", write_args, run_id="r1")
        unknown = await gateway.call("crm__drop_all_tables", {}, run_id="r1")
        deal = await gateway.call("crm__get_crm_objects", {"objectType": "deals", "objectIds": ["5014"], "properties": ["renewal_risk_level"]}, run_id="r1")

    assert write.is_error and write.text.startswith("Denied by policy")
    assert unknown.is_error and unknown.text.startswith("Denied by policy")
    # The denied write never reached the CRM.
    assert json.loads(deal.text)["results"][0]["properties"]["renewal_risk_level"] is None

    rows = store.audit_for_run("r1")
    assert [(r["tool"], r["decision"]) for r in rows] == [
        ("manage_crm_objects", "denied"),
        ("drop_all_tables", "denied"),
        ("get_crm_objects", "allowed"),
    ]
    assert rows[0]["scope"] == "write" and "approval executor" in rows[0]["reason"]


async def test_allowed_calls_are_audited_with_timing(settings, store):
    async with make_gateway(settings, store) as gateway:
        outcome = await gateway.call("usage__get_usage_trend", {"account_id": "halcyon-robotics"}, run_id="r2")
    assert not outcome.is_error
    assert json.loads(outcome.text)["summary"]["pct_change"] == pytest.approx(-45, abs=1)
    (row,) = store.audit_for_run("r2")
    assert row["decision"] == "allowed" and row["scope"] == "read" and row["latency_ms"] is not None


async def test_server_errors_come_back_as_tool_errors(settings, store):
    async with make_gateway(settings, store) as gateway:
        outcome = await gateway.call("usage__get_usage_trend", {"account_id": "no-such-account"}, run_id="r3")
    assert outcome.is_error and "no-such-account" in outcome.text


def test_audit_log_is_append_only(store):
    store.conn.execute(
        "INSERT INTO audit_log (ts, actor, system, tool, scope, decision, args_json) VALUES ('t', 'agent', 'crm', 'x', 'read', 'allowed', '{}')"
    )
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store.conn.execute("UPDATE audit_log SET decision='denied'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store.conn.execute("DELETE FROM audit_log")


async def test_every_seeded_account_lands_in_its_designed_band(settings, store):
    config = RiskConfig.load(settings.config_dir / "risk.yaml")
    accounts = load_seed(settings.seed_file).accounts
    async with make_gateway(settings, store) as gateway:
        results = {a.slug: score((await collect_signals(gateway, a.slug)).signals, config).band for a in accounts}
    assert results == {a.slug: a.expected_band for a in accounts}
    bands = sorted(results.values())
    assert bands.count("healthy") == 3 and bands.count("at-risk") == 3 and bands.count("critical") == 2
