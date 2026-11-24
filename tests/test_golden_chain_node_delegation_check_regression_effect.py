# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for the delegation check regression alarm (OMN-26619).

The chain is the contract's declared path: a runtime tick starts a scan, the scan reads each watched check's finished runs,
each green to red transition publishes one regression event, hands one alarm to the lab alarm delivery command and queues one
fix lane, and the scan's result is the terminal state. Every topic the chain publishes is declared in the contract, and the
alarm command is the one topic the lab alarm delivery node subscribes to.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.models.lab_alarm_delivery import ModelLabAlarmDeliveryRequest
from omnimarket.nodes.node_delegation_check_regression_effect.models.model_regression import (
    ModelFixLaneRequest,
    ModelRegressionEvent,
    ModelRegressionScanRequest,
)
from tests.unit.regression_support import TOPICS, Rig, recorded_runs, settings

pytestmark = pytest.mark.unit

NODES = Path(__file__).resolve().parents[1] / "src" / "omnimarket" / "nodes"
REPLAY_AT = datetime(2026, 11, 23, 14, 45, tzinfo=UTC)
CHECKS = [
    {
        "check_id": c,
        "repository": "example-org/example-infra",
        "workflow": f"{c.lower()}.yml",
        "interval_seconds": 3600,
    }
    for c in ("C14", "C29")
]


def _contract(node: str) -> dict[str, Any]:
    loaded = yaml.safe_load(
        (NODES / node / "contract.yaml").read_text(encoding="utf-8")
    )
    assert isinstance(loaded, dict)
    return loaded


def test_hop1_a_runtime_tick_starts_the_scan_and_a_rebuild_event_is_recorded() -> None:
    contract = _contract("node_delegation_check_regression_effect")
    assert contract["topics"]["scan_tick"] in contract["event_bus"]["subscribe_topics"]
    assert (
        contract["topics"]["rebuild_completed"]
        in contract["event_bus"]["subscribe_topics"]
    )
    operations = {
        h["operation"]: h["topic"] for h in contract["handler_routing"]["handlers"]
    }
    assert (
        operations["delegation_check_regression.scan"]
        == contract["topics"]["scan_tick"]
    )
    assert (
        operations["delegation_check_regression.record_dev_build"]
        == contract["topics"]["rebuild_completed"]
    )


def test_hop2_a_scan_publishes_the_event_the_alarm_and_the_fix_lane_on_declared_topics() -> (
    None
):
    rig = Rig(
        recorded_runs(),
        cfg=settings(checks=CHECKS, max_transition_age_seconds=4500),
        until=REPLAY_AT,
    )
    result = rig.handler.handle(ModelRegressionScanRequest(now=REPLAY_AT))
    contract = _contract("node_delegation_check_regression_effect")
    published = {topic for topic, _, _ in rig.publisher.published}
    assert published <= set(contract["event_bus"]["publish_topics"])
    assert published == {
        "onex.evt.omnimarket.delegation-check-regression-detected.v1",
        "onex.evt.omnimarket.delegation-check-fix-lane-queued.v1",
        "onex.cmd.omnimarket.lab-alarm-delivery-requested.v1",
        "onex.evt.omnimarket.delegation-check-regression-scan-completed.v1",
    }
    assert {
        e.check_id
        for e in rig.publisher.on(TOPICS["regression_detected"])
        if isinstance(e, ModelRegressionEvent)
    } == {
        "C14",
        "C29",
    }
    assert {
        f.check_id
        for f in rig.publisher.on(TOPICS["fix_lane_queued"])
        if isinstance(f, ModelFixLaneRequest)
    } == {
        "C14",
        "C29",
    }
    alarms = [
        a
        for a in rig.publisher.on(TOPICS["alarm_delivery_requested"])
        if isinstance(a, ModelLabAlarmDeliveryRequest)
    ]
    assert alarms
    assert result.publish_failures == ()
    assert len(rig.publisher.on(TOPICS["scan_completed"])) == 1


def test_hop3_the_alarm_command_is_the_topic_the_lab_alarm_delivery_node_subscribes_to() -> (
    None
):
    delivery = _contract("node_lab_alarm_deliver_effect")
    assert (
        TOPICS["alarm_delivery_requested"] in delivery["event_bus"]["subscribe_topics"]
    )


def test_hop4_the_scan_result_is_the_declared_terminal_state() -> None:
    contract = _contract("node_delegation_check_regression_effect")
    assert contract["terminal_event"] == contract["topics"]["scan_completed"]
    assert contract["terminal_event"] in contract["event_bus"]["publish_topics"]
