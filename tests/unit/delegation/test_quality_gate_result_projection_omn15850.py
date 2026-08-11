# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15850: quality-gate-result.v1 must reach the delegation_events projection.

Live-traced root cause (business-proof gate run 31535991358, correlation_id
2411cb46-39db-410f-be79-2487d16fd0a0): the deterministic-scoring path
(``score_source=deterministic_acceptance``, no LLM judge -- the default path
absent a judge-combinable task class) resolves via
``HandlerQualityGateIntent.handle_async`` (handler_quality_gate_intent.py:198-200),
which publishes ONLY ``ModelQualityGateResult`` to
``onex.evt.omnibase-infra.quality-gate-result.v1``. It publishes
``ModelDelegationJudgeVerdictEvent`` to
``onex.evt.omnibase-infra.delegation-judge-verdict.v1`` ONLY when
``judge_verdict is not None`` -- i.e. only for judge-combinable task classes with
no explicit response_contract. Before this fix, ``node_projection_delegation``'s
contract subscribed to the judge-verdict topic but never the
quality-gate-result topic, so a deterministic-path verdict had no consumer that
writes ``delegation_events`` for it via this route.

This matters because the business-proof gate's own ``quality_gate`` check
(``omninode_infra/scripts/evaluate_business_proof.py::_check_quality_gate``)
reads ``GET /v1/tenants/me/delegations`` (``routers/delegation_savings.py``),
which selects ``d.correlation_id AS run_id`` /
``"passed" if d.quality_gate_passed else "failed"`` straight off the
``delegation_events`` row -- an absent row is scored FAIL, never skip. The fix
target is ``delegation_events`` (the table this check reads), not
``delegation_judge_verdict_events`` (a distinct evidence table requiring judge
identity fields -- rubric_hash, prompt_hash, judge_model -- that
``ModelQualityGateResult`` does not carry).
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest
import yaml

from omnimarket.models.delegation.wire.model_quality_gate import (
    SCORE_SOURCE_DETERMINISTIC_ACCEPTANCE,
    ModelQualityGateResult,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    TABLE,
    HandlerProjectionDelegation,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

_ROOT = Path(__file__).resolve().parents[3]
_PROJECTION_CONTRACT_PATH = (
    _ROOT / "src/omnimarket/nodes/node_projection_delegation/contract.yaml"
)
_QUALITY_GATE_RESULT_TOPIC = "onex.evt.omnibase-infra.quality-gate-result.v1"


def _deterministic_result(
    *,
    correlation_id: UUID,
    passed: bool = True,
    quality_score: float = 0.91,
    actual_score: float | None = 0.91,
    failure_reasons: tuple[str, ...] = (),
) -> ModelQualityGateResult:
    return ModelQualityGateResult(
        correlation_id=correlation_id,
        passed=passed,
        quality_score=quality_score,
        score_source=SCORE_SOURCE_DETERMINISTIC_ACCEPTANCE,
        actual_score=actual_score,
        failure_reasons=failure_reasons,
    )


@pytest.mark.unit
def test_contract_subscribes_to_quality_gate_result_topic() -> None:
    """The deterministic verdict topic must be a declared subscription.

    Without this, the effects pod never wires a consumer for
    ``quality-gate-result.v1`` on ``node_projection_delegation`` at all --
    confirmed live: the pod subscribes this node to exactly its 8
    contract-declared topics, and separately wires the same topic only to
    the unrelated ``node_projection_live_events``.
    """
    projection = yaml.safe_load(_PROJECTION_CONTRACT_PATH.read_text())
    assert _QUALITY_GATE_RESULT_TOPIC in projection["event_bus"]["subscribe_topics"]


@pytest.mark.unit
def test_deterministic_quality_gate_result_projects_delegation_events_row() -> None:
    """A deterministic-path verdict (no judge event ever published) must still
    reach ``delegation_events`` -- the exact table/column the business-proof
    ``quality_gate`` check reads via ``GET /v1/tenants/me/delegations``.
    """
    correlation_id = uuid4()
    event = _deterministic_result(correlation_id=correlation_id)
    db = InmemoryDatabaseAdapter()
    payload = event.model_dump(mode="json")
    payload["_db"] = db
    payload["_event_type"] = _QUALITY_GATE_RESULT_TOPIC

    result = HandlerProjectionDelegation().handle(payload)

    assert result == {"rows_upserted": 1, "table": TABLE}
    row = db.query(TABLE, {"correlation_id": str(correlation_id)})[0]
    assert row["quality_gate_passed"] is True
    assert row["score_source"] == SCORE_SOURCE_DETERMINISTIC_ACCEPTANCE
    assert row["actual_score"] == pytest.approx(0.91)


@pytest.mark.unit
def test_deterministic_quality_gate_result_records_failure_detail() -> None:
    correlation_id = uuid4()
    event = _deterministic_result(
        correlation_id=correlation_id,
        passed=False,
        quality_score=0.4,
        actual_score=0.4,
        failure_reasons=("missing required marker", "response too short"),
    )
    db = InmemoryDatabaseAdapter()
    payload = event.model_dump(mode="json")
    payload["_db"] = db
    payload["_event_type"] = _QUALITY_GATE_RESULT_TOPIC

    HandlerProjectionDelegation().handle(payload)

    row = db.query(TABLE, {"correlation_id": str(correlation_id)})[0]
    assert row["quality_gate_passed"] is False
    assert row["quality_gate_detail"] == "missing required marker; response too short"


@pytest.mark.unit
def test_quality_gate_result_does_not_clobber_existing_terminal_fields() -> None:
    """Cross-boundary seam test: the SAME ``delegation_events`` row, the SAME
    ``correlation_id`` conflict key, written first by the canonical terminal
    path (``HandlerProjectionDelegation.project`` -- what a
    delegation-completed/failed.v1 event drives) and then updated by the new
    quality-gate-result path. Proves the two write paths share the exact
    table + key and that the new path performs a targeted-column UPSERT
    (matching ``PostgresSyncProjectionAdapter.upsert``'s
    ``ON CONFLICT ... DO UPDATE SET <only the incoming columns>``), not a
    full-row replace that would erase task_type/delegated_to/model_name
    written by the terminal event.
    """
    correlation_id = uuid4()
    db = InmemoryDatabaseAdapter()

    # Seed the row via the plain ModelTaskDelegatedEvent path (handle()'s
    # default fallback for an event_type that matches none of the special-cased
    # substrings: not judge-verdict, not node-generation, not a delegate-skill
    # terminal, not delegation-completed/-failed). This exercises the same
    # `project()` write path a canonical delegation-completed/failed.v1 event
    # ultimately reaches (via `_canonical_result_to_task_delegated_payload`),
    # against the identical `delegation_events` table + `correlation_id`
    # conflict key, without needing to replicate that converter's payload shape.
    terminal_payload: dict[str, object] = {
        "correlation_id": str(correlation_id),
        "task_type": "code_generation",
        "delegated_to": "claude",
        "model_name": "glm-5.2",
        "quality_gate_passed": False,
        "_db": db,
        "_event_type": "onex.test.seed-terminal-row.v1",
    }
    HandlerProjectionDelegation().handle(dict(terminal_payload))

    pre_row = db.query(TABLE, {"correlation_id": str(correlation_id)})[0]
    assert pre_row["task_type"] == "code_generation"
    assert pre_row["delegated_to"] == "claude"

    verdict_event = _deterministic_result(correlation_id=correlation_id, passed=True)
    verdict_payload = verdict_event.model_dump(mode="json")
    verdict_payload["_db"] = db
    verdict_payload["_event_type"] = _QUALITY_GATE_RESULT_TOPIC

    HandlerProjectionDelegation().handle(verdict_payload)

    post_row = db.query(TABLE, {"correlation_id": str(correlation_id)})[0]
    # Fields owned by the terminal write path are untouched.
    assert post_row["task_type"] == "code_generation"
    assert post_row["delegated_to"] == "claude"
    assert post_row["model_name"] == "glm-5.2"
    # Field owned by the quality-gate-result write path is updated.
    assert post_row["quality_gate_passed"] is True
    assert post_row["score_source"] == SCORE_SOURCE_DETERMINISTIC_ACCEPTANCE


@pytest.mark.unit
def test_quality_gate_result_projection_tolerates_topic_metadata_key_omn14855() -> None:
    """OMN-14855 regression parity: the multi-topic dispatch fan-out injects an
    envelope-only "_topic" key. ``ModelQualityGateResult`` sets
    ``extra="forbid"``, so an unstripped "_topic" would raise
    ``extra_forbidden`` and route the event to the malformed DLQ, exactly as
    it did for the judge-verdict event before that fix.
    """
    correlation_id = uuid4()
    event = _deterministic_result(correlation_id=correlation_id)
    db = InmemoryDatabaseAdapter()
    payload = event.model_dump(mode="json")
    payload["_db"] = db
    payload["_event_type"] = _QUALITY_GATE_RESULT_TOPIC
    payload["_topic"] = _QUALITY_GATE_RESULT_TOPIC

    result = HandlerProjectionDelegation().handle(payload)

    assert result == {"rows_upserted": 1, "table": TABLE}
