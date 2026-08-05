# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for node_projection_delegation_inference_response (OMN-15707).

RED->GREEN proof that ``HandlerProjectionDelegationInferenceResponse.project()``
no longer reads ``projection_delegation_inference_response_text`` before
upserting -- the contract declares ``access: write`` only for that table, and
the real runtime (``ProjectionTableOperation._assert_read_declared`` in
omnibase_infra) raises ``PermissionError`` on any ``.query()`` against a
write-only-declared table. This was live-reproduced flooding onex-dev
(correlation ce0bff7a-95e7-4656-8bcc-98a021f125ea, run 30970742725) -- every
event DLQ'd.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.nodes.node_projection_delegation_inference_response.handlers.handler_projection_delegation_inference_response import (
    TABLE,
    HandlerProjectionDelegationInferenceResponse,
)
from omnimarket.nodes.node_projection_delegation_inference_response.models.model_inference_response_projection import (
    DEFAULT_TENANT,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter


class _WriteOnlyDatabaseAdapter:
    """Reproduces the production access-declaration refusal (OMN-15707).

    Mirrors ``ProjectionTableOperation._assert_read_declared`` in
    ``omnibase_infra/runtime/auto_wiring/handler_wiring.py`` byte-for-byte:
    the contract for this table declares ``access: write`` only
    (``contract.yaml``), so any real Postgres-backed dispatch raises
    ``PermissionError`` on ``.query()``. The node's other tests would use
    ``InmemoryDatabaseAdapter`` directly, which does not model this
    declared-access check at all -- this double closes that gap so the
    regression is caught locally instead of only live on a real cluster.
    """

    def __init__(self) -> None:
        self._inner = InmemoryDatabaseAdapter()

    def upsert(self, table: str, conflict_key: str, row: dict[str, object]) -> bool:
        return self._inner.upsert(table, conflict_key, row)

    def query(
        self, table: str, filters: dict[str, object] | None = None
    ) -> list[dict[str, object]]:
        raise PermissionError(f"tenant.{table} declares access='write'; read refused")

    def assert_only_query(
        self, table: str, filters: dict[str, object] | None = None
    ) -> list[dict[str, object]]:
        """Test-only escape hatch: read the underlying store for assertions.

        Bypasses the write-only refusal deliberately -- this is the test
        harness inspecting stored state, not the handler under test reading
        it (the handler must never call the real ``query`` above).
        """
        return self._inner.query(table, filters)


def _payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "correlation_id": "ce0bff7a-95e7-4656-8bcc-98a021f125ea",
        "model_used": "sonnet-5",
        "task_type": "delegation",
        "content": "generated text",
        "prompt_tokens": 12,
        "completion_tokens": 34,
        "latency_ms": 567,
        "tenant_id": "acme",
    }
    base.update(overrides)
    return base


class TestHandlerProjectionDelegationInferenceResponseWriteOnlyAccess:
    """OMN-15707: project() must not read the projection table.

    ``_WriteOnlyDatabaseAdapter`` reproduces the live PermissionError
    locally so this class is a real RED->GREEN proof, not just a live
    incident description.
    """

    def test_project_does_not_read_the_table(self) -> None:
        handler = HandlerProjectionDelegationInferenceResponse()
        db = _WriteOnlyDatabaseAdapter()

        # Must not raise PermissionError: access='write'; read refused.
        result = handler.project(_payload(), db)

        assert result.rows_upserted == 1
        assert result.singleton_key == "acme"

    def test_handle_does_not_read_the_table(self) -> None:
        handler = HandlerProjectionDelegationInferenceResponse()
        db = _WriteOnlyDatabaseAdapter()
        input_data: dict[str, Any] = {
            "_db": db,
            "_topic": "onex.evt.omnibase-infra.inference-response.v1",
            **_payload(),
        }

        result = handler.handle(input_data)

        assert result["rows_upserted"] == 1

    def test_repeat_event_still_upserts_with_zero_reads(self) -> None:
        """Second event for the same tenant must also avoid any .query()."""
        handler = HandlerProjectionDelegationInferenceResponse()
        db = _WriteOnlyDatabaseAdapter()

        handler.project(_payload(content="first"), db)
        handler.project(_payload(content="second"), db)

        rows = db.assert_only_query(TABLE)
        assert len(rows) == 1
        assert rows[0]["latest_generated_text"] == "second"
        # OMN-15707 disclosed behavior change: recent_responses holds only
        # the current event (window size 1), not an accumulated history --
        # see the handler module docstring for why the true FIFO window
        # cannot be preserved without a read against a write-only table.
        recent_responses = rows[0]["recent_responses"]
        assert isinstance(recent_responses, list)
        assert len(recent_responses) == 1
        entry = recent_responses[0]
        assert isinstance(entry, dict)
        assert entry == {
            "correlation_id": "ce0bff7a-95e7-4656-8bcc-98a021f125ea",
            "model_name": "sonnet-5",
            "task_type": "delegation",
            "generated_text": "second",
            "prompt_tokens": 12,
            "completion_tokens": 34,
            "latency_ms": 567,
            "captured_at": entry["captured_at"],
        }


class TestHandlerProjectionDelegationInferenceResponseTenantKeying:
    """Regression coverage for OMN-14894 tranche-2 per-tenant re-keying,
    now exercised against the write-only double so a future regression in
    tenant resolution can't hide behind an accidental read.
    """

    def test_missing_tenant_id_falls_back_to_default_tenant(self) -> None:
        handler = HandlerProjectionDelegationInferenceResponse()
        db = _WriteOnlyDatabaseAdapter()

        result = handler.project(_payload(tenant_id=None), db)

        assert result.singleton_key == DEFAULT_TENANT

    def test_distinct_tenants_get_distinct_rows(self) -> None:
        handler = HandlerProjectionDelegationInferenceResponse()
        db = _WriteOnlyDatabaseAdapter()

        handler.project(_payload(tenant_id="tenant-a"), db)
        handler.project(_payload(tenant_id="tenant-b"), db)

        rows = db.assert_only_query(TABLE)
        assert {row["singleton_key"] for row in rows} == {"tenant-a", "tenant-b"}


@pytest.mark.parametrize("missing_key", ["correlation_id", "model_used"])
def test_payload_missing_optional_fields_defaults_to_empty(missing_key: str) -> None:
    handler = HandlerProjectionDelegationInferenceResponse()
    db = _WriteOnlyDatabaseAdapter()
    payload = _payload()
    del payload[missing_key]

    result = handler.project(payload, db)

    assert result.rows_upserted == 1
