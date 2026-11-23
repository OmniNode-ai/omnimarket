# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26474: the node contract's declared shape (topics, routing, grants, runtime scope, dev-only block, tables)."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_tenant_api_key_effect/contract.yaml"
)
EXEMPLAR = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_branch_claim_check_effect/contract.yaml"
)
REQUESTED = {
    "onex.cmd.omnimarket.tenant-api-key-register-requested.v1",
    "onex.cmd.omnimarket.tenant-api-key-revoke-requested.v1",
}
COMPLETED = {
    "onex.evt.omnimarket.tenant-api-key-register-completed.v1",
    "onex.evt.omnimarket.tenant-api-key-revoke-completed.v1",
}


def _contract() -> dict[str, Any]:
    raw = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def test_the_contract_subscribes_to_the_requested_topics_and_publishes_only_completed() -> (
    None
):
    bus = _contract()["event_bus"]
    assert set(bus["subscribe_topics"]) == REQUESTED
    assert set(bus["publish_topics"]) == COMPLETED
    assert not any(t.endswith("-failed.v1") for t in bus["publish_topics"])


def test_topic_match_routes_each_requested_topic_to_its_own_handler() -> None:
    routing = _contract()["handler_routing"]
    assert routing["routing_strategy"] == "topic_match"
    by_topic = {h["topic"]: h for h in routing["handlers"]}
    assert set(by_topic) == REQUESTED
    for entry in by_topic.values():
        module = importlib.import_module(entry["handler"]["module"])
        assert callable(getattr(module, entry["handler"]["name"]))
        importlib.import_module(entry["event_model"].rsplit(".", 1)[0])


def test_the_operator_plane_grants_write_on_requested_and_read_on_completed() -> None:
    grants = {
        g["principal"]: {(r["name"], r["operation"]) for r in g["grants"]}
        for g in _contract()["broker_acl_grants"]
    }
    operator = grants["${ONEX_ACL_TENANT_OPERATOR_PRINCIPAL}"]
    runtime = grants["${ONEX_ACL_RUNTIME_PRINCIPAL}"]
    assert {(t, "WRITE") for t in REQUESTED} <= operator
    assert {(t, "READ") for t in COMPLETED} <= operator
    assert {(t, "READ") for t in REQUESTED} <= runtime
    assert {(t, "WRITE") for t in COMPLETED} <= runtime
    # no principal reads a requested topic for write other than the operator, and none writes a completed topic but the runtime
    for principal, rows in grants.items():
        for topic, operation in rows:
            if operation == "WRITE" and topic in REQUESTED:
                assert principal.endswith("TENANT_OPERATOR_PRINCIPAL}")
            if operation == "WRITE" and topic in COMPLETED:
                assert principal.endswith("RUNTIME_PRINCIPAL}")


def test_the_node_is_owned_by_the_main_runtime_which_carries_the_identity_dsn() -> None:
    contract = _contract()
    assert contract["runtime_profiles"] == ["main"]
    # The lane list is declared by the orchestrator's decision (see the contract's comment); the exemplar still names one.
    assert "runtime_lanes" not in contract
    assert "runtime_lanes" in yaml.safe_load(EXEMPLAR.read_text(encoding="utf-8"))


def test_the_dev_only_block_holds_only_the_allowed_lane_name() -> None:
    assert _contract()["dev_only"] == {"lane": "dev"}


def test_db_tables_use_the_six_key_shape_on_the_cloud_database() -> None:
    tables = {t["name"]: t for t in _contract()["identity_io"]["db_tables"]}
    assert set(tables) == {"tenant_api_keys", "tenants"}
    for table in tables.values():
        assert set(table) == {
            "name",
            "database_ref",
            "schema",
            "migration",
            "access",
            "role",
        }
        assert (table["database_ref"], table["schema"]) == ("omninode_cloud", "public")
    assert {name: t["role"] for name, t in tables.items()} == {
        "tenant_api_keys": "tenant_api_keys",
        "tenants": "tenants",
    }
    assert tables["tenant_api_keys"]["access"] == "read_write"
    assert tables["tenants"]["access"] == "read"
    assert tables["tenant_api_keys"]["migration"] == "20251211_m4_api_keys.sql"
    assert tables["tenants"]["migration"] == "20251207_tenants_uuid_pk.sql"
