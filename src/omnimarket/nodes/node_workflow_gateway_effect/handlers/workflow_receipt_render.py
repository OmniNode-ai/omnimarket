# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Render the terminal receipt of one workflow row."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from omnibase_core.models.delegation.wire.model_quality_gate import (
    ModelQualityRuleEvaluation,
)

from omnimarket.nodes.node_workflow_gateway_effect.models.model_workflow_envelope import (
    ModelWorkflowReceipt,
)
from omnimarket.nodes.node_workflow_gateway_effect.models.model_workflow_store import (
    ModelWorkflowRow,
)

_TERMINAL_STATUSES = frozenset({"completed", "failed"})


class WorkflowReceiptNotTerminalError(RuntimeError):
    """The row is not terminal, or a terminal field is missing."""


def decode_rule_evaluations(raw: object) -> tuple[ModelQualityRuleEvaluation, ...]:
    """Decode a stored rule-evaluation column into typed evaluations."""
    if raw is None:
        return ()
    if isinstance(raw, (str, bytes)):
        raw = json.loads(raw)
    if not isinstance(raw, list):
        raise TypeError(f"rule evaluations must be a list, got {type(raw).__name__}")
    return tuple(ModelQualityRuleEvaluation.model_validate(item) for item in raw)


def render_workflow_receipt(
    row: ModelWorkflowRow, runner_identity: str
) -> ModelWorkflowReceipt:
    """The receipt for one terminal row, with its two content hashes."""
    if row.status not in _TERMINAL_STATUSES:
        raise WorkflowReceiptNotTerminalError(
            f"workflow {row.workflow_id} is not terminal (status {row.status!r})"
        )
    for name in ("terminal_model_used", "terminal_total_tokens", "terminal_latency_ms"):
        if getattr(row, name) is None:
            raise WorkflowReceiptNotTerminalError(
                f"terminal row {row.workflow_id} is missing {name}"
            )

    rule_evaluations = decode_rule_evaluations(row.terminal_rule_evaluations)
    # The two payload shapes below are byte-pinned: omnibase_infra's receipt replay
    # verifier mirrors terminal_event_payload exactly, and the projection hash keeps
    # the hosted gateway's key set so a receipt rendered here matches one rendered
    # there. Datetimes are hashed through json's default=str, as before.
    projection_row_payload: dict[str, Any] = {
        "workflow_id": str(row.workflow_id),
        "tenant_id": str(row.tenant_id),
        "workflow_type": row.workflow_type,
        "status": row.status,
        "correlation_id": str(row.correlation_id),
        "submitted_at": row.submitted_at,
        "updated_at": row.updated_at,
        "terminal_model_used": row.terminal_model_used,
        "terminal_total_tokens": row.terminal_total_tokens,
        "terminal_latency_ms": row.terminal_latency_ms,
        "route": row.terminal_route,
        "provider": row.terminal_provider,
        "credential_source": row.terminal_credential_source,
        "result_content": row.result_content,
        "terminal_failure_class": row.terminal_failure_class,
        "terminal_failure_code": row.terminal_failure_code,
        "terminal_failure_reason": row.terminal_failure_reason,
        "terminal_remediation": row.terminal_remediation,
        "rule_evaluations": [item.model_dump(mode="json") for item in rule_evaluations],
    }
    terminal_event_payload: dict[str, Any] = {
        "correlation_id": str(row.correlation_id),
        "status": row.status,
        "terminal_model_used": row.terminal_model_used,
        "terminal_total_tokens": row.terminal_total_tokens,
        "terminal_latency_ms": row.terminal_latency_ms,
    }

    def _sha256(payload: dict[str, Any]) -> str:
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()

    return ModelWorkflowReceipt(
        workflow_id=row.workflow_id,
        tenant_id=row.tenant_id,
        correlation_id=row.correlation_id,
        workflow_type=row.workflow_type,
        status=row.status,
        submitted_at=row.submitted_at,
        completed_at=row.updated_at,
        terminal_model_used=row.terminal_model_used,
        terminal_total_tokens=row.terminal_total_tokens,
        terminal_latency_ms=row.terminal_latency_ms,
        route=row.terminal_route,
        provider=row.terminal_provider,
        credential_source=row.terminal_credential_source,
        result_content=row.result_content,
        terminal_failure_class=row.terminal_failure_class,
        terminal_failure_code=row.terminal_failure_code,
        terminal_failure_reason=row.terminal_failure_reason,
        terminal_remediation=row.terminal_remediation,
        rule_evaluations=rule_evaluations,
        event_count=1,
        projection_row_hash=_sha256(projection_row_payload),
        terminal_event_hash=_sha256(terminal_event_payload),
        verifier=runner_identity,
    )


__all__: list[str] = [
    "WorkflowReceiptNotTerminalError",
    "decode_rule_evaluations",
    "render_workflow_receipt",
]
