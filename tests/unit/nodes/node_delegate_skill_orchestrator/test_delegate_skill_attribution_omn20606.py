# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-process evidence terminal names its caller, ticket and lineage (OMN-20606).

On the in-process path the port's own evidence terminal is the only terminal
that reaches the bus (OMN-20154). It never copied the request's caller lane or
ticket, so every in-process fallback row on the h201 dev lane had an empty
caller_lane, and it had no way to name the failed delegation it answered.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from omnimarket.events.emit_effect_topic_publisher import EmitEffectTopicPublisher
from omnimarket.models.delegation.delegation_lineage import LINEAGE_KEYS
from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    _attribution_dispatch_kwargs,
    _request_attribution,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_llm_delegation_call_effect.models.model_llm_delegation_call_result import (
    ModelLlmDelegationCallResult,
)

pytestmark = pytest.mark.unit

_PARENT = "5b0c8a52-9a43-4f3c-a9f1-0d6f2b4e7c11"
_LANE = "deleg-fallback-mark-9143"
_LINEAGE: dict[str, str] = {
    "parent_correlation_id": _PARENT,
    "lineage_kind": "fallback",
    "parent_failure_cause": "provider_quota_exhausted",
}


def _row(db_path: Path, correlation_id: str) -> dict[str, Any]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        found = conn.execute(
            "SELECT * FROM delegation_events WHERE correlation_id = ?",
            (correlation_id,),
        ).fetchone()
    finally:
        conn.close()
    assert found is not None
    return dict(found)


def _request(**metadata: str) -> ModelDelegateSkillRequest:
    return ModelDelegateSkillRequest(
        prompt="summarize the diff",
        task_type="document",
        source="claude-code",
        metadata=metadata,
    )


def test_the_handler_hands_the_port_the_caller_ticket_and_lineage() -> None:
    request = _request(
        caller_lane=_LANE,
        ticket_id="OMN-20606",
        parent_correlation_id=_PARENT,
        lineage_kind="fallback",
        parent_failure_cause="exit_124",
    )
    assert _request_attribution(request) == {
        "caller_lane": _LANE,
        "ticket_id": "OMN-20606",
        "parent_correlation_id": _PARENT,
        "lineage_kind": "fallback",
        "parent_failure_cause": "exit_124",
    }


def test_the_handler_drops_a_malformed_lineage_and_keeps_the_rest() -> None:
    request = _request(caller_lane=_LANE, lineage_kind="fallback")
    assert _request_attribution(request) == {"caller_lane": _LANE}


def test_an_anonymous_request_passes_no_attribution_keyword() -> None:
    """A port that predates the keyword is called exactly as before."""
    assert _attribution_dispatch_kwargs(_request()) == {}


def _result() -> ModelLlmDelegationCallResult:
    return ModelLlmDelegationCallResult(
        request_id="req-omn20606",
        success=True,
        content="an answer",
        tokens_in=11,
        tokens_out=22,
        latency_ms=33,
        actual_cost_usd=Decimal("0"),
        savings_usd=Decimal("0"),
    )


def _project(
    tmp_path: Path, attribution: dict[str, str] | None
) -> tuple[dict[str, object], dict[str, Any]]:
    published: list[dict[str, object]] = []

    class _Publisher:
        def publish(self, **event: object) -> bool:
            published.append(event)
            return True

    db_path = tmp_path / "delegation.sqlite"
    port = LocalDelegationDispatchPort(
        evidence_db_path=db_path,
        terminal_publisher=cast(EmitEffectTopicPublisher, _Publisher()),
    )
    correlation_id = uuid4()
    port._project_evidence(
        correlation_id=correlation_id,
        task_type="document",
        endpoint_ref="local",
        model_id="model-local",
        result=_result(),
        prompt="summarize the diff",
        source_session_id=None,
        tenant_id="omninode",
        quality_passed=True,
        failure_message="",
        cost_usd=Decimal("0"),
        baseline_savings=None,
        escalation_count=0,
        attempts=[],
        actual_score=None,
        required_bar=None,
        attribution=attribution,
    )
    assert len(published) == 1
    payload = published[0]["payload"]
    assert isinstance(payload, dict)
    return payload, _row(db_path, str(correlation_id))


def test_in_process_evidence_carries_caller_ticket_and_lineage(
    tmp_path: Path,
) -> None:
    """The only terminal that reaches the bus on the in-process path."""
    attribution = {"caller_lane": _LANE, "ticket_id": "OMN-20606", **_LINEAGE}
    payload, row = _project(tmp_path, attribution)
    for key, value in attribution.items():
        assert payload[key] == value
        assert row[key] == value


def test_the_in_process_evidence_copies_only_the_named_keys(tmp_path: Path) -> None:
    payload, _ = _project(
        tmp_path, {"caller_lane": _LANE, "status": "failed", "tenant_id": "other"}
    )
    assert payload["caller_lane"] == _LANE
    assert payload["status"] == "completed"
    assert payload["tenant_id"] == "omninode"


def test_the_in_process_evidence_without_attribution_is_unchanged(
    tmp_path: Path,
) -> None:
    payload, row = _project(tmp_path, None)
    assert not ({"caller_lane", "ticket_id"} | LINEAGE_KEYS) & set(payload)
    assert row.get("caller_lane") is None
