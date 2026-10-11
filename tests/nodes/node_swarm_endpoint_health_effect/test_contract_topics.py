"""The health effect's declared terminal event is the topic its result is published on."""

from __future__ import annotations

from pathlib import Path

import yaml

_CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_swarm_endpoint_health_effect/contract.yaml"
)


def test_the_completed_event_is_the_terminal_event_and_a_publish_topic() -> None:
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    assert (
        contract["terminal_event"]
        == "onex.evt.omnimarket.swarm-endpoint-health-completed.v1"
    )
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.swarm-endpoint-health-completed.v1"
    ]
    assert [event["topic"] for event in contract["published_events"]] == [
        "onex.evt.omnimarket.swarm-endpoint-health-completed.v1"
    ]
