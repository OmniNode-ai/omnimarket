# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The flush's contract routes both triggers to the handler and grants the runtime the tenant topic."""

from __future__ import annotations

from importlib import resources
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_tenant_event_outbox_flush_effect.handlers.store_postgres import (
    MARK_FAILED_SQL,
    MARK_PUBLISHED_SQL,
    PENDING_SQL,
)
from omnimarket.nodes.node_tenant_event_outbox_flush_effect.models.model_tenant_outbox_flush import (
    ModelTenantOutboxFlushCommand,
)

pytestmark = pytest.mark.unit

_NODE = "node_tenant_event_outbox_flush_effect"
FLUSH_REQUESTED = "onex.cmd.omnimarket.tenant-event-outbox-flush-requested.v1"
PROVISION_COMPLETED = "onex.evt.omnimarket.tenant-provision-completed.v1"
TENANT_EVENTS = "onex.tenant.events"


def _contract() -> dict[str, Any]:
    text = (
        resources.files(f"omnimarket.nodes.{_NODE}")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    loaded: dict[str, Any] = yaml.safe_load(text)
    return loaded


def test_both_triggers_route_to_the_flush_handler() -> None:
    routes = _contract()["handler_routing"]["handlers"]
    assert {(r["topic"], r["handler"]["name"]) for r in routes} == {
        (FLUSH_REQUESTED, "HandlerTenantOutboxFlush"),
        (PROVISION_COMPLETED, "HandlerTenantOutboxFlush"),
    }
    assert set(_contract()["event_bus"]["subscribe_topics"]) == {
        FLUSH_REQUESTED,
        PROVISION_COMPLETED,
    }


def test_runtime_is_granted_write_on_the_tenant_topic() -> None:
    grants = {
        (block["principal"], g["name"], g["operation"])
        for block in _contract()["broker_acl_grants"]
        for g in block["grants"]
    }
    assert ("${ONEX_ACL_RUNTIME_PRINCIPAL}", TENANT_EVENTS, "WRITE") in grants


def test_provision_completed_payload_is_accepted_as_the_signal() -> None:
    command = ModelTenantOutboxFlushCommand.model_validate(
        {"command_id": "c", "outcome": "created", "tenant_slug": "x"}
    )
    assert command.limit == 50


def test_sql_selects_only_unpublished_rows_and_stamps_only_unpublished() -> None:
    assert "published_at IS NULL" in PENDING_SQL
    assert "ORDER BY id" in PENDING_SQL
    assert "published_at IS NULL" in MARK_PUBLISHED_SQL
    assert "published_at IS NULL" in MARK_FAILED_SQL
