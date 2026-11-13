# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: receipt, merge, read and verdict hops meet the shipped contracts and models."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.events.pr_landing_github.model_pr_landing_github_request import (
    ModelPrLandingGithubRequest,
)
from omnimarket.nodes.node_merge_override_alarm_effect.models import (
    ModelMergeOverrideFinding,
    ModelMergeOverrideSlackCommand,
    ModelMergeOverrideVerdictEvent,
)
from omnimarket.nodes.node_slack_publish_effect.models.model_slack_publish import (
    ModelSlackPublish,
)
from tests.unit.nodes.node_merge_override_alarm_effect.support import (
    FINDING_SUFFIX,
    READ_SUFFIX,
    SLACK_SUFFIX,
    VERDICT_SUFFIX,
    arm_receipt,
    drive_merge,
    make_rig,
)

pytestmark = pytest.mark.unit
NODES = Path(__file__).resolve().parents[4] / "src/omnimarket/nodes"
MINE = "node_merge_override_alarm_effect"
COMPLETED = "onex.evt.omnimarket.pr-landing-github-completed.v1"
FAILED = "onex.evt.omnimarket.pr-landing-github-failed.v1"


def _contract(node: str) -> dict[str, Any]:
    raw = yaml.safe_load((NODES / node / "contract.yaml").read_text())
    assert isinstance(raw, dict)
    return raw


def test_golden_chain_topics_meet_the_neighbouring_contracts() -> None:
    mine = _contract(MINE)
    landing = _contract("node_pr_landing_github_effect")
    slack = _contract("node_slack_publish_effect")
    assert {COMPLETED, FAILED} <= set(landing["event_bus"]["publish_topics"])
    assert {COMPLETED, FAILED} <= set(mine["event_bus"]["subscribe_topics"])
    assert set(mine["event_bus"]["subscribe_topics"]) == {
        "onex.evt.github.pr-merged.v1",
        "onex.evt.omnimarket.repo-merged.v1",
        COMPLETED,
        FAILED,
        "onex.evt.platform.node-heartbeat.v1",
    }
    # The read command this node publishes is the one the landing effect consumes.
    assert (
        "onex.cmd.omnimarket.pr-landing-github-requested.v1"
        in mine["event_bus"]["publish_topics"]
    )
    assert (
        "onex.cmd.omnimarket.pr-landing-github-requested.v1"
        in landing["event_bus"]["subscribe_topics"]
    )
    slack_topic = "onex.cmd.omnimarket.slack-publish.v1"
    assert slack_topic in mine["event_bus"]["publish_topics"]
    assert slack_topic in slack["event_bus"]["subscribe_topics"]
    assert mine["terminal_event"] == "onex.evt.omnimarket.merge-override-verdict.v1"
    routed = {entry["topic"] for entry in mine["handler_routing"]["handlers"]}
    assert routed == set(mine["event_bus"]["subscribe_topics"])


def test_golden_chain_landing_verdict_end_to_end(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(monkeypatch, tmp_path)
    rig.handler.handle(arm_receipt())
    drive_merge(rig)
    [read] = rig.publisher.on(READ_SUFFIX)
    request = ModelPrLandingGithubRequest.model_validate(read)
    assert request.operation.value == "read_pr_state"
    assert request.mode.value == "enforce"
    [verdict] = rig.publisher.on(VERDICT_SUFFIX)
    event = ModelMergeOverrideVerdictEvent.model_validate(verdict)
    assert event.verdict.value == "landing"
    assert event.receipt_seen is True
    assert str(event.correlation_id) == str(request.correlation_id)
    assert rig.publisher.on(FINDING_SUFFIX) == []


def test_golden_chain_slack_command_validates_against_the_slack_node(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(monkeypatch, tmp_path)
    drive_merge(rig)
    [command] = rig.publisher.on(SLACK_SUFFIX)
    ModelSlackPublish.model_validate(command)
    mirror = ModelMergeOverrideSlackCommand.model_validate(command)
    assert set(mirror.model_dump()) == set(ModelSlackPublish.model_fields)
    ModelMergeOverrideFinding.model_validate(rig.publisher.on(FINDING_SUFFIX)[0])


def test_golden_chain_contract_carries_no_channel_literal() -> None:
    text = (NODES / MINE / "contract.yaml").read_text()
    assert "SLACK_CHANNEL_ID" in _contract(MINE)["secrets"]
    assert "C0" not in text
