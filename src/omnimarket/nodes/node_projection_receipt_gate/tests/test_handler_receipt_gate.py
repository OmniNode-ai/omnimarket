# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17210: HandlerReceiptGateProjectionRunner -- the Kafka->Postgres writer for
``receipt_gate_rows``.

WHY THIS EXISTS
---------------
``node_projection_receipt_gate`` shipped a *pure* reducer
(``reduce_receipt_gate``: ``(rows, event) -> rows``) and a ``handle_dict`` shim
around it. Neither consumes Kafka and neither writes a row: the reducer's whole
output is an in-memory tuple the caller is expected to keep. So
``public.receipt_gate_rows`` stayed migrated-and-empty on staging, the
projection API served an empty list for
``onex.snapshot.projection.receipt-gate.v1``, and the omnidash receipt-gate
widget rendered a truthful empty state indistinguishable from "no receipts have
been signed yet" (OMN-17191 "Projection coverage" -> OMN-17210).

Every sibling projection node closes that gap with a standalone
``BaseProjectionRunner`` subclass carrying its own ``__main__`` -- the shared
dispatch kernel deliberately no-ops ``*ProjectionRunner``-suffixed classes, so a
dedicated process is the sanctioned way to run one. This module is the RED half
of adding that runner: written against a class that does not exist yet.

The runner does NOT reimplement the event->row mapping. It delegates to the
existing pure reducer, so the two shapes the reducer already handles
(verification-receipt-completed, evidence-validated) stay a single source of
truth and the reducer's own tests keep covering them.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.nodes.node_projection_receipt_gate.handlers.handler_receipt_gate import (
    HandlerReceiptGateProjectionRunner,
)
from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta

RECEIPT_TOPIC = "onex.evt.omnimarket.verification-receipt-completed.v1"
EVIDENCE_TOPIC = "onex.evt.omnimarket.evidence-validated.v1"


def _mock_db() -> Any:
    mock_db = MagicMock(spec=AsyncpgAdapter)
    mock_db.execute = AsyncMock(return_value=None)
    return mock_db


def _meta(topic: str) -> MessageMeta:
    return MessageMeta(partition=0, offset=17, fallback_id="fallback", topic=topic)


def _runner(db: Any) -> HandlerReceiptGateProjectionRunner:
    runner = HandlerReceiptGateProjectionRunner()
    runner._db = db  # noqa: SLF001 -- same seam the sibling runner tests use
    return runner


@pytest.mark.unit
class TestRunnerShape:
    """It must be the same deployable process shape as the sibling writers."""

    def test_is_a_base_projection_runner(self) -> None:
        assert issubclass(HandlerReceiptGateProjectionRunner, BaseProjectionRunner)

    def test_subscribes_to_exactly_the_contract_topics(self) -> None:
        runner = HandlerReceiptGateProjectionRunner()
        assert runner.subscribe_topics == [RECEIPT_TOPIC, EVIDENCE_TOPIC]
        # ``topics`` is the abstract property the base class consumes; it must
        # not drift from the contract-derived list.
        assert runner.topics == runner.subscribe_topics

    def test_module_exposes_a_main_entrypoint(self) -> None:
        """The Deployment runs ``python -m <module>``; without a ``__main__``
        block the container starts, exits 0 and the pod CrashLoopBackOffs with
        no error to read."""
        import inspect

        from omnimarket.nodes.node_projection_receipt_gate.handlers import (
            handler_receipt_gate,
        )

        source = inspect.getsource(handler_receipt_gate)
        assert 'if __name__ == "__main__":' in source
        assert "asyncio.run(runner.run())" in source

    def test_table_name_comes_from_the_contract_not_a_literal(self) -> None:
        runner = HandlerReceiptGateProjectionRunner()
        assert runner.table_receipt_gate_rows == "receipt_gate_rows"


@pytest.mark.unit
class TestVerificationReceiptProjection:
    def test_one_row_is_written_per_check_dimension(self) -> None:
        db = _mock_db()
        runner = _runner(db)
        event: dict[str, Any] = {
            "task_id": "OMN-17210",
            "pr_number": 1234,
            "repo": "omninode_infra",
            "verifier": "node_verification_receipt_generator",
            "verified_at": "2026-08-30T12:00:00+00:00",
            "overall_pass": True,
            "checks": [
                {"dimension": "ci_checks", "passed": True, "summary": "all green"},
                {"dimension": "pytest", "passed": False, "summary": "2 failed"},
            ],
        }

        assert asyncio.run(
            runner.project_event(RECEIPT_TOPIC, event, _meta(RECEIPT_TOPIC))
        )

        assert db.execute.await_count == 2
        names = [call.args[1] for call in db.execute.await_args_list]
        assert names == ["ci_checks", "pytest"]
        passes = [call.args[2] for call in db.execute.await_args_list]
        assert passes == [True, False]
        # pr_ref is the reducer's composition, proving the runner delegates
        # rather than re-deriving.
        assert call_arg(db, 0, 4) == "OMN-17210 / #1234"

    def test_the_insert_targets_the_contract_table(self) -> None:
        db = _mock_db()
        runner = _runner(db)
        asyncio.run(
            runner.project_event(
                RECEIPT_TOPIC,
                {"overall_pass": True, "task_id": "OMN-1", "checks": []},
                _meta(RECEIPT_TOPIC),
            )
        )
        sql = db.execute.await_args_list[0].args[0]
        assert "receipt_gate_rows" in sql
        assert "INSERT INTO" in sql

    def test_observed_at_is_bound_as_a_datetime_not_a_string(self) -> None:
        """asyncpg binds TIMESTAMPTZ from a datetime; a str raises DataError
        and CrashLoopBackOffs the writer (the OMN-15905 round-2 defect)."""
        from datetime import datetime

        db = _mock_db()
        runner = _runner(db)
        asyncio.run(
            runner.project_event(
                RECEIPT_TOPIC,
                {
                    "overall_pass": True,
                    "task_id": "OMN-1",
                    "verified_at": "2026-08-30T12:00:00+00:00",
                },
                _meta(RECEIPT_TOPIC),
            )
        )
        observed_at = db.execute.await_args_list[0].args[-1]
        assert isinstance(observed_at, datetime)
        assert observed_at.tzinfo is not None


@pytest.mark.unit
class TestEvidenceValidatedProjection:
    def test_evidence_validated_writes_one_occ_row(self) -> None:
        db = _mock_db()
        runner = _runner(db)
        event: dict[str, Any] = {
            "ticket_id": "OMN-17210",
            "pr_number": 99,
            "validation_state": "PASSED",
            "evidence_bundle_hash": "abc123",
            "validated_at": "2026-08-30T12:00:00+00:00",
            "evidence_lifecycle_state": "VALIDATED",
        }

        assert asyncio.run(
            runner.project_event(EVIDENCE_TOPIC, event, _meta(EVIDENCE_TOPIC))
        )

        assert db.execute.await_count == 1
        assert call_arg(db, 0, 1) == "occ-evidence"
        assert call_arg(db, 0, 2) is True
        assert call_arg(db, 0, 8) == "abc123"

    def test_topic_selects_the_event_shape_rather_than_field_sniffing(self) -> None:
        """The reducer falls back to structural guessing when no event-type hint
        is present. The runner knows the topic, so it must pass that through --
        otherwise a receipt payload that happens to carry
        ``evidence_lifecycle_state`` is projected as an OCC row."""
        db = _mock_db()
        runner = _runner(db)
        asyncio.run(
            runner.project_event(
                RECEIPT_TOPIC,
                {
                    "task_id": "OMN-1",
                    "overall_pass": True,
                    "verified_at": "2026-08-30T12:00:00+00:00",
                    "evidence_lifecycle_state": "VALIDATED",
                },
                _meta(RECEIPT_TOPIC),
            )
        )
        assert call_arg(db, 0, 1) == "overall"

    def test_the_incoming_event_dict_is_not_mutated(self) -> None:
        db = _mock_db()
        runner = _runner(db)
        event = {"ticket_id": "OMN-1", "validation_state": "PASSED"}
        before = dict(event)
        asyncio.run(runner.project_event(EVIDENCE_TOPIC, event, _meta(EVIDENCE_TOPIC)))
        assert event == before


@pytest.mark.unit
class TestUnknownTopic:
    def test_an_unsubscribed_topic_is_refused_rather_than_best_effort_written(
        self,
    ) -> None:
        """The base class only delivers subscribed topics, so anything else is a
        wiring bug. Writing a ``_best_effort_row`` for it would put junk rows in
        the widget and hide the bug."""
        db = _mock_db()
        runner = _runner(db)
        assert not asyncio.run(
            runner.project_event(
                "onex.evt.omnimarket.something-else.v1",  # onex-topic-allow: negative proof
                {"name": "x"},
                _meta(
                    "onex.evt.omnimarket.something-else.v1"
                ),  # onex-topic-allow: negative proof
            )
        )
        db.execute.assert_not_awaited()


def call_arg(db: Any, call_index: int, arg_index: int) -> Any:
    return db.execute.await_args_list[call_index].args[arg_index]
