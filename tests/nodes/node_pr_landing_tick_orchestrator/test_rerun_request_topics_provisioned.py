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
HOST_INSTALLER_DIR = NODES / "node_host_installer_registry_units"

# A consumed command topic that no contract's event_bus declares is not created by boot
# provisioning (OMN-23991), so whoever publishes it must arrange the topic elsewhere. Each name
# below is listed with the reason it is not boot provisioned; a new one is a decision someone
# must add here.
_NO_HANDLER_REASON = (
    "node_intelligence_orchestrator subscribes none of the three; OMN-18013 removed the subscriptions that had "
    "no handler; re-declare each only with its handler"
)
KNOWN_NO_LIVE_CONSUMER_COMMAND_TOPICS: dict[str, str] = {
    "onex.cmd.omnimarket.code-analysis.v1": _NO_HANDLER_REASON,
    "onex.cmd.omnimarket.document-ingestion.v1": _NO_HANDLER_REASON,
    "onex.cmd.omnimarket.pattern-learning.v1": _NO_HANDLER_REASON,
    "onex.cmd.omnimarket.host-disk-read-requested.v1": (
        "no publisher or consumer exists yet; the host disk orchestrator is a later ticket"
    ),
}


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


def test_the_request_topic_is_a_tick_publish_topic_and_never_a_subscription() -> None:
    contract = _contract(TICK_DIR)
    bus = contract["event_bus"]

    request_topic = contract["rerun_request"]["command_topic"]
    assert request_topic in bus["publish_topics"]
    assert request_topic in contract["externally_consumed_topics"]
    assert request_topic not in bus["subscribe_topics"]


def test_the_rerun_compute_contract_declares_no_request_topic() -> None:
    contract = _contract(RERUN_COMPUTE_DIR)
    request_topic = "onex.cmd.omnimarket.pr-landing-rerun-requested.v1"

    assert contract["event_bus"]["publish_topics"] == []
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
    missing = unprovisioned - set(KNOWN_NO_LIVE_CONSUMER_COMMAND_TOPICS)
    stale = set(KNOWN_NO_LIVE_CONSUMER_COMMAND_TOPICS) - unprovisioned
    assert unprovisioned == set(KNOWN_NO_LIVE_CONSUMER_COMMAND_TOPICS), (
        f"consumed command topics not boot provisioned and not listed with a reason: {sorted(missing)}; "
        f"listed with a reason but now boot provisioned (stale entry): {sorted(stale)}"
    )


def test_repo_state_and_declared_clone_refresh_topics_are_boot_provisioned() -> None:
    manifest = discover_contracts_from_paths([HOST_INSTALLER_DIR / "contract.yaml"])
    provisioned: set[str] = set()
    for contract in manifest.contracts:
        provisioned.update(_contract_provision_topics(contract))

    assert "onex.cmd.omnimarket.repo-state-query-requested.v1" in provisioned
    assert "onex.cmd.omnimarket.declared-clone-refresh-requested.v1" in provisioned


def test_serve_topics_are_declared_externally_consumed() -> None:
    contract = _contract(HOST_INSTALLER_DIR)

    assert (
        "onex.cmd.omnimarket.repo-state-query-requested.v1"
        in contract["externally_consumed_topics"]
    )
    assert (
        "onex.cmd.omnimarket.declared-clone-refresh-requested.v1"
        in contract["externally_consumed_topics"]
    )
