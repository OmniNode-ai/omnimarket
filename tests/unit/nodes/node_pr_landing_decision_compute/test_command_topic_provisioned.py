# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing decision command topic is provisioned from consumed_events (OMN-23395)."""

from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_infra.tools.contract_topic_extractor import ContractTopicExtractor

from tests.chain.landing_chain import decision_topics

pytestmark = pytest.mark.unit

COMMAND_TOPIC = "onex.cmd.omnimarket.pr-landing-decision-requested.v1"

NODE_DIR = (
    Path(__file__).parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_pr_landing_decision_compute"
)


def _contract() -> dict[str, Any]:
    return yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))


def test_provisioner_extracts_the_command_topic() -> None:
    result = ContractTopicExtractor().extract(NODE_DIR)

    assert COMMAND_TOPIC in {entry.topic for entry in result}
    assert _contract()["runtime_dispatch"]["command_topic"] == COMMAND_TOPIC


def test_command_topic_is_the_chain_topic() -> None:
    assert decision_topics()["command"] == COMMAND_TOPIC
    assert _contract()["runtime_dispatch"]["command_topic"] == COMMAND_TOPIC


def test_declaration_adds_no_wiring() -> None:
    contract = _contract()

    assert "event_bus" not in contract
    assert {event["topic"] for event in contract["consumed_events"]} == {COMMAND_TOPIC}


def test_control_contract_without_the_declaration_provisions_nothing(
    tmp_path: Path,
) -> None:
    contract = _contract()
    contract.pop("consumed_events", None)
    node_dir = tmp_path / "node_x"
    node_dir.mkdir()
    (node_dir / "contract.yaml").write_text(yaml.safe_dump(contract), encoding="utf-8")

    result = ContractTopicExtractor().extract(node_dir)

    assert COMMAND_TOPIC not in {entry.topic for entry in result}
