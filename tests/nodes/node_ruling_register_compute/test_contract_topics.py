# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The topics of the ruling register compute node's contract (OMN-24478): the command it is asked on and the event it answers with.

The seam is the contract to its consumers: the command topic and the judged event topic are named by the contract and
asked for by the broker grants, so a renamed topic in one place fails here, and a contract with no judged event fails the
terminal event check.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

import omnimarket.nodes.node_ruling_register_compute as node_package

pytestmark = pytest.mark.unit

COMMAND_TOPIC = "onex.cmd.omnimarket.ruling-register-requested.v1"
JUDGED_TOPIC = "onex.evt.omnimarket.ruling-register-judged.v1"


def contract() -> dict[str, Any]:
    path = Path(node_package.__file__).parent / "contract.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_the_contract_is_asked_on_the_command_topic_and_answers_on_the_judged_topic() -> (
    None
):
    document = contract()
    bus = document["event_bus"]
    assert bus["subscribe_topics"] == [COMMAND_TOPIC]
    assert bus["publish_topics"] == [JUDGED_TOPIC]
    assert document["terminal_event"] == JUDGED_TOPIC


def test_the_broker_grants_read_the_command_topic_and_write_the_judged_topic() -> None:
    grants = contract()["broker_acl_grants"]
    pairs = {
        (grant["name"], grant["operation"])
        for entry in grants
        for grant in entry["grants"]
    }
    assert (COMMAND_TOPIC, "READ") in pairs
    assert (JUDGED_TOPIC, "WRITE") in pairs


def test_a_renamed_judged_topic_is_not_the_one_the_contract_publishes() -> None:
    """Failing control: a topic renamed away from the contract's differs from what the contract publishes."""
    renamed = JUDGED_TOPIC.replace("judged", "done")
    assert [renamed] != contract()["event_bus"]["publish_topics"]
