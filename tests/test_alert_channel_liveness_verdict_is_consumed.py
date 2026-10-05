# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The alert-channel liveness verdict has a consumer that RECORDS it.

``node_alert_channel_liveness_effect`` publishes its verdict on
``onex.evt.omnimarket.alert-channel-liveness-checked.v1`` precisely so a dead
alert channel is reported on a surface independent of that channel. Until a
subscriber records it, the verdict is declared, published and read by nobody:
a DEAD channel is exactly as invisible as before the checker existed.

An orchestrator routes and leaves nothing behind, so the assertion is for a
subscriber that declares a relation it writes (the OMN-18999 AC4 distinction).
The topic is derived from the producer's own contract, so a rename on either
side fails here rather than leaving a subscription to nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

_NODES = Path(__file__).resolve().parents[1] / "src/omnimarket/nodes"
_PRODUCER = "node_alert_channel_liveness_effect"
_CONSUMER = "node_projection_alert_channel_liveness"


def _contract(node: str) -> dict[str, Any]:
    loaded = yaml.safe_load((_NODES / node / "contract.yaml").read_text())
    assert isinstance(loaded, dict)
    return loaded


def _subscribers(topic: str) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for path in sorted(_NODES.glob("*/contract.yaml")):
        loaded = yaml.safe_load(path.read_text())
        if isinstance(loaded, dict) and topic in (loaded.get("event_bus") or {}).get(
            "subscribe_topics", []
        ):
            found[path.parent.name] = loaded
    return found


def _written_tables(contract: dict[str, Any]) -> set[str]:
    return {
        str(t.get("name"))
        for t in (contract.get("db_io") or {}).get("db_tables") or []
        if str(t.get("access", "")) in {"write", "read_write"}
    }


def test_the_checked_event_has_a_recording_subscriber() -> None:
    topic = _contract(_PRODUCER)["terminal_event"]
    subscribers = _subscribers(topic)
    assert subscribers, f"nothing subscribes to {topic}: the verdict has no consumer"
    recording = {n for n, c in subscribers.items() if _written_tables(c)}
    assert recording, (
        f"{sorted(subscribers)} subscribe to {topic} but none declares a relation "
        "to write the verdict into"
    )


def test_the_consumer_is_the_canonical_projection_node() -> None:
    topic = _contract(_PRODUCER)["terminal_event"]
    assert _CONSUMER in _subscribers(topic)
    contract = _contract(_CONSUMER)
    assert "alert_channel_liveness_verdicts" in _written_tables(contract)
    assert contract["event_bus"]["dlq_topics"], (
        "a malformed verdict must be quarantined"
    )


def test_the_consumer_is_registered_as_a_node() -> None:
    import tomllib

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    entry_points = tomllib.loads(pyproject.read_text())["project"]["entry-points"][
        "onex.nodes"
    ]
    assert entry_points.get(_CONSUMER) == f"omnimarket.nodes.{_CONSUMER}"
