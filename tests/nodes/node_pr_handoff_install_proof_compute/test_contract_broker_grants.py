# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The install-proof contract's topics and exact runtime broker grants (OMN-25266).

The command is read and the decided event is written, with DESCRIBE on both topics.
A renamed decided topic in the grant list must differ from the contract's grants.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

import omnimarket.nodes.node_pr_handoff_install_proof_compute as node_package

pytestmark = pytest.mark.unit

COMMAND_TOPIC = "onex.cmd.omnimarket.pr-handoff-install-proof-requested.v1"
DECIDED_TOPIC = "onex.evt.omnimarket.pr-handoff-install-proof-decided.v1"


def contract() -> dict[str, Any]:
    path = Path(node_package.__file__).parent / "contract.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_the_contract_subscribes_to_the_command_and_publishes_the_decided_event() -> (
    None
):
    document = contract()
    bus = document["event_bus"]
    assert bus["subscribe_topics"] == [COMMAND_TOPIC]
    assert bus["publish_topics"] == [DECIDED_TOPIC]
    assert document["terminal_event"] == DECIDED_TOPIC
    assert bus["terminal_event"] == DECIDED_TOPIC


def test_the_runtime_principal_has_exactly_the_required_broker_grants() -> None:
    entries = contract()["broker_acl_grants"]
    assert len(entries) == 1
    assert entries[0]["principal"] == "${ONEX_ACL_RUNTIME_PRINCIPAL}"
    grants = entries[0]["grants"]
    assert len(grants) == 4
    assert {(grant["name"], grant["operation"]) for grant in grants} == {
        (COMMAND_TOPIC, "READ"),
        (COMMAND_TOPIC, "DESCRIBE"),
        (DECIDED_TOPIC, "WRITE"),
        (DECIDED_TOPIC, "DESCRIBE"),
    }
    assert all(grant["resource_type"] == "TOPIC" for grant in grants)
    assert all(grant["pattern"] == "LITERAL" for grant in grants)


def test_a_renamed_decided_topic_does_not_match_the_contract_grants() -> None:
    """Failing control: renaming the decided topic changes the grant list."""
    grants = contract()["broker_acl_grants"][0]["grants"]
    renamed = [
        {**grant, "name": grant["name"].replace("decided", "done")} for grant in grants
    ]
    assert renamed != grants


def test_the_grants_cover_the_subscribed_and_published_topics() -> None:
    """Each subscribed topic is granted READ and each published topic WRITE."""
    document = contract()
    bus = document["event_bus"]
    granted = {
        (grant["name"], grant["operation"])
        for entry in document["broker_acl_grants"]
        for grant in entry["grants"]
    }
    assert {(topic, "READ") for topic in bus["subscribe_topics"]} <= granted
    assert {(topic, "WRITE") for topic in bus["publish_topics"]} <= granted
