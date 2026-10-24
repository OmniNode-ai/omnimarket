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
    CONFLICT_KEY,
    TABLE,
    HandlerProjectionDelegation,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
from omnimarket.projection.tenant_isolation import HOUSE_TENANT_SLUG
from tests.helpers.tenant_registry import (
    PROJECTION_TENANT_SLUG,
    PROJECTION_TENANT_UUID,
    seed_tenant_registry,
)

_ROOT = Path(__file__).resolve().parents[3]
_PROJECTION_CONTRACT_PATH = (
    _ROOT / "src/omnimarket/nodes/node_projection_delegation/contract.yaml"
)
_QUALITY_GATE_RESULT_TOPIC = "onex.evt.omnibase-infra.quality-gate-result.v1"


_ENVELOPE_TIMESTAMP = "2026-09-08T10:02:41.550000+00:00"


def _envelope_for(correlation_id: UUID) -> dict[str, object]:
    """The envelope keys ``unwrap_envelope`` attaches to every delivered payload.

    OMN-18565: it also carries ``tenant_id``, for the same reason and with the
    same consequence one field over. ``ModelQualityGateResult`` declares no
    tenant field either, so the envelope stamp is the verdict's only possible
    attribution -- and an unattributed verdict now authors NO row rather than
    taking the house tenant, because a house-attributed verdict that wins the
    race against its own terminal makes the terminal's conflict-update
    unwritable under FORCE ROW LEVEL SECURITY. These tests are about the
    verdict's own columns, so the envelope supplies the attribution they do not
    exist to test.

    OMN-15583: ``ModelEventEnvelope.envelope_timestamp`` is
    ``default_factory``-populated, so every real record on this topic carries
    one -- and it is the ONLY authoritative event time a quality-gate-result
    projection can see, because ``ModelQualityGateResult`` is ``extra="forbid"``
    and declares no time field. These tests previously handed ``handle()`` a
    bare payload with no envelope at all, which is why none of them noticed
    that the row this path proposes named no ``timestamp`` -- the NOT NULL
    column that made every verdict on onex-dev poison to the DLQ.
    """
    return {
        "payload": {},
        "correlation_id": str(correlation_id),
        "event_type": "omnibase-infra.quality-gate-result",
        "envelope_timestamp": _ENVELOPE_TIMESTAMP,
        "tenant_id": HOUSE_TENANT_SLUG,
    }


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


class _RecordingPublisher:
    def __init__(self) -> None:
        self.messages: list[object] = []

    def publish(self, message: object) -> bool:
        self.messages.append(message)
        return True


def _terminal_row(correlation_id: UUID) -> dict[str, object]:
    return {
        "correlation_id": str(correlation_id),
        "tenant_id": "820272f9-4aaf-5add-a2df-0af942852ab2",
        "task_type": "code_generation",
        "delegated_to": "glm-5.2",
        "model_name": "glm-5.2",
        "backend_id": "zai",
        "host": "h201",
        "quality_gate_passed": True,
        "quality_gate_detail": "terminal accepted on the second rung",
        "actual_score": 0.97,
        "timestamp": "2026-01-01T00:00:00+00:00",
        "created_at": "2026-01-01T00:00:00+00:00",
        "score_source": None,
    }


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
    """OMN-19448 rule 1: a verdict-first event creates and republishes no row."""
    correlation_id = uuid4()
    event = _deterministic_result(correlation_id=correlation_id)
    db = InmemoryDatabaseAdapter()
    publisher = _RecordingPublisher()
    payload = event.model_dump(mode="json")
    payload["_db"] = db
    payload["_event_type"] = _QUALITY_GATE_RESULT_TOPIC
    payload["_envelope"] = _envelope_for(correlation_id)
    result = HandlerProjectionDelegation(publisher=publisher).handle(payload)
    assert result == {"rows_upserted": 0, "table": TABLE}
    assert db.query(TABLE, {"correlation_id": str(correlation_id)}) == []
    assert publisher.messages == []


@pytest.mark.unit
def test_deterministic_quality_gate_result_records_failure_detail() -> None:
    """OMN-19448 rule 1: a failed rung cannot replace a passed terminal's detail."""
    correlation_id = uuid4()
    event = _deterministic_result(
        correlation_id=correlation_id,
        passed=False,
        quality_score=0.4,
        actual_score=0.4,
        failure_reasons=("missing required marker", "response too short"),
    )
    db = InmemoryDatabaseAdapter()
    terminal = _terminal_row(correlation_id)
    db.upsert(TABLE, CONFLICT_KEY, terminal)
    payload = event.model_dump(mode="json")
    payload["_db"] = db
    payload["_event_type"] = _QUALITY_GATE_RESULT_TOPIC
    payload["_envelope"] = _envelope_for(correlation_id)
    result = HandlerProjectionDelegation(publisher=_RecordingPublisher()).handle(
        payload
    )
    assert result == {"rows_upserted": 1, "table": TABLE}
    (row,) = db.query(TABLE, {"correlation_id": str(correlation_id)})
    assert {key: row[key] for key in terminal} == {
        **terminal,
        "score_source": SCORE_SOURCE_DETERMINISTIC_ACCEPTANCE,
    }
    assert row["quality_gate_passed"] is True
    assert row["quality_gate_detail"] == "terminal accepted on the second rung"
    assert row["actual_score"] == pytest.approx(0.97)


@pytest.mark.unit
@pytest.mark.parametrize("passed", [True, False])
def test_quality_gate_result_does_not_clobber_existing_terminal_fields(
    passed: bool,
) -> None:
    """OMN-19448 rule 1: the terminal owns every run fact except a blank source.

    OMN-19448 rule 2: passed and failed terminals retain model, backend and host.
    """
    correlation_id = uuid4()
    db = InmemoryDatabaseAdapter()
    seed_tenant_registry(db)
    publisher = _RecordingPublisher()
    handler = HandlerProjectionDelegation(publisher=publisher)
    terminal_payload: dict[str, object] = {
        "tenant_id": PROJECTION_TENANT_SLUG,
        "correlation_id": str(correlation_id),
        "task_type": "code_generation",
        "delegated_to": "claude",
        "model_name": "glm-5.2",
        "backend_id": "zai",
        "host": "h201",
        "quality_gate_passed": passed,
        "quality_gate_detail": "terminal decision",
        "actual_score": 0.97 if passed else 0.4,
        "timestamp": "2026-01-01T00:00:00+00:00",
        "_db": db,
        "_event_type": "onex.test.seed-terminal-row.v1",
    }
    assert handler.handle(dict(terminal_payload)) == {
        "rows_upserted": 1,
        "table": TABLE,
    }
    (before,) = db.query(TABLE, {"correlation_id": str(correlation_id)})
    assert before["task_type"] == "code_generation"
    assert before["delegated_to"] == "claude"
    assert before["model_name"] == "glm-5.2"
    assert before["backend_id"] == "zai"
    assert before["host"] == "h201"
    assert before["tenant_id"] == str(PROJECTION_TENANT_UUID)
    verdict_payload = _deterministic_result(
        correlation_id=correlation_id, passed=not passed
    ).model_dump(mode="json")
    verdict_payload["_db"] = db
    verdict_payload["_event_type"] = _QUALITY_GATE_RESULT_TOPIC
    verdict_payload["_envelope"] = {
        **_envelope_for(correlation_id),
        "tenant_id": PROJECTION_TENANT_SLUG,
    }
    assert handler.handle(verdict_payload) == {"rows_upserted": 1, "table": TABLE}
    (after,) = db.query(TABLE, {"correlation_id": str(correlation_id)})
    for column in (
        "task_type",
        "delegated_to",
        "model_name",
        "backend_id",
        "host",
        "quality_gate_passed",
        "quality_gate_detail",
        "actual_score",
        "timestamp",
        "tenant_id",
    ):
        assert after[column] == before[column], column
    assert after["quality_gate_passed"] is passed
    assert after["score_source"] == SCORE_SOURCE_DETERMINISTIC_ACCEPTANCE


@pytest.mark.unit
def test_quality_gate_result_projection_tolerates_topic_metadata_key_omn14855() -> None:
    """OMN-19448 rule 1: metadata is stripped before filling an existing row."""
    correlation_id = uuid4()
    event = _deterministic_result(correlation_id=correlation_id)
    db = InmemoryDatabaseAdapter()
    terminal = _terminal_row(correlation_id)
    db.upsert(TABLE, CONFLICT_KEY, terminal)
    payload = event.model_dump(mode="json")
    payload["_db"] = db
    payload["_event_type"] = _QUALITY_GATE_RESULT_TOPIC
    payload["_envelope"] = _envelope_for(correlation_id)
    payload["_topic"] = _QUALITY_GATE_RESULT_TOPIC
    result = HandlerProjectionDelegation(publisher=_RecordingPublisher()).handle(
        payload
    )
    assert result == {"rows_upserted": 1, "table": TABLE}
    (row,) = db.query(TABLE, {"correlation_id": str(correlation_id)})
    assert {key: row[key] for key in terminal} == {
        **terminal,
        "score_source": SCORE_SOURCE_DETERMINISTIC_ACCEPTANCE,
    }


@pytest.mark.unit
def test_result_first_quality_gate_result_stamps_created_at() -> None:
    """OMN-19448 rule 1: verdict-first creates no row or created_at stamp."""
    correlation_id = uuid4()
    db = InmemoryDatabaseAdapter()
    publisher = _RecordingPublisher()
    handler = HandlerProjectionDelegation(publisher=publisher)
    payload = _deterministic_result(correlation_id=correlation_id).model_dump(
        mode="json"
    )
    payload["_db"] = db
    payload["_event_type"] = _QUALITY_GATE_RESULT_TOPIC
    payload["_envelope"] = _envelope_for(correlation_id)
    assert handler.handle(payload) == {"rows_upserted": 0, "table": TABLE}
    assert db.query(TABLE) == []
    assert publisher.messages == []


@pytest.mark.unit
def test_quality_gate_result_does_not_clobber_existing_created_at() -> None:
    """OMN-19448 rule 1: a late verdict preserves created_at and terminal facts."""
    correlation_id = uuid4()
    db = InmemoryDatabaseAdapter()
    terminal = _terminal_row(correlation_id)
    db.upsert(TABLE, CONFLICT_KEY, terminal)
    verdict_payload = _deterministic_result(
        correlation_id=correlation_id, passed=False
    ).model_dump(mode="json")
    verdict_payload["_db"] = db
    verdict_payload["_event_type"] = _QUALITY_GATE_RESULT_TOPIC
    verdict_payload["_envelope"] = _envelope_for(correlation_id)
    result = HandlerProjectionDelegation(publisher=_RecordingPublisher()).handle(
        verdict_payload
    )
    assert result == {"rows_upserted": 1, "table": TABLE}
    (row,) = db.query(TABLE, {"correlation_id": str(correlation_id)})
    assert row["created_at"] == "2026-01-01T00:00:00+00:00"
    assert row["quality_gate_passed"] is True
    assert {key: row[key] for key in terminal} == {
        **terminal,
        "score_source": SCORE_SOURCE_DETERMINISTIC_ACCEPTANCE,
    }
