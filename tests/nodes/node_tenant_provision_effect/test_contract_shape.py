# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The contract declares what the node uses: topics, routing, grants, the identity tables and the runtime placement."""

from __future__ import annotations

from importlib import resources
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

_NODE = "node_tenant_provision_effect"
PROVISION_REQUESTED = "onex.cmd.omnimarket.tenant-provision-requested.v1"
READ_REQUESTED = "onex.cmd.omnimarket.tenant-read-requested.v1"
SUSPEND_REQUESTED = "onex.cmd.omnimarket.tenant-suspend-requested.v1"
TENANT_EVENTS = "onex.tenant.events"


def _contract() -> dict[str, Any]:
    text = (
        resources.files(f"omnimarket.nodes.{_NODE}")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    loaded: dict[str, Any] = yaml.safe_load(text)
    return loaded


def _grants() -> set[tuple[str, str, str]]:
    return {
        (block["principal"], grant["name"], grant["operation"])
        for block in _contract()["broker_acl_grants"]
        for grant in block["grants"]
    }


def test_node_type_placement_and_routing() -> None:
    contract = _contract()
    assert contract["node_type"] == "effect"
    assert contract["runtime_profiles"] == ["effects"]
    assert contract["descriptor"]["runtime_profiles"] == ["effects"]
    assert "runtime_lanes" not in contract
    routing = contract["handler_routing"]
    assert routing["routing_strategy"] == "topic_match"
    assert {(e["topic"], e["handler"]["name"]) for e in routing["handlers"]} == {
        (PROVISION_REQUESTED, "HandlerTenantProvision"),
        (READ_REQUESTED, "HandlerTenantRead"),
        (SUSPEND_REQUESTED, "HandlerTenantSuspend"),
    }


def test_topics_are_requested_in_completed_out_and_there_is_no_failed_topic() -> None:
    contract = _contract()
    completed = {v["completed"] for v in contract["topics"].values()}
    assert set(contract["event_bus"]["subscribe_topics"]) == {
        PROVISION_REQUESTED,
        READ_REQUESTED,
        SUSPEND_REQUESTED,
    }
    assert completed == {
        "onex.evt.omnimarket.tenant-provision-completed.v1",
        "onex.evt.omnimarket.tenant-read-completed.v1",
        "onex.evt.omnimarket.tenant-suspend-completed.v1",
    }
    assert set(contract["event_bus"]["publish_topics"]) == {contract["terminal_event"]}
    assert set(contract["externally_consumed_topics"]) == completed
    assert all(c.endswith("-completed.v1") for c in completed)
    every = set(contract["event_bus"]["subscribe_topics"]) | completed
    assert not [t for t in every if "failed" in t]


def test_broker_grants_follow_the_operator_plane_authority() -> None:
    contract = _contract()
    operator = "${ONEX_ACL_TENANT_OPERATOR_PRINCIPAL}"
    runtime = "${ONEX_ACL_RUNTIME_PRINCIPAL}"
    grants = _grants()
    for topics in contract["topics"].values():
        assert (operator, topics["requested"], "WRITE") in grants
        assert (operator, topics["completed"], "READ") in grants
        assert (runtime, topics["requested"], "READ") in grants
        assert (runtime, topics["completed"], "WRITE") in grants
    assert (runtime, TENANT_EVENTS, "WRITE") in grants
    writers = {(p, n) for p, n, op in grants if op == "WRITE"}
    assert {
        p
        for p, n in writers
        if n in {t["requested"] for t in contract["topics"].values()}
    } == {operator}
    assert all(p in {operator, runtime} for p, _n, _op in grants)


def test_identity_tables_are_declared_in_the_six_key_shape() -> None:
    tables = {t["name"]: t for t in _contract()["identity_io"]["db_tables"]}
    assert {n: t["access"] for n, t in tables.items()} == {
        "tenants": "read_insert_update",
        "tenant_plans": "read_insert",
        "tenant_event_outbox": "read_insert",
        "plan_tiers": "read",
        "tenant_api_keys": "read",
        "tenant_inference_credentials": "read",
    }
    for table in tables.values():
        assert set(table) == {
            "name",
            "database_ref",
            "schema",
            "migration",
            "access",
            "role",
        }
        assert table["database_ref"] == "omninode_cloud"
        assert table["schema"] == "public"
        assert table["role"] == "tenant_identity"
    assert {t["migration"] for t in tables.values()} == {
        "20251207_tenants_uuid_pk.sql",
        "20251209_m4_usage_schema.sql",
        "20251211_m4_api_keys.sql",
        "0000_create_tenant_inference_credentials.sql",
        "20260816_tenant_event_outbox.sql",
    }


def test_the_dsn_is_a_declared_secret_read_from_the_overlay() -> None:
    assert "tenant_identity.database.dsn" in _contract()["secret_dependencies"]
    example = resources.files(f"omnimarket.nodes.{_NODE}").joinpath(
        "overlay.example.yaml"
    )
    assert yaml.safe_load(example.read_text(encoding="utf-8"))["database"][
        "dsn_secret"
    ] == ("tenant_identity.database.dsn")
