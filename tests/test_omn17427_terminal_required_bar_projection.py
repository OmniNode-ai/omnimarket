# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Kafka delegation writer persists the terminal's declared quality bar.

THE DEFECT, measured 2026-10-03 on the .201 dev lane, window 02:00-06:00Z:
875 ``delegation_events`` rows, 0 with a non-null ``required_bar``. Of the 616
run receipts on that host whose correlation has a row, 601 carry
``required_quality_bar`` (542 at 0.85, 59 at 0.8) in the terminal payload the
deployed lane published on ``onex.evt.omnimarket.delegate-skill-completed.v1``.
The receipt and the projection disagree on every one of them.

Two causes, both on the projection side:

* ``ModelDelegateSkillTerminalProjection.required_bar`` read only the keys
  ``required_bar``/``requiredBar``. The producer names the bar
  ``required_quality_bar`` (the field ``ModelDelegateSkillResponse`` declares
  and validates against ``score_vs_required_bar``), so the projection field was
  None for every deployed-lane terminal.
* The async Kafka writer's delegate-skill row builder never named
  ``actual_score`` or ``required_bar`` at all, unlike the sync row builder,
  which has written both since the local-store fix for the same columns.

THE RULE: a terminal that declares a bar writes that bar; a terminal that
declares none writes nothing (NULL), never zero, and the inherited
``quality_score`` default of 0.0 never becomes a graded score.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillTerminalProjection,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
    DelegationProjectionRunner,
)
from omnimarket.projection.runner import MessageMeta

if TYPE_CHECKING:
    from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter

pytestmark = pytest.mark.unit


def _deployed_lane_terminal(
    *, correlation_id: str, bar: float | None = 0.85
) -> dict[str, Any]:
    """The delegate-skill-completed payload as the deployed lane publishes it.

    Trimmed from a real receipt on the .201 dev lane (run 6e28b51e, terminal
    payload): the bar rides as ``required_quality_bar`` with its typed
    comparison, the score as ``quality_score``; there is no ``required_bar`` or
    ``actual_score`` key.
    """
    payload: dict[str, Any] = {
        "status": "completed",
        "correlation_id": correlation_id,
        "task_type": "document",
        "model_name": "Qwen3.8-27B",
        "provider": "local",
        "quality_gate_passed": True,
        "quality_gates_failed": [],
        "quality_score": 1.0,
        "attempts_count": 1,
        "caller_lane": "trigger-sha-read-2a21",
    }
    if bar is not None:
        payload["required_quality_bar"] = bar
        payload["score_vs_required_bar"] = "at_or_above_bar"
    return payload


def _mock_db() -> AsyncMock:
    db = AsyncMock()
    db.execute = AsyncMock(return_value=[])
    db.fetchval = AsyncMock(return_value=None)
    return db


def _insert_columns(mock_db: AsyncMock) -> dict[str, object]:
    inserts = [
        c
        for c in mock_db.execute.await_args_list
        if str(c.args[0]).strip().startswith("INSERT INTO delegation_events")
    ]
    assert len(inserts) == 1
    sql = str(inserts[0].args[0])
    columns = [c.strip() for c in sql.split("(", 1)[1].split(")", 1)[0].split(",")]
    slots = [s.strip() for s in sql.split("VALUES (", 1)[1].split(")", 1)[0].split(",")]
    bound = [c for c, s in zip(columns, slots, strict=True) if s.startswith("$")]
    return dict(zip(bound, inserts[0].args[1:], strict=True))


def _project(payload: dict[str, Any]) -> dict[str, object]:
    runner = DelegationProjectionRunner()
    mock_db = _mock_db()
    runner._db = cast("AsyncpgAdapter", mock_db)
    topic = runner._topic_delegate_skill_completed
    assert topic, "contract must declare a delegate-skill-completed topic"
    meta = MessageMeta(partition=0, offset=7, fallback_id=payload["correlation_id"])
    assert asyncio.run(runner.project_event(topic, payload, meta)) is True
    return _insert_columns(mock_db)


def test_terminal_model_reads_the_producer_bar() -> None:
    terminal = ModelDelegateSkillTerminalProjection.from_payload(
        _deployed_lane_terminal(correlation_id=str(uuid4()))
    )
    assert terminal.required_bar == 0.85


def test_kafka_writer_binds_the_declared_bar() -> None:
    columns = _project(_deployed_lane_terminal(correlation_id=str(uuid4())))
    assert columns.get("required_bar") == 0.85


def test_kafka_writer_binds_a_reported_actual_score() -> None:
    payload = _deployed_lane_terminal(correlation_id=str(uuid4()))
    payload["actual_score"] = 0.9
    columns = _project(payload)
    assert columns.get("actual_score") == 0.9


def test_unscored_terminal_names_neither_column() -> None:
    payload = _deployed_lane_terminal(correlation_id=str(uuid4()), bar=None)
    payload.update(
        status="failed",
        quality_gate_passed=False,
        quality_score=0.0,
        error_message="inference_failed",
    )
    columns = _project(payload)
    assert "required_bar" not in columns
    assert "actual_score" not in columns
