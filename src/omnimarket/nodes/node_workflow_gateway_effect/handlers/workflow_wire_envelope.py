# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build the canonical wire envelope from an admitted command envelope."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any, Final

from omnimarket.nodes.node_workflow_gateway_effect.models.model_workflow_catalog import (
    ModelWorkflowWireSpec,
)
from omnimarket.nodes.node_workflow_gateway_effect.models.model_workflow_envelope import (
    ModelWorkflowCommandEnvelope,
)

# These mirror the canonical event envelope's own defaults.
_PRIORITY: Final[int] = 5
_RETRY_COUNT: Final[int] = 0
_ONEX_VERSION: Final[dict[str, int]] = {"major": 1, "minor": 0, "patch": 0}
_ENVELOPE_VERSION: Final[dict[str, int]] = {"major": 2, "minor": 1, "patch": 0}


def _iso_utc(value: datetime) -> str:
    """Render a tz-aware datetime in UTC with a Z suffix, as the wire carries it."""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def to_canonical_wire_envelope(
    envelope: ModelWorkflowCommandEnvelope,
    tenant_slug: str | None,
    spec: ModelWorkflowWireSpec,
) -> dict[str, Any]:
    """The bus-facing envelope, stamped per the workflow's wire spec."""
    payload: dict[str, Any] = {
        key: envelope.payload[key]
        for key in spec.payload_keys
        if key in envelope.payload
    }
    payload["correlation_id"] = str(envelope.correlation_id)
    if spec.stamp_emitted_at:
        payload["emitted_at"] = _iso_utc(envelope.emitted_at)
    if spec.stamp_context_pack:
        context_pack = envelope.payload.get("context_pack")
        if context_pack:
            payload["context_pack"] = context_pack
            payload["context_pack_hash"] = hashlib.sha256(
                str(context_pack).encode("utf-8")
            ).hexdigest()
    if spec.canonical_tenant_id:
        payload["tenant_id"] = str(envelope.source_tenant_id)
    elif tenant_slug:
        payload["tenant_id"] = tenant_slug
    if spec.stamp_tenant_principal:
        payload["tenant_principal_id"] = envelope.source_tenant_principal_id

    return {
        "payload": payload,
        "envelope_id": str(envelope.envelope_id),
        "envelope_timestamp": _iso_utc(envelope.emitted_at),
        "correlation_id": str(envelope.correlation_id),
        "tenant_id": str(envelope.source_tenant_id),
        "source_tool": envelope.source_gateway_instance,
        "metadata": {
            "headers": {},
            "tags": {
                "workflow_id": str(envelope.workflow_id),
                "workflow_type": envelope.workflow_type,
                "contract_id": envelope.contract_id,
                "schema_version": envelope.schema_version,
                "causation_id": str(envelope.causation_id),
                "source_tenant_id": str(envelope.source_tenant_id),
                "source_tenant_principal_id": envelope.source_tenant_principal_id,
                "authenticated_user_id": envelope.authenticated_user_id,
                "source_gateway_instance": envelope.source_gateway_instance,
            },
        },
        "event_type": envelope.event_type,
        "priority": _PRIORITY,
        "retry_count": _RETRY_COUNT,
        "onex_version": dict(_ONEX_VERSION),
        "envelope_version": dict(_ENVELOPE_VERSION),
    }


__all__: list[str] = ["to_canonical_wire_envelope"]
