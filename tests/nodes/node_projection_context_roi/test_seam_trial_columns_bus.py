# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam test: the extended context-ROI row survives the bus and projects (OMN-23749).

Runs the try/control rows over a bus (the in-memory bus always, a real broker
when ``RUN_REDPANDA_E3_SEAM=1``), projects them through the real handler into an
in-memory database, and pins that the 14 new trial columns read back exactly
what was sent. The error seam dead-letters a malformed row, and the migration
seam pins the model's trial columns against migration 004's columns.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.events.context_roi import ModelAttemptReductionRow
from omnimarket.models.context_roi_trial_fields import (
    EnumContextRoiRowKind,
    ModelTrialFields,
)
from omnimarket.nodes.node_code_contract_score_effect.models.enum_code_score import (
    EnumCodeScoreVerdict,
)
from omnimarket.nodes.node_projection_context_roi.handlers.handler_projection_context_roi import (
    HandlerProjectionContextRoi,
    ModelContextRoiRunCompletedEvent,
    ModelProjectionResult,
)
from omnimarket.projection.dlq import route_to_dlq
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
from tests.chains.piece16.test_piece16_chain import (
    MemoryChainBus,
    ProtocolChainBus,
    RedpandaChainBus,
)

pytestmark = pytest.mark.unit

HANDLER = HandlerProjectionContextRoi()
RUNNER_TERMINAL_TOPIC = "onex.evt.omnimarket.context-roi-run-completed.v1"
APPLIED_TOPIC = "onex.evt.omnimarket.projection-context-roi-applied.v1"
DLQ_TOPIC = "onex.dlq.omnimarket.projection-context-roi-malformed.v1"
MIGRATION_PATH = Path(
    "src/omnimarket/nodes/node_projection_context_roi/migrations/004_add_code_eval_trial_columns.sql"
)
REDPANDA_FLAG = "RUN_REDPANDA_E3_SEAM"

BACKENDS = ["memory"] + (["redpanda"] if os.environ.get(REDPANDA_FLAG) == "1" else [])


@pytest.fixture(params=BACKENDS)
def bus(request: pytest.FixtureRequest) -> ProtocolChainBus:
    return MemoryChainBus() if request.param == "memory" else RedpandaChainBus()


def _row(
    *,
    correlation_id: str,
    row_kind: EnumContextRoiRowKind = EnumContextRoiRowKind.TRY,
    check_verdict: EnumCodeScoreVerdict | None = None,
    size_band: str = "S",
    try_ordinal: int = 0,
) -> ModelAttemptReductionRow:
    return ModelAttemptReductionRow(
        run_id="run-001",
        correlation_id=correlation_id,
        task_id="task-A",
        run_order=1,
        context_factor_subset="golden_exemplar",
        context_pack_hash="abc123",
        attempt_count=1,
        first_pass_success=True,
        final_success=True,
        prompt_tokens=100,
        completion_tokens=50,
        estimated_cost=0.0,
        model_id="qwen3-coder-30b",
        provider="local",
        endpoint_ref="local-coder",
        row_kind=row_kind,
        check_verdict=check_verdict,
        size_band=size_band,
        try_ordinal=try_ordinal,
    )


def seam_problems(model_columns: set[str], migration_columns: set[str]) -> list[str]:
    """The column names that differ between the model and the migration."""
    problems: list[str] = []
    for column in sorted(model_columns - migration_columns):
        problems.append(f"model column {column!r} missing from migration")
    for column in sorted(migration_columns - model_columns):
        problems.append(f"migration column {column!r} missing from model")
    return problems


@pytest.mark.asyncio
async def test_golden_seam_run_completed_event_survives_the_bus_and_projects(
    bus: ProtocolChainBus,
) -> None:
    """A run-completed event round-trips the bus and projects all trial columns."""
    db = InmemoryDatabaseAdapter()

    try_row = ModelAttemptReductionRow(
        run_id="run-001",
        correlation_id="row-try-1",
        task_id="task-A",
        run_order=1,
        context_factor_subset="golden_exemplar",
        context_pack_hash="abc123",
        attempt_count=1,
        first_pass_success=True,
        final_success=True,
        prompt_tokens=100,
        completion_tokens=50,
        estimated_cost=0.0,
        model_id="qwen3-coder-30b",
        provider="local",
        endpoint_ref="local-coder",
        row_kind=EnumContextRoiRowKind.TRY,
        check_verdict=EnumCodeScoreVerdict.PASS,
        size_band="M",
        try_ordinal=2,
        max_turns=8,
        try_timeout_s=90.0,
        generation_s=1.5,
        env_sync_s=0.5,
        check_s=0.25,
        sequence_id="seq-1",
        diff_hash="abc",
        suite_digest="def",
        try_end="completed",
    )
    control_row = _row(
        correlation_id="row-control-1",
        row_kind=EnumContextRoiRowKind.CONTROL,
    )
    event = ModelContextRoiRunCompletedEvent(
        run_id="run-001", rows=(try_row, control_row)
    )

    delivered = await bus.roundtrip(
        RUNNER_TERMINAL_TOPIC, event.model_dump_json().encode()
    )
    parsed = ModelContextRoiRunCompletedEvent.model_validate_json(delivered)

    out = HANDLER.handle({**parsed.model_dump(mode="json"), "_db": db})
    projection = ModelProjectionResult(
        rows_upserted=out["rows_upserted"],
        table=out["table"],
    )
    await bus.roundtrip(APPLIED_TOPIC, projection.model_dump_json().encode())

    rows = db.query("context_roi_scores")
    by_id = {row["correlation_id"]: row for row in rows}
    stored = by_id["row-try-1"]
    for name in ModelTrialFields.model_fields:
        assert stored[name] == getattr(try_row, name), (
            f"column {name!r} drifted over the bus"
        )

    assert projection.rows_upserted == 2


@pytest.mark.asyncio
async def test_error_seam_malformed_row_after_the_bus_dead_letters(
    bus: ProtocolChainBus,
) -> None:
    """A malformed row raises ValidationError, writes no row, and dead-letters."""
    db = InmemoryDatabaseAdapter()
    message = {
        "run_id": "run-bad-001",
        "rows": [
            {"correlation_id": "run-chain-bad", "row_kind": "bogus", "size_band": "XL"}
        ],
    }
    delivered = await bus.roundtrip(RUNNER_TERMINAL_TOPIC, json.dumps(message).encode())
    payload = json.loads(delivered)

    with pytest.raises(ValidationError):
        HANDLER.handle({**payload, "_db": db})
    assert db.query("context_roi_scores") == [], "no row may be written on failure"

    published: list[tuple[str, bytes]] = []

    async def _publish(topic: str, value: bytes) -> None:
        published.append((topic, value))

    routed = await route_to_dlq(
        publish=_publish,
        dlq_topics=[DLQ_TOPIC],
        original_message=message,
        failure_reason="malformed row",
        handler="HandlerProjectionContextRoi",
        correlation_id="run-chain-bad",
    )
    assert routed is True
    assert [topic for topic, _ in published] == [DLQ_TOPIC]
    envelope = json.loads(published[0][1])
    assert envelope["handler"] == "HandlerProjectionContextRoi"

    dead = await bus.roundtrip(DLQ_TOPIC, json.dumps(envelope).encode())
    assert json.loads(dead)["handler"] == "HandlerProjectionContextRoi"


def test_seam_model_columns_equal_migration_004_columns() -> None:
    """The model's trial columns equal migration 004's added columns."""
    sql = MIGRATION_PATH.read_text(encoding="utf-8")
    migration_columns = set(re.findall(r"ADD COLUMN IF NOT EXISTS\s+(\w+)", sql))
    model_columns = set(ModelTrialFields.model_fields)
    assert seam_problems(model_columns, migration_columns) == []


def test_failing_control_a_column_renamed_on_one_side_turns_the_seam_red() -> None:
    """A renamed column on one side turns the seam red; the real sets agree."""
    model_columns = set(ModelTrialFields.model_fields)
    sql = MIGRATION_PATH.read_text(encoding="utf-8")
    migration_columns = set(re.findall(r"ADD COLUMN IF NOT EXISTS\s+(\w+)", sql))

    renamed = set(migration_columns)
    renamed.discard("check_s")
    renamed.add("check_seconds")
    problems = seam_problems(model_columns, renamed)
    assert problems, "a renamed column must turn the seam red"
    assert any("check_s" in problem for problem in problems)
    assert any("check_seconds" in problem for problem in problems)

    assert seam_problems(model_columns, migration_columns) == []
