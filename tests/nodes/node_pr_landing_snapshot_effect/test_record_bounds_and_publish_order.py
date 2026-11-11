# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tick record is bounded under the broker limit and journaled only once the broker accepted it (OMN-22921).

On 2026-11-11 the cutover snapshot step's record carried the old controller's whole facts (about 5.7 MB
against the dev broker's limit of 1,048,588 bytes), every publish failed with EventPayloadTooLargeError, and
the step had already journaled each record, so its journal grew 5.7 MB a tick. Each test names the break
that turns it red.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import BaseModel

from omnimarket.models.landing_controller_carry import REASON_MAX_LENGTH
from omnimarket.models.landing_decision import ModelLandingFacts
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_state import (
    ModelLandingControllerState,
    ModelLandingPrRecord,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.handlers.handler_pr_landing_snapshot import (
    HandlerPrLandingSnapshot,
    carry_controller_state,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.handlers.snapshot_ports import (
    InMemoryLandingCarrySource,
    InMemoryLandingFactsSource,
    InMemoryLandingTickRecorder,
    InMemoryLandingWriteFactsSource,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.models.model_landing_snapshot import (
    EnumSnapshotFailureReason,
    EnumSnapshotStatus,
    ModelLandingSnapshotRequest,
)
from tests.nodes.node_pr_landing_snapshot_effect.controller_inputs import (
    NODE_TICK,
    REPO,
    carry,
    node_facts_bare,
)
from tests.nodes.node_pr_landing_snapshot_effect.snapshot_inputs import overlay

pytestmark = pytest.mark.unit

BROKER_LIMIT = 1_048_588  # the dev broker's message size limit, in bytes


class _Journal:
    """A publisher and recorder that log their calls in order, so publish-before-journal is a read."""

    def __init__(
        self, *, refuse_record: bool = False, refuse_failed: bool = False
    ) -> None:
        self.calls: list[str] = []
        self.published: list[tuple[str, BaseModel]] = []
        self._refuse_record = refuse_record
        self._refuse_failed = refuse_failed
        self.recorder = InMemoryLandingTickRecorder()

    def publish(self, topic: str, event: BaseModel) -> None:
        is_record = topic == overlay().record_topic
        self.calls.append("publish_record" if is_record else "publish_failed")
        if (is_record and self._refuse_record) or (
            not is_record and self._refuse_failed
        ):
            raise RuntimeError(
                "EventPayloadTooLargeError: payload over the broker limit"
            )
        self.published.append((topic, event))

    def record(self, record: BaseModel) -> None:
        self.calls.append("journal")
        self.recorder.record(record)  # type: ignore[arg-type]

    def recorded(self) -> tuple[BaseModel, ...]:
        return self.recorder.recorded()


def _handler(
    journal: _Journal,
    *,
    ov: object | None = None,
    facts: ModelLandingFacts | None = None,
    controller: object | None = None,
) -> HandlerPrLandingSnapshot:
    return HandlerPrLandingSnapshot(
        overlay=ov or overlay(),  # type: ignore[arg-type]
        facts_source=InMemoryLandingFactsSource(
            {NODE_TICK: facts or node_facts_bare()}
        ),
        write_source=InMemoryLandingWriteFactsSource(()),
        recorder=journal,  # type: ignore[arg-type]
        publisher=journal,  # type: ignore[arg-type]
        controller_source=controller,  # type: ignore[arg-type]
    )


def _request() -> ModelLandingSnapshotRequest:
    return ModelLandingSnapshotRequest(correlation_id=uuid4(), tick=NODE_TICK)


def test_publish_first_then_journal() -> None:
    journal = _Journal()
    result = _handler(journal).handle(_request())
    assert result.status is EnumSnapshotStatus.RECORDED
    assert journal.calls == ["publish_record", "journal"]
    assert len(journal.recorded()) == 1


def test_error_chain_a_refused_publish_is_a_typed_failure_and_journals_nothing() -> (
    None
):
    """Before: the record was journaled first and a failed publish still counted it recorded."""
    journal = _Journal(refuse_record=True, refuse_failed=True)
    handler = _handler(journal)
    result = handler.handle(_request())
    assert result.status is EnumSnapshotStatus.FAILED
    assert result.failure_reason is EnumSnapshotFailureReason.PUBLISH_FAILED
    assert "EventPayloadTooLargeError" in result.failure_detail
    assert journal.recorded() == ()
    assert "journal" not in journal.calls
    # the tick is not marked recorded: the next pass takes it again, it is not refused as already recorded
    again = handler.handle(_request())
    assert again.failure_reason is EnumSnapshotFailureReason.PUBLISH_FAILED


def test_error_chain_a_refused_publish_still_publishes_the_failed_event_when_the_bus_takes_it() -> (
    None
):
    journal = _Journal(refuse_record=True)
    result = _handler(journal).handle(_request())
    assert result.failure_reason is EnumSnapshotFailureReason.PUBLISH_FAILED
    ((topic, event),) = journal.published
    assert topic == overlay().failed_topic
    assert event.model_dump(mode="json")["reason"] == "publish_failed"


def test_a_record_over_the_overlays_limit_is_never_offered_to_the_broker() -> None:
    small = overlay().model_copy(update={"record_max_bytes": 100})
    journal = _Journal()
    result = _handler(journal, ov=small).handle(_request())
    assert result.failure_reason is EnumSnapshotFailureReason.RECORD_TOO_LARGE
    assert "record_max_bytes 100" in result.failure_detail
    assert journal.calls == ["publish_failed"]
    assert journal.recorded() == ()
    control = _handler(
        _Journal(), ov=overlay().model_copy(update={"record_max_bytes": 10_000_000})
    )
    assert control.handle(_request()).status is EnumSnapshotStatus.RECORDED


def _heavy_carry(records: int) -> object:
    """The controller's facts as the Mac measured them: state holding a record for every pull request it ever saw."""
    base = carry()
    state = ModelLandingControllerState(
        records=(
            *(
                ModelLandingPrRecord(pr=f"OmniNode-ai/other#{n}", ladder_index=0)
                for n in range(1, records + 1)
            ),
            ModelLandingPrRecord(pr=f"{REPO}#713"),
        )
    )
    return base.model_copy(
        update={"facts": base.facts.model_copy(update={"state": state})}
    )


def test_the_record_with_the_controllers_whole_state_is_bounded_under_the_broker_limit() -> (
    None
):
    heavy = _heavy_carry(6000)
    # failing control: the controller's facts as they stood are over the broker limit on their own
    assert len(heavy.facts.model_dump_json().encode()) > BROKER_LIMIT  # type: ignore[attr-defined]
    unbounded = carry_controller_state(node_facts_bare(), heavy)  # type: ignore[arg-type]
    assert len(unbounded.state.records) == 1
    journal = _Journal()
    bounded_overlay = overlay().model_copy(update={"record_max_bytes": 1_000_000})
    result = _handler(
        journal,
        ov=bounded_overlay,
        controller=InMemoryLandingCarrySource(heavy),  # type: ignore[arg-type]
    ).handle(_request())
    assert result.status is EnumSnapshotStatus.RECORDED
    ((_, event),) = journal.published
    assert len(event.model_dump_json().encode()) < BROKER_LIMIT
    assert result.record is not None
    assert "facts" not in type(result.record.controller).model_fields  # type: ignore[arg-type]
    assert {r.pr for r in result.record.facts.state.records} <= {
        p.pr for p in result.record.facts.prs
    }


def test_decision_rows_carry_kind_subject_outcome_and_a_bounded_reason() -> None:
    from omnimarket.models.landing_controller_carry import ModelLandingControllerLogged
    from omnimarket.models.landing_decision import EnumLandingActionKind

    long_reason = "x" * (REASON_MAX_LENGTH + 50)
    with pytest.raises(ValueError, match="at most 200"):
        ModelLandingControllerLogged(
            kind=EnumLandingActionKind.DISPATCH_WORKER,
            subject=f"{REPO}#714",
            outcome="spawned",
            reason=long_reason,
        )
    row = ModelLandingControllerLogged(
        kind=EnumLandingActionKind.DISPATCH_WORKER,
        subject=f"{REPO}#714",
        outcome="spawned",
        reason="real_red",
    )
    base = carry()
    journal = _Journal()
    result = _handler(
        journal,
        controller=InMemoryLandingCarrySource(
            base.model_copy(update={"logged": (row,)})
        ),
    ).handle(_request())
    assert result.record is not None
    assert result.record.controller is not None
    assert [
        (d.kind, d.subject, d.outcome, d.reason)
        for d in result.record.controller.decisions
    ] == [(EnumLandingActionKind.DISPATCH_WORKER, f"{REPO}#714", "spawned", "real_red")]


@pytest.mark.kafka
@pytest.mark.integration
def test_join_redpanda_the_bounded_record_is_accepted_by_the_real_broker_and_the_whole_one_is_refused() -> (
    None
):
    """The record carrying the controller's whole facts is refused by the real broker; the bounded one is accepted."""
    import os
    from pathlib import Path

    from omnimarket.models.landing_tick_record import ModelLandingTickRecord
    from omnimarket.nodes.node_pr_landing_snapshot_effect.handlers.adapter_publisher import (
        LaneBusSnapshotPublisher,
    )

    lane, root = (
        os.environ.get("LANDING_SNAPSHOT_JOIN_LANE"),
        os.environ.get("OMNIBASE_PATH"),
    )
    if not (lane and root):
        pytest.skip(
            "Kafka not reachable: LANDING_SNAPSHOT_JOIN_LANE and OMNIBASE_PATH name no declared bus lane"
        )
    publisher = LaneBusSnapshotPublisher(lane, Path(root))
    heavy = _heavy_carry(6000)
    topic = overlay().record_topic
    # control: the record as it was before (the controller's whole facts on it) is over the limit and the broker refuses it
    whole = ModelLandingTickRecord.model_validate(
        {
            "correlation_id": uuid4(),
            "tick": NODE_TICK,
            "facts": carry_controller_state(node_facts_bare(), heavy).model_dump(
                mode="json"
            ),  # type: ignore[arg-type]
            "provenance": "p9-observe2-bounds-proof",
            "controller": {
                "controller_tick": 5,
                "observed_at": heavy.observed_at,  # type: ignore[attr-defined]
                "decisions": [],
                "source": "p9-observe2-bounds-proof",
            },
        }
    )
    oversized = whole.model_copy(update={"provenance": "x" * (BROKER_LIMIT + 1)})
    with pytest.raises(Exception, match=r"(?i)too.?large|size|payload"):
        publisher.publish(topic, oversized)
    # the step's own record, bounded, is accepted
    journal = _Journal()
    result = HandlerPrLandingSnapshot(
        overlay=overlay().model_copy(update={"record_max_bytes": 1_000_000}),
        facts_source=InMemoryLandingFactsSource({NODE_TICK: node_facts_bare()}),
        write_source=InMemoryLandingWriteFactsSource(()),
        recorder=journal.recorder,
        publisher=publisher,
        controller_source=InMemoryLandingCarrySource(heavy),  # type: ignore[arg-type]
    ).handle(_request())
    assert result.status is EnumSnapshotStatus.RECORDED, result.failure_detail
    assert journal.recorder.recorded() != ()
