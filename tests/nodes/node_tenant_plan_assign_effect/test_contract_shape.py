# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The contract declares the one command topic, its answer topic, the operator-plane ACL and the identity tables."""

from __future__ import annotations

from importlib import resources
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

_NODE = "node_tenant_plan_assign_effect"
REQUESTED = "onex.cmd.omnimarket.tenant-plan-assign-requested.v1"
COMPLETED = "onex.evt.omnimarket.tenant-plan-assign-completed.v1"


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


def test_routing_and_topics() -> None:
    contract = _contract()
    assert contract["node_type"] == "effect"
    assert contract["runtime_profiles"] == ["effects"]
    assert "runtime_lanes" not in contract
    (entry,) = contract["handler_routing"]["handlers"]
    assert (entry["topic"], entry["handler"]["name"]) == (
        REQUESTED,
        "HandlerTenantPlanAssign",
    )
    assert contract["event_bus"]["subscribe_topics"] == [REQUESTED]
    assert contract["event_bus"]["publish_topics"] == [COMPLETED]
    assert contract["terminal_event"] == COMPLETED
    assert {e["event_type"]: e["topic"] for e in contract["published_events"]} == {
        "ModelTenantPlanAssignResult": COMPLETED
    }


def test_only_the_operator_principal_may_write_the_command_topic() -> None:
    grants = _grants()
    operator = "${ONEX_ACL_TENANT_OPERATOR_PRINCIPAL}"
    runtime = "${ONEX_ACL_RUNTIME_PRINCIPAL}"
    assert (operator, REQUESTED, "WRITE") in grants
    assert (operator, COMPLETED, "READ") in grants
    assert (runtime, REQUESTED, "READ") in grants
    assert (runtime, COMPLETED, "WRITE") in grants
    assert {p for p, n, op in grants if op == "WRITE" and n == REQUESTED} == {operator}
    assert all(p in {operator, runtime} for p, _n, _op in grants)


def test_identity_tables_and_secret() -> None:
    contract = _contract()
    tables = {t["name"]: t["access"] for t in contract["identity_io"]["db_tables"]}
    assert tables == {
        "tenants": "read",
        "tenant_plans": "read_write",
        "plan_tiers": "read",
        "plan_entitlements": "read",
    }
    assert "tenant_identity.database.dsn" in contract["secret_dependencies"]
