# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Acceptance tests for the durable definition-of-done verdict (OMN-18900).

Decision 3 of the Jev typed-decision shadow plan. These are the falsifiers
named on the ticket's acceptance criteria and in the node contract's
``dod_evidence`` block, and every one of them executes the product rather
than inspecting it.

The captured payload the fixtures are built from is real: the single verdict
the 2026-09-20 inventory could read off this fleet, for ticket OMN-17372 --
88 checks, 13 verified, 2 failed, 1 skipped, 72 NON-PROBATIVE and ZERO
behaviour-proving. It is used as the fixture on purpose. A test suite built
only from invented healthy payloads would never have exercised the case the
plan added the second conjunct for.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from omnimarket.enums.enum_dod_verify_status import EnumDodVerifyStatus
from omnimarket.enums.enum_dod_verify_unresolved_cause import (
    EnumDodVerifyUnresolvedCause,
)
from omnimarket.nodes.node_projection_dod_verdict.handlers.handler_dod_verdict_runner import (
    DodVerdictProjectionWriter,
)
from omnimarket.nodes.node_projection_dod_verdict.handlers.handler_projection_dod_verdict import (
    MINIMUM_BEHAVIOR_PROVING_CHECKS,
    HandlerProjectionDodVerdict,
    resolve_dod_eval_outcome,
)
from omnimarket.nodes.node_projection_dod_verdict.models import (
    EnumDodEvalOutcome,
    EnumDodEvalRefusal,
    ModelDodEvalVerdict,
    ModelDodVerdictProjectionRequest,
    ModelDodVerdictWire,
)

pytestmark = pytest.mark.unit

CORRELATION = UUID("d4396b48-e783-4523-98b1-5f795b5f7b51")
STARTED = datetime(2026, 9, 20, 11, 0, tzinfo=UTC)
COMPLETED = datetime(2026, 9, 20, 11, 4, tzinfo=UTC)

#: The verdict recorded for OMN-17372, field for field. 72 of 88 checks
#: non-probative and none behaviour-proving.
CAPTURED_PAYLOAD: dict[str, Any] = {
    "correlation_id": str(CORRELATION),
    "ticket_id": "OMN-17372",
    "status": "failed",
    "started_at": STARTED.isoformat(),
    "completed_at": COMPLETED.isoformat(),
    "total_checks": 88,
    "verified_count": 13,
    "failed_count": 2,
    "skipped_count": 1,
    "superseded_count": 0,
    "non_probative_count": 72,
    "behavior_proving_count": 0,
    # Absent from the captured payload, which predates both counters. Their
    # absence is what a real older event looks like on this topic.
}


CAPTURED_PAYLOAD_TIMES = {"completed_at": COMPLETED, "started_at": STARTED}


def _payload(**overrides: Any) -> dict[str, Any]:
    """The captured payload with named fields replaced."""
    return {**CAPTURED_PAYLOAD, **overrides}


def _fold(**overrides: Any) -> Any:
    """Run the pure fold over one payload and return its result."""
    event = ModelDodVerdictWire.model_validate(_payload(**overrides))
    return HandlerProjectionDodVerdict().handle(
        ModelDodVerdictProjectionRequest(event=event)
    )


class _RecordingDb:
    """A database double that records every statement and its bound values.

    It exists to walk the write path, not to type-check the SQL. The question
    a double cannot answer -- a string bound where a TIMESTAMPTZ or a UUID is
    declared -- is answered by the real-Postgres gate in
    ``test_omn18900_real_postgres_dod_verdict_write_path.py``.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.rows_to_return: list[dict[str, Any]] | None = None

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def execute(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append((sql, args))
        if self.rows_to_return is not None:
            return self.rows_to_return
        return [
            {
                "ticket_id": args[0],
                "correlation_id": args[1],
                "completed_at": args[2],
                "outcome": args[15],
                "outcome_refusal": args[16],
            }
        ]


def _bound(db: _RecordingDb) -> dict[str, Any]:
    """Name the positional binds of the single upsert this writer issues."""
    assert len(db.calls) == 1, f"expected exactly one statement, got {len(db.calls)}"
    _sql, args = db.calls[0]
    names = (
        "ticket_id",
        "correlation_id",
        "completed_at",
        "started_at",
        "status",
        "unresolved_cause",
        "total_checks",
        "verified_count",
        "failed_count",
        "skipped_count",
        "superseded_count",
        "non_probative_count",
        "behavior_proving_count",
        "readback_proving_count",
        "unbindable_overlay_count",
        "outcome",
        "outcome_refusal",
        "error_message",
        "projected_at",
        # OMN-19514: the delegation run the verification judged.
        "delegation_correlation_id",
    )
    assert len(args) == len(names), f"bind count moved: {len(args)} vs {len(names)}"
    return dict(zip(names, args, strict=True))


def _write(**overrides: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Drive the real writer over one payload; return its report and the binds."""
    writer = DodVerdictProjectionWriter()
    db = _RecordingDb()
    writer._db = db  # type: ignore[assignment]
    report = writer.handle(dict(_payload(**overrides)))
    return report, _bound(db)


# --------------------------------------------------------------------------
# AC-1 — every class count reaches the row, field by field
# --------------------------------------------------------------------------


def test_ac1_every_class_count_reaches_the_row_field_by_field() -> None:
    """Each count column equals the payload field of the same name.

    Compared one field at a time rather than as an aggregate: a total that
    happens to match while two components are transposed is exactly the
    failure an aggregate assertion cannot see.
    """
    _report, bound = _write()

    for field in (
        "total_checks",
        "verified_count",
        "failed_count",
        "skipped_count",
        "superseded_count",
        "non_probative_count",
        "behavior_proving_count",
    ):
        assert bound[field] == CAPTURED_PAYLOAD[field], field

    assert bound["ticket_id"] == "OMN-17372"
    assert bound["correlation_id"] == CORRELATION
    assert bound["completed_at"] == COMPLETED
    assert bound["started_at"] == STARTED
    assert bound["status"] == EnumDodVerifyStatus.FAILED.value


def test_ac1_counters_absent_from_an_older_payload_project_as_zero() -> None:
    """The two counters added after the capture default rather than refuse.

    An event predating ``readback_proving_count`` and
    ``unbindable_overlay_count`` must still project. Refusing it would turn a
    schema addition on the producer into silent data loss on this side, which
    is the OMN-18868 class the consumer-first rule exists for.
    """
    _report, bound = _write()
    assert bound["readback_proving_count"] == 0
    assert bound["unbindable_overlay_count"] == 0


def test_ac1_the_row_carries_the_event_clock_and_never_a_wall_clock() -> None:
    """Every timestamp on the row but ``projected_at`` is the event's own.

    A replay has to reproduce the row rather than re-date it; otherwise the
    key would not converge and one run would land twice.
    """
    first, first_bound = _write()
    second, second_bound = _write()
    for field in ("completed_at", "started_at"):
        assert (
            first_bound[field] == second_bound[field] == CAPTURED_PAYLOAD_TIMES[field]
        )
    assert first["dod_verdict_rows"] == second["dod_verdict_rows"]


# --------------------------------------------------------------------------
# AC-2 — a failure is as durable as a pass
# --------------------------------------------------------------------------


def test_ac2_a_failing_verdict_writes_a_row() -> None:
    """The captured payload IS a failing verdict, and it writes.

    A verdict surface that exists only on success cannot distinguish a
    failure from a verification nobody ran, and telling those apart is the
    whole reason to have one.
    """
    report, bound = _write()
    assert bound["status"] == EnumDodVerifyStatus.FAILED.value
    assert report["rows_upserted"] == 1


def test_ac2_an_unresolved_verdict_writes_a_row() -> None:
    """An unresolved run writes too, carrying its cause.

    Before the unresolved member existed such a run was indistinguishable
    from the model's ``pending`` default, which reads as "not yet attempted".
    A projection that dropped it would put that ambiguity back.
    """
    report, bound = _write(
        status="unresolved",
        unresolved_cause=EnumDodVerifyUnresolvedCause.RUN_ERROR_OR_TIMEOUT.value,
    )
    assert report["rows_upserted"] == 1
    assert bound["status"] == EnumDodVerifyStatus.UNRESOLVED.value
    assert (
        bound["unresolved_cause"]
        == EnumDodVerifyUnresolvedCause.RUN_ERROR_OR_TIMEOUT.value
    )


def test_ac2_a_verified_verdict_writes_a_row() -> None:
    """The positive control: the passing case writes by the same path.

    Without it the two tests above would also pass over a writer that wrote
    on every input and a writer that wrote on none.
    """
    report, bound = _write(status="verified", failed_count=0, behavior_proving_count=4)
    assert report["rows_upserted"] == 1
    assert bound["status"] == EnumDodVerifyStatus.VERIFIED.value


def test_ac2_no_status_appears_as_a_literal_anywhere_in_the_writer() -> None:
    """There is no status filter to go stale, and the check is mechanical.

    A future edit adding ``if status == ...`` to the write path is the way
    this surface would quietly go back to recording successes only, and a
    reviewer reading a diff is not a mechanism.
    """
    import inspect

    from omnimarket.nodes.node_projection_dod_verdict.handlers import (
        handler_dod_verdict_runner,
    )

    source = inspect.getsource(handler_dod_verdict_runner)
    body = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )
    for member in EnumDodVerifyStatus:
        assert f'"{member.value}"' not in body, (
            f"the writer spells the status literal {member.value!r}; a status "
            "filter here is how a verdict surface silently becomes "
            "success-only"
        )


# --------------------------------------------------------------------------
# AC-3 — the done predicate, and its typed refusal
# --------------------------------------------------------------------------


def test_ac3_a_pass_with_no_behavior_proving_check_is_refused() -> None:
    """Zero failures and zero behaviour-proving checks is REFUSED, not done.

    This is the conjunct the plan added. The captured payload's own shape --
    72 of 88 checks non-probative, none behaviour-proving -- is why: a
    verdict whose checks prove no behaviour is not an outcome label.
    """
    verdict = resolve_dod_eval_outcome(
        status=EnumDodVerifyStatus.VERIFIED,
        failed_count=0,
        total_checks=88,
        behavior_proving_count=0,
    )
    assert verdict.outcome is EnumDodEvalOutcome.REFUSED
    assert verdict.refusal is EnumDodEvalRefusal.NO_BEHAVIOR_PROVING_CHECK
    assert verdict.is_done is False


def test_ac3_a_pass_with_one_behavior_proving_check_is_done() -> None:
    """The positive direction, so the refusal above is not vacuous.

    The floor is read from the named constant rather than spelled again, so a
    change to the bound moves this test with it instead of leaving it green
    against a stale number.
    """
    verdict = resolve_dod_eval_outcome(
        status=EnumDodVerifyStatus.VERIFIED,
        failed_count=0,
        total_checks=88,
        behavior_proving_count=MINIMUM_BEHAVIOR_PROVING_CHECKS,
    )
    assert verdict.outcome is EnumDodEvalOutcome.DONE
    assert verdict.refusal is None
    assert verdict.is_done is True


@pytest.mark.parametrize(
    ("status", "failed", "total", "behaviour", "expected"),
    [
        # A failure refuses whatever the status claims. The counts are the
        # evidence; the status is a summary of them.
        (
            EnumDodVerifyStatus.VERIFIED,
            2,
            88,
            9,
            EnumDodEvalRefusal.CHECKS_FAILED,
        ),
        (EnumDodVerifyStatus.FAILED, 2, 88, 0, EnumDodEvalRefusal.CHECKS_FAILED),
        # No failures, but the run did not pass.
        (
            EnumDodVerifyStatus.SKIPPED,
            0,
            3,
            1,
            EnumDodEvalRefusal.STATUS_NOT_VERIFIED,
        ),
        (
            EnumDodVerifyStatus.UNRESOLVED,
            0,
            0,
            0,
            EnumDodEvalRefusal.STATUS_NOT_VERIFIED,
        ),
        # `pending` is the model default and reads as "not yet attempted". It
        # must never resolve to done by defaulting.
        (
            EnumDodVerifyStatus.PENDING,
            0,
            5,
            5,
            EnumDodEvalRefusal.STATUS_NOT_VERIFIED,
        ),
        # Green by vacuum: no verdict-bearing checks ran at all.
        (EnumDodVerifyStatus.VERIFIED, 0, 0, 0, EnumDodEvalRefusal.NO_CHECKS_RUN),
    ],
)
def test_ac3_each_refusal_reason_is_reachable_and_named(
    status: EnumDodVerifyStatus,
    failed: int,
    total: int,
    behaviour: int,
    expected: EnumDodEvalRefusal,
) -> None:
    """Every member of the refusal enum is reachable, and precedence is pinned.

    The first two rows are the precedence assertion, not a duplicate: a
    verified status beside a non-zero failure count is a contradiction, and
    the rule that resolves it -- counts first -- is a decision, not an
    accident of the order the conditions are written in.
    """
    verdict = resolve_dod_eval_outcome(
        status=status,
        failed_count=failed,
        total_checks=total,
        behavior_proving_count=behaviour,
    )
    assert verdict.outcome is EnumDodEvalOutcome.REFUSED
    assert verdict.refusal is expected


def test_ac3_every_refusal_member_is_covered_by_the_table_above() -> None:
    """A new refusal member with no reachability case is a red test.

    Without this, adding a fifth member and never returning it anywhere would
    leave a typed reason that looks available and can never be produced.
    """
    reached = {
        resolve_dod_eval_outcome(
            status=status,
            failed_count=failed,
            total_checks=total,
            behavior_proving_count=behaviour,
        ).refusal
        for status, failed, total, behaviour in (
            (EnumDodVerifyStatus.VERIFIED, 2, 88, 9),
            (EnumDodVerifyStatus.SKIPPED, 0, 3, 1),
            (EnumDodVerifyStatus.VERIFIED, 0, 0, 0),
            (EnumDodVerifyStatus.VERIFIED, 0, 88, 0),
        )
    }
    assert reached == set(EnumDodEvalRefusal)


def test_ac3_the_predicate_reads_a_stored_row_without_the_payload() -> None:
    """The outcome is computable from the row ALONE.

    The four arguments are read out of a dictionary standing in for a
    database row -- no event, no payload, no re-read of the verify node's
    output. That is the acceptance criterion, asserted by the call shape
    rather than by a comment.
    """
    stored_row: dict[str, Any] = {
        "ticket_id": "OMN-17372",
        "status": "verified",
        "failed_count": 0,
        "total_checks": 88,
        "behavior_proving_count": 0,
        "non_probative_count": 72,
    }
    verdict = resolve_dod_eval_outcome(
        status=EnumDodVerifyStatus(stored_row["status"]),
        failed_count=stored_row["failed_count"],
        total_checks=stored_row["total_checks"],
        behavior_proving_count=stored_row["behavior_proving_count"],
    )
    assert verdict.refusal is EnumDodEvalRefusal.NO_BEHAVIOR_PROVING_CHECK


def test_ac3_the_row_and_the_returned_verdict_cannot_disagree() -> None:
    """One derivation, two renderings. The fold returns both from one call."""
    result = _fold(status="verified", failed_count=0, behavior_proving_count=0)
    assert result.row.outcome is result.verdict.outcome
    assert result.row.outcome_refusal is result.verdict.refusal


def test_ac3_a_refusal_without_a_reason_cannot_be_constructed() -> None:
    """The pairing invariant is enforced at construction, not documented.

    An unreasoned refusal is unactionable and a reasoned pass misreports a run
    that counted. Both shapes are refused, so no caller can build one.
    """
    with pytest.raises(ValueError, match="refusal is set exactly when"):
        ModelDodEvalVerdict(outcome=EnumDodEvalOutcome.REFUSED)
    with pytest.raises(ValueError, match="refusal is set exactly when"):
        ModelDodEvalVerdict(
            outcome=EnumDodEvalOutcome.DONE,
            refusal=EnumDodEvalRefusal.NO_CHECKS_RUN,
        )


def test_ac3_the_outcome_columns_are_bound_to_the_write() -> None:
    """The verdict reaches the database, not only the return value."""
    _report, bound = _write(status="verified", failed_count=0, behavior_proving_count=0)
    assert bound["outcome"] == EnumDodEvalOutcome.REFUSED.value
    assert (
        bound["outcome_refusal"] == EnumDodEvalRefusal.NO_BEHAVIOR_PROVING_CHECK.value
    )

    _report, bound = _write(status="verified", failed_count=0, behavior_proving_count=2)
    assert bound["outcome"] == EnumDodEvalOutcome.DONE.value
    assert bound["outcome_refusal"] is None


# --------------------------------------------------------------------------
# AC-4 — the runtime actually dispatches this writer, and reads its answer
# --------------------------------------------------------------------------


def test_ac4_the_writer_declares_in_process_runtime_dispatch() -> None:
    """Declared by name, never inferred from the class name's spelling.

    Undeclared, the runtime classifies a runner-shaped class as STANDALONE,
    subscribes its topics and dispatches nothing. There is no dedicated
    writer Deployment for this node, so undeclared it would store nothing
    while reading healthy on consumer lag and on every watermark.
    """
    assert DodVerdictProjectionWriter.onex_runtime_inprocess_dispatch is True


def test_ac4_the_pure_fold_does_not_declare_dispatch() -> None:
    """The double-dispatch the roster exists to refuse.

    Declaring the capability on the operation that already runs in-process
    through the pure handler writes every row twice.
    """
    assert not hasattr(
        HandlerProjectionDodVerdict, "onex_runtime_inprocess_dispatch"
    ), "the pure fold must not claim the dispatch branch"


def test_ac4_handle_reports_the_row_count_key_the_runtime_guard_reads() -> None:
    """A written row cannot be scored zero by the write-path guard.

    The guard understands ``rows_upserted`` and ``projected`` and scores every
    other shape zero, suppressing the terminal event and logging that the
    handler wrote no rows -- including for the messages that really did write.
    """
    report, _bound = _write()
    assert report["rows_upserted"] == 1
    assert isinstance(report["rows_upserted"], int)


def test_ac4_a_refused_write_reports_zero_rather_than_a_truthy_ack() -> None:
    """``None`` from the database is reported as zero rows, not as success."""
    writer = DodVerdictProjectionWriter()
    db = _RecordingDb()
    db.rows_to_return = []
    writer._db = db  # type: ignore[assignment]
    report = writer.handle(dict(_payload()))
    assert report["rows_upserted"] == 0
    assert report["dod_verdict_rows"] == []


def test_ac4_the_writer_reads_its_topics_and_dlq_from_the_contract() -> None:
    """Read, never restated, so declaration and behaviour cannot diverge.

    The base class defaults the DLQ list to empty, which means a node can
    declare ``dlq_topics`` and quarantine nothing: the declaration reads as
    wired and is not.
    """
    writer = DodVerdictProjectionWriter()
    assert writer.subscribe_topics == ["onex.evt.omnimarket.dod-verify-completed.v1"]
    assert writer.topics == writer.subscribe_topics
    assert writer.poison_dlq_topics == [
        "onex.dlq.omnimarket.projection-dod-verdict-malformed.v1"
    ]


def test_the_fold_is_pure_of_the_database_and_the_broker() -> None:
    """The pure half touches neither, which is what makes it unit-falsifiable."""
    import inspect

    from omnimarket.nodes.node_projection_dod_verdict.handlers import (
        handler_projection_dod_verdict,
    )

    source = inspect.getsource(handler_projection_dod_verdict)
    for forbidden in ("_db", "asyncpg", "datetime.now", "publish", "INSERT INTO"):
        assert forbidden not in source, (
            f"the pure fold references {forbidden!r}; a fold that reaches a "
            "database or a clock can only be falsified by a live lane"
        )
