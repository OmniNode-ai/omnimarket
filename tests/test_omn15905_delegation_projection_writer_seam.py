# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Cross-boundary seam test for OMN-15905 (delegation projection writer parity).

Drives the REAL standalone-writer entrypoint --
``DelegationProjectionRunner.project_event()``, the exact method the
``omnimarket-projection-delegation-writer`` Deployment (OMN-15905 §4.1) runs
in production via its ``python -m ...handler_delegation`` ``__main__`` -- with
real delegation-terminal- and quality-gate-result-shaped payloads (the same
dict vocabulary the live bus emitters use and the accepted unit suite for
``_canonical_result_to_task_delegated_payload`` already treats as canonical:
``correlation_id``, ``tenant_id``, ``cumulative_attempt_cost``,
``cost_tier_name``, ``quality_passed``/``passed`` -- see
``tests/unit/delegation/test_terminal_cost_hoist_omn13408.py`` and
``test_ceiling_quality_gate_wiring_omn13335.py`` for the same shape used as a
"real producer" fixture elsewhere in this repo), and asserts the actual SQL
INSERT the runtime would execute against Postgres carries the event's
``correlation_id``, the correct ``tenant_id``, and ``quality_gate_passed`` --
the OMN-14208 seam this ticket closes (one test that drives BOTH the
canonical-terminal AND quality-gate-result write paths through the REAL
runner class, not two isolated unit suites).

RED (pre-port, confirmed by running this file against the unmodified
``handler_delegation.py``):
  * ``quality-gate-result.v1`` had NO branch in ``project_event`` at all --
    the topic fell through every ``elif`` to ``return False``, so
    ``test_quality_gate_result_writes_row_with_matching_verdict`` failed on
    ``assert ok is True`` (never reaching a write).
  * The canonical-terminal path's ``_canonical_result_to_task_delegated_payload``
    was the module-local, silently-diverged copy: it never read
    ``tenant_id`` at all, so
    ``test_canonical_terminal_writes_row_with_tenant_and_quality_gate``
    failed asserting ``tenant_id`` was ever bound as a SQL param.

GREEN (post-port): both branches route through the ported, parity-reaching
write path (``_project_typed_event_async`` / ``_project_quality_gate_result``),
both against a real (test) ``omnidash_analytics``-shaped DB double whose
captured SQL is the same targeted-column UPSERT the runtime issues.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
    DelegationProjectionRunner,
)
from omnimarket.projection.runner import MessageMeta

_TENANT = "beta-business-proof"
_CORRELATION_ID = "9c6a9b1e-3f7a-4b8e-8a5a-2c1d0e4f7a11"


def _param_by_column(call_args: tuple[object, ...]) -> dict[str, object]:
    """Map column name -> bound value for a dynamic-UPSERT INSERT call.

    Mirrors ``tests/test_projection_handlers.py::_param_by_column`` -- the
    ported writer's ``_dynamic_upsert`` builds its column list (and
    therefore its positional-param order) from a Python dict's insertion
    order, so a name-based lookup is the robust way to assert a specific
    column's bound value.
    """
    sql = str(call_args[0])
    columns_segment = sql.split("(", 1)[1].split(")", 1)[0]
    columns = [c.strip() for c in columns_segment.split(",")]
    values = call_args[1:]
    return dict(zip(columns, values, strict=True))


def _mock_db() -> AsyncMock:
    db = AsyncMock()
    # Every SELECT (evidence-preservation / existing-row probe) returns no
    # rows -- these are fresh-row scenarios; the empty-list return matches
    # the real AsyncpgAdapter.execute() contract for a no-match SELECT.
    db.execute = AsyncMock(return_value=[])
    return db


def _real_delegation_completed_payload(
    *, correlation_id: str, tenant_id: str
) -> dict[str, Any]:
    """The real ``onex.evt.omnibase-infra.delegation-completed.v1`` shape.

    Field vocabulary verified against the live emitter
    (``omnibase_infra/src/omnibase_infra/runtime/service_delegation_dispatch_port.py``,
    which threads ``tenant_id`` and resolves ``cost_tier_name`` /
    ``cumulative_attempt_cost``/``final_attempt_cost`` onto the terminal
    result payload) and against the accepted "real producer" fixtures in
    ``tests/unit/delegation/test_terminal_cost_hoist_omn13408.py`` and
    ``test_ceiling_quality_gate_wiring_omn13335.py`` -- this is not a
    canonical-shaped stand-in invented for this test.
    """
    return {
        "correlation_id": correlation_id,
        "tenant_id": tenant_id,
        "task_type": "code-review",
        "model_used": "glm-5.2",
        "content": "the model's real answer",
        "quality_passed": True,
        "quality_score": 0.95,
        "latency_ms": 1800,
        "prompt_tokens": 210,
        "completion_tokens": 480,
        "cumulative_attempt_cost": 0.0142,
        "cost_tier_name": "cheap_cloud",
        "context_pack_hash": "sha256:omn15905-seam",
    }


def _real_quality_gate_result_payload(
    *, correlation_id: str, passed: bool
) -> dict[str, Any]:
    """The real ``onex.evt.omnibase-infra.quality-gate-result.v1`` shape.

    Matches ``ModelQualityGateResult`` (``omnimarket/models/delegation/wire/
    model_quality_gate.py``) field-for-field: this IS the wire model the
    deterministic-scoring path publishes -- not a hand-rolled stand-in.
    """
    return {
        "correlation_id": correlation_id,
        "passed": passed,
        "fail_category": "pass" if passed else "fail_deterministic",
        "quality_score": 0.91 if passed else 0.40,
        "failure_reasons": () if passed else ("score_below_required_bar",),
        "score_source": "deterministic_acceptance",
    }


@pytest.mark.unit
class TestDelegationCompletedTerminalWriterParity:
    """Canonical delegation-completed.v1 -> delegation_events (OMN-15905 §4.1)."""

    def test_canonical_terminal_writes_row_with_tenant_and_quality_gate(self) -> None:
        runner = DelegationProjectionRunner()
        mock_db = _mock_db()
        runner._db = mock_db  # type: ignore[assignment]

        topic = runner._topic_delegation_completed
        assert topic, "contract must declare a delegation-completed topic"
        data = _real_delegation_completed_payload(
            correlation_id=_CORRELATION_ID, tenant_id=_TENANT
        )
        meta = MessageMeta(partition=0, offset=0, fallback_id=_CORRELATION_ID)

        ok = asyncio.run(runner.project_event(topic, data, meta))

        assert ok is True
        insert_calls = [
            c
            for c in mock_db.execute.await_args_list
            if str(c.args[0]).strip().startswith("INSERT INTO delegation_events")
        ]
        assert len(insert_calls) == 1, (
            "exactly one delegation_events write for a fresh correlation_id"
        )
        by_column = _param_by_column(insert_calls[0].args)
        assert by_column["correlation_id"] == _CORRELATION_ID
        # The seam this ticket closes: tenant_id must reach the write. The
        # pre-port async converter never read tenant_id off the wire at all.
        assert by_column["tenant_id"] == _TENANT
        assert by_column["quality_gate_passed"] is True
        # OMN-13408: cost_usd resolves from cumulative_attempt_cost via the
        # measured-cost re-pricing path, not a hardcoded 0.0.
        assert by_column["cost_usd"] > 0
        assert by_column["cost_tier_name"] == "cheap_cloud"
        # OMN-13644: context_pack_hash propagates.
        assert by_column["context_pack_hash"] == "sha256:omn15905-seam"

    def test_canonical_terminal_failed_still_refuses_write_without_tenant_only_if_enforced(
        self,
    ) -> None:
        """Baseline (non-enforcement lane): a terminal with NO tenant_id still
        writes, falling through to the column default -- proving the tenant
        seam is additive (stamps when present) and not a regression of the
        OMN-14058 interim default when isolation enforcement is off."""
        runner = DelegationProjectionRunner()
        mock_db = _mock_db()
        runner._db = mock_db  # type: ignore[assignment]

        topic = runner._topic_delegation_failed
        assert topic, "contract must declare a delegation-failed topic"
        data = _real_delegation_completed_payload(
            correlation_id=_CORRELATION_ID, tenant_id=""
        )
        data["quality_passed"] = False
        data["failure_reason"] = "quota_exhausted"
        del data["tenant_id"]
        meta = MessageMeta(partition=0, offset=1, fallback_id=_CORRELATION_ID)

        ok = asyncio.run(runner.project_event(topic, data, meta))

        assert ok is True
        insert_calls = [
            c
            for c in mock_db.execute.await_args_list
            if str(c.args[0]).strip().startswith("INSERT INTO delegation_events")
        ]
        assert len(insert_calls) == 1
        by_column = _param_by_column(insert_calls[0].args)
        # No tenant_id column bound at all -- the row falls through to the
        # DB-level DEFAULT 'omninode', never a hand-stamped None (OMN-14058).
        assert "tenant_id" not in by_column
        assert by_column["quality_gate_passed"] is False


@pytest.mark.unit
class TestQualityGateResultWriterParity:
    """quality-gate-result.v1 -> delegation_events (OMN-15850/OMN-15905)."""

    def test_quality_gate_result_writes_row_with_matching_verdict(self) -> None:
        """The deterministic-scoring path publishes ONLY this topic. Before
        OMN-15905 the standalone runner had no branch for it at all --
        ``project_event`` fell through to ``return False`` and the
        business-proof ``quality_gate`` check FAILed with "no delegation
        projection row" for every deterministic-path delegation.
        """
        runner = DelegationProjectionRunner()
        mock_db = _mock_db()
        runner._db = mock_db  # type: ignore[assignment]

        topic = runner._topic_quality_gate_result
        assert topic, (
            "contract must already declare quality-gate-result.v1 "
            "(landed by omnimarket#2052 / OMN-15850)"
        )
        data = _real_quality_gate_result_payload(
            correlation_id=_CORRELATION_ID, passed=True
        )
        meta = MessageMeta(partition=0, offset=2, fallback_id=_CORRELATION_ID)

        ok = asyncio.run(runner.project_event(topic, data, meta))

        # This is the RED assertion pre-port: `project_event` returned False
        # because no topic branch matched quality-gate-result.v1 at all.
        assert ok is True
        insert_calls = [
            c
            for c in mock_db.execute.await_args_list
            if str(c.args[0]).strip().startswith("INSERT INTO delegation_events")
        ]
        assert len(insert_calls) == 1
        by_column = _param_by_column(insert_calls[0].args)
        assert by_column["correlation_id"] == _CORRELATION_ID
        assert by_column["quality_gate_passed"] is True
        assert by_column["score_source"] == "deterministic_acceptance"

    def test_quality_gate_result_on_existing_row_only_updates_verdict_columns(
        self,
    ) -> None:
        """A terminal event already wrote task_type/delegated_to/tenant_id;
        the quality-gate-result UPSERT must not clobber those columns --
        the targeted-column UPSERT (OMN-15905's ``_dynamic_upsert``) only
        names the verdict-owned columns, matching
        ``HandlerProjectionDelegation.project_quality_gate_result``'s own
        documented ON CONFLICT semantics.
        """
        runner = DelegationProjectionRunner()
        mock_db = _mock_db()
        # Simulate an existing row from an earlier terminal write.
        mock_db.execute = AsyncMock(
            side_effect=[
                [
                    {
                        "correlation_id": _CORRELATION_ID,
                        "tenant_id": _TENANT,
                        "task_type": "code-review",
                        "quality_gate_passed": False,
                    }
                ],
                None,
            ]
        )
        runner._db = mock_db  # type: ignore[assignment]

        topic = runner._topic_quality_gate_result
        data = _real_quality_gate_result_payload(
            correlation_id=_CORRELATION_ID, passed=True
        )
        meta = MessageMeta(partition=0, offset=3, fallback_id=_CORRELATION_ID)

        ok = asyncio.run(runner.project_event(topic, data, meta))

        assert ok is True
        insert_call = mock_db.execute.await_args_list[-1]
        sql = str(insert_call.args[0])
        assert "task_type" not in sql, (
            "quality-gate-result UPSERT must not touch task_type -- it is not "
            "the verdict's column to own"
        )
        assert "tenant_id" not in sql, (
            "quality-gate-result UPSERT must not touch tenant_id -- an "
            "already-resolved tenant on the row is untouched"
        )
        by_column = _param_by_column(insert_call.args)
        assert by_column["quality_gate_passed"] is True
        # A pre-existing row means created_at is NOT re-stamped (OMN-13171
        # sticky-created_at semantics).
        assert "created_at" not in by_column
