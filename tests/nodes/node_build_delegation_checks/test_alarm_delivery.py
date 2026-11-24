# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The node's alarm request, delivered by the lab alarm delivery node: one alarm row and one owner row, naming the owner."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import yaml

from omnimarket.models.lab_alarm_delivery import EnumDeliveryOutcome
from omnimarket.models.overlay_documents.model_build_delegation_checks_overlay import (
    ModelBuildDelegationChecksOverlay,
)
from omnimarket.models.test_selector_reading import ModelAttributionCandidate
from omnimarket.nodes.node_build_delegation_checks.adapters.inmemory_adapters import (
    InMemoryBuildStore,
    InMemoryMergeSource,
    RecordedResultsExecutor,
    RecordingSink,
)
from omnimarket.nodes.node_build_delegation_checks.handlers.handler_build_delegation_checks import (
    HandlerBuildDelegationChecks,
)
from omnimarket.nodes.node_build_delegation_checks.models.model_build_delegation_checks import (
    EnumCheckReading,
    ModelCheckOutcome,
    ModelRebuildCompletedObserved,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.adapters.inmemory_adapters import (
    InMemoryAlarmChannel,
    InMemoryAlarmLedger,
    InMemoryDeliveryState,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.handlers.handler_lab_alarm_deliver import (
    HandlerLabAlarmDeliver,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.overlay import ModelDeliveryOverlay

DELIVER_NODE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_lab_alarm_deliver_effect"
)
GREEN, RED = "a" * 40, "b" * 40
OWNER = "delegation-owner"


def _delivery_overlay(condition: str) -> ModelDeliveryOverlay:
    document = yaml.safe_load(
        (DELIVER_NODE / "overlay.example.yaml").read_text(encoding="utf-8")
    )
    route = dict(document["owner_routes"]["example_condition"])
    route["lane"] = OWNER
    document["owner_routes"] = {condition: route}
    return ModelDeliveryOverlay.model_validate(document)


def test_the_alarm_request_is_delivered_with_the_named_owner_and_no_prod_target() -> (
    None
):
    settings = ModelBuildDelegationChecksOverlay(checks=("C29",), owner=OWNER)
    at = datetime(2026, 11, 23, 14, 0, tzinfo=UTC)
    merge = ModelAttributionCandidate(commit_sha="c" * 40, pr_number=6618, merged_at=at)
    sink = RecordingSink()
    node = HandlerBuildDelegationChecks(
        settings=settings,
        runner=RecordedResultsExecutor(
            {
                GREEN: {
                    "C29": ModelCheckOutcome(
                        reading=EnumCheckReading.PASS,
                        run_id="45179185106",
                        finished_at=at,
                    )
                },
                RED: {
                    "C29": ModelCheckOutcome(
                        reading=EnumCheckReading.FAIL,
                        run_id="45189064181",
                        finished_at=at,
                    )
                },
            }
        ),
        store=InMemoryBuildStore(),
        merges=InMemoryMergeSource({(GREEN, RED): (merge,)}),
        sink=sink,
        clock=lambda: at,
    )
    node.handle(ModelRebuildCompletedObserved(git_sha=GREEN, runtime_lane="dev"))
    node.handle(ModelRebuildCompletedObserved(git_sha=RED, runtime_lane="dev"))
    [request] = sink.alarms

    channel, ledger = InMemoryAlarmChannel(), InMemoryAlarmLedger()
    delivery = HandlerLabAlarmDeliver(
        overlay=_delivery_overlay(settings.alarm_condition),
        channel=channel,
        ledger=ledger,
        state=InMemoryDeliveryState(),
        clock=lambda: at,
    )
    result = delivery.handle(request)

    assert result.outcome is EnumDeliveryOutcome.DELIVERED
    kinds = [row.split(" | ")[1] for _, row in ledger.rows]
    assert kinds == ["FRICTION", "MSG"]  # one alarm row and one owner row
    friction_row = ledger.rows[0][1]
    owner_row = ledger.rows[1][1]
    assert "C29" in friction_row
    assert "#6618" in friction_row
    assert f"to={OWNER}" in owner_row
    for _, row in ledger.rows:
        assert not re.search(r"(?<![a-z])prod(?![a-z])", row.lower())
    print("ALARM-ROW", friction_row)
    print("OWNER-ROW", owner_row)
