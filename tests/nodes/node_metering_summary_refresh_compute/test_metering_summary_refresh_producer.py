# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17427: the shared-lane producer of the metering refresh command.

A delegation terminal asks for its tenant's day; the runtime tick, once per
interval, asks for every tenant whole. Both publish the small refresh command
the metering projection consumes, never a fold request carrying the runs.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.models.model_metering_summary_refresh_requested import (
    EnumMeteringRefreshReason,
    ModelMeteringSummaryRefreshRequested,
)
from omnimarket.nodes.node_metering_summary_refresh_compute import (
    HandlerMeteringSummaryRefreshOnTerminal,
    HandlerMeteringSummaryRefreshSchedule,
    ModelDelegationTerminalRefreshTrigger,
    schedule_config,
)

pytestmark = pytest.mark.unit

_SRC = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes"
_CONTRACT = _SRC / "node_metering_summary_refresh_compute/contract.yaml"
_CONSUMER = _SRC / "node_projection_metering_summary/contract.yaml"
_TENANT = "820272f9-4aaf-5add-a2df-0af942852ab2"


def _tick(now: datetime, interval_ms: int = 1000) -> ModelRuntimeTick:
    return ModelRuntimeTick(
        now=now,
        tick_id=UUID(int=1),
        sequence_number=1,
        scheduled_at=now,
        correlation_id=UUID(int=2),
        scheduler_id="runtime-test",
        tick_interval_ms=interval_ms,
    )


def test_terminal_asks_for_its_tenants_day() -> None:
    emitted = datetime(2026, 10, 19, 23, 59, 59, tzinfo=UTC)
    out = HandlerMeteringSummaryRefreshOnTerminal().handle(
        ModelDelegationTerminalRefreshTrigger.model_validate(
            {
                "correlation_id": "c-1",
                "tenant_id": _TENANT,
                "emitted_at": emitted.isoformat(),
                "model_used": "qwen",  # the rest of the terminal is ignored
            }
        )
    )
    assert out == ModelMeteringSummaryRefreshRequested(
        refresh_id="delegation-terminal-c-1",
        reason=EnumMeteringRefreshReason.DELEGATION_TERMINAL,
        tenant_ids=(_TENANT,),
        days=frozenset({date(2026, 10, 19)}),
        requested_at=emitted,
    )


def test_terminal_day_is_the_utc_day() -> None:
    trigger = ModelDelegationTerminalRefreshTrigger.model_validate(
        {
            "correlation_id": "c-2",
            "tenant_id": _TENANT,
            "timestamp": "2026-10-19T20:00:00-07:00",
        }
    )
    out = HandlerMeteringSummaryRefreshOnTerminal().handle(trigger)
    assert out is not None
    # 20:00 at -07:00 is 03:00 UTC the next day.
    assert out.days == frozenset({date(2026, 10, 20)})


def test_terminal_without_a_tenant_asks_for_nothing() -> None:
    # No tenant, no row to refresh it into; the schedule backstop covers it.
    trigger = ModelDelegationTerminalRefreshTrigger.model_validate(
        {"correlation_id": "c-3", "emitted_at": "2026-10-19T08:00:00Z"}
    )
    assert HandlerMeteringSummaryRefreshOnTerminal().handle(trigger) is None


def test_terminal_without_a_time_asks_for_nothing() -> None:
    trigger = ModelDelegationTerminalRefreshTrigger.model_validate(
        {"correlation_id": "c-4", "tenant_id": _TENANT}
    )
    assert HandlerMeteringSummaryRefreshOnTerminal().handle(trigger) is None


def test_tick_refreshes_every_tenant_whole_once_per_interval() -> None:
    cfg = schedule_config()
    start = datetime(2026, 10, 19, 8, 0, tzinfo=UTC)
    window = start + timedelta(seconds=cfg.offset_seconds)
    handler = HandlerMeteringSummaryRefreshSchedule()
    fires = [
        out
        for second in range(cfg.interval_seconds)
        if (out := handler.handle(_tick(window + timedelta(seconds=second))))
        is not None
    ]
    assert len(fires) == 1
    (fire,) = fires
    assert fire.reason is EnumMeteringRefreshReason.SCHEDULE
    assert fire.tenant_ids is None
    assert fire.days is None
    assert fire.refresh_id == f"{cfg.workflow}-{window:%Y%m%dT%H%M%SZ}"


def test_contract_publishes_the_command_the_projection_consumes() -> None:
    contract = yaml.safe_load(_CONTRACT.read_text())
    consumer = yaml.safe_load(_CONSUMER.read_text())
    command = contract["terminal_event"]
    assert contract["event_bus"]["publish_topics"] == [command]
    assert command in consumer["event_bus"]["subscribe_topics"]
    assert (
        consumer["config"]["metering_summary"]["store_refresh_command_topic"] == command
    )
    subscribed = set(contract["event_bus"]["subscribe_topics"])
    assert "onex.intent.platform.runtime-tick.v1" in subscribed
    # The delegation terminals the delegation projection writes runs from.
    delegation = yaml.safe_load(
        (_SRC / "node_projection_delegation/contract.yaml").read_text()
    )
    terminals = subscribed - {"onex.intent.platform.runtime-tick.v1"}
    assert terminals
    assert terminals <= set(delegation["event_bus"]["subscribe_topics"])


def test_consumer_contract_names_this_node_as_the_producer() -> None:
    consumer = yaml.safe_load(_CONSUMER.read_text())
    contract = yaml.safe_load(_CONTRACT.read_text())
    producers = {
        entry["topic"]: entry["producer"]
        for entry in consumer["externally_produced_topics"]
    }
    assert contract["name"] in producers[contract["terminal_event"]]


def test_the_schedule_is_declared_in_the_contract() -> None:
    contract = yaml.safe_load(_CONTRACT.read_text())
    block = contract["config"]["metering_summary_refresh"]["schedule"]
    assert schedule_config().interval_seconds == block["interval_seconds"]
    assert block["interval_seconds"] <= 3600
