# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The idle proof node's contract declares its result topic and terminal event (OMN-22952)."""

from __future__ import annotations

from pathlib import Path

import yaml

import omnimarket.nodes.node_runner_stop_idle_proof_compute as node_pkg

_COMMAND = "onex.cmd.omnimarket.runner-stop-idle-proof-requested.v1"
_RESULT = "onex.evt.omnimarket.runner-stop-idle-proof-computed.v1"


def _contract() -> dict[str, object]:
    path = Path(node_pkg.__file__).parent / "contract.yaml"
    loaded = yaml.safe_load(path.read_text())
    assert isinstance(loaded, dict)
    return loaded


def test_contract_publishes_the_computed_topic() -> None:
    bus = _contract()["event_bus"]
    assert isinstance(bus, dict)
    assert bus["publish_topics"] == [_RESULT]


def test_contract_terminal_event_is_the_computed_topic() -> None:
    assert _contract()["terminal_event"] == _RESULT


def test_contract_subscribes_to_the_requested_topic() -> None:
    bus = _contract()["event_bus"]
    assert isinstance(bus, dict)
    assert bus["subscribe_topics"] == [_COMMAND]
