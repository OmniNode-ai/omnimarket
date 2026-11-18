# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The run-node sender and the caller rows agree on the same two topics (OMN-19998).

Seam: the sender `onex run-node` (omnibase_core.cli.cli_run_node._resolve_node_topics, which sends the request to
event_bus.subscribe_topics[0] and reads the contract's terminal_event) <-> the caller rows HandlerBrokerAclDerive
derives over load_topic_uses (a calls role holds WRITE+DESCRIBE on the command use and READ+DESCRIBE on the terminal
use). The test fails when either side changes shape: a drifted sender topic or a drifted contract use stops matching,
and a node whose contract declares no terminal_event is refused by both sides.
Exemplar followed: test_derive_callers.py (fixture contracts, the same request builder).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from omnibase_core.cli import cli_run_node
from omnibase_core.errors.model_onex_error import ModelOnexError

from omnimarket.models.broker_acl import (
    EnumBrokerAclFindingCode,
    ModelBrokerAclDeriveRequest,
    ModelBrokerAclPrincipalsOverlay,
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
NODE = "node_start_path_census_compute"
LOGIN = "User:seam-login"


def _contract(root: Path, name: str, terminal: str | None = EVT) -> None:
    target = root / name
    target.mkdir(parents=True, exist_ok=True)
    document: dict[str, object] = {
        "name": name,
        "event_bus": {"subscribe_topics": [CMD], "publish_topics": [EVT]},
    }
    if terminal is not None:
        document["terminal_event"] = terminal
    (target / "contract.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")


def _overlay(calls: tuple[str, ...]) -> ModelBrokerAclPrincipalsOverlay:
    return ModelBrokerAclPrincipalsOverlay.model_validate(
        {
            "scope": "broker_acl_principals",
            "ticket": "OMN-19998",
            "why": "a test",
            "roles": [
                {
                    "name": "senders",
                    "role": "a sender on every host",
                    "principals": [LOGIN],
                    "calls": calls,
                }
            ],
        }
    )


def _derive(root: Path | None, calls: tuple[str, ...], only_node: str | None = None):
    """root None reads the packaged contracts; the rows of one calls role over those uses."""
    return HandlerBrokerAclDerive().handle(
        ModelBrokerAclDeriveRequest(
            scope="broker_acl_principals",
            topic_uses=load_topic_uses(root),
            principals=_overlay(calls),
            only_node=only_node,
        )
    )


def _mine(result) -> set[tuple[str, str, str]]:
    return {
        (r.resource_type.value, r.name, r.operation.value)
        for r in result.rows
        if r.principal == LOGIN
    }


def _sender_refusal(node: str) -> str | None:
    """The sender's refusal text for node; None when it resolves or cannot be resolved for another reason."""
    try:
        cli_run_node._resolve_node_topics(node)
    except ModelOnexError as error:
        return str(error)
    except Exception:
        return None
    return None


def test_the_sender_topics_match_the_caller_rows_for_every_packaged_node() -> None:
    """The command topic run-node sends to and the terminal it reads are the command and terminal uses the derivation reads."""
    by_node: dict[str, dict[str, str]] = {}
    for use in load_topic_uses():
        row = by_node.setdefault(use.node, {})
        if use.command:
            row["command"] = use.topic
        if use.terminal:
            row["terminal"] = use.topic
    compared = 0
    mismatches: list[str] = []
    for node, row in by_node.items():
        if "command" not in row or "terminal" not in row:
            continue
        try:
            resolved = cli_run_node._resolve_node_topics(node)
        except Exception:
            continue
        compared += 1
        if resolved[1] != row["command"] or resolved[2] != row["terminal"]:
            mismatches.append(
                f"{node}: sender command {resolved[1]!r} != command use {row['command']!r}, "
                f"sender terminal {resolved[2]!r} != terminal use {row['terminal']!r}"
            )
    assert compared >= 100, (
        f"only {compared} nodes compared; an empty loop must not pass"
    )
    assert not mismatches, "first mismatches: " + "; ".join(mismatches[:5])


def test_a_caller_of_a_packaged_node_holds_the_rows_on_the_sender_topics() -> None:
    """Golden: a role that only calls a packaged node holds exactly the reverse of the topics run-node uses for it."""
    (_path, cmd, term, _) = cli_run_node._resolve_node_topics(NODE)
    result = _derive(None, (NODE,), NODE)
    assert result.status == "derived"
    assert result.findings == ()
    assert _mine(result) == {
        ("TOPIC", cmd, "WRITE"),
        ("TOPIC", cmd, "DESCRIBE"),
        ("TOPIC", term, "READ"),
        ("TOPIC", term, "DESCRIBE"),
    }
    assert not any(r.resource_type.value == "GROUP" for r in result.rows), (
        "a caller holds no consumer group"
    )


def test_a_drifted_sender_topic_is_seen_by_the_seam_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failing control: a sender command topic that drifts from the contract's command use no longer matches the caller's rows."""
    (path, cmd, term, inject) = cli_run_node._resolve_node_topics(NODE)
    monkeypatch.setattr(
        cli_run_node,
        "_resolve_node_topics",
        lambda _name: (path, "onex.cmd.omnimarket.seam-drift.v1", term, inject),
    )
    sender_cmd = cli_run_node._resolve_node_topics(NODE)[1]
    assert sender_cmd != cmd
    result = _derive(None, (NODE,), NODE)
    derived_cmds = {
        r.name
        for r in result.rows
        if r.resource_type.value == "TOPIC" and r.operation.value == "WRITE"
    }
    assert derived_cmds == {cmd}
    assert sender_cmd not in derived_cmds, (
        "the seam check must fail when the sender drifts"
    )


def test_a_caller_of_a_node_without_a_terminal_event_is_refused_like_the_sender(
    tmp_path: Path,
) -> None:
    """Error chain: the contract names no terminal_event, so the derivation refuses and the sender's resolution raises."""
    _contract(tmp_path, "node_alpha_effect", terminal=None)
    result = _derive(tmp_path, ("node_alpha_effect",))
    assert result.status == "refused"
    assert _CODES.CALLED_NODE_WITHOUT_TERMINAL in {f.code for f in result.findings}
    assert result.rows == ()
    uses = load_topic_uses()
    no_terminal = sorted(
        {u.node for u in uses if u.command} - {u.node for u in uses if u.terminal}
    )
    refusals = [_sender_refusal(node) for node in no_terminal]
    messages = [message for message in refusals if message is not None]
    assert messages, "the sender refused no packaged node for a missing terminal_event"
    assert all("terminal_event" in message for message in messages)
