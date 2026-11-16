# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tenant-scoped reader of one delegation attempt's event snapshot (OMN-20577).

Implements ``ProtocolDelegationEventSnapshot`` over ``delegation_events``. The reader it
is built on is bound to one tenant and sets ``app.tenant_id`` before the statement, so a
correlation id of another tenant reads as absent; the statement also filters on the
tenant explicitly.
"""

from __future__ import annotations

import json

from omnimarket.handlers.delegation_eval_read_binding import build_tenant_row_query
from omnimarket.nodes.node_delegation_eval_orchestrator.models import (
    ModelDelegationEventSnapshot,
)
from omnimarket.nodes.node_delegation_eval_orchestrator.protocols import (
    ProtocolDelegationEventSnapshot,
)
from omnimarket.protocols.protocol_tenant_row_query import ProtocolTenantRowQuery

_TABLE = "delegation_events"


class SnapshotNotFoundError(LookupError):
    """The tenant holds no delegation event for the correlation id."""


def _deciding_check(row: dict[str, object]) -> str:
    failed = row.get("quality_gates_failed_jsonb")
    if isinstance(failed, str):
        failed = json.loads(failed)
    if isinstance(failed, list) and failed:
        return ",".join(str(check) for check in failed)
    return str(row.get("quality_gate_detail") or "")[:200]


class TenantEventSnapshot:
    """Event snapshots of the tenant this source was built for, and of no other."""

    def __init__(self, tenant_id: str, reader: ProtocolTenantRowQuery) -> None:
        self._tenant_id = tenant_id
        self._reader = reader

    def get_snapshot(
        self, correlation_id: str, attempt_index: int
    ) -> ModelDelegationEventSnapshot:
        try:
            rows = self._reader.query(
                _TABLE,
                {"tenant_id": self._tenant_id, "correlation_id": correlation_id},
            )
        finally:
            self._reader.close()
        if not rows:
            raise SnapshotNotFoundError(
                f"no delegation_events row for correlation id {correlation_id}"
            )
        row = rows[0]
        return ModelDelegationEventSnapshot(
            prompt=str(row["prompt_text"] or ""),
            response=str(row["response_text"] or ""),
            task_class=str(row["task_type"] or ""),
            gate_verdict="accepted" if row["quality_gate_passed"] else "refused",
            deciding_check=_deciding_check(row),
        )


def snapshots_for_tenant(tenant_id: str) -> ProtocolDelegationEventSnapshot:
    """The runtime's source for one command: the overlay-bound reader of ``tenant_id``."""
    return TenantEventSnapshot(tenant_id, build_tenant_row_query(tenant_id))
