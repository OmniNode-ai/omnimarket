# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The node is shadow only: it names no deploy-agent topic and publishes only its two decision events."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tests.unit.nodes.node_dev_roll_request_compute.chain_support import (
    CONTRACT,
    DECIDED_TOPIC,
    REFUSED_TOPIC,
)

pytestmark = pytest.mark.unit

NODE_DIR = CONTRACT.parent
DEPLOY_AGENT_COMMAND_MARKER = "rebuild-requested"


def names_deploy_agent_command(text: str) -> bool:
    return DEPLOY_AGENT_COMMAND_MARKER in text


def test_the_marker_check_sees_a_deploy_agent_topic_in_a_sample() -> None:
    """The failing control: a sample that names the deploy-agent command topic is caught."""
    assert names_deploy_agent_command("onex.cmd.omnibase-infra.rebuild-requested.v1")
    assert not names_deploy_agent_command(
        "onex.cmd.omnimarket.dev-roll-decision-requested.v1"
    )


def test_no_file_of_the_node_names_the_deploy_agent_command_topic() -> None:
    named = [
        str(path.relative_to(NODE_DIR))
        for path in sorted(NODE_DIR.rglob("*"))
        if path.is_file()
        and path.suffix in {".py", ".yaml"}
        and names_deploy_agent_command(path.read_text(encoding="utf-8"))
    ]
    assert named == []


def test_the_contract_publishes_only_the_decided_and_refused_events() -> None:
    contract = yaml.safe_load(Path(CONTRACT).read_text(encoding="utf-8"))
    published = {event["topic"] for event in contract["published_events"]}
    assert published == {DECIDED_TOPIC, REFUSED_TOPIC}
    assert contract["event_bus"]["subscribe_topics"] == [
        contract["runtime_dispatch"]["command_topic"]
    ]
    assert not contract["event_bus"].get("publish_topics")
