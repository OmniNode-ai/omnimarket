# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The run-node sender and the caller rows agree on the same two topics (OMN-19998).

Seam: the sender `onex run-node` (omnibase_core.cli.cli_run_node._resolve_node_topics, which sends the request to
event_bus.subscribe_topics[0] and reads the contract's terminal_event) <-> the caller rows HandlerBrokerAclDerive
derives over load_topic_uses (a calls role holds WRITE+DESCRIBE on the command use and READ+DESCRIBE on the terminal
use). The test fails when either side changes shape: a drifted sender topic or a drifted contract use stops matching,
a sender refusal or crash is reported instead of skipped, and a node whose contract declares no terminal_event is
refused by both sides.
Exemplar followed: test_derive_callers.py (fixture contracts, the same request builder).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from importlib import resources
from importlib.metadata import entry_points
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
    """The sender's refusal text for node (its entry point name); None when it resolves (anything else propagates)."""
    try:
        cli_run_node._resolve_node_topics(node)
    except ModelOnexError as error:
        return str(error)
    return None


def _entry_names() -> dict[str, str]:
    """Contract name -> the entry point name `onex run-node` takes, for every packaged contract with an entry point.

    The sender resolves the node's directory name through the onex.nodes entry points while a calls role and the
    derivation use the contract's own name. A contract whose directory has no entry point, or whose entry point name two
    distributions register (the sender refuses it as a duplicate), cannot be sent to at all and is left out by this
    explicit membership test, never by swallowing a failure.
    """
    counts = Counter(ep.name for ep in entry_points(group="onex.nodes"))
    registered = {name for name, count in counts.items() if count == 1}
    root = Path(str(resources.files("omnimarket.nodes")))
    names: dict[str, str] = {}
    for path in sorted(root.rglob("contract.yaml")):
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        name = loaded.get("name") if isinstance(loaded, dict) else None
        if isinstance(name, str) and path.parent.name in registered:
            names[name] = path.parent.name
    return names


def _command_and_terminal_by_node() -> dict[str, dict[str, str]]:
    """The command use topic and the terminal use topic of every callable packaged node, keyed by entry point name."""
    entries = _entry_names()
    by_node: dict[str, dict[str, str]] = {}
    for use in load_topic_uses():
        if use.node not in entries:
            continue
        row = by_node.setdefault(entries[use.node], {})
        if use.command:
            row["command"] = use.topic
        if use.terminal:
            row["terminal"] = use.topic
    return by_node


def _sender_mismatches(
    by_node: dict[str, dict[str, str]],
    resolve: Callable[[str], tuple[Path, str, str, bool]],
) -> tuple[int, list[str]]:
    """Run the sender's resolver over the command and terminal uses of every node that has both.

    Returns the number of nodes the sender resolved and a mismatch for every node whose topics
    drift from its uses, that the sender refused (ModelOnexError), or whose resolution raised
    anything else -- a resolver failure is reported, never swallowed.
    """
    compared = 0
    mismatches: list[str] = []
    for node, row in by_node.items():
        if "command" not in row or "terminal" not in row:
            continue
        try:
            resolved = resolve(node)
        except ModelOnexError as error:
            mismatches.append(f"{node}: the sender refused the node: {error}")
            continue
        except Exception as error:
            mismatches.append(
                f"{node}: the sender resolution raised {type(error).__name__}: {error}"
            )
            continue
        compared += 1
        if resolved[1] != row["command"] or resolved[2] != row["terminal"]:
            mismatches.append(
                f"{node}: sender command {resolved[1]!r} != command use {row['command']!r}, "
                f"sender terminal {resolved[2]!r} != terminal use {row['terminal']!r}"
            )
    return compared, mismatches


def test_the_sender_topics_match_the_caller_rows_for_every_packaged_node() -> None:
    """The command topic run-node sends to and the terminal it reads are the command and terminal uses the derivation reads."""
    compared, mismatches = _sender_mismatches(
        _command_and_terminal_by_node(), cli_run_node._resolve_node_topics
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


def test_a_drifted_sender_topic_is_seen_by_the_seam_check() -> None:
    """Failing control: a sender command topic that drifts from the contract's command use is reported as a mismatch."""
    real = cli_run_node._resolve_node_topics

    def drifted(name: str) -> tuple[Path, str, str, bool]:
        (path, cmd, term, inject) = real(name)
        if name == NODE:
            cmd = "onex.cmd.omnimarket.seam-drift.v1"
        return (path, cmd, term, inject)

    (_compared, mismatches) = _sender_mismatches(
        _command_and_terminal_by_node(), drifted
    )
    assert any(
        message.startswith(f"{NODE}:") and "seam-drift" in message
        for message in mismatches
    ), "the seam check must fail when the sender drifts"


def test_a_sender_refusal_is_seen_by_the_seam_check() -> None:
    """Failing control: a sender that refuses to resolve a node (ModelOnexError) is reported, not swallowed."""
    real = cli_run_node._resolve_node_topics

    def refusing(name: str) -> tuple[Path, str, str, bool]:
        if name == NODE:
            raise ModelOnexError(
                f"Node '{name}' does not declare terminal_event (seam refusal control)"
            )
        return real(name)

    (_compared, mismatches) = _sender_mismatches(
        _command_and_terminal_by_node(), refusing
    )
    assert any(message.startswith(f"{NODE}:") for message in mismatches), (
        "the seam check must fail when the sender refuses a node"
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
    entries = _entry_names()
    no_terminal = sorted(
        {entries[u.node] for u in uses if u.command and u.node in entries}
        - {entries[u.node] for u in uses if u.terminal and u.node in entries}
    )
    refusals = [_sender_refusal(node) for node in no_terminal]
    messages = [message for message in refusals if message is not None]
    assert messages, "the sender refused no packaged node for a missing terminal_event"
    assert all("terminal_event" in message for message in messages)


def test_a_sender_terminal_that_is_not_an_event_topic_is_refused_by_the_derivation(
    tmp_path: Path,
) -> None:
    """Seam: the sender reads the terminal the derivation grants a read on, so a non-event terminal is refused there.

    Failing control: the same node under an event terminal derives.
    """
    _contract(tmp_path, "node_beta_effect", terminal=CMD)
    refused = _derive(tmp_path, ("node_beta_effect",))
    assert refused.status == "refused"
    assert _CODES.CALLED_NODE_TERMINAL_NOT_AN_EVENT_TOPIC in {
        f.code for f in refused.findings
    }
    assert refused.rows == ()
    _contract(tmp_path, "node_gamma_effect", terminal=EVT)
    assert _derive(tmp_path, ("node_gamma_effect",)).status != "refused"
