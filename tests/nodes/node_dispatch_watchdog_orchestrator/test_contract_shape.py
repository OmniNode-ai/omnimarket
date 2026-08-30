# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-shape guardrails for node_dispatch_watchdog_orchestrator [OMN-17017].

The node declared three publish topics while its Python contained zero publish
calls, and no test asserted any of them — so the false declarations were
invisible to the suite as well as to the contract-state-coverage gate
(2026-08-29 beta off-the-rails analysis rev 2, §RC-J).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

_NODE_NAME = "node_dispatch_watchdog_orchestrator"


def _contract() -> dict[str, Any]:
    path = (
        Path(__file__).resolve().parents[3]
        / "src"
        / "omnimarket"
        / "nodes"
        / _NODE_NAME
        / "contract.yaml"
    )
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


@pytest.mark.unit
def test_watchdog_terminal_event() -> None:
    assert (
        _contract()["terminal_event"]
        == "onex.evt.omnimarket.watchdog-check-completed.v1"
    )


@pytest.mark.unit
def test_watchdog_publish_topics_are_exactly_the_terminal_event() -> None:
    """OMN-17017: publish_topics is now only what something actually publishes.

    ``onex.evt.omnimarket.watchdog-stall-detected.v1`` and
    ``onex.evt.omnimarket.watchdog-task-escalated.v1`` were declared and never
    published — stall events and escalations are typed result fields. The
    terminal event is published by the runtime on the handler's behalf.
    """
    event_bus = _contract()["event_bus"]

    assert event_bus["publish_topics"] == [
        "onex.evt.omnimarket.watchdog-check-completed.v1"
    ]
    assert event_bus["subscribe_topics"] == ["onex.cmd.omnimarket.watchdog-check.v1"]
    assert event_bus["dlq_topics"] == ["onex.dlq.omnimarket.watchdog-check.v1"]


@pytest.mark.unit
def test_watchdog_contract_declares_no_deferred_handlers() -> None:
    """The handlers exist; the contract no longer says they are deferred."""
    metadata = _contract()["metadata"]

    assert "handlers_deferred_until_wave" not in metadata
    assert "implementation_wave" not in metadata
    assert _contract()["node_not_implemented"] is False
