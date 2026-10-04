# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Chain gate: an adr-canary-completed event must become a capability_scores row.

``public.capability_scores`` held zero rows on the .201 dev lane. Three hops
sit between a canary run and that table, and the first two were never
exercised end to end:

1. ``node_adr_canary_orchestrator`` publishes
   ``onex.evt.omnimarket.adr-canary-completed.v1``. Only a canary command
   produces it; a delegated call never does.
2. ``node_canary_score_reducer`` subscribes to that topic and the consolidated
   ``tenant-projection`` writer attaches a consumer group to it. That hop works.
3. Because the contract declares ``db_io.db_tables``, auto-wiring selects the
   PROJECTION arm, which calls ``handler.handle(input_data)`` with the runtime's
   own ``_db`` adapter. ``HandlerCanaryScoreReducer`` exposed only
   ``accumulate()`` and ``materialize()``, so the first event would die on
   ``AttributeError: ... has no attribute 'handle'`` and no row could ever be
   written, even with a producer.

Nothing wrote the rows ``materialize()`` computes: the repo's tests called
``accumulate()`` and ``materialize()`` directly and never entered the real
dispatch seam, the same blind spot ``test_event_chain_gate_projection_write.py``
documents for the delegation projections.

What is real here: the real ``contract.yaml``, the real auto-wiring arm
selection, the real dispatch engine and the real ``ProjectionDatabaseOperations``
(its DSN points at the discard port, so the write is guaranteed to fail and the
only question is whether the handler was entered and reached the database). The
row content is asserted against the in-memory projection adapter.
"""

from __future__ import annotations

import pytest

from omnimarket.events.canary import ModelCanaryReport, ModelModelScore
from omnimarket.nodes.node_canary_score_reducer.handlers.handler_canary_score_reducer import (
    TASK_TYPE,
    HandlerCanaryScoreReducer,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
from omnimarket.projection.tenant_isolation import HOUSE_TENANT_UUID
from tests.chains.test_event_chain_gate_projection_write import (
    ProjectionChainCase,
    _run_projection_chain,
)

CANARY_COMPLETED_TOPIC = "onex.evt.omnimarket.adr-canary-completed.v1"
CAPABILITY_SCORES_TABLE = "capability_scores"


def _report(*, success: bool = True) -> ModelCanaryReport:
    return ModelCanaryReport(
        run_id="20261003-230000-abc123",
        manifest_path="/fake/manifest.yaml",
        entries_total=10,
        entries_completed=9,
        entries_failed=1,
        model_scores=[
            ModelModelScore(
                model_key="qwen3.8-27b",
                entries_evaluated=10,
                entries_failed=1,
                avg_recall=0.9,
                avg_precision=0.8,
                avg_fidelity=0.7,
                avg_format_compliance=1.0,
                total_latency_ms=5000,
                estimated_cost_usd=0.05,
            ),
            ModelModelScore(model_key="no-grades", entries_evaluated=4),
        ],
        evidence_dir="/fake/evidence",
        scorecard_path="/fake/scorecard.md",
        success=success,
    )


CANARY_SCORE_CHAIN_CASE = ProjectionChainCase(
    chain_id="canary-score-reducer-write",
    node_dir="node_canary_score_reducer",
    entry_topic=CANARY_COMPLETED_TOPIC,
    dlq_topic="onex.dlq.omnimarket.canary-score-reducer-malformed.v1",
    writer_handler_name="HandlerCanaryScoreReducer",
    wire_payload=_report().model_dump(mode="json"),
)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_canary_completed_event_reaches_the_database(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The real dispatch enters the reducer and the handler attempts the write.

    On the broken wiring the handler had no ``handle()``, so the dispatch
    raised ``AttributeError`` before any database call. With the write aimed at
    the discard port, the correct outcome is the OMN-17379 fail-closed raise
    (a write-path failure withholds the offset), not an AttributeError.
    """
    run = await _run_projection_chain(CANARY_SCORE_CHAIN_CASE, monkeypatch, caplog)

    assert CANARY_SCORE_CHAIN_CASE.writer_handler_name in run.handlers_entered
    missing_entrypoint = [
        record.getMessage()
        for record in caplog.records
        if "has no attribute 'handle'" in record.getMessage()
    ]
    assert not missing_entrypoint, (
        "the projection arm called handle() on HandlerCanaryScoreReducer, which "
        f"does not define it, so no capability_scores row can be written: "
        f"{missing_entrypoint}"
    )
    assert run.not_materialized_failures, (
        "the write to the discard port did not fail closed, so the handler never "
        f"reached the database. DLQ reasons: {run.dlq_failure_reasons or '(none)'}"
    )
    assert not run.terminal_messages


@pytest.mark.unit
def test_successful_report_upserts_one_row_per_scored_model() -> None:
    """handle() turns the wire payload into capability_scores rows, keyed per model."""
    db = InmemoryDatabaseAdapter()
    payload: dict[str, object] = {
        **_report().model_dump(mode="json"),
        "_db": db,
        "_topic": CANARY_COMPLETED_TOPIC,
        "_partition": 0,
        "_offset": 7,
    }

    result = HandlerCanaryScoreReducer().handle(payload)

    rows = {str(r["model_key"]): r for r in db.tables[CAPABILITY_SCORES_TABLE]}
    assert set(rows) == {"qwen3.8-27b", "no-grades"}
    scored = rows["qwen3.8-27b"]
    assert scored["task_type"] == TASK_TYPE
    expected_composite = 0.9 * 0.35 + 0.8 * 0.35 + 0.7 * 0.20 + 1.0 * 0.10
    assert scored["success_rate"] == pytest.approx(expected_composite)
    assert (
        scored["total_count"],
        scored["success_count"],
        scored["failure_count"],
    ) == (
        10,
        9,
        1,
    )
    # avg_latency_ms is an INT column; a float would be refused by the driver.
    assert scored["avg_latency_ms"] == 500
    assert isinstance(scored["avg_latency_ms"], int)
    assert scored["total_cost"] == pytest.approx(0.05)
    assert "last_updated" in scored
    # RLS admits the row only under the tenant it names; scores are platform-own.
    assert scored["tenant_id"] == str(HOUSE_TENANT_UUID)
    # The model with no grades still gets a row; the NOT NULL columns get 0.
    assert rows["no-grades"]["success_rate"] == 0.0
    assert rows["no-grades"]["total_cost"] == 0.0
    assert result["rows_upserted"] == 2


@pytest.mark.unit
def test_upsert_is_idempotent_per_model_and_task_type() -> None:
    """Redelivery of the same event overwrites the row; it never adds a second."""
    db = InmemoryDatabaseAdapter()
    payload: dict[str, object] = {**_report().model_dump(mode="json"), "_db": db}

    handler = HandlerCanaryScoreReducer()
    handler.handle(dict(payload))
    handler.handle(dict(payload))

    assert len(db.tables[CAPABILITY_SCORES_TABLE]) == 2


@pytest.mark.unit
def test_unsuccessful_report_writes_nothing() -> None:
    """A failed canary run must not move the learned scores."""
    db = InmemoryDatabaseAdapter()
    payload: dict[str, object] = {
        **_report(success=False).model_dump(mode="json"),
        "_db": db,
    }

    result = HandlerCanaryScoreReducer().handle(payload)

    assert db.tables.get(CAPABILITY_SCORES_TABLE, []) == []
    assert result["rows_upserted"] == 0


@pytest.mark.unit
def test_handle_refuses_a_missing_database_adapter() -> None:
    """No adapter is a wiring bug, and it must be loud rather than a silent no-op."""
    with pytest.raises(TypeError, match="DatabaseAdapter"):
        HandlerCanaryScoreReducer().handle(_report().model_dump(mode="json"))
