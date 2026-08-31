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

RED #2 (OMN-15905 acceptance-lane defect, comment 0a99e8d7): the AsyncMock DB
double above hid a second class of bug entirely -- every ``_dynamic_upsert``
call site that stamps a ``timestamp``/``created_at``/``*_event_at`` column
bound a wall-clock ``.isoformat()`` STRING instead of a ``datetime`` object.
AsyncMock accepts anything, so the seam test's original assertions (value
equality, presence/absence of a column) passed against a string just as
happily as a real ``datetime`` -- only live asyncpg enforces the type and
raises ``DataError``. The business-proof gate's synthetic workflow event
carried no ``timestamp`` field, hit the ``event.timestamp or now`` fallback
at ``handler_delegation.py:685/690``, and crashed the INSERT every time. The
tests below assert bound-param TYPES (``isinstance(..., datetime)``), not
just presence, for every timestamp-bearing column across all four
``_dynamic_upsert`` call sites -- RED against the unmodified handler (string),
GREEN post-fix (real ``datetime``).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from omnibase_core.models.delegation.wire import EnumTierCostType, ModelTierCost

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
    # OMN-16804: the write path now resolves tenant identity through
    # tenant_registry_mirror via db.fetchval() before falling back to the
    # closed legacy map. An unconfigured AsyncMock returns another AsyncMock
    # instance, which _coerce_registry_uuid correctly refuses as "not a UUID
    # or its string form" -- these tests are not exercising the registry
    # mirror, so None (mirror has no row for this slug, same as an
    # unprovisioned-lane fresh start) is the correct stand-in and lets
    # resolution fall through to the legacy map for the fixtures' slugs.
    db.fetchval = AsyncMock(return_value=None)
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
        # OMN-15683: delegation_events.tenant_id is now UUID -- the handler
        # resolves the verified slug (_TENANT) to its canonical UUID before
        # the row is built, so the written value is the UUID, not the slug.
        assert by_column["tenant_id"] == "91c74442-1233-4c97-b191-911a10346fdf"
        assert by_column["quality_gate_passed"] is True
        # OMN-13408: cost_usd resolves from cumulative_attempt_cost via the
        # measured-cost re-pricing path, not a hardcoded 0.0.
        assert by_column["cost_usd"] > 0
        assert by_column["cost_tier_name"] == "cheap_cloud"
        # OMN-13644: context_pack_hash propagates.
        assert by_column["context_pack_hash"] == "sha256:omn15905-seam"
        # OMN-15905 acceptance-lane defect (comment 0a99e8d7): the fixture
        # payload above carries no wire "timestamp" field (the exact shape
        # of the business-proof gate's synthetic event), so this exercises
        # the `event.timestamp or now` fallback at handler_delegation.py:685.
        # `timestamp` is a TIMESTAMPTZ column -- asyncpg requires a real
        # datetime object, not an isoformat() string, or the INSERT raises
        # asyncpg.exceptions.DataError.
        assert isinstance(by_column["timestamp"], datetime), (
            "delegation_events.timestamp must bind a datetime object, not a "
            f"wall-clock isoformat() string (got {type(by_column['timestamp'])!r})"
        )

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
        # OMN-15905 acceptance-lane defect: a fresh row stamps created_at
        # (handler_delegation.py:461) -- must be a real datetime, not a
        # wall-clock isoformat() string.
        assert isinstance(by_column["created_at"], datetime), (
            "delegation_events.created_at must bind a datetime object, not a "
            f"wall-clock isoformat() string (got {type(by_column['created_at'])!r})"
        )

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


def _real_delegate_skill_terminal_payload(*, correlation_id: str) -> dict[str, Any]:
    """The real ``onex.evt.omniclaude.delegate-skill-completed.v1`` shape.

    Minimal-but-real: matches the fixture used by
    ``test_projection_accepts_and_preserves_additive_structured_truth``
    (``tests/unit/nodes/node_delegate_skill_orchestrator/
    test_structured_terminal_truth_omn15539.py``) -- ``ModelDelegateSkillResponse``
    fields not given here (``emitted_at``, etc.) fall back to their typed
    defaults via ``_payload_with_envelope_timestamp``.
    """
    return {
        "status": "completed",
        "correlation_id": correlation_id,
        "task_type": "code-review",
        "quality_gate_passed": True,
        "quality_score": 0.9,
        "model_name": "glm-5.2",
        "attempts_count": 1,
    }


@pytest.mark.unit
class TestDelegateSkillTerminalWriterParity:
    """delegate-skill-completed.v1 -> delegation_events (OMN-15905 §4.1).

    ``_upsert_delegate_skill_projection_row`` reads an already-typed
    ``AwareDatetime`` off ``ModelDelegationEventProjectionRow.timestamp``
    (``model_delegate_skill_terminal_projection.py:163``) and then calls
    ``.isoformat()`` on it before binding -- the same str-where-datetime bug
    class as the canonical-terminal path, at a different call site
    (handler_delegation.py:857/864/868).
    """

    def test_delegate_skill_terminal_writes_datetime_not_string(self) -> None:
        runner = DelegationProjectionRunner()
        mock_db = _mock_db()
        runner._db = mock_db  # type: ignore[assignment]

        topic = runner._topic_delegate_skill_completed
        assert topic, "contract must declare a delegate-skill-completed topic"
        correlation_id = str(uuid4())
        data = _real_delegate_skill_terminal_payload(correlation_id=correlation_id)
        meta = MessageMeta(partition=0, offset=4, fallback_id=correlation_id)

        ok = asyncio.run(runner.project_event(topic, data, meta))

        assert ok is True
        insert_calls = [
            c
            for c in mock_db.execute.await_args_list
            if str(c.args[0]).strip().startswith("INSERT INTO delegation_events")
        ]
        assert len(insert_calls) == 1
        by_column = _param_by_column(insert_calls[0].args)
        assert by_column["correlation_id"] == correlation_id
        assert isinstance(by_column["timestamp"], datetime), (
            "delegation_events.timestamp (delegate-skill-terminal path) must "
            "bind a datetime object, not row_model.timestamp.isoformat() "
            f"(got {type(by_column['timestamp'])!r})"
        )
        assert isinstance(by_column["created_at"], datetime), (
            "delegation_events.created_at (delegate-skill-terminal path) must "
            "bind a datetime object, not row_model.timestamp.isoformat() "
            f"(got {type(by_column['created_at'])!r})"
        )


@pytest.mark.unit
class TestBudgetStateMaterializationWriterParity:
    """Delegation terminal -> delegation_budget_state (OMN-13235/OMN-15905).

    ``_materialize_budget_state_async`` builds ``now_iso``/``event_iso`` via
    ``.isoformat()`` and stamps 4 TIMESTAMPTZ columns
    (``first_event_at``/``last_event_at``/``created_at``/``updated_at``) with
    those strings -- the same bug class, dormant in the live acceptance run
    only because the crash at the delegation_events INSERT (defect #1) always
    fired first and short-circuited this call.
    """

    def test_budget_state_upsert_binds_datetime_not_string(self) -> None:
        runner = DelegationProjectionRunner()
        mock_db = _mock_db()
        runner._db = mock_db  # type: ignore[assignment]

        stub_cost = ModelTierCost(
            cost_type=EnumTierCostType.BUDGETED,
            rate_per_1k_usd=0.01,
            monthly_cap_usd=100.0,
            overage_rate_per_1k_usd=0.02,
        )
        with patch(
            "omnimarket.nodes.node_projection_delegation.handlers."
            "handler_delegation.resolve_tier_cost",
            return_value=stub_cost,
        ):
            asyncio.run(
                runner._materialize_budget_state_async(
                    correlation_id=_CORRELATION_ID,
                    cost_tier_name="cheap_cloud",
                    cost_measurement_source="budgeted_in_budget",
                    budget_headroom_consumed_usd=1.5,
                    cost_usd=1.5,
                    tenant_id=_TENANT,
                    timestamp=None,
                )
            )

        insert_calls = [
            c
            for c in mock_db.execute.await_args_list
            if str(c.args[0]).strip().startswith("INSERT INTO delegation_budget_state")
        ]
        assert len(insert_calls) == 1
        by_column = _param_by_column(insert_calls[0].args)
        for column in ("first_event_at", "last_event_at", "created_at", "updated_at"):
            assert isinstance(by_column[column], datetime), (
                f"delegation_budget_state.{column} must bind a datetime "
                f"object, not an isoformat() string (got {type(by_column[column])!r})"
            )

    def test_budget_state_upsert_preserves_existing_first_event_at_as_datetime(
        self,
    ) -> None:
        """A second event for the same tenant/tier/period keeps the ORIGINAL
        ``first_event_at`` -- read back from the DB as a real ``datetime``
        (asyncpg's native TIMESTAMPTZ codec), not re-stringified via
        ``str(existing.get("first_event_at") or event_iso)``.
        """
        runner = DelegationProjectionRunner()
        mock_db = _mock_db()
        original_first_event_at = datetime(2026, 8, 1, tzinfo=UTC)
        mock_db.execute = AsyncMock(
            side_effect=[
                [
                    {
                        "tenant_id": _TENANT,
                        "cost_tier_name": "cheap_cloud",
                        "budget_period": original_first_event_at.strftime("%Y-%m"),
                        "consumed_usd": 0,
                        "overage_usd": 0,
                        "delegation_count": 1,
                        "last_correlation_id": "some-other-correlation-id",
                        "first_event_at": original_first_event_at,
                    }
                ],
                None,
            ]
        )
        runner._db = mock_db  # type: ignore[assignment]

        stub_cost = ModelTierCost(
            cost_type=EnumTierCostType.BUDGETED,
            rate_per_1k_usd=0.01,
            monthly_cap_usd=100.0,
            overage_rate_per_1k_usd=0.02,
        )
        with patch(
            "omnimarket.nodes.node_projection_delegation.handlers."
            "handler_delegation.resolve_tier_cost",
            return_value=stub_cost,
        ):
            asyncio.run(
                runner._materialize_budget_state_async(
                    correlation_id=_CORRELATION_ID,
                    cost_tier_name="cheap_cloud",
                    cost_measurement_source="budgeted_in_budget",
                    budget_headroom_consumed_usd=1.5,
                    cost_usd=1.5,
                    tenant_id=_TENANT,
                    timestamp=None,
                )
            )

        insert_call = mock_db.execute.await_args_list[-1]
        by_column = _param_by_column(insert_call.args)
        assert by_column["first_event_at"] == original_first_event_at
        assert isinstance(by_column["first_event_at"], datetime), (
            "a re-read existing first_event_at must stay a datetime object, "
            f"not str()-ified (got {type(by_column['first_event_at'])!r})"
        )
