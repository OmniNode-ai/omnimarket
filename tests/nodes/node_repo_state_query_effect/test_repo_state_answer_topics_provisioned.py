# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The repo-state answer topics are provisioned at boot (OMN-19998).

`repo-state query` publishes its request on the installer contract's
repo-state-query-requested command topic and waits, group-less, for the answer on the completed
or failed topic, timing out after 30 s with "Topic ... not found in cluster metadata" when the
topic does not exist. The dev runtime's per-contract boot provisioning creates only the topics
_contract_provision_topics returns (subscribe, publish, dlq and projection_api topics), and the
repo-state serve contract declares no event_bus block, so nothing creates the two answers. The
node_host_installer_registry_units contract lists them (OMN-23991's pattern) beside the two serve
command topics; the fourth test is the control that fails the whole fix if the repo-state
contract ever provisions its own answers alone.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import _contract_provision_topics

pytestmark = pytest.mark.unit

NODES = Path(__file__).parents[3] / "src" / "omnimarket" / "nodes"
REPO_STATE_DIR = NODES / "node_repo_state_query_effect"
HOST_INSTALLER_DIR = NODES / "node_host_installer_registry_units"


def _contract(node_dir: Path) -> dict[str, Any]:
    return yaml.safe_load((node_dir / "contract.yaml").read_text(encoding="utf-8"))


def _installer_boot_provision_topics() -> set[str]:
    """The topics the dev runtime's boot provisioning creates for the installer contract."""
    manifest = discover_contracts_from_paths([HOST_INSTALLER_DIR / "contract.yaml"])
    provisioned: set[str] = set()
    for contract in manifest.contracts:
        provisioned.update(_contract_provision_topics(contract))
    return provisioned


@pytest.mark.parametrize("role", ["success", "failure"])
def test_each_answer_topic_is_boot_provisioned(role: str) -> None:
    terminal = _contract(REPO_STATE_DIR)["runtime_dispatch"]["terminal_events"]

    assert terminal[role] in _installer_boot_provision_topics()


def test_answer_topics_equal_the_published_events() -> None:
    contract = _contract(REPO_STATE_DIR)
    terminal = set(contract["runtime_dispatch"]["terminal_events"].values())
    published = {event["topic"] for event in contract["published_events"]}

    assert terminal == published


def test_answer_topics_are_declared_externally_consumed_and_never_subscribed() -> None:
    installer = _contract(HOST_INSTALLER_DIR)
    terminal = set(
        _contract(REPO_STATE_DIR)["runtime_dispatch"]["terminal_events"].values()
    )

    assert terminal <= set(installer["externally_consumed_topics"])
    assert not terminal & set(installer["event_bus"]["subscribe_topics"])


def test_the_repo_state_contract_alone_provisions_nothing() -> None:
    # The repo-state serve contract declares no event_bus block, so its boot provisioning
    # returns no topics at all; this is why the installer contract lists the two answers
    # beside its serve command topics.
    manifest = discover_contracts_from_paths([REPO_STATE_DIR / "contract.yaml"])
    provisioned: set[str] = set()
    for contract in manifest.contracts:
        provisioned.update(_contract_provision_topics(contract))

    assert provisioned == set()
