# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A watched group's lag over a declared ceiling alarms, naming the bound and its owner (OMN-26609).

The incident: the dev work-ledger query answerer's group held 4681 requests at 16:08:55Z and drained them
at about one per second (4253 at 16:15:38Z, 655 at 16:57:51Z, 471 at 17:00:51Z). The alarm's rule is growth
between samples, and "a high flat lag is not an alarm" by design, so a lag that only drains never alarmed;
and the overlay watched six projection nodes, not the query effect. The ceiling is the missing reading: a
bound and an owner, both data in the overlay, never literals in the handler.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_lab_alarm.adapters.inmemory_adapters import (
    InMemoryLag,
    InMemoryManifest,
    InMemoryRunRecords,
    InMemorySamples,
)
from omnimarket.nodes.node_lab_alarm.handlers.handler_broker_lag import (
    HandlerBrokerLag,
    parse_command,
)
from omnimarket.nodes.node_lab_alarm.models.model_lab_alarm import (
    EnumBrokerLagOutcome,
    ModelBrokerLagCommand,
)
from omnimarket.nodes.node_lab_alarm.overlay import ModelLabAlarmOverlay

pytestmark = pytest.mark.unit

_NODE = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes/node_lab_alarm"
_TOPIC = "onex.evt.example.one.v1"
_GROUP = (
    f"local.examplepkg.projection_example.consume.1.2.0.__i.runtime-main.__t.{_TOPIC}"
)
_CEILING = {"max_lag": 500, "owner": "example-owner-lane"}


def _overlay(ceiling: dict[str, Any] | None) -> ModelLabAlarmOverlay:
    raw = yaml.safe_load((_NODE / "overlay.example.yaml").read_text(encoding="utf-8"))
    raw["lag"].pop("ceiling", None)
    if ceiling is not None:
        raw["lag"]["ceiling"] = ceiling
    return ModelLabAlarmOverlay.model_validate(raw)


def _handler(
    ceiling: dict[str, Any] | None, lag: int, previous: int | None
) -> tuple[HandlerBrokerLag, InMemoryLag]:
    reader = InMemoryLag({_GROUP: lag})
    manifest = {
        "contracts": [
            {
                "name": "projection_example",
                "package_name": "examplepkg",
                "contract_version": "1.2.0",
                "event_bus": {"subscribe_topics": [_TOPIC]},
            }
        ]
    }
    handler = HandlerBrokerLag(
        overlay=_overlay(ceiling),
        manifest=InMemoryManifest(manifest),
        lag=reader,
        samples=InMemorySamples(None if previous is None else {_GROUP: previous}),
        records=InMemoryRunRecords(),
        clock=lambda: datetime(2026, 11, 23, 17, 0, tzinfo=UTC),
    )
    return handler, reader


def _run(handler: HandlerBrokerLag):
    command = parse_command(json.dumps({"correlation_id": "ceiling-1"}).encode())
    assert isinstance(command, ModelBrokerLagCommand)
    return handler.handle(command)


def test_incident_a_draining_backlog_over_the_ceiling_alarms() -> None:
    """4253 then 4100: the lag shrank, so growth-only never alarms; the ceiling does."""
    handler, _ = _handler(_CEILING, lag=4100, previous=4253)
    result = _run(handler)
    assert result.outcome is EnumBrokerLagOutcome.ALARM
    assert len(result.alarms) == 1
    assert result.alarms[0].startswith(f"{_GROUP}: ")
    assert "4100" in result.alarms[0]
    assert "500" in result.alarms[0]
    assert "example-owner-lane" in result.alarms[0]


def test_control_without_a_ceiling_the_same_draining_backlog_is_ok() -> None:
    """The behaviour the incident ran under: nothing alarms."""
    handler, _ = _handler(None, lag=4100, previous=4253)
    assert _run(handler).outcome is EnumBrokerLagOutcome.OK


def test_lag_at_or_under_the_ceiling_does_not_alarm() -> None:
    handler, _ = _handler(_CEILING, lag=500, previous=500)
    assert _run(handler).outcome is EnumBrokerLagOutcome.OK


def test_first_sample_over_the_ceiling_alarms_without_a_baseline() -> None:
    """A ceiling is a state, not a change: it needs no previous sample."""
    handler, _ = _handler(_CEILING, lag=501, previous=None)
    result = _run(handler)
    assert result.outcome is EnumBrokerLagOutcome.ALARM
    assert "example-owner-lane" in result.alarms[0]


def test_an_unreadable_group_stays_indeterminate_under_a_ceiling() -> None:
    handler, reader = _handler(_CEILING, lag=9999, previous=9999)
    reader.refused[_GROUP] = "refused DESCRIBE (GroupAuthorizationFailedError)"
    assert _run(handler).outcome is EnumBrokerLagOutcome.INDETERMINATE


@pytest.mark.parametrize(
    "bad",
    [
        {"max_lag": 500},
        {"owner": "example-owner-lane"},
        {"max_lag": 0, "owner": "example-owner-lane"},
        {"max_lag": 500, "owner": ""},
    ],
)
def test_the_bound_and_the_owner_are_both_required_data(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _overlay(bad)
