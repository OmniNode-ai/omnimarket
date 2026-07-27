# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit + regression tests for the canonical projection handler shim (OMN-13825).

The runtime auto-wiring injects ``_db`` and ``_event_type`` into every projection
``handle()`` payload. When the inbound event model is ``extra="forbid"`` (e.g.
``ModelDepHealthSweepCompletedEvent``) an unstripped ``_event_type`` makes
``Model(**input_data)`` raise ``ValidationError [extra_forbidden]`` and the event
is consumed-then-dropped (``LAG=0``, zero rows). These tests pin the shared
:func:`split_projection_input` behaviour and prove the whole projection-shim
class is defended against that failure mode.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.projection.handler_shim import (
    INJECTED_EVENT_TYPE_KEY,
    split_projection_input,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter


class _StrictEvent(BaseModel):
    """A strict inbound event model, mirroring the ``extra="forbid"`` reducers."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(...)
    value: int = Field(default=0)


class TestSplitProjectionInput:
    def test_strips_db_and_event_type_from_payload(self) -> None:
        db = InmemoryDatabaseAdapter()
        input_data: dict[str, object] = {
            "_db": db,
            "_event_type": "omnimarket.some-event",
            "run_id": "r1",
            "value": 3,
        }
        out_db, payload, meta = split_projection_input(input_data)

        assert out_db is db
        assert payload == {"run_id": "r1", "value": 3}
        assert "_db" not in payload
        assert INJECTED_EVENT_TYPE_KEY not in payload
        assert meta == {"_event_type": "omnimarket.some-event"}

    def test_is_non_destructive(self) -> None:
        db = InmemoryDatabaseAdapter()
        input_data: dict[str, object] = {"_db": db, "_event_type": "x", "run_id": "r"}
        split_projection_input(input_data)
        # The caller's dict must be untouched (still carries the injected keys).
        assert input_data["_db"] is db
        assert input_data["_event_type"] == "x"

    def test_preserves_domain_underscore_alias_key(self) -> None:
        """A blanket leading-underscore strip would destroy legitimate domain
        aliases like ``ModelSandboxDecisionEvent`` (wire key ``_runtime_backend``).
        Only the two runtime-injected keys may be stripped.
        """
        db = InmemoryDatabaseAdapter()
        input_data: dict[str, object] = {
            "_db": db,
            "_event_type": "omnimarket.generated-node-invoked",
            "_runtime_backend": "sandbox",
            "correlation_id": "c1",
        }
        _, payload, _meta = split_projection_input(input_data)
        assert payload["_runtime_backend"] == "sandbox"
        assert "correlation_id" in payload

    def test_strict_model_builds_from_stripped_payload(self) -> None:
        """The canonical path lets an ``extra="forbid"`` model build even when the
        runtime injected ``_event_type`` — the exact OMN-13825 defect-1 shape.
        """
        db = InmemoryDatabaseAdapter()
        input_data: dict[str, object] = {
            "_db": db,
            "_event_type": "omnimarket.dep-health-sweep-completed",
            "run_id": "r1",
            "value": 7,
        }
        _, payload, _meta = split_projection_input(input_data)
        event = _StrictEvent(**payload)  # must NOT raise ValidationError
        assert event.run_id == "r1"
        assert event.value == 7

    def test_raises_type_error_when_db_missing(self) -> None:
        with pytest.raises(TypeError, match="DatabaseAdapter"):
            split_projection_input({"run_id": "r1"})

    def test_raises_type_error_when_db_not_adapter(self) -> None:
        with pytest.raises(TypeError, match="DatabaseAdapter"):
            split_projection_input({"_db": "not-an-adapter", "run_id": "r1"})


class TestSandboxDecisionsInjectedKeyRegression:
    """The sandbox-decisions reducer must survive the injected ``_event_type``
    while still honoring its ``_runtime_backend`` alias — proves the 2-key strip
    does not clobber domain fields.
    """

    def test_handle_with_injected_event_type_and_runtime_backend(self) -> None:
        from omnimarket.nodes.node_projection_sandbox_decisions.handlers.handler_projection_sandbox_decisions import (
            HandlerProjectionSandboxDecisions,
        )

        db = InmemoryDatabaseAdapter()
        input_data: dict[str, object] = {
            "_db": db,
            "_event_type": "omnimarket.generated-node-invoked",
            "_runtime_backend": "runtime",
            "correlation_id": "corr-1",
            "node_name": "node_demo",
            "status": "completed",
        }
        result = HandlerProjectionSandboxDecisions().handle(input_data)
        assert result["rows_inserted"] == 1
        rows = db.query("sandbox_decisions")
        assert len(rows) == 1
        # The underscore-aliased domain value survived the strip.
        assert rows[0]["runtime_backend"] == "runtime"
        assert rows[0]["correlation_id"] == "corr-1"
