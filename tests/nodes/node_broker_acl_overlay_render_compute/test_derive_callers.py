# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A role's calls: the logins of a role that only sends a node its run-node request hold its reverse rows (OMN-19998).

Seam: the principals overlay's roles (overlays repository, scope broker_acl_principals) -> HandlerBrokerAclDerive. A
caller writes the node's primary command topic (event_bus.subscribe_topics[0]) and reads the contract's terminal_event,
the reverse of a runner, and holds no consumer group; a called node that declares neither topic refuses and derives
nothing. Failing control: a role that runs the same node keeps the runner's shape, so callers are not runners renamed.
Exemplar followed: test_derive_roles.py (fixture contracts, the same request builder).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.broker_acl import (
    EnumBrokerAclFindingCode,
    ModelBrokerAclDeriveRequest,
    ModelBrokerAclPrincipalsOverlay,
    ModelNodeRole,
)
from omnimarket.nodes.node_broker_acl_overlay_render_compute.handlers.handler_broker_acl_derive import (
    HandlerBrokerAclDerive,
)
from omnimarket.nodes.node_broker_acl_reconcile_effect.handlers.contract_topics import (
    load_topic_uses,
)

pytestmark = pytest.mark.unit

_CODES = EnumBrokerAclFindingCode
CMD = "onex.cmd.omnimarket.alpha-requested.v1"
EVT = "onex.evt.omnimarket.alpha-completed.v1"
LOGIN = "User:caller-login"


def _contract(
    root: Path,
    name: str,
    subscribe: list[str],
    publish: list[str],
    terminal: str | None = EVT,
) -> None:
    target = root / name
    target.mkdir(parents=True, exist_ok=True)
    document: dict[str, Any] = {
        "name": name,
        "event_bus": {"subscribe_topics": subscribe, "publish_topics": publish},
    }
    if terminal is not None:
        document["terminal_event"] = terminal
    (target / "contract.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")


def _fixture(root: Path) -> None:
    _contract(root, "node_alpha_effect", [CMD], [EVT])


def _overlay(**sections: object) -> ModelBrokerAclPrincipalsOverlay:
    return ModelBrokerAclPrincipalsOverlay.model_validate(
        {
            "scope": "broker_acl_principals",
            "ticket": "OMN-19998",
            "why": "a test",
            **sections,
        }
    )


def _derive(
    root: Path, overlay: ModelBrokerAclPrincipalsOverlay, only_node: str | None = None
):
    return HandlerBrokerAclDerive().handle(
        ModelBrokerAclDeriveRequest(
            scope="broker_acl_principals",
            topic_uses=load_topic_uses(root),
            principals=overlay,
            only_node=only_node,
        )
    )


def _role(**sections: object) -> dict[str, object]:
    return {
        "name": "senders",
        "role": "a sender on every host",
        "principals": [LOGIN],
        **sections,
    }


def test_a_caller_holds_the_reverse_rows_on_the_command_and_terminal_topics(
    tmp_path: Path,
) -> None:
    """Golden: each login of a calling role writes the command topic and reads the terminal one, and nothing else."""
    _fixture(tmp_path)
    result = _derive(tmp_path, _overlay(roles=[_role(calls=["node_alpha_effect"])]))
    assert result.status == "derived"
    assert result.findings == ()
    mine = {
        (r.resource_type.value, r.name, r.operation.value)
        for r in result.rows
        if r.principal == LOGIN
    }
    assert mine == {
        ("TOPIC", CMD, "WRITE"),
        ("TOPIC", CMD, "DESCRIBE"),
        ("TOPIC", EVT, "READ"),
        ("TOPIC", EVT, "DESCRIBE"),
    }
    assert not any(r.resource_type.value == "GROUP" for r in result.rows), (
        "a caller holds no consumer group"
    )


def test_an_only_called_node_is_known_to_the_readiness_check(tmp_path: Path) -> None:
    """A node only called is a known node: only_node derives its caller rows and raises no node_without_principal."""
    _fixture(tmp_path)
    overlay = _overlay(roles=[_role(calls=["node_alpha_effect"])])
    asked = _derive(tmp_path, overlay, "node_alpha_effect")
    assert asked.status == "derived"
    assert not any(f.code == _CODES.NODE_WITHOUT_PRINCIPAL for f in asked.findings)
    assert {r.operation.value for r in asked.rows if r.name == CMD} == {
        "WRITE",
        "DESCRIBE",
    }


def test_a_called_node_without_a_terminal_topic_refuses(tmp_path: Path) -> None:
    """Error chain: the contract names no terminal_event, so the caller's read has no topic and the derivation refuses."""
    _contract(tmp_path, "node_alpha_effect", [CMD], [EVT], terminal=None)
    result = _derive(tmp_path, _overlay(roles=[_role(calls=["node_alpha_effect"])]))
    assert result.status == "refused"
    assert _CODES.CALLED_NODE_WITHOUT_TERMINAL in {f.code for f in result.findings}
    assert result.rows == ()


def test_a_called_node_without_a_command_topic_refuses(tmp_path: Path) -> None:
    """Error chain: the contract has no subscribe topic, so the caller's write has no topic and the derivation refuses."""
    _contract(tmp_path, "node_alpha_effect", [], [EVT])
    result = _derive(tmp_path, _overlay(roles=[_role(calls=["node_alpha_effect"])]))
    assert result.status == "refused"
    assert _CODES.CALLED_NODE_WITHOUT_COMMAND in {f.code for f in result.findings}
    assert result.rows == ()


def test_a_calls_entry_without_a_contract_names_the_node(tmp_path: Path) -> None:
    """Error chain: a called node with no contract is refused with node_without_contract, like a run one."""
    _fixture(tmp_path)
    result = _derive(tmp_path, _overlay(roles=[_role(calls=["node_ghost_effect"])]))
    assert _CODES.NODE_WITHOUT_CONTRACT in {f.code for f in result.findings}
    assert result.rows == ()


def test_a_role_that_runs_the_node_keeps_the_runner_shape(tmp_path: Path) -> None:
    """Failing control: nodes= still reads the command topic and writes the terminal one; no caller direction appears."""
    _fixture(tmp_path)
    result = _derive(tmp_path, _overlay(roles=[_role(nodes=["node_alpha_effect"])]))
    assert result.status == "derived"
    assert result.findings == ()
    mine = {
        (r.resource_type.value, r.name, r.operation.value)
        for r in result.rows
        if r.principal == LOGIN
    }
    assert ("TOPIC", CMD, "READ") in mine
    assert ("TOPIC", CMD, "WRITE") not in mine
    assert ("TOPIC", EVT, "WRITE") in mine
    assert ("TOPIC", EVT, "READ") not in mine
    assert any(r.resource_type.value == "GROUP" for r in result.rows), (
        "the runner keeps its consumer group"
    )


def test_a_role_with_neither_nodes_nor_calls_is_refused() -> None:
    """The model refuses a role that declares neither side, so an empty role cannot pass as a shape."""
    with pytest.raises(ValidationError, match="declares no nodes and no calls"):
        ModelNodeRole(name="units", role="a unit", principals=(LOGIN,))


def test_a_terminal_event_outside_publish_topics_serves_a_caller_not_a_runner(
    tmp_path: Path,
) -> None:
    """A terminal_event the publish topics do not carry gives the caller its read row and the runner no write row.

    66 packaged contracts name a terminal_event outside event_bus.publish_topics: the runner's rows and the topic coverage
    check stay as they were (declared topics only), and only a caller derives a row on the undeclared terminal topic.
    """
    _contract(tmp_path, "node_alpha_effect", [CMD], [], terminal=EVT)
    called = _derive(tmp_path, _overlay(roles=[_role(calls=["node_alpha_effect"])]))
    assert ("TOPIC", EVT, "READ") in {
        (r.resource_type.value, r.name, r.operation.value) for r in called.rows
    }
    ran = _derive(tmp_path, _overlay(roles=[_role(nodes=["node_alpha_effect"])]))
    assert EVT not in {r.name for r in ran.rows}
    assert EVT not in ran.uncovered_topics


def test_a_called_node_reacting_to_an_event_topic_is_refused(tmp_path: Path) -> None:
    """Error chain: a first subscribe topic outside the command prefix would grant a write on an event topic; it refuses.

    The measured case: ci_watch subscribes to onex.evt.github.pr-status.v1, so a caller row would let six lab logins publish
    pull request status events that other consumers trust. Failing control: the same node under a command topic derives.
    """
    event_topic = "onex.evt.github.pr-status.v1"
    _contract(tmp_path, "node_alpha_effect", [event_topic], [EVT])
    refused = _derive(tmp_path, _overlay(roles=[_role(calls=["node_alpha_effect"])]))
    assert refused.status == "refused"
    assert _CODES.CALLED_NODE_COMMAND_NOT_A_COMMAND_TOPIC in {
        f.code for f in refused.findings
    }
    assert refused.rows == ()
    _contract(tmp_path, "node_alpha_effect", [CMD], [EVT])
    derived = _derive(tmp_path, _overlay(roles=[_role(calls=["node_alpha_effect"])]))
    assert derived.status == "derived"


def test_a_called_node_whose_terminal_is_not_an_event_topic_refuses(
    tmp_path: Path,
) -> None:
    """Error chain: a terminal_event outside the event prefix would grant a read on another node's topics; it refuses.

    A caller's read lands on the called node's terminal_event, so a command or snapshot terminal would let a login open
    topics no caller may read. Failing control: the same node under an event terminal still derives.
    """
    command_terminal = "onex.cmd.omnimarket.alpha-answered.v1"
    _contract(tmp_path, "node_alpha_effect", [CMD], [EVT], terminal=command_terminal)
    refused = _derive(tmp_path, _overlay(roles=[_role(calls=["node_alpha_effect"])]))
    assert refused.status == "refused"
    assert _CODES.CALLED_NODE_TERMINAL_NOT_AN_EVENT_TOPIC in {
        f.code for f in refused.findings
    }
    assert refused.rows == ()
    snapshot_terminal = "onex.snapshot.projection.alpha-state.v1"
    _contract(tmp_path, "node_alpha_effect", [CMD], [EVT], terminal=snapshot_terminal)
    snapshot_refused = _derive(
        tmp_path, _overlay(roles=[_role(calls=["node_alpha_effect"])])
    )
    assert snapshot_refused.status == "refused"
    assert _CODES.CALLED_NODE_TERMINAL_NOT_AN_EVENT_TOPIC in {
        f.code for f in snapshot_refused.findings
    }
    assert snapshot_refused.rows == ()
    _contract(tmp_path, "node_alpha_effect", [CMD], [EVT])
    derived = _derive(tmp_path, _overlay(roles=[_role(calls=["node_alpha_effect"])]))
    assert derived.status == "derived"
    assert derived.findings == ()
