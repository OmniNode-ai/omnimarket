# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The readiness check: does the broker hold every row derived for one node (OMN-24005, plan M4(b)).

Seam: node_broker_acl_overlay_render_compute (derive_broker_acl_rows, only_node) -> node_broker_acl_reconcile_effect (mode
ready_check). A node that asks before it starts is told the exact rows the broker lacks instead of failing on its first
produce; an answer that cannot be read is never read as ready; the check lists and never creates.
Exemplar followed: test_derived_rows_on_reconcile.py (overlay on tmp roots, FakeRemote broker).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from pydantic import ValidationError

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
)
from tests.nodes.node_broker_acl_reconcile_effect.test_aggregates_declared_grants import (
    ENV,
    FakeRemote,
)

pytestmark = pytest.mark.unit

RENDER = "node_broker_acl_overlay_render_compute"
OTHER = "node_broker_acl_reconcile_effect"
LOGIN = "User:ready-login"
OTHER_LOGIN = "User:other-login"


def _env(
    tmp_path: Path, pairs: tuple[tuple[str, str], ...] = ((LOGIN, RENDER),)
) -> dict[str, str]:
    scope = contract()["broker_acl_reconcile"]["derive_principals_scope"]
    target = tmp_path / scope
    target.mkdir(parents=True)
    document = {
        "scope": scope,
        "ticket": "OMN-19998",
        "why": "a test scope",
        "principals": [
            {"principal": principal, "role": "a login", "nodes": [node]}
            for principal, node in pairs
        ],
    }
    (target / "overlay.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
    return {**ENV, "ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}


def _ask(
    env: dict[str, str], remote: Any, node: str = RENDER
) -> ModelBrokerAclReconcileResult:
    return HandlerBrokerAclReconcile(environ=env, remote_runner=remote).handle(
        ModelBrokerAclReconcileRequest(mode="ready_check", node=node)
    )


def _derived_live(tmp_path: Path, node: str = RENDER) -> set[driver.AclGrant]:
    """The rows the broker would hold after an apply of this node's derivation (the apply path is the oracle)."""
    remote = FakeRemote()
    HandlerBrokerAclReconcile(
        environ=_env(tmp_path / "oracle"), remote_runner=remote
    ).handle(ModelBrokerAclReconcileRequest(mode="apply"))
    return {g for g in remote.live if g.principal == LOGIN}


def test_ready_when_every_derived_row_is_live(tmp_path: Path) -> None:
    """The broker lists all of the node's derived rows: ready, nothing missing."""
    remote = FakeRemote()
    remote.live = _derived_live(tmp_path)
    result = _ask(_env(tmp_path / "ask"), remote)
    assert (result.ready, result.exit_code, result.missing) == (True, 0, 0)
    assert result.derived == len(remote.live)


def test_not_ready_names_each_missing_row(tmp_path: Path) -> None:
    """One row left off the broker: not ready and exactly that row is named."""
    remote = FakeRemote()
    live = sorted(_derived_live(tmp_path))
    left_out = live[0]
    remote.live = set(live[1:])
    result = _ask(_env(tmp_path / "ask"), remote)
    assert (result.ready, result.exit_code, result.missing) == (False, 1, 1)
    assert len(result.missing_rows) == 1
    assert left_out.name in result.missing_rows[0]
    assert left_out.operation in result.missing_rows[0]
    assert result.derived == len(live)


def test_ready_check_fails_when_a_row_is_missing_not_when_present(
    tmp_path: Path,
) -> None:
    """Failing control: the same request flips from not ready to ready when the missing row appears on the broker."""
    live = _derived_live(tmp_path)
    remote = FakeRemote()
    remote.live = set(sorted(live)[1:])
    env = _env(tmp_path / "ask")
    assert _ask(env, remote).ready is False
    remote.live = live
    assert _ask(env, remote).ready is True


def test_never_creates(tmp_path: Path) -> None:
    """Every script the remote runner receives only lists: the check reads and creates nothing."""
    remote = FakeRemote()
    result = _ask(_env(tmp_path), remote)
    assert result.ready is False
    assert remote.scripts
    assert not remote.live
    assert all(
        command[:3] == ["security", "acl", "list"] for command in remote.commands
    )


def test_node_without_principal_is_not_ready(tmp_path: Path) -> None:
    """A node no principal runs gets no rows derived and is refused naming the finding, never read as ready."""
    remote = FakeRemote()
    result = _ask(_env(tmp_path), remote, node=OTHER)
    assert (result.exit_code, result.ready) == (2, None)
    assert "node_without_principal" in result.error
    assert remote.scripts == []


def test_no_overlay_is_not_ready(tmp_path: Path) -> None:
    """With no principals overlay a dry run derives nothing and succeeds; a ready_check answers that it cannot know."""
    remote = FakeRemote()
    result = _ask(ENV, remote)
    assert (result.exit_code, result.ready) == (2, None)
    assert "no principals overlay" in result.error
    assert remote.scripts == []


def test_unreadable_listing_is_not_ready(tmp_path: Path) -> None:
    """A listing the broker path could not return is exit 2 with no answer."""

    def broken(target: str, script: str) -> str:
        raise RuntimeError("broker unreachable")

    result = _ask(_env(tmp_path), broken)
    assert (result.exit_code, result.ready) == (2, None)
    assert "broker unreachable" in result.error


def test_only_the_nodes_rows_are_asked_for(tmp_path: Path) -> None:
    """Two nodes under two logins: a check for one lists only that login's rows."""
    remote = FakeRemote()
    env = _env(tmp_path, ((LOGIN, RENDER), (OTHER_LOGIN, OTHER)))
    result = _ask(env, remote)
    assert result.ready is False
    assert all(LOGIN in script for script in remote.scripts)
    assert not any(OTHER_LOGIN in script for script in remote.scripts)


def test_request_shape() -> None:
    """node is required for ready_check and refused for every other mode."""
    with pytest.raises(ValidationError):
        ModelBrokerAclReconcileRequest(mode="ready_check")
    with pytest.raises(ValidationError):
        ModelBrokerAclReconcileRequest(mode="dry_run", node=RENDER)
    assert ModelBrokerAclReconcileRequest(mode="apply").node is None


async def _over_bus(env: dict[str, str], remote: Any) -> ModelBrokerAclReconcileResult:
    loaded = contract()
    command, terminal = (
        loaded["runtime_dispatch"]["command_topic"],
        loaded["terminal_event"],
    )
    bus = EventBusInmemory(environment="test", group="broker-acl-ready")
    await bus.start()
    done: asyncio.Queue[ModelBrokerAclReconcileResult] = asyncio.Queue()

    async def on_command(message: Any) -> None:
        result = HandlerBrokerAclReconcile(environ=env, remote_runner=remote).handle(
            ModelBrokerAclReconcileRequest.model_validate_json(message.value)
        )
        await bus.publish(terminal, None, result.model_dump_json().encode())

    async def observe(message: Any) -> None:
        await done.put(ModelBrokerAclReconcileResult.model_validate_json(message.value))

    request = ModelBrokerAclReconcileRequest(mode="ready_check", node=RENDER)
    try:
        await bus.subscribe_ungrouped(terminal, observe)
        await bus.subscribe_ungrouped(command, on_command)
        await bus.publish(command, None, request.model_dump_json().encode())
        return await asyncio.wait_for(done.get(), timeout=30)
    finally:
        await bus.close()


def test_golden_chain_ready_over_the_bus(tmp_path: Path) -> None:
    """Golden: a ready_check command in, the ready answer on the contract's terminal topic."""
    remote = FakeRemote()
    remote.live = _derived_live(tmp_path)
    result = asyncio.run(_over_bus(_env(tmp_path / "ask"), remote))
    assert (result.mode, result.ready, result.exit_code) == ("ready_check", True, 0)


def test_error_chain_unreadable_listing_over_the_bus(tmp_path: Path) -> None:
    """Error: an unreadable listing answers exit 2 with no ready answer on the same terminal topic."""

    def broken(target: str, script: str) -> str:
        raise RuntimeError("broker unreachable")

    result = asyncio.run(_over_bus(_env(tmp_path), broken))
    assert (result.exit_code, result.ready) == (2, None)


def test_ready_check_for_one_login_of_a_role(tmp_path: Path) -> None:
    """A node run under two logins is asked once per login: the login with its rows is ready, the other is named missing."""
    scope = contract()["broker_acl_reconcile"]["derive_principals_scope"]
    target = tmp_path / "roles" / scope
    target.mkdir(parents=True)
    document = {
        "scope": scope,
        "ticket": "OMN-24005",
        "why": "a test scope",
        "roles": [
            {
                "name": "units",
                "role": "a unit on two hosts",
                "nodes": [RENDER],
                "principals": [LOGIN, OTHER_LOGIN],
            }
        ],
    }
    (target / "overlay.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
    env = {**ENV, "ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "roles")}
    remote = FakeRemote()
    remote.live = _derived_live(tmp_path)  # LOGIN's rows only
    ready = HandlerBrokerAclReconcile(environ=env, remote_runner=remote).handle(
        ModelBrokerAclReconcileRequest(mode="ready_check", node=RENDER, principal=LOGIN)
    )
    lacking = HandlerBrokerAclReconcile(environ=env, remote_runner=remote).handle(
        ModelBrokerAclReconcileRequest(
            mode="ready_check", node=RENDER, principal=OTHER_LOGIN
        )
    )
    wrong = HandlerBrokerAclReconcile(environ=env, remote_runner=remote).handle(
        ModelBrokerAclReconcileRequest(
            mode="ready_check", node=RENDER, principal="User:nobody"
        )
    )
    assert (ready.ready, ready.exit_code) == (True, 0)
    assert (lacking.ready, lacking.exit_code) == (False, 1)
    assert all(OTHER_LOGIN in row for row in lacking.missing_rows)
    assert (wrong.exit_code, wrong.ready) == (2, None)
