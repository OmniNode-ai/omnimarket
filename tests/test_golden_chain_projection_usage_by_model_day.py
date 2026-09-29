# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for ``node_projection_usage_by_model_day`` (OMN-19978).

onex.evt.omniintelligence.llm-call-completed.v1   (one event per delegated call)
    -> public.usage_by_model_day_calls / usage_by_model_day   (tenant, UTC day, model)
    -> onex.snapshot.projection.usage-by-model-day.v1         (bus-backed exposure)
    -> onex.evt.omnimarket.projection-usage-by-model-day-applied.v1 (terminal)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_projection_usage_by_model_day.handlers.handler_projection_usage_by_model_day import (
    HandlerProjectionUsageByModelDay,
)
from omnimarket.nodes.node_projection_usage_by_model_day.handlers.handler_usage_by_model_day_store import (
    AGGREGATE_TABLE,
    apply_usage_call,
)
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter
from tests.test_omn19978_usage_by_model_day import CALLS, _event

pytestmark = pytest.mark.unit

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_usage_by_model_day/contract.yaml"
)
_CALL_TOPIC = "onex.evt.omniintelligence.llm-call-completed.v1"  # onex-topic-allow: the declared subscribe topic
_SNAPSHOT_TOPIC = "onex.snapshot.projection.usage-by-model-day.v1"  # onex-topic-allow: projection snapshot topics use onex.snapshot.* by convention
_TERMINAL_TOPIC = "onex.evt.omnimarket.projection-usage-by-model-day-applied.v1"  # onex-topic-allow: this node's declared terminal
_DLQ_TOPIC = "onex.dlq.omnimarket.projection-usage-by-model-day-malformed.v1"  # onex-topic-allow: this node's declared DLQ


def _contract() -> dict[str, Any]:
    loaded = yaml.safe_load(_CONTRACT_PATH.read_text())
    assert isinstance(loaded, dict)
    return loaded


def test_golden_chain_hops_are_the_declared_topics() -> None:
    contract = _contract()
    event_bus = contract["event_bus"]
    assert event_bus["subscribe_topics"] == [_CALL_TOPIC]
    assert event_bus["publish_topics"] == [_TERMINAL_TOPIC]
    assert event_bus["dlq_topics"] == [_DLQ_TOPIC]
    assert contract["terminal_event"] == _TERMINAL_TOPIC
    assert contract["projection_api"]["topic"] == _SNAPSHOT_TOPIC
    assert contract["projection_api"]["bus_backed"] is True


def test_golden_chain_ten_calls_end_as_four_model_day_rows(tmp_path: Path) -> None:
    db = SqliteDatabaseAdapter(tmp_path / "usage.sqlite")
    fold = HandlerProjectionUsageByModelDay()
    applied = [apply_usage_call(fold.handle(_event(call)), db) for call in CALLS]
    assert all(applied)
    rows = db.query(AGGREGATE_TABLE)
    assert len(rows) == 4
    assert sum(int(str(r["call_count"])) for r in rows) == len(CALLS)
