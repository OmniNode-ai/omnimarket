# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""node_emit_daemon's contract must be a demand `onex run-node` accepts (OMN-25030).

`onex run-node` publishes to the node's first subscribe topic and polls the node's
top-level `terminal_event`. The contract declared its terminal events only under
`runtime_dispatch.terminal_events`, so the declared demand path refused it with
"does not declare a terminal_event" and no lane could send the daemon a lifecycle
command.
"""

from __future__ import annotations

from pathlib import Path

import yaml

import omnimarket.nodes.node_emit_daemon as emit_daemon_pkg

_CONTRACT = Path(emit_daemon_pkg.__file__).parent / "contract.yaml"


def _contract() -> dict[str, object]:
    loaded = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_top_level_terminal_event_is_the_success_event() -> None:
    contract = _contract()
    terminal_event = contract.get("terminal_event")
    assert isinstance(terminal_event, str)
    assert terminal_event.strip()
    dispatch = contract["runtime_dispatch"]
    assert isinstance(dispatch, dict)
    assert terminal_event == dispatch["terminal_events"]["success"]


def test_terminal_event_is_published_and_command_topic_is_first_subscribe() -> None:
    contract = _contract()
    event_bus = contract["event_bus"]
    assert isinstance(event_bus, dict)
    assert contract["terminal_event"] in event_bus["publish_topics"]
    dispatch = contract["runtime_dispatch"]
    assert isinstance(dispatch, dict)
    assert event_bus["subscribe_topics"][0] == dispatch["command_topic"]


def test_run_node_resolver_accepts_the_contract() -> None:
    from omnibase_core.cli.cli_run_node import _resolve_node_topics

    _, command_topic, terminal_event, _ = _resolve_node_topics("node_emit_daemon")
    contract = _contract()
    assert command_topic == "onex.cmd.omnimarket.emit-daemon-lifecycle.v1"
    assert terminal_event == contract["terminal_event"]
