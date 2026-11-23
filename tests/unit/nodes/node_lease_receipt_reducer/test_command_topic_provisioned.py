# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lease receipt topics are provisioned from the event_bus section (seam L6, OMN-24484).

The dev runtime creates a contract's topics only from ``event_bus`` (OMN-23459). The three topics are
read from the contract through the same discovery and provisioning the dev runtime runs, and each is
five segments in the platform's ``onex.<kind>.<service>.<event>.v<n>`` form. The control removes the
section and shows nothing is provisioned.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import _contract_provision_topics
from omnibase_infra.tools.contract_topic_extractor import ContractTopicExtractor

pytestmark = pytest.mark.unit

COMMAND_TOPIC = "onex.cmd.omnimarket.lease-receipt-requested.v1"
RECORDED_TOPIC = "onex.evt.omnimarket.lease-receipt-recorded.v1"
REFUSED_TOPIC = "onex.evt.omnimarket.lease-receipt-refused.v1"

NODE_DIR = (
    Path(__file__).parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_lease_receipt_reducer"
)
CONTRACT_PATH = NODE_DIR / "contract.yaml"


def _contract() -> dict[str, Any]:
    return yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))


def test_the_contract_names_the_three_topics() -> None:
    dispatch = _contract()["runtime_dispatch"]

    assert dispatch["command_topic"] == COMMAND_TOPIC
    assert dispatch["terminal_events"] == {
        "success": RECORDED_TOPIC,
        "failure": REFUSED_TOPIC,
    }
    assert _contract()["event_bus"]["subscribe_topics"] == [COMMAND_TOPIC]
    assert _contract()["event_bus"]["publish_topics"] == [RECORDED_TOPIC, REFUSED_TOPIC]


def test_every_topic_has_five_segments() -> None:
    for topic in (COMMAND_TOPIC, RECORDED_TOPIC, REFUSED_TOPIC):
        assert len(topic.split(".")) == 5, topic


def test_the_provisioner_extracts_all_three_topics() -> None:
    topics = {entry.topic for entry in ContractTopicExtractor().extract(NODE_DIR)}

    assert {COMMAND_TOPIC, RECORDED_TOPIC, REFUSED_TOPIC} <= topics


def test_dev_runtime_provisions_all_three_topics() -> None:
    manifest = discover_contracts_from_paths([CONTRACT_PATH])
    assert len(manifest.contracts) == 1, (
        f"the lease contract was not discovered: {manifest.errors} {manifest.skips}"
    )

    assert set(_contract_provision_topics(manifest.contracts[0])) == {
        COMMAND_TOPIC,
        RECORDED_TOPIC,
        REFUSED_TOPIC,
    }


def test_control_without_the_event_bus_section_nothing_is_provisioned(
    tmp_path: Path,
) -> None:
    contract = _contract()
    contract.pop("event_bus")
    node_dir = tmp_path / "node_x"
    node_dir.mkdir()
    control_path = node_dir / "contract.yaml"
    control_path.write_text(yaml.safe_dump(contract), encoding="utf-8")

    manifest = discover_contracts_from_paths([control_path])
    assert len(manifest.contracts) == 1, f"{manifest.errors} {manifest.skips}"

    assert _contract_provision_topics(manifest.contracts[0]) == ()
