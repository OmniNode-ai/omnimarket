# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seam between this node's contract.yaml and dev main's boot priority (OMN-23535).

The dev main runtime policy in omnibase_infra
(``contracts/services/runtime_policy.contract.yaml``, ``boot.priority_contracts``)
names this node by the ``name:`` field of its contract.yaml and by its request
topic, so the request-topic consumer joins in the first wave at boot. Neither
side is checked against the other at boot: if the contract name or the command
topic changes here, the priority silently stops applying. These tests pin both
sides of that seam.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

# omnibase_infra's runtime policy (boot.priority_contracts) names these two
# values; this node's contract.yaml must keep carrying them.
BOOT_PRIORITY_CONTRACT_NAME = "work_ledger_query_effect"
BOOT_PRIORITY_REQUEST_TOPIC = "onex.cmd.omnimarket.work-ledger-query-requested.v1"

CONTRACT_PATH = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_work_ledger_query_effect"
    / "contract.yaml"
)

pytestmark = pytest.mark.unit


def _load_contract() -> dict[str, object]:
    return yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))


def test_contract_name_is_the_name_the_dev_main_boot_priority_list_carries() -> None:
    contract = _load_contract()
    assert contract["name"] == BOOT_PRIORITY_CONTRACT_NAME


def test_the_request_topic_the_boot_priority_is_for_is_still_the_subscribe_topic() -> (
    None
):
    contract = _load_contract()
    event_bus = contract["event_bus"]
    runtime_dispatch = contract["runtime_dispatch"]
    assert BOOT_PRIORITY_REQUEST_TOPIC in event_bus["subscribe_topics"]
    assert runtime_dispatch["command_topic"] == BOOT_PRIORITY_REQUEST_TOPIC
