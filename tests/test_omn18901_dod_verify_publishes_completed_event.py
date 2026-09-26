# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Acceptance tests for the definition-of-done verdict reaching the bus (OMN-18901).

The producer half of Task 5. Its sibling OMN-18900 landed the consumer first,
so this suite's job is to prove the two halves actually meet: that what the
verify path produces is what the already-merged projection accepts, field for
field, and that it arrives on every terminal outcome rather than only on the
happy one.

**Why there is no capturing-publisher double here.** The ticket's falsifiers
were written expecting the handler to hold a publisher and call it. It does
not, and deliberately: for a definition-B handler the runtime publishes the
RETURNED MODEL, wrapping any returned ``BaseModel`` as an output event and
routing it to the contract's declared terminal topic. The node already had a
declared terminal and a declared publish topic, so it was already publishing
-- it was publishing a payload the merged consumer had to reject, because
``ModelDodVerifyState`` carried no run window and the wire model requires one.

That makes the real seam the SHAPE of the returned state, not a call to a
publisher, and it is why these tests drive the actual objects on both sides of
the topic instead of asserting against a mock. A test that watched a double
receive a message would have passed just as happily against the broken shape.

Two ways to get this wrong were live options and both are refused by tests
below: returning the completed-event model instead (which changes the
receipt's declared result model and silently stops the evidence-autoclose
sweep flipping tickets), and letting a rehearsal become a durable row.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_dod_verify.handlers import handler_dod_verify as producer_mod
from omnimarket.nodes.node_dod_verify.handlers.handler_dod_verify import (
    HandlerDodVerify,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_start_command import (
    ModelDodVerifyStartCommand,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumDodVerifyStatus,
    EnumEvidenceCheckStatus,
    ModelDodVerifyState,
    ModelEvidenceCheckResult,
)
from omnimarket.nodes.node_projection_dod_verdict.handlers.handler_dod_verdict_runner import (
    ROWS_REFUSED_KEY,
    DodVerdictProjectionWriter,
)
from omnimarket.nodes.node_projection_dod_verdict.handlers.handler_projection_dod_verdict import (
    HandlerProjectionDodVerdict,
)
from omnimarket.nodes.node_projection_dod_verdict.models import (
    ModelDodVerdictProjectionRequest,
    ModelDodVerdictWire,
)

pytestmark = pytest.mark.unit

PRODUCER_CONTRACT = (
    Path(producer_mod.__file__).resolve().parent.parent / "contract.yaml"
)
COMPLETED_TOPIC = "onex.evt.omnimarket.dod-verify-completed.v1"

#: The counters the projection reads off the wire. Named once so a test
#: cannot quietly check a subset of them.
COUNT_FIELDS = (
    "total_checks",
    "verified_count",
    "failed_count",
    "skipped_count",
    "superseded_count",
    "non_probative_count",
    "behavior_proving_count",
    "readback_proving_count",
    "unbindable_overlay_count",
)


def _command(ticket_id: str = "OMN-18901", dry_run: bool = False) -> Any:
    return ModelDodVerifyStartCommand(
        correlation_id=uuid4(),
        ticket_id=ticket_id,
        dry_run=dry_run,
        requested_at=datetime.now(tz=UTC),
    )


def _check(
    evidence_id: str,
    status: EnumEvidenceCheckStatus,
    message: str | None = None,
) -> ModelEvidenceCheckResult:
    return ModelEvidenceCheckResult(
        evidence_id=evidence_id,
        description=f"check {evidence_id}",
        status=status,
        message=message,
    )


VERIFIED_CHECKS = [
    _check("dod-001", EnumEvidenceCheckStatus.VERIFIED),
    _check("dod-002", EnumEvidenceCheckStatus.VERIFIED),
]
FAILED_CHECKS = [
    _check("dod-001", EnumEvidenceCheckStatus.VERIFIED),
    _check("dod-002", EnumEvidenceCheckStatus.FAILED, "the assertion did not hold"),
]


def _run(checks: list[ModelEvidenceCheckResult], **kw: Any) -> ModelDodVerifyState:
    """Run the real verify path over supplied evidence and return its state."""
    state = HandlerDodVerify().handle(_command(**kw), evidence_results=checks)
    assert isinstance(state, ModelDodVerifyState)
    return state


def _published_payload(state: ModelDodVerifyState) -> dict[str, Any]:
    """The bytes-equivalent dict the runtime publishes for a returned state.

    The runtime wraps a returned model and serialises it; what reaches the
    topic is this dump. Taking it from the state rather than hand-writing a
    fixture is the point -- a fixture would keep passing after the model moved.
    """
    return dict(state.model_dump(mode="json"))


class _RecordingDb:
    """Records the statements the writer issues. Same shape as OMN-18900's."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def execute(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append((sql, args))
        return [
            {
                "ticket_id": args[0],
                "correlation_id": args[1],
                "completed_at": args[2],
                "outcome": args[15],
                "outcome_refusal": args[16],
            }
        ]


def _project(payload: dict[str, Any]) -> tuple[dict[str, Any], _RecordingDb]:
    """Drive the REAL merged writer over a payload; return its report and the db."""
    writer = DodVerdictProjectionWriter()
    db = _RecordingDb()
    writer._db = db  # type: ignore[assignment]
    report = writer.handle(dict(payload))
    return report, db


# --------------------------------------------------------------------------
# AC-1 — what the producer emits is what the merged consumer accepts
# --------------------------------------------------------------------------


def test_ac1_the_state_the_runtime_publishes_validates_against_the_merged_wire_model() -> (
    None
):
    """The published payload is accepted by the consumer that already landed.

    This is the whole ticket in one assertion. Before this change the same
    call produced a payload the wire model rejected outright for want of a run
    window, so the projection would have dead-lettered every message on a
    topic whose consumer group reads perfectly healthy.
    """
    state = _run(VERIFIED_CHECKS)

    event = ModelDodVerdictWire.model_validate(_published_payload(state))

    assert event.ticket_id == state.ticket_id
    assert event.correlation_id == state.correlation_id
    assert event.status is state.status


def test_ac1_every_counter_survives_the_crossing_field_by_field() -> None:
    """Each counter equals the producer's field of the same name.

    One field at a time, not as a total: two counters transposed still sum
    correctly, and an aggregate assertion cannot see it.
    """
    state = _run(FAILED_CHECKS)

    event = ModelDodVerdictWire.model_validate(_published_payload(state))

    for field in COUNT_FIELDS:
        assert getattr(event, field) == getattr(state, field), (
            f"{field} did not survive the crossing: producer "
            f"{getattr(state, field)!r} against consumer {getattr(event, field)!r}"
        )


def test_ac1_the_run_window_is_real_rather_than_defaulted() -> None:
    """The state carries a window that brackets the run it describes.

    A default-constructed timestamp would satisfy the consumer's schema and
    still be a lie. Asserting the window contains the call proves the handler
    read the clock around the work rather than at model construction.
    """
    before = datetime.now(tz=UTC)
    state = _run(VERIFIED_CHECKS)
    after = datetime.now(tz=UTC)

    assert state.started_at.tzinfo is not None, "a naive timestamp is not a moment"
    assert state.completed_at.tzinfo is not None
    assert before <= state.started_at <= state.completed_at <= after


def test_ac1_the_event_twin_agrees_with_the_state_about_when_the_run_happened() -> None:
    """The completed-event form reads its window off the state, not the clock.

    Two objects describing one run must not disagree about when it was. They
    did before: the event re-read the clock at construction, so the pair a
    single verification produced carried two different completion times.
    """
    handler = HandlerDodVerify()
    state, completed = handler.run_verification(_command(), VERIFIED_CHECKS)

    assert completed.started_at == state.started_at
    assert completed.completed_at == state.completed_at


# --------------------------------------------------------------------------
# AC-2 — every terminal outcome reaches the row, failure included
# --------------------------------------------------------------------------


def test_ac2_a_failed_verdict_produces_a_row() -> None:
    """THE positive control on the whole write path.

    A verdict surface that exists only on success cannot tell a failure apart
    from a verification nobody ran, which is the entire reason to have one.
    This drives the real producer and the real merged writer end to end and
    asserts a row was issued carrying the failure.
    """
    state = _run(FAILED_CHECKS)
    assert state.status is EnumDodVerifyStatus.FAILED, (
        "fixture drifted: this control is only meaningful on a failing verdict"
    )

    report, db = _project(_published_payload(state))

    assert len(db.calls) == 1, "a failing verdict must issue exactly one upsert"
    _sql, args = db.calls[0]
    assert args[0] == state.ticket_id
    assert args[4] == EnumDodVerifyStatus.FAILED.value
    assert args[8] == state.failed_count
    assert report["rows_upserted"] == 1


def test_ac2_a_verified_verdict_produces_a_row_too() -> None:
    """The matched control. Both arms write, so neither is the special case."""
    state = _run(VERIFIED_CHECKS)
    assert state.status is EnumDodVerifyStatus.VERIFIED

    report, db = _project(_published_payload(state))

    assert len(db.calls) == 1
    assert db.calls[0][1][4] == EnumDodVerifyStatus.VERIFIED.value
    assert report["rows_upserted"] == 1


def test_ac2_the_verify_path_gates_its_return_on_no_status_literal() -> None:
    """No status filter stands between a verdict and its publication.

    Asserted over the source rather than by enumerating statuses, because the
    failure this guards against is a future edit adding a filter, not today's
    behaviour.

    The check is "no branch survives the verdict", not "no status word
    appears". A bare word search is the wrong instrument here and says so
    from experience: ``verified`` occurs inside ``verified_count=verified``,
    so a substring matcher reports the field name as a status filter and the
    only way to satisfy it is to rename a field for the test's benefit.
    """
    verify_src = inspect.getsource(HandlerDodVerify._handle_typed)

    tail = verify_src.split("state = ModelDodVerifyState(", 1)
    assert len(tail) == 2, "the state construction moved; re-anchor this test"
    after_construction = tail[1]

    branches = [
        line.strip()
        for line in after_construction.splitlines()
        if line.strip().startswith(("if ", "elif ", "match ", "return None"))
    ]
    assert branches == [], (
        f"a branch survives the verdict: {branches!r}. Once the state is "
        "built it is returned unconditionally, because the runtime publishes "
        "the returned model and a branch here would make publication depend "
        "on which terminal the run reached"
    )

    # Positive control: the same matcher DOES find branches earlier in the
    # same function, where the verdict is still being decided. A matcher that
    # cannot fire is not evidence of absence.
    before_construction = tail[0]
    assert [
        line
        for line in before_construction.splitlines()
        if line.strip().startswith(("if ", "elif "))
    ], "the branch matcher found nothing anywhere; it is not measuring"


# --------------------------------------------------------------------------
# AC-3 — a publishing fault cannot destroy a verdict
# --------------------------------------------------------------------------


def test_ac3_the_handler_holds_no_publisher_so_a_publish_fault_is_downstream() -> None:
    """The verdict is complete and returned before anything is published.

    The structural form of "a publisher fault never fails the verification".
    The handler takes no bus, holds no bus and calls none: it returns its
    state, and the runtime publishes afterwards. A fault therefore happens
    after the value the caller receives already exists, and cannot unmake it.

    This is a stronger guarantee than a fault-injection test around a
    try/except, because there is no publish inside the verification to fault.
    """
    params = set(inspect.signature(HandlerDodVerify).parameters)
    assert "event_publisher" not in params, (
        "the handler has taken a publisher; a publish fault can now reach the "
        "verification and this ticket's AC-3 needs a fault-injection test"
    )
    assert "event_bus" not in params

    source = Path(producer_mod.__file__).read_text(encoding="utf-8")
    for forbidden in ("event_bus.publish", "self._bus", "await bus.publish"):
        assert forbidden not in source, f"{forbidden!r} appeared in the verify path"


def test_ac3_the_returned_model_is_the_state_so_the_receipt_contract_holds() -> None:
    """The return type is load-bearing far outside this node. Pin it.

    The evidence-autoclose sweep in the infrastructure repository decides how
    to read a definition-of-done receipt by matching its declared result-model
    string against this exact class. Returning the completed-event twin here
    would publish a valid event and silently stop ticket flips, an inversion
    that has already happened once on this surface.
    """
    state = _run(VERIFIED_CHECKS)

    assert type(state) is ModelDodVerifyState
    qualified = f"{type(state).__module__}.{type(state).__qualname__}"
    assert qualified == (
        "omnimarket.nodes.node_dod_verify.models.model_dod_verify_state."
        "ModelDodVerifyState"
    ), (
        "the receipt's declared result model moved; the evidence-autoclose "
        "sweep matches this string literally and will stop parsing verdicts"
    )


# --------------------------------------------------------------------------
# A rehearsal is not an attempt
# --------------------------------------------------------------------------


def test_a_dry_run_verdict_is_not_projected() -> None:
    """A rehearsal produces no row.

    The producing node's dry-run flag is documented as running the checks and
    emitting nothing. Nothing was emitted at all until this ticket, so the
    flag had never had to mean anything. Now that a run publishes, a rehearsal
    reaching the table would inflate the attempts-until-done measure the table
    exists to feed.
    """
    state = _run(VERIFIED_CHECKS, dry_run=True)
    assert state.dry_run is True

    report, db = _project(_published_payload(state))

    assert db.calls == [], "a rehearsal was written to the verdict table"
    assert report["rows_upserted"] == 0


def test_the_same_verdict_without_the_flag_is_projected() -> None:
    """The paired positive control for the refusal above.

    Without it, a writer broken in any other way would produce the same empty
    result and read as the dry-run guard working.
    """
    state = _run(VERIFIED_CHECKS, dry_run=False)

    report, db = _project(_published_payload(state))

    assert len(db.calls) == 1
    assert report["rows_upserted"] == 1


def test_the_dry_run_skip_reports_a_refusal_rather_than_a_silent_zero() -> None:
    """The skip reports zero rows AND one declined row.

    The runtime logs a bare zero-row projection as an ERROR, and its apply
    counter reads it as a write path that stored nothing, precisely so a
    projection that stores nothing cannot pass for healthy. A deliberate skip
    must therefore report zero rows honestly and ALSO name itself a refusal,
    under the key the runtime reads for exactly that (OMN-18992), so it is
    logged as expected rather than training people to skip the error class.
    """
    state = _run(VERIFIED_CHECKS, dry_run=True)

    report, _db = _project(_published_payload(state))

    assert report["rows_upserted"] == 0
    assert report[ROWS_REFUSED_KEY] == 1
    assert report["dod_verdict_rows"] == []


def test_a_written_verdict_reports_no_refusal() -> None:
    """Positive control for the refusal key: a real row declines nothing."""
    state = _run(VERIFIED_CHECKS, dry_run=False)

    report, _db = _project(_published_payload(state))

    assert report["rows_upserted"] == 1
    assert report[ROWS_REFUSED_KEY] == 0


def test_the_rehearsal_decision_is_the_pure_folds() -> None:
    """The fold, not the writer, decides a rehearsal is not stored (rule 7a).

    The writer persists what the fold hands it and nothing else, so the
    decision is falsifiable here without a database. The verdict is still
    computed, so an in-memory caller sees what the rehearsal concluded.
    """
    fold = HandlerProjectionDodVerdict()
    rehearsal = ModelDodVerdictWire.model_validate(
        _published_payload(_run(VERIFIED_CHECKS, dry_run=True))
    )
    real = ModelDodVerdictWire.model_validate(
        _published_payload(_run(VERIFIED_CHECKS, dry_run=False))
    )

    declined = fold.handle(ModelDodVerdictProjectionRequest(event=rehearsal))
    kept = fold.handle(ModelDodVerdictProjectionRequest(event=real))

    assert declined.row is None
    assert declined.verdict == kept.verdict
    assert kept.row is not None
    assert kept.row.ticket_id == real.ticket_id


def test_a_state_without_a_run_window_cannot_be_built() -> None:
    """Fail closed: no default window for the projection to store as history.

    A defaulted timestamp is the moment the model happened to be constructed,
    which the table could not tell apart from a real run window.
    """
    with pytest.raises(ValidationError):
        ModelDodVerifyState(correlation_id=uuid4(), ticket_id="OMN-18901")
    with pytest.raises(ValidationError):
        ModelDodVerifyState(
            correlation_id=uuid4(),
            ticket_id="OMN-18901",
            started_at=datetime.now(tz=UTC),
        )


# --------------------------------------------------------------------------
# AC-4 — the contract's declarations, and the surviving spelling
# --------------------------------------------------------------------------


def _contract() -> dict[str, Any]:
    return dict(yaml.safe_load(PRODUCER_CONTRACT.read_text(encoding="utf-8")))


def test_ac4_the_terminal_topic_is_not_declared_externally_consumed() -> None:
    """The sink declaration stays gone.

    Removed by the consumer half once a node contract began subscribing to
    this topic. Pinned here because the producer landing is exactly when
    somebody might re-add it.
    """
    raw = _contract()
    declared = raw.get("externally_consumed_topics") or []

    assert COMPLETED_TOPIC not in declared, (
        "the terminal topic is declared a sink again, and it has a subscriber"
    )


def test_ac4_the_contract_declares_the_terminal_as_a_publish_topic() -> None:
    """The paired positive assertion. An absence alone proves nothing.

    Deleting the whole block would satisfy the test above and break the node.
    """
    raw = _contract()
    publish = list(raw.get("event_bus", {}).get("publish_topics", []))

    assert COMPLETED_TOPIC in publish
    assert raw.get("terminal_event") == COMPLETED_TOPIC, (
        "the terminal must be the completed topic: the dispatch-result applier "
        "routes a returned model to the terminal when it is a publish topic, "
        "and that routing is what publishes this verdict at all"
    )


def test_ac4_this_contract_declares_exactly_one_completed_spelling() -> None:
    """One spelling of the completed event in this node's own contract.

    Scoped to this contract deliberately. The second spelling under another
    service namespace is retired fleet-wide by OMN-19153; its emit-daemon
    absence is asserted in
    tests/test_omn19153_emit_daemon_drops_duplicate_dod_verify_topic.py.
    """
    raw = _contract()
    event_bus = raw.get("event_bus", {})
    all_topics = [
        *event_bus.get("publish_topics", []),
        *event_bus.get("subscribe_topics", []),
        *(raw.get("externally_consumed_topics") or []),
    ]

    completed = [t for t in all_topics if t.endswith("dod-verify-completed.v1")]

    assert completed == [COMPLETED_TOPIC], (
        f"expected exactly the omnimarket spelling, found {completed!r}"
    )

    # Positive control: the matcher does fire on this contract's topic set, so
    # the single-element result above is a measurement and not an empty search.
    assert [t for t in all_topics if t.endswith("dod-verify-start.v1")]


def test_the_projection_subscribes_the_topic_this_node_publishes() -> None:
    """The two contracts name the same topic. The join, asserted directly.

    Read from both files rather than from a constant either side could drift
    from independently.
    """
    import omnimarket.nodes.node_projection_dod_verdict as consumer_pkg

    consumer_path = Path(consumer_pkg.__file__).resolve().parent / "contract.yaml"
    consumer = dict(yaml.safe_load(consumer_path.read_text(encoding="utf-8")))

    produced = list(_contract().get("event_bus", {}).get("publish_topics", []))
    subscribed = list(consumer.get("event_bus", {}).get("subscribe_topics", []))

    assert COMPLETED_TOPIC in produced
    assert COMPLETED_TOPIC in subscribed


def test_the_wire_model_ignores_the_fields_the_state_carries_and_it_does_not() -> None:
    """The producer's extra fields are discarded, not rejected.

    The state carries governance provenance and the per-check records that the
    verdict table deliberately does not store. The wire model must ignore
    them; were it strict, every publish would dead-letter on a field the
    producer is right to send.
    """
    state = _run(VERIFIED_CHECKS)
    payload = _published_payload(state)

    assert "occ_governance_ref" in payload
    assert "checks" in payload

    event = ModelDodVerdictWire.model_validate(payload)

    assert not hasattr(event, "occ_governance_ref")
    assert not hasattr(event, "checks")


def test_a_correlation_id_crosses_as_a_uuid_rather_than_a_string() -> None:
    """The key half the table indexes on keeps its type across the topic."""
    state = _run(VERIFIED_CHECKS)
    payload = _published_payload(state)

    assert isinstance(payload["correlation_id"], str), (
        "json mode must render the identifier as a string on the wire"
    )

    event = ModelDodVerdictWire.model_validate(payload)

    assert isinstance(event.correlation_id, UUID)
    assert event.correlation_id == state.correlation_id
