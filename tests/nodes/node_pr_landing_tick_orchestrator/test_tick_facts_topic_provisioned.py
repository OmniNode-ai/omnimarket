# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing tick-facts topic is provisioned, and the tick orchestrator declares it (OMN-23494).

The runtime's topic manager provisions the topics the loaded contracts declare. The tick-facts
record topic is declared in the producer's event_bus section (node_pr_landing_snapshot_effect
publish_topics), the way node_pr_landing_github_effect's topics reach the dev broker. The tick
orchestrator reads that topic through its own topic reader under its overlay's consumer group, so
it declares the topic as a consumed event and never as an event_bus subscription: a subscription
would make the runtime wire the orchestrator's handler to every record under the runtime's own
group.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_infra.tools.contract_topic_extractor import (
    ContractTopicExtractor as ProvisioningExtractor,
)
from omnibase_infra.topics.contract_topic_extractor import (
    ContractTopicExtractor as DirectionExtractor,
)

pytestmark = pytest.mark.unit

TOPIC = "onex.evt.omnimarket.pr-landing-tick-facts-recorded.v1"

NODES = Path(__file__).parents[3] / "src" / "omnimarket" / "nodes"
SNAPSHOT_DIR = NODES / "node_pr_landing_snapshot_effect"
TICK_DIR = NODES / "node_pr_landing_tick_orchestrator"


def _contract(node_dir: Path) -> dict[str, Any]:
    return yaml.safe_load((node_dir / "contract.yaml").read_text(encoding="utf-8"))


def test_provisioner_extracts_the_tick_facts_topic_from_the_snapshot_event_bus() -> (
    None
):
    producer = DirectionExtractor().extract_from_file(SNAPSHOT_DIR / "contract.yaml")

    assert TOPIC in producer.publish_topics
    assert TOPIC in {entry.topic for entry in ProvisioningExtractor().extract(TICK_DIR)}


def test_the_tick_orchestrator_declares_the_topic_as_consumed_and_wires_no_subscription() -> (
    None
):
    contract = _contract(TICK_DIR)

    assert {event["topic"] for event in contract["consumed_events"]} == {TOPIC}
    assert TOPIC not in contract["event_bus"]["subscribe_topics"]
    assert (
        TOPIC
        not in DirectionExtractor()
        .extract_from_file(TICK_DIR / "contract.yaml")
        .subscribe_topics
    )


def test_the_record_topic_is_the_snapshot_overlay_topic() -> None:
    example = yaml.safe_load(
        (SNAPSHOT_DIR / "snapshot.overlay.redpanda.example.yaml").read_text(
            encoding="utf-8"
        )
    )

    assert example["record_topic"] == TOPIC


def test_control_contract_without_the_declaration_provisions_nothing_for_the_orchestrator(
    tmp_path: Path,
) -> None:
    contract = _contract(TICK_DIR)
    contract.pop("consumed_events", None)
    node_dir = tmp_path / "node_x"
    node_dir.mkdir()
    (node_dir / "contract.yaml").write_text(yaml.safe_dump(contract), encoding="utf-8")

    assert TOPIC not in {
        entry.topic for entry in ProvisioningExtractor().extract(node_dir)
    }
