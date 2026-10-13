# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for ``node_projection_run_trace`` (OMN-19987).

onex.evt.omnimarket.delegation-run-step.v1        (one event per step of a run)
    -> public.run_trace                           (tenant, run, step sequence)
    -> onex.snapshot.projection.run-trace.v1      (bus-backed exposure)
    -> onex.evt.omnimarket.projection-run-trace-applied.v1 (terminal)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.models.model_delegation_run_step import ModelDelegationRunStep
from omnimarket.nodes.node_projection_run_trace.handlers.handler_projection_run_trace import (
    HandlerProjectionRunTrace,
)
from omnimarket.nodes.node_projection_run_trace.handlers.handler_run_trace_store import (
    RUN_TRACE_TABLE,
    apply_run_step,
)
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[1]
_CONTRACT_PATH = _ROOT / "src/omnimarket/nodes/node_projection_run_trace/contract.yaml"
_PUBLISHER_CONTRACT_PATH = (
    _ROOT / "src/omnimarket/nodes/node_delegate_skill_orchestrator/contract.yaml"
)
_CHAIN_PATH = _ROOT / "tests/fixtures/omn19987/run_step_chain.json"
_STEP_TOPIC = "onex.evt.omnimarket.delegation-run-step.v1"  # onex-topic-allow: the declared subscribe topic
_SNAPSHOT_TOPIC = "onex.snapshot.projection.run-trace.v1"  # onex-topic-allow: projection snapshot topics use onex.snapshot.* by convention
_TERMINAL_TOPIC = "onex.evt.omnimarket.projection-run-trace-applied.v1"  # onex-topic-allow: this node's declared terminal
_DLQ_TOPIC = "onex.dlq.omnimarket.projection-run-trace-malformed.v1"  # onex-topic-allow: this node's declared DLQ


def _contract(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text())
    assert isinstance(loaded, dict)
    return loaded


def test_golden_chain_hops_are_the_declared_topics() -> None:
    contract = _contract(_CONTRACT_PATH)
    event_bus = contract["event_bus"]
    assert event_bus["subscribe_topics"] == [_STEP_TOPIC]
    assert event_bus["publish_topics"] == [_TERMINAL_TOPIC]
    assert event_bus["dlq_topics"] == [_DLQ_TOPIC]
    assert contract["terminal_event"] == _TERMINAL_TOPIC
    assert contract["projection_api"]["topic"] == _SNAPSHOT_TOPIC
    assert contract["projection_api"]["bus_backed"] is True


def test_golden_chain_the_step_topic_has_a_declared_publisher() -> None:
    publisher = _contract(_PUBLISHER_CONTRACT_PATH)
    assert _STEP_TOPIC in publisher["event_bus"]["publish_topics"]
    assert publisher["runtime_dispatch"]["run_step_event"] == _STEP_TOPIC


def test_golden_chain_a_runs_steps_end_as_its_rows_in_step_order(
    tmp_path: Path,
) -> None:
    chain = json.loads(_CHAIN_PATH.read_text(encoding="utf-8"))
    db = SqliteDatabaseAdapter(tmp_path / "trace.sqlite")
    fold = HandlerProjectionRunTrace()
    applied = [
        apply_run_step(fold.handle(ModelDelegationRunStep.model_validate(event)), db)
        for event in reversed(chain)
    ]
    assert all(applied)
    rows = db.query(RUN_TRACE_TABLE, order_by="step_sequence")
    assert [row["step_kind"] for row in rows] == [event["step_kind"] for event in chain]
    assert rows[0]["step_kind"] == "request"
    assert rows[-1]["step_kind"] == "terminal"
