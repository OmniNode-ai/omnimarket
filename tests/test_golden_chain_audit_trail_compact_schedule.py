# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: the audit trail compactor has a scheduled producer.

node_audit_trail_compactor subscribes to its command topic, but until the
schedule node existed nothing in the graph published that command, so the
rollup never ran. These tests bind the schedule node to the compactor's
declared command topic and to the runtime tick, from the contracts on disk.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.models.model_audit_trail_compactor_command import ModelCompactorCommand
from omnimarket.nodes.node_audit_trail_compact_schedule_compute.handlers.handler_audit_trail_compact_schedule import (
    HandlerAuditTrailCompactSchedule,
    schedule_config,
)
from omnimarket.validators.contract_topic_graph import GRAPH_PACKAGES, build_graph

pytestmark = pytest.mark.unit

NODES = Path(__file__).parents[1] / "src" / "omnimarket" / "nodes"
COMPACTOR = "node_audit_trail_compactor"
SCHEDULE = "node_audit_trail_compact_schedule_compute"
TICK_TOPIC = "onex.intent.platform.runtime-tick.v1"


def _contract(node: str) -> dict[str, Any]:
    return yaml.safe_load((NODES / node / "contract.yaml").read_text())


def _tick(now: dt.datetime, interval_ms: int = 1000) -> ModelRuntimeTick:
    return ModelRuntimeTick(
        now=now,
        tick_id=uuid4(),
        sequence_number=1,
        scheduled_at=now,
        correlation_id=uuid4(),
        scheduler_id="test-scheduler",
        tick_interval_ms=interval_ms,
    )


def _slot_time() -> dt.datetime:
    cfg = schedule_config()
    return dt.datetime(2026, 10, 5, cfg.run_hour_utc, cfg.run_minute_utc, tzinfo=dt.UTC)


def test_compactor_command_topic_has_a_node_producer(tmp_path: Path) -> None:
    command_topic = _contract(COMPACTOR)["runtime_dispatch"]["command_topic"]
    # The producer is an omnimarket node, so the graph over the real omnimarket
    # contracts is sound for this edge. Every other census package resolves to an
    # empty root here, which keeps the test independent of
    # CONTRACT_GRAPH_CHECKOUT_ROOT (CI does not set it for the unit shards).
    roots = {package: tmp_path / package for package in GRAPH_PACKAGES}
    for root in roots.values():
        root.mkdir(parents=True, exist_ok=True)
    roots["omnimarket"] = NODES.parent
    producers = build_graph(roots=roots).producers.get(command_topic, ())
    assert any(SCHEDULE in p for p in producers), (
        f"{command_topic} has no node producer; producers={producers}"
    )


def test_schedule_publishes_exactly_the_compactor_command_topic() -> None:
    command_topic = _contract(COMPACTOR)["runtime_dispatch"]["command_topic"]
    bus = _contract(SCHEDULE)["event_bus"]
    assert bus["publish_topics"] == [command_topic]
    assert bus["subscribe_topics"] == [TICK_TOPIC]
    assert command_topic in _contract(COMPACTOR)["event_bus"]["subscribe_topics"]


def test_compactor_contract_unchanged_by_the_wire() -> None:
    contract = _contract(COMPACTOR)
    assert contract["event_bus"] == {
        "subscribe_topics": ["onex.cmd.omnimarket.audit-trail-compact.v1"],
        "publish_topics": ["onex.evt.omnimarket.audit-trail-compacted.v1"],
    }


def test_tick_in_the_daily_slot_emits_a_compactor_command() -> None:
    cmd = HandlerAuditTrailCompactSchedule().handle(_tick(_slot_time()))
    assert isinstance(cmd, ModelCompactorCommand)
    cfg = schedule_config()
    assert cmd.lookback_days == cfg.lookback_days
    assert cmd.dry_run is cfg.dry_run


def test_tick_outside_the_slot_emits_nothing() -> None:
    handler = HandlerAuditTrailCompactSchedule()
    assert handler.handle(_tick(_slot_time() - dt.timedelta(seconds=5))) is None
    assert handler.handle(_tick(_slot_time() + dt.timedelta(minutes=5))) is None


def test_exactly_one_tick_per_day_fires_and_it_is_stateless() -> None:
    handler = HandlerAuditTrailCompactSchedule()
    day = _slot_time().replace(hour=0, minute=0)
    fired = [
        t
        for t in (day + dt.timedelta(seconds=s) for s in range(0, 86400, 30))
        if handler.handle(_tick(t, interval_ms=30000)) is not None
    ]
    assert len(fired) == 1
    assert fired[0] == _slot_time()
