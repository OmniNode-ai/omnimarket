# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The topics of the one-off register node's contract (OMN-24441): the command it is asked on and the event it answers with.

The seam is the contract to its consumers: the command topic and the completed event topic are named by the contract and
asked for by the broker grants, so a renamed topic in one place fails here, and a contract with no completed event fails the
terminal event check.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

import omnimarket.nodes.node_oneoff_register_compute as node_package

pytestmark = pytest.mark.unit

COMMAND_TOPIC = "onex.cmd.omnimarket.oneoff-register-requested.v1"
COMPLETED_TOPIC = "onex.evt.omnimarket.oneoff-register-completed.v1"


def contract() -> dict[str, Any]:
    path = Path(node_package.__file__).parent / "contract.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_the_contract_is_asked_on_the_command_topic_and_answers_on_the_completed_topic() -> (
    None
):
    document = contract()
    bus = document["event_bus"]
    assert bus["subscribe_topics"] == [COMMAND_TOPIC]
    assert bus["publish_topics"] == [COMPLETED_TOPIC]
    assert document["terminal_event"] == COMPLETED_TOPIC


def test_the_broker_grants_read_the_command_topic_and_write_the_completed_topic() -> (
    None
):
    grants = contract()["broker_acl_grants"]
    pairs = {
        (grant["name"], grant["operation"])
        for entry in grants
        for grant in entry["grants"]
    }
    assert (COMMAND_TOPIC, "READ") in pairs
    assert (COMPLETED_TOPIC, "WRITE") in pairs


def test_a_renamed_completed_topic_is_not_the_one_the_contract_publishes() -> None:
    """Failing control: a topic renamed away from the contract's differs from what the contract publishes."""
    renamed = COMPLETED_TOPIC.replace("completed", "done")
    assert [renamed] != contract()["event_bus"]["publish_topics"]
