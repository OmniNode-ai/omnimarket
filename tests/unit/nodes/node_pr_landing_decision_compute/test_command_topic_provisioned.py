# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing decision topics are provisioned from the event_bus section (OMN-23459)."""

from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import _contract_provision_topics
from omnibase_infra.tools.contract_topic_extractor import ContractTopicExtractor

from tests.chain.landing_chain import decision_topics

pytestmark = pytest.mark.unit

COMMAND_TOPIC = "onex.cmd.omnimarket.pr-landing-decision-requested.v1"
SUCCESS_TOPIC = "onex.evt.omnimarket.pr-landing-decision-completed.v1"
FAILURE_TOPIC = "onex.evt.omnimarket.pr-landing-decision-failed.v1"

NODE_DIR = (
    Path(__file__).parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_pr_landing_decision_compute"
)

CONTRACT_PATH = NODE_DIR / "contract.yaml"


WORKFLOW = (
    Path(__file__).parents[4]
    / "src"
    / "omnimarket"
    / "contracts"
    / "landing_workflow"
    / "landing_workflow.v1.yaml"
)


def _contract() -> dict[str, Any]:
    return yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))


def test_provisioner_extracts_the_command_topic() -> None:
    result = ContractTopicExtractor().extract(NODE_DIR)

    assert COMMAND_TOPIC in {entry.topic for entry in result}
    assert _contract()["runtime_dispatch"]["command_topic"] == COMMAND_TOPIC


def test_command_topic_is_the_chain_topic() -> None:
    assert decision_topics()["command"] == COMMAND_TOPIC
    assert _contract()["runtime_dispatch"]["command_topic"] == COMMAND_TOPIC


def test_command_topic_is_the_composed_workflow_topic() -> None:
    """The landing workflow's decision step names the topic the node declares.

    The step is found by its node, not its place, so a step added beside it by
    another piece (the snapshot step) does not move this read.
    """
    steps = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["steps"]
    decision_steps = [
        step for step in steps if step["node"] == "node_pr_landing_decision_compute"
    ]

    assert [step["command_topic"] for step in decision_steps] == [COMMAND_TOPIC]


def test_event_bus_declares_the_command_subscription() -> None:
    contract = _contract()

    assert contract["event_bus"]["subscribe_topics"] == [COMMAND_TOPIC]
    assert "consumed_events" not in contract


def test_dev_runtime_provisions_all_three_topics() -> None:
    """The dev runtime creates a contract's topics only from ``event_bus``.

    The per-contract provisioning (``_contract_provision_topics``) reads the
    section the discovery builds from the real contract file; nothing else
    creates a topic while ONEX_BOOT_UNIVERSE_PROVISION=0, so a topic missing
    here never exists on the dev broker.
    """
    manifest = discover_contracts_from_paths([CONTRACT_PATH])
    assert len(manifest.contracts) == 1, (
        f"the decision contract was not discovered: {manifest.errors} {manifest.skips}"
    )

    provisioned = set(_contract_provision_topics(manifest.contracts[0]))

    assert provisioned == {COMMAND_TOPIC, SUCCESS_TOPIC, FAILURE_TOPIC}


def test_provisioner_extracts_the_failure_terminal() -> None:
    result = ContractTopicExtractor().extract(NODE_DIR)

    assert FAILURE_TOPIC in {entry.topic for entry in result}
    assert (
        _contract()["runtime_dispatch"]["terminal_events"]["failure"] == FAILURE_TOPIC
    )
    assert decision_topics()["failure"] == FAILURE_TOPIC


def test_event_bus_declares_both_terminals() -> None:
    contract = _contract()

    assert contract["event_bus"]["publish_topics"] == [SUCCESS_TOPIC, FAILURE_TOPIC]
    assert "published_events" not in contract


def test_control_the_legacy_declarations_provision_nothing(tmp_path: Path) -> None:
    """consumed_events and published_events create no topic on the dev runtime.

    The control removes the ``event_bus`` section and restores the pre-OMN-23459
    declarations the old provisioning relied on, then reads the copy through the
    same discovery and provisioning the dev runtime runs: the legacy sections are
    not read there, so the three topics would never exist without ``event_bus``.
    """
    contract = _contract()
    contract.pop("event_bus")
    contract["consumed_events"] = [
        {"event_type": "ModelLandingFacts", "topic": COMMAND_TOPIC},
    ]
    contract["published_events"] = [
        {"event_type": "LandingCoverageError", "topic": FAILURE_TOPIC},
    ]
    node_dir = tmp_path / "node_x"
    node_dir.mkdir()
    control_path = node_dir / "contract.yaml"
    control_path.write_text(yaml.safe_dump(contract), encoding="utf-8")

    manifest = discover_contracts_from_paths([control_path])
    assert len(manifest.contracts) == 1, (
        f"the control contract was not discovered: {manifest.errors} {manifest.skips}"
    )

    assert _contract_provision_topics(manifest.contracts[0]) == ()
