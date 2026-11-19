# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seams of the reconcile mode (OMN-24566): each test fails when either side of its seam changes shape.

- contract removal scope <-> the topics of node_branch_claim_check_effect (the incident's rows) and the driver's resource types
- request model <-> result model (field sets, modes)
- driver report lines <-> the typed result's row tuples
- scoped overlay model (ModelBrokerAclScopeOverlay) <-> the declared rows the reconcile protects
- in-memory bus: command topic -> handler -> terminal topic, both read from the contract
"""

from __future__ import annotations

import typing
from importlib import resources
from pathlib import Path

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.models.broker_acl_reconcile import (
    ModelBrokerAclReconcileRequest,
    ModelBrokerAclReconcileResult,
)
from omnimarket.nodes.node_broker_acl_reconcile_effect.handlers import (
    driver_broker_acl as driver,
)
from omnimarket.nodes.node_broker_acl_reconcile_effect.handlers.handler_broker_acl_reconcile import (
    HandlerBrokerAclReconcile,
    contract,
    scope_overlay_rows,
)
from tests.nodes.node_broker_acl_reconcile_effect.test_reconcile_removes_undeclared import (
    LOGINS,
    NAMES,
    STALE,
    STALE_LINES,
    STATUS_TOPIC,
    TERMINAL_TOPIC,
    FakeBroker,
    grant,
    handle,
    make_env,
    scope_overlay,
)

pytestmark = pytest.mark.unit


def _claim_check_topics() -> set[str]:
    text = (
        resources.files("omnimarket.nodes.node_branch_claim_check_effect")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    loaded = yaml.safe_load(text)
    return {
        loaded["terminal_event"],
        *loaded["event_bus"]["subscribe_topics"],
    }


def test_seam_contract_scope_covers_the_incident_topics() -> None:
    scope = contract()["broker_acl_reconcile"]["reconcile_scope"]
    assert set(scope["resource_types"]) <= driver.RESOURCE_TYPES
    assert scope["declared_overlay_scope_prefix"]
    topics = _claim_check_topics()
    assert {STATUS_TOPIC, TERMINAL_TOPIC} <= topics, (
        "the incident topics are no longer the claim check's: rename the test's names with the contract"
    )
    for topic in topics:
        assert driver.RemovalScope.from_mapping(scope).covers(
            grant(LOGINS[0], topic, "READ")
        ), f"{topic} lies outside the contract's removal scope"


def test_seam_scope_matches_only_the_configured_namespace() -> None:
    scope = contract()["broker_acl_reconcile"]["reconcile_scope"]
    assert scope["namespaces"] == ["onex"]
    removal_scope = driver.RemovalScope.from_mapping(scope)
    assert removal_scope.covers(grant(LOGINS[0], STATUS_TOPIC, "READ"))
    assert not removal_scope.covers(grant(LOGINS[0], "onex", "READ"))
    assert not removal_scope.covers(grant(LOGINS[0], "onex-extra.topic", "READ"))


def test_seam_request_and_result_shape() -> None:
    assert set(ModelBrokerAclReconcileRequest.model_fields) == {
        "mode",
        "node",
        "principal",
        "remove_names",
        "expected_removals",
    }
    assert {
        "removed",
        "would_remove_rows",
        "removed_rows",
        "out_of_scope_rows",
        "held_rows",
    } <= set(ModelBrokerAclReconcileResult.model_fields)
    request_modes = typing.get_args(
        ModelBrokerAclReconcileRequest.model_fields["mode"].annotation
    )
    result_modes = typing.get_args(
        ModelBrokerAclReconcileResult.model_fields["mode"].annotation
    )
    assert (
        set(request_modes)
        == set(result_modes)
        == {"dry_run", "apply", "ready_check", "reconcile"}
    )


def test_seam_driver_report_lines_round_trip_into_the_typed_result(
    tmp_path: Path,
) -> None:
    collaborator = grant("User:collaborator-login", STATUS_TOPIC, "WRITE")
    held = grant(LOGINS[0], "onex.evt.other.topic.v1", "WRITE")
    outside = grant(LOGINS[0], "local.other.topic.v1", "READ")
    env = make_env(
        tmp_path, declared=[grant("User:collaborator-login", "onex.evt.c.v1", "READ")]
    )
    broker = FakeBroker([*STALE, collaborator, held, outside])
    result = handle(
        env, broker, mode="reconcile", remove_names=NAMES, expected_removals=8
    )
    assert result.exit_code == 0
    assert result.would_remove_rows == STALE_LINES
    assert set(result.removed_rows) == set(STALE_LINES)
    assert set(result.out_of_scope_rows) == {
        f"{r.principal} {r.resource_type} {r.pattern} {r.name} {r.operation}"
        for r in (collaborator, outside)
    }
    assert result.held_rows == (
        f"{held.principal} {held.resource_type} {held.pattern} {held.name} {held.operation}",
    )


def test_seam_scoped_overlay_model_to_declared_rows(tmp_path: Path) -> None:
    rows = [
        grant(LOGINS[0], "onex.evt.example.v1", "READ"),
        grant(LOGINS[1], "onex.evt.example.v1", "WRITE"),
    ]
    env = make_env(tmp_path, declared=rows)
    settings = contract()["broker_acl_reconcile"]
    read = scope_overlay_rows(settings, env)
    assert sorted(driver.AclGrant(**row) for row in read) == sorted(rows)
    assert all(
        set(row) == {"principal", "resource_type", "pattern", "name", "operation"}
        for row in read
    )
    # The document the test writes is the overlay model's own shape: an unknown key refuses the whole read.
    broken = scope_overlay(rows, scope="broker_acls_broken")
    broken["unexpected_key"] = "x"
    target = tmp_path / "broker_acls_broken"
    target.mkdir()
    (target / "overlay.yaml").write_text(yaml.safe_dump(broken), encoding="utf-8")
    with pytest.raises(ValueError, match="broker_acls_broken"):
        scope_overlay_rows(settings, env)


async def test_seam_in_memory_bus_reconcile_command_to_completed_terminal(
    tmp_path: Path, event_bus: EventBusInmemory
) -> None:
    loaded = contract()
    command = loaded["runtime_dispatch"]["command_topic"]
    terminal = loaded["terminal_event"]
    assert command in loaded["event_bus"]["subscribe_topics"]
    assert terminal in loaded["event_bus"]["publish_topics"]
    broker = FakeBroker(STALE)
    handler = HandlerBrokerAclReconcile(
        environ=make_env(tmp_path), remote_runner=broker
    )

    async def on_command(message: typing.Any) -> None:
        request = ModelBrokerAclReconcileRequest.model_validate_json(message.value)
        await event_bus.publish(
            terminal, key=None, value=handler.handle(request).model_dump_json().encode()
        )

    await event_bus.start()
    try:
        await event_bus.subscribe(
            command, on_message=on_command, group_id="test-broker-acl-reconcile-seam"
        )
        await event_bus.publish(
            command,
            key=None,
            value=ModelBrokerAclReconcileRequest(
                mode="reconcile", remove_names=NAMES, expected_removals=8
            )
            .model_dump_json()
            .encode(),
        )
        history = await event_bus.get_event_history(topic=terminal)
        assert len(history) == 1
        result = ModelBrokerAclReconcileResult.model_validate_json(history[0].value)
        assert (result.exit_code, result.removed) == (0, 8)
        assert not broker.live & set(STALE)
    finally:
        await event_bus.close()
