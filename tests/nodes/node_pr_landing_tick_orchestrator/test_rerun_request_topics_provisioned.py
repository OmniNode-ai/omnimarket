# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The rerun-request topics are provisioned at boot (OMN-23964).

`onex scm rerun-request` publishes its request on the tick orchestrator contract's
rerun_request.command_topic and waits for the answer on the completed or failed topic. The dev
runtime's per-contract boot provisioning creates only the topics _contract_provision_topics
returns (subscribe, publish, dlq and projection_api topics), and the command topic is declared
only under consumed_events / externally_produced_topics, so nothing creates it. The first test
fails on the current contracts for exactly that reason; the third keeps the whole class of
consumed-only command topics a listed decision instead of a silent gap.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import _contract_provision_topics

pytestmark = pytest.mark.unit

NODES = Path(__file__).parents[3] / "src" / "omnimarket" / "nodes"
TICK_DIR = NODES / "node_pr_landing_tick_orchestrator"
RERUN_COMPUTE_DIR = NODES / "node_pr_landing_rerun_request_compute"

# Adding a new consumed-only command topic is a decision someone must list here (OMN-23964):
# each name below is read by some contract without any contract's event_bus declaring it, so
# boot provisioning never creates it and whoever publishes it must arrange the topic elsewhere.
KNOWN_CONSUMED_ONLY_COMMAND_TOPICS = frozenset(
    {
        "onex.cmd.omnimarket.code-analysis.v1",
        "onex.cmd.omnimarket.declared-clone-refresh-requested.v1",
        "onex.cmd.omnimarket.document-ingestion.v1",
        "onex.cmd.omnimarket.host-disk-read-requested.v1",
        "onex.cmd.omnimarket.pattern-learning.v1",
        "onex.cmd.omnimarket.repo-state-query-requested.v1",
    }
)


def _contract(node_dir: Path) -> dict[str, Any]:
    return yaml.safe_load((node_dir / "contract.yaml").read_text(encoding="utf-8"))


def _boot_provision_topics() -> set[str]:
    """The topics the dev runtime's boot provisioning creates for the rerun pair."""
    manifest = discover_contracts_from_paths(
        [TICK_DIR / "contract.yaml", RERUN_COMPUTE_DIR / "contract.yaml"]
    )
    provisioned: set[str] = set()
    for contract in manifest.contracts:
        provisioned.update(_contract_provision_topics(contract))
    return provisioned


@pytest.mark.parametrize("role", ["command_topic", "completed_topic", "failed_topic"])
def test_every_rerun_request_topic_is_boot_provisioned(role: str) -> None:
    topic = _contract(TICK_DIR)["rerun_request"][role]

    assert topic in _boot_provision_topics()


def test_the_request_topic_is_not_a_tick_publish_or_subscribe_topic() -> None:
    contract = _contract(TICK_DIR)
    bus = contract["event_bus"]

    request_topic = contract["rerun_request"]["command_topic"]
    assert request_topic not in bus["publish_topics"]
    assert request_topic not in bus["subscribe_topics"]


def test_the_rerun_compute_contract_provisions_and_declares_the_request_topic() -> None:
    contract = _contract(RERUN_COMPUTE_DIR)
    request_topic = "onex.cmd.omnimarket.pr-landing-rerun-requested.v1"

    assert contract["event_bus"]["publish_topics"] == [request_topic]
    assert contract["externally_consumed_topics"] == [request_topic]
    assert request_topic not in contract["event_bus"]["subscribe_topics"]


def test_consumed_only_command_topics_are_named() -> None:
    declared: set[str] = set()
    wired: set[str] = set()
    for path in sorted(NODES.glob("*/contract.yaml")):
        contract = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(contract, dict):
            continue
        bus = contract.get("event_bus") or {}
        for key in ("subscribe_topics", "publish_topics"):
            wired.update(bus.get(key) or [])
        for section in ("consumed_events", "externally_produced_topics"):
            for entry in contract.get(section) or []:
                topic = entry if isinstance(entry, str) else entry.get("topic")
                if isinstance(topic, str) and topic.startswith("onex.cmd."):
                    declared.add(topic)

    unprovisioned = declared - wired
    assert unprovisioned == KNOWN_CONSUMED_ONLY_COMMAND_TOPICS
