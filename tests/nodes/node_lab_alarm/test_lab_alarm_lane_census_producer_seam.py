# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam: the census event builder (omnimarket.models.lane_census_event) to the lane_drift and
container_unhealthy verbs of node_lab_alarm (OMN-24425).

The census side builds the snapshot; the alarm side reads it. Each test builds the snapshot with the
real builder and reads it with the real verb, so a key, type, severity or event-type change on either
side turns one of them red. In-memory source, no broker: the verbs read a snapshot document and publish
nothing themselves (the real-broker reading belongs to the runtime step).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from omnimarket.models.lane_census_event import (
    FINDING_KIND_SEVERITY,
    build_event,
    build_observed_event,
    validate_event,
)
from omnimarket.nodes.node_lab_alarm.adapters.condition_adapters import (
    InMemoryCensusSource,
)
from omnimarket.nodes.node_lab_alarm.adapters.inmemory_adapters import (
    InMemoryRunRecords,
)
from omnimarket.nodes.node_lab_alarm.handlers.handler_lane_census import (
    HandlerLaneCensus,
)
from omnimarket.nodes.node_lab_alarm.models.model_lab_alarm_lane_census import (
    EnumLaneCensusCondition,
    EnumLaneCensusFault,
    EnumLaneCensusOutcome,
    ModelLaneCensusCommand,
)
from omnimarket.nodes.node_lab_alarm.overlay import ModelLaneCensusSettings

pytestmark = pytest.mark.unit

_FIXTURE: dict[str, Any] = json.loads(
    (
        Path(__file__).resolve().parents[2]
        / "models"
        / "fixtures"
        / "lane_census_event_parity.json"
    ).read_text(encoding="utf-8")
)
_NOW = datetime.fromisoformat(_FIXTURE["now"])
_OUTAGE = _FIXTURE["cases"]["prod_outage"]
_CLEAN = _FIXTURE["cases"]["clean"]


def _built(case: dict[str, Any]) -> dict[str, Any]:
    return build_event(
        host=case["host"], plan=case["plan"], topic=case["drift"]["topic"], now=_NOW
    )


def _read(document: dict[str, Any], condition: EnumLaneCensusCondition) -> Any:
    handler = HandlerLaneCensus(
        condition=condition,
        settings=ModelLaneCensusSettings(
            snapshot_path="unused",
            snapshot_command=None,
            max_age_seconds=7200,
            timeout_seconds=10,
        ),
        source=InMemoryCensusSource(document),
        delivery=None,
        records=InMemoryRunRecords(),
        clock=lambda: _NOW,
    )
    return handler.handle(
        ModelLaneCensusCommand(
            correlation_id="seam-1", write=False, fault=EnumLaneCensusFault.NONE
        )
    )


def test_census_seam_a_built_clean_event_reads_ok_for_both_conditions() -> None:
    event = _built(_CLEAN)
    assert validate_event(event, kind_severity=FINDING_KIND_SEVERITY) == []
    for condition in EnumLaneCensusCondition:
        assert _read(event, condition).outcome is EnumLaneCensusOutcome.OK


def test_census_seam_a_built_outage_event_alarms_with_the_builders_alert_key() -> None:
    event = _built(_OUTAGE)
    result = _read(event, EnumLaneCensusCondition.LANE_DRIFT)
    assert result.outcome is EnumLaneCensusOutcome.ALARM
    [alarm] = result.alarms
    assert alarm.startswith(f"{event['alert_key']}: ")
    assert "container_absent" in alarm
    assert "network_detached" in alarm


def test_census_seam_failing_control_a_changed_builder_shape_reads_indeterminate() -> (
    None
):
    event = _built(_OUTAGE)
    renamed = {k: v for k, v in event.items() if k != "alert_key"}
    renamed["alert_id"] = event["alert_key"]
    assert (
        _read(renamed, EnumLaneCensusCondition.LANE_DRIFT).outcome
        is EnumLaneCensusOutcome.INDETERMINATE
    )
    retyped = {**event, "drift_count": str(event["drift_count"])}
    assert (
        _read(retyped, EnumLaneCensusCondition.LANE_DRIFT).outcome
        is EnumLaneCensusOutcome.INDETERMINATE
    )


def test_census_seam_the_observed_event_is_not_a_snapshot_for_the_verbs() -> None:
    observed = build_observed_event(
        host=_OUTAGE["host"],
        plan=_OUTAGE["plan"],
        topic=_OUTAGE["observed"]["topic"],
        now=_NOW,
    )
    result = _read(observed, EnumLaneCensusCondition.LANE_DRIFT)
    assert result.outcome is EnumLaneCensusOutcome.INDETERMINATE
