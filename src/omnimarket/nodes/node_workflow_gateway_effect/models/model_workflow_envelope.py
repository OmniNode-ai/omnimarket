# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Envelope, submit request, ack, status and receipt models of the workflow gateway.

Ported from the hosted gateway's workflow models. The command envelope is the
internal record of an admitted submission; the canonical wire envelope is built
from it by ``handlers.workflow_wire_envelope`` and is what reaches the bus.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal, Self
from uuid import UUID

from omnibase_core.models.delegation.wire.model_quality_gate import (
    ModelQualityRuleEvaluation,
)
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

_SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")

_FAILURE_FIELDS = (
    "terminal_failure_class",
    "terminal_failure_code",
    "terminal_failure_reason",
    "terminal_remediation",
)


class ModelWorkflowCommandEnvelope(BaseModel):
    """The record of an admitted submission, every field populated.

    There is no partial-envelope path: a submission that cannot populate all of
    these is refused before publish. The tenant slug is deliberately absent so
    no in-gateway surface can authorize on the mutable slug.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    envelope_id: UUID
    correlation_id: UUID
    causation_id: UUID
    source_tenant_id: UUID
    authenticated_user_id: str
    schema_version: str
    emitted_at: datetime
    source_gateway_instance: str
    source_tenant_principal_id: str
    workflow_id: UUID
    workflow_type: str
    contract_id: str
    command_topic: str
    event_type: str
    payload: dict[str, Any]

    @field_validator("emitted_at")
    @classmethod
    def _emitted_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError("emitted_at must be timezone-aware")
        return value

    @field_validator("schema_version")
    @classmethod
    def _schema_version_is_semver(cls, value: str) -> str:
        if not _SEMVER.match(value):
            raise ValueError(f"schema_version must be major.minor.patch: {value!r}")
        return value

    @field_validator(
        "authenticated_user_id",
        "source_gateway_instance",
        "source_tenant_principal_id",
        "workflow_type",
        "contract_id",
        "command_topic",
        "event_type",
    )
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("field must be a non-empty string")
        return value


class ModelWorkflowSubmitRequest(BaseModel):
    """Accepted body of ``POST /v1/workflows``.

    ``extra="forbid"`` is load-bearing: a caller-supplied topic, command topic or
    tenant field is refused, never ignored. Users submit workflows, not routing.
    """

    model_config = ConfigDict(extra="forbid")

    workflow_type: str
    payload: dict[str, Any]
    correlation_id: UUID | None = None
    causation_id: UUID | None = None


class ModelWorkflowAck(BaseModel):
    """Acknowledgement returned once the command envelope is published."""

    model_config = ConfigDict(extra="forbid")

    workflow_id: UUID
    envelope_id: UUID
    correlation_id: UUID
    workflow_type: str
    status: str
    accepted_at: datetime


class ModelWorkflowStatus(BaseModel):
    """Tenant-scoped read model of ``GET /v1/workflows/{id}/status``.

    The terminal fields stay ``None`` until a terminal event has been applied to
    the row. A completed workflow may not carry a failure attribution; the
    converse is not asserted, so an old failed row still renders.
    """

    model_config = ConfigDict(extra="forbid")

    workflow_id: UUID
    workflow_type: str
    status: str
    envelope_id: UUID
    correlation_id: UUID
    command_topic: str
    submitted_at: datetime
    updated_at: datetime
    terminal_model_used: str | None = None
    terminal_total_tokens: int | None = None
    terminal_latency_ms: int | None = None
    terminal_failure_class: str | None = None
    terminal_failure_code: str | None = None
    terminal_failure_reason: str | None = None
    terminal_remediation: str | None = None
    rule_evaluations: tuple[ModelQualityRuleEvaluation, ...] = ()

    @model_validator(mode="after")
    def _completed_carries_no_failure(self) -> Self:
        if self.status == "completed":
            for name in _FAILURE_FIELDS:
                if getattr(self, name) is not None:
                    raise ValueError(f"completed workflow cannot carry {name}")
        return self


class ModelWorkflowReceipt(BaseModel):
    """``workflow_receipt.json`` of one terminal workflow.

    ``verifier`` is the caller-supplied runner identity; it is never defaulted
    and never copied from the gateway instance (verified-by is not ran-by).
    """

    model_config = ConfigDict(extra="forbid")

    workflow_id: UUID
    tenant_id: UUID
    correlation_id: UUID
    workflow_type: str
    status: str
    submitted_at: datetime
    completed_at: datetime
    terminal_model_used: str
    terminal_total_tokens: int
    terminal_latency_ms: int
    route: str | None = None
    provider: str | None = None
    credential_source: Literal["customer_key", "house", "none"] | None = None
    result_content: str | None
    terminal_failure_class: str | None
    terminal_failure_code: str | None
    terminal_failure_reason: str | None
    terminal_remediation: str | None
    rule_evaluations: tuple[ModelQualityRuleEvaluation, ...] = ()
    event_count: int
    projection_row_hash: str
    terminal_event_hash: str
    verifier: str

    @field_validator("status")
    @classmethod
    def _status_is_terminal(cls, value: str) -> str:
        if value not in ("completed", "failed"):
            raise ValueError(
                f"receipt status must be 'completed' or 'failed', got {value!r}"
            )
        return value

    @field_validator("verifier")
    @classmethod
    def _verifier_is_explicit(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("verifier must be a non-empty explicit runner identity")
        return value

    @model_validator(mode="after")
    def _receipt_is_coherent(self) -> Self:
        if self.status == "completed":
            for name in _FAILURE_FIELDS:
                if getattr(self, name) is not None:
                    raise ValueError(f"completed receipt cannot carry {name}")
        if (self.route is None) != (self.provider is None):
            raise ValueError("route and provider must be paired")
        for name in ("route", "provider"):
            value = getattr(self, name)
            if value is not None and not value.strip():
                raise ValueError(f"{name} must be non-blank when present")
        return self


__all__: list[str] = [
    "ModelWorkflowAck",
    "ModelWorkflowCommandEnvelope",
    "ModelWorkflowReceipt",
    "ModelWorkflowStatus",
    "ModelWorkflowSubmitRequest",
]
