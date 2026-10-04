# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20168: each durable attempt retains its lineage and placement."""

from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from pydantic import ValidationError

from omnimarket.enums.enum_delegation_attempt_kind import EnumDelegationAttemptKind
from omnimarket.models.delegation.delegation_attempt_lineage import (
    ATTEMPT_ID_NAMESPACE,
    attempt_id_for,
    endpoint_host,
    stamp_attempt_lineage,
)
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillAttemptRecord,
)
from omnimarket.models.model_delegation_split_recombine import EnumDelegationSizeBand
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    _attempt_records,
    _lineage_fields,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_llm_delegation_call_effect.models.model_llm_delegation_call_result import (
    ModelLlmDelegationCallResult,
)

pytestmark = pytest.mark.unit

_LINEAGE_KEYS = (
    "attempt_id",
    "attempt_kind",
    "parent_attempt_id",
    "split_id",
    "host",
    "size_band",
)
_SPLIT_KINDS = (
    EnumDelegationAttemptKind.SPLIT_CHILD,
    EnumDelegationAttemptKind.UNIT_ESCALATION,
    EnumDelegationAttemptKind.RECOMBINE_CHECK,
)


def _attempt(
    *, tier: str, passed: bool, score: float, decision: str, reason: str
) -> dict[str, object]:
    """One rung carrying the full local attempt-ladder seam's key set."""
    return {
        "tier": tier,
        "backend_id": f"backend-{tier}",
        "model_id": f"model-{tier}",
        "quality_gate_passed": passed,
        "quality_score": score,
        "cost_usd": 0.0,
        "failure_class": None,
        "error_message": "",
        "acceptance_decision": decision,
        "acceptance_reason": reason,
        "acceptance_detail": f"actual_score={score:.3f} required_bar=0.800",
        "input_tokens_measured": 120,
        "input_token_budget": 8000,
        "reasoning_preamble_rule": "no_boundary_found",
        "reasoning_preamble": "",
        "host": "lab-host-1",
        "rubric_verdict": {
            "rubric_version": "delegation-class-rubrics.v1",
            "task_class": "document",
            "outcome": "PASS" if passed else "FAIL",
        },
    }


def _ladder(length: int) -> list[dict[str, object]]:
    return [
        _attempt(
            tier=f"tier-{index}",
            passed=index == length - 1,
            score=0.9 if index == length - 1 else 0.4,
            decision="accept" if index == length - 1 else "climb",
            reason=(
                "quality_bar_met"
                if index == length - 1
                else "deterministic_floor_failed"
            ),
        )
        for index in range(length)
    ]


def _project(
    tmp_path: Path, attempts: list[dict[str, object]], correlation_id: UUID
) -> list[dict[str, Any]]:
    """Write through the real local port and read its SQLite attempt_history."""
    db_path = tmp_path / "delegation.sqlite"
    port = LocalDelegationDispatchPort(evidence_db_path=db_path)
    port._project_evidence(
        correlation_id=correlation_id,
        task_type="document",
        endpoint_ref="local",
        model_id="model-local",
        result=ModelLlmDelegationCallResult(
            request_id="req-omn20168",
            success=True,
            content="an answer",
            tokens_in=11,
            tokens_out=22,
            latency_ms=33,
            actual_cost_usd=Decimal("0"),
            savings_usd=Decimal("0.5"),
        ),
        prompt="the prompt",
        source_session_id=None,
        tenant_id="omninode",
        quality_passed=True,
        failure_message="",
        cost_usd=Decimal("0"),
        baseline_savings=None,
        escalation_count=len(attempts) - 1,
        attempts=attempts,
        actual_score=0.9,
        required_bar=0.8,
    )
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT * FROM delegation_events WHERE correlation_id = ?",
            (str(correlation_id),),
        ).fetchone()
    finally:
        connection.close()
    assert row is not None, "the evidence write produced no row"
    assert row["task_type"] == "document"
    return json.loads(row["attempt_history"])


@pytest.mark.parametrize(
    "kind",
    list(EnumDelegationAttemptKind),
    ids=[
        f"test_{kind.value}_attempt_writes_a_row_with_the_lineage_fields"
        for kind in EnumDelegationAttemptKind
    ],
)
def test_attempt_writes_a_row_with_the_lineage_fields(
    tmp_path: Path, kind: EnumDelegationAttemptKind
) -> None:
    correlation_id = uuid4()
    attempts = _ladder(1 if kind is EnumDelegationAttemptKind.FIRST_TRY else 2)
    index = len(attempts) - 1
    parent = None if index == 0 else str(attempt_id_for(correlation_id, index - 1))
    split_id = str(uuid4()) if kind in _SPLIT_KINDS else None
    if kind not in (
        EnumDelegationAttemptKind.FIRST_TRY,
        EnumDelegationAttemptKind.WHOLE_ESCALATION,
    ):
        attempts[-1].update(attempt_kind=kind.value, parent_attempt_id=parent)
        if split_id is not None:
            attempts[-1]["split_id"] = split_id
    stored = _project(tmp_path, attempts, correlation_id)
    assert len(stored) == len(attempts)
    rung = stored[-1]
    assert rung["backend_id"] == attempts[-1]["backend_id"]
    assert rung["model_id"] == attempts[-1]["model_id"]
    assert rung["host"] == "lab-host-1"
    assert isinstance(rung["attempt_id"], str)
    assert UUID(rung["attempt_id"]) == attempt_id_for(correlation_id, index)
    assert rung["attempt_kind"] == kind.value
    assert rung["parent_attempt_id"] == parent
    assert rung["split_id"] == split_id
    assert rung["size_band"] is None
    assert rung["rubric_verdict"]["rubric_version"] == "delegation-class-rubrics.v1"
    assert rung["rubric_verdict"]["outcome"] == "PASS"
    for entry, source in zip(stored, attempts, strict=True):
        assert entry["rubric_verdict"]["outcome"] == source["rubric_verdict"]["outcome"]


def test_lineage_defaults_link_each_rung_to_the_previous_attempt(
    tmp_path: Path,
) -> None:
    correlation_id = uuid4()
    stored = _project(tmp_path, _ladder(3), correlation_id)
    assert [rung["attempt_kind"] for rung in stored] == [
        "first_try",
        "whole_escalation",
        "whole_escalation",
    ]
    for index, rung in enumerate(stored):
        assert rung["attempt_id"] == str(attempt_id_for(correlation_id, index))
        assert rung["parent_attempt_id"] == (
            None if index == 0 else stored[index - 1]["attempt_id"]
        )
        assert rung["split_id"] is None


def test_size_band_is_a_present_null_on_every_stored_rung(tmp_path: Path) -> None:
    for rung in _project(tmp_path, _ladder(3), uuid4()):
        assert "size_band" in rung
        assert rung["size_band"] is None


def _valid_lineage(kind: EnumDelegationAttemptKind) -> dict[str, Any]:
    return {
        **_ladder(1)[0],
        "attempt_kind": kind,
        "attempt_id": uuid4(),
        "parent_attempt_id": (
            None if kind is EnumDelegationAttemptKind.FIRST_TRY else uuid4()
        ),
        "split_id": uuid4() if kind in _SPLIT_KINDS else None,
    }


@pytest.mark.parametrize("kind", list(EnumDelegationAttemptKind))
def test_each_kind_requires_an_attempt_id(kind: EnumDelegationAttemptKind) -> None:
    with pytest.raises(ValidationError, match=f"{kind.value}.*attempt_id"):
        ModelDelegateSkillAttemptRecord.model_validate(
            _valid_lineage(kind) | {"attempt_id": None}
        )


@pytest.mark.parametrize("kind", list(EnumDelegationAttemptKind))
def test_each_kind_refuses_itself_as_parent(kind: EnumDelegationAttemptKind) -> None:
    raw = _valid_lineage(kind)
    raw["parent_attempt_id"] = raw["attempt_id"]
    with pytest.raises(ValidationError, match=f"{kind.value}.*parent_attempt_id"):
        ModelDelegateSkillAttemptRecord.model_validate(raw)


@pytest.mark.parametrize(
    "kind", [kind for kind in EnumDelegationAttemptKind if kind.value != "first_try"]
)
def test_later_kinds_require_a_parent(kind: EnumDelegationAttemptKind) -> None:
    with pytest.raises(ValidationError, match=f"{kind.value}.*parent_attempt_id"):
        ModelDelegateSkillAttemptRecord.model_validate(
            _valid_lineage(kind) | {"parent_attempt_id": None}
        )


def test_first_try_forbids_a_parent() -> None:
    with pytest.raises(ValidationError, match=r"first_try.*parent_attempt_id"):
        ModelDelegateSkillAttemptRecord.model_validate(
            _valid_lineage(EnumDelegationAttemptKind.FIRST_TRY)
            | {"parent_attempt_id": uuid4()}
        )


@pytest.mark.parametrize(
    "kind",
    [
        EnumDelegationAttemptKind.FIRST_TRY,
        EnumDelegationAttemptKind.WHOLE_ESCALATION,
        EnumDelegationAttemptKind.CONFIG_SWAP,
    ],
)
def test_whole_request_kinds_forbid_a_split(kind: EnumDelegationAttemptKind) -> None:
    with pytest.raises(ValidationError, match=f"{kind.value}.*split_id"):
        ModelDelegateSkillAttemptRecord.model_validate(
            _valid_lineage(kind) | {"split_id": uuid4()}
        )


@pytest.mark.parametrize("kind", _SPLIT_KINDS)
def test_split_kinds_require_a_split(kind: EnumDelegationAttemptKind) -> None:
    with pytest.raises(ValidationError, match=f"{kind.value}.*split_id"):
        ModelDelegateSkillAttemptRecord.model_validate(
            _valid_lineage(kind) | {"split_id": None}
        )


def test_a_record_predating_lineage_decodes_with_all_six_fields_none() -> None:
    record = ModelDelegateSkillAttemptRecord.model_validate(
        _ladder(1)[0] | dict.fromkeys(_LINEAGE_KEYS)
    )
    assert all(getattr(record, key) is None for key in _LINEAGE_KEYS)


def test_stamping_is_pure_idempotent_and_keeps_explicit_values() -> None:
    correlation_id = uuid4()
    explicit = {
        "attempt_id": str(uuid4()),
        "attempt_kind": "config_swap",
        "parent_attempt_id": str(uuid4()),
        "split_id": None,
        "host": "custom-host",
        "size_band": None,
    }
    attempts = _ladder(2)
    attempts[-1].update(explicit)
    before = deepcopy(attempts)
    stamped = stamp_attempt_lineage(attempts, correlation_id=correlation_id)
    assert attempts == before
    assert all(new is not old for new, old in zip(stamped, attempts, strict=True))
    assert {key: stamped[-1][key] for key in explicit} == explicit
    assert stamp_attempt_lineage(stamped, correlation_id=correlation_id) == stamped


def test_stamping_keeps_explicit_nulls_even_when_a_default_would_differ() -> None:
    raw = [dict.fromkeys(_LINEAGE_KEYS)]
    assert stamp_attempt_lineage(raw, correlation_id=uuid4()) == raw


def test_stamping_serializes_uuid_ids_and_enum_kind() -> None:
    raw = _valid_lineage(EnumDelegationAttemptKind.SPLIT_CHILD)
    stamped = stamp_attempt_lineage([raw], correlation_id=uuid4())[0]
    for key in ("attempt_id", "parent_attempt_id", "split_id"):
        assert stamped[key] == str(raw[key])
    assert stamped["attempt_kind"] == "split_child"
    json.dumps(stamped)


def test_attempt_ids_use_the_delegation_namespace_and_refuse_negative_indices() -> None:
    correlation_id = uuid4()
    assert uuid5(NAMESPACE_URL, "omnimarket:delegation-attempt") == ATTEMPT_ID_NAMESPACE
    assert attempt_id_for(correlation_id, 2) == uuid5(
        ATTEMPT_ID_NAMESPACE, f"{correlation_id}:2"
    )
    with pytest.raises(ValueError, match="attempt_index"):
        attempt_id_for(correlation_id, -1)


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("https://LAB-HOST-1:8443/v1", "lab-host-1"),
        ("https://user:secret@Example.COM/path", "example.com"),
        ("http://[::1]:8000/v1", "::1"),
        ("local", None),
        ("", None),
        ("http://[invalid", None),
    ],
)
def test_endpoint_host_never_raises(endpoint: str, expected: str | None) -> None:
    assert endpoint_host(endpoint) == expected


def _rung(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "tier_name": "local",
        "model_used": "m",
        "backend_ref": "backend-local",
        "host": "lab-host-1",
        "quality_score": 0.0,
        "latency_ms": 1,
        "fallback_recommended": True,
        "attempted_at": datetime.now(UTC).isoformat(),
        "acceptance_decision": "climb",
        "acceptance_reason": "provider_call_failed",
    }
    payload.update(overrides)
    return payload


def test_bus_attempt_records_keep_hosts_and_stamp_the_escalation_history() -> None:
    correlation_id = uuid4()
    records = _attempt_records(
        {
            "correlation_id": str(correlation_id),
            "escalation_history": [
                _rung(),
                _rung(
                    tier_name="claude",
                    model_used="m-cloud",
                    backend_ref="backend-cloud",
                    quality_score=0.9,
                    acceptance_decision="accept",
                    acceptance_reason="quality_bar_met",
                ),
            ],
        }
    )
    assert [record.attempt_kind for record in records] == [
        EnumDelegationAttemptKind.FIRST_TRY,
        EnumDelegationAttemptKind.WHOLE_ESCALATION,
    ]
    assert [record.attempt_id for record in records] == [
        attempt_id_for(correlation_id, index) for index in range(2)
    ]
    assert records[0].parent_attempt_id is None
    assert records[1].parent_attempt_id == records[0].attempt_id
    assert [record.host for record in records] == ["lab-host-1", "lab-host-1"]
    assert [record.backend_id for record in records] == [
        "backend-local",
        "backend-cloud",
    ]


@pytest.mark.parametrize("correlation_id", [None, "not-a-uuid"])
def test_an_unparseable_correlation_leaves_lineage_unstamped(
    correlation_id: object,
) -> None:
    record = _attempt_records(
        {"correlation_id": correlation_id, "attempts": _ladder(1)}
    )[0]
    assert record.attempt_id is None
    assert record.attempt_kind is None
    assert record.host == "lab-host-1"


@pytest.mark.parametrize("history", [False, True])
def test_invalid_lineage_falls_back_without_losing_the_terminal(history: bool) -> None:
    raw = _rung() if history else _ladder(1)[0]
    raw.update(attempt_kind="split_child", split_id="malformed", host="lab-host-1")
    record = _attempt_records(
        {
            "correlation_id": str(uuid4()),
            "escalation_history" if history else "attempts": [raw],
        }
    )[0]
    assert record.model_id == ("m" if history else "model-tier-0")
    assert all(getattr(record, key) is None for key in _LINEAGE_KEYS)


def test_lineage_fields_type_valid_values_and_drop_malformed_values() -> None:
    raw = _valid_lineage(EnumDelegationAttemptKind.SPLIT_CHILD)
    raw["size_band"] = "small"
    fields = _lineage_fields(raw)
    for key in ("attempt_id", "parent_attempt_id", "split_id"):
        assert isinstance(fields[key], UUID)
    assert fields["attempt_kind"] is EnumDelegationAttemptKind.SPLIT_CHILD
    assert fields["size_band"] is EnumDelegationSizeBand.SMALL
    assert fields["host"] == "lab-host-1"
    malformed = dict.fromkeys(_LINEAGE_KEYS, "malformed") | {"host": 1}
    assert all(value is None for value in _lineage_fields(malformed).values())
