# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The controller carry: the snapshot step carries the old controller's state onto the node's facts (OMN-23665).

The measured case: omnimemory 713 is held behind omnimemory 725 by the old controller; the PR watcher's
state carries neither the hold nor the blocker, so facts without the controller's state dispatch a worker
for it. Each test names the break that turns it red.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from omnimarket.models.landing_controller_carry import (
    ModelLandingControllerCarry,
    decisions_of,
)
from omnimarket.models.landing_decision import (
    EnumLandingActionKind,
    ModelLandingFacts,
    ModelLandingPrFacts,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.handlers.handler_pr_landing_snapshot import (
    CARRIED_FACT_FIELDS,
    CARRIED_PR_FIELDS,
    HandlerPrLandingSnapshot,
    carry_controller_state,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.handlers.snapshot_errors import (
    ControllerStateStaleError,
    ControllerTickOpenError,
    SnapshotSourceError,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.handlers.snapshot_ports import (
    InMemoryLandingCarrySource,
    InMemoryLandingFactsSource,
    InMemoryLandingSnapshotPublisher,
    InMemoryLandingTickRecorder,
    InMemoryLandingWriteFactsSource,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.handlers.snapshot_sources import (
    LandingCarryStateDirSource,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.models.model_landing_snapshot import (
    EnumSnapshotFailureReason,
    EnumSnapshotStatus,
    ModelLandingSnapshotFailedEvent,
    ModelLandingSnapshotRequest,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.models.model_snapshot_overlay import (
    ModelSnapshotOverlay,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.protocols.protocol_landing_snapshot import (
    ProtocolLandingControllerSource,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.tick_ports import (
    NodePathDecider,
)
from tests.nodes.node_pr_landing_snapshot_effect.controller_inputs import (
    CONTROLLER_TICK,
    NODE_TICK,
    REPO,
    T,
    carry,
    node_facts_bare,
    pr,
    write_controller_state,
)
from tests.nodes.node_pr_landing_snapshot_effect.snapshot_inputs import overlay

pytestmark = pytest.mark.unit

NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_pr_landing_snapshot_effect"
)

HELD_PR = f"{REPO}#713"
NEW_RED_PR = f"{REPO}#714"


def _dispatched(facts: ModelLandingFacts) -> set[str]:
    decision = NodePathDecider().decide(facts, True)
    return {
        a.subject
        for a in decision.actions
        if a.kind is EnumLandingActionKind.DISPATCH_WORKER
    }


def test_held_pr_dispatches_without_the_carried_state_and_holds_with_it() -> None:
    # failing control: the facts as the PR watcher's state gives them dispatch for the held PR
    assert HELD_PR in _dispatched(node_facts_bare())
    carried = carry_controller_state(node_facts_bare(), carry())
    assert HELD_PR not in _dispatched(carried)
    # and the node now decides what the controller decided from its own facts
    assert _dispatched(carried) == _dispatched(carry().facts)


def test_new_red_still_dispatches_with_the_carried_state() -> None:
    assert NEW_RED_PR in _dispatched(node_facts_bare())
    assert NEW_RED_PR in _dispatched(carry_controller_state(node_facts_bare(), carry()))


def test_the_carried_state_lands_on_the_facts_and_the_node_keeps_its_own_tick() -> None:
    carried = carry_controller_state(node_facts_bare(), carry())
    assert carried.tick == NODE_TICK
    assert carried.state == carry().facts.state
    held = {p.pr: p for p in carried.prs}[HELD_PR]
    controller_held = {p.pr: p for p in carry().facts.prs}[HELD_PR]
    for name in CARRIED_PR_FIELDS:
        assert getattr(held, name) == getattr(controller_held, name), name


def test_the_carried_names_are_fields_of_the_shared_facts_models() -> None:
    """A seam test: renaming a facts field the carry copies turns this red."""
    assert set(CARRIED_FACT_FIELDS) <= set(ModelLandingFacts.model_fields)
    assert set(CARRIED_PR_FIELDS) <= set(ModelLandingPrFacts.model_fields)


def test_a_pr_at_another_head_or_unknown_to_the_controller_keeps_its_own_values() -> (
    None
):
    node = ModelLandingFacts(
        tick=NODE_TICK,
        observed_at=T,
        prs=(
            pr(713, "c"),
            pr(715, "d"),
        ),  # 713 moved head; 715 the controller never saw
    )
    carried = carry_controller_state(node, carry())
    assert carried.prs == node.prs


def test_the_controller_state_dir_source_reads_the_finished_tick(
    tmp_path: Path,
) -> None:
    facts, ticks, decisions = write_controller_state(tmp_path)
    source = LandingCarryStateDirSource(
        facts,
        ticks,
        decisions,
        sidecar_path=facts.parent / "sidecar.json",
        max_age_seconds=900,
        now=lambda: T + timedelta(seconds=60),
    )
    got = source.read_carry()
    assert got.controller_tick == CONTROLLER_TICK
    assert got.facts == carry().facts
    # the parent_msg row (not an action) and the earlier tick's row are dropped
    assert [(r.kind, r.subject, r.outcome) for r in got.logged] == [
        (EnumLandingActionKind.DISPATCH_WORKER, NEW_RED_PR, "spawned")
    ]


def test_error_chain_a_controller_tick_still_open_is_refused(tmp_path: Path) -> None:
    facts, ticks, decisions = write_controller_state(tmp_path, last_finished_tick=4)
    source = LandingCarryStateDirSource(
        facts,
        ticks,
        decisions,
        sidecar_path=facts.parent / "sidecar.json",
        max_age_seconds=900,
        now=lambda: T,
    )
    with pytest.raises(ControllerTickOpenError, match=r"tick 5.*finished tick is 4"):
        source.read_carry()


def test_error_chain_a_stale_controller_tick_is_refused(tmp_path: Path) -> None:
    facts, ticks, decisions = write_controller_state(tmp_path)
    source = LandingCarryStateDirSource(
        facts,
        ticks,
        decisions,
        sidecar_path=facts.parent / "sidecar.json",
        max_age_seconds=900,
        now=lambda: T + timedelta(seconds=901),
    )
    with pytest.raises(ControllerStateStaleError, match="past its bound of 900s"):
        source.read_carry()


def test_error_chain_a_missing_facts_file_is_unread(tmp_path: Path) -> None:
    _, ticks, decisions = write_controller_state(tmp_path)
    source = LandingCarryStateDirSource(
        tmp_path / "absent.json",
        ticks,
        decisions,
        sidecar_path=tmp_path / "sidecar.json",
        max_age_seconds=900,
        now=lambda: T,
    )
    with pytest.raises(SnapshotSourceError, match="unreadable"):
        source.read_carry()


class _Raising:
    name = "raising-controller"

    def __init__(self, exc: SnapshotSourceError) -> None:
        self._exc = exc

    def read_carry(self) -> ModelLandingControllerCarry:
        raise self._exc


def _handler(
    controller_source: ProtocolLandingControllerSource | None,
    publisher: InMemoryLandingSnapshotPublisher | None = None,
    recorder: InMemoryLandingTickRecorder | None = None,
) -> HandlerPrLandingSnapshot:
    return HandlerPrLandingSnapshot(
        overlay=overlay(),
        facts_source=InMemoryLandingFactsSource({NODE_TICK: node_facts_bare()}),
        write_source=InMemoryLandingWriteFactsSource(()),
        recorder=recorder or InMemoryLandingTickRecorder(),
        publisher=publisher or InMemoryLandingSnapshotPublisher(),
        controller_source=controller_source,
    )


def test_golden_chain_the_record_carries_the_controller_tick_and_the_gate() -> None:
    recorder = InMemoryLandingTickRecorder()
    result = _handler(InMemoryLandingCarrySource(carry()), recorder=recorder).handle(
        ModelLandingSnapshotRequest(correlation_id=uuid4(), tick=NODE_TICK)
    )
    assert result.status is EnumSnapshotStatus.RECORDED
    assert result.record is not None
    assert result.record.controller == decisions_of(carry())
    assert result.record.provenance.endswith("+inmemory-controller-state")
    held = {p.pr: p for p in result.record.facts.prs}[HELD_PR]
    assert held.gate_reasons
    assert recorder.recorded() == (result.record,)


def test_without_a_controller_source_the_record_is_as_before() -> None:
    result = _handler(None).handle(
        ModelLandingSnapshotRequest(correlation_id=uuid4(), tick=NODE_TICK)
    )
    assert result.record is not None
    assert result.record.controller is None
    assert result.record.facts == node_facts_bare()


@pytest.mark.parametrize(
    ("exc", "reason"),
    [
        (
            ControllerTickOpenError("open"),
            EnumSnapshotFailureReason.CONTROLLER_TICK_OPEN,
        ),
        (
            ControllerStateStaleError("old"),
            EnumSnapshotFailureReason.CONTROLLER_STATE_STALE,
        ),
        (
            SnapshotSourceError("gone"),
            EnumSnapshotFailureReason.CONTROLLER_STATE_UNREAD,
        ),
    ],
)
def test_error_chain_a_controller_failure_records_nothing_and_publishes_the_failed_event(
    exc: SnapshotSourceError, reason: EnumSnapshotFailureReason
) -> None:
    recorder, publisher = (
        InMemoryLandingTickRecorder(),
        InMemoryLandingSnapshotPublisher(),
    )
    result = _handler(_Raising(exc), publisher, recorder).handle(
        ModelLandingSnapshotRequest(correlation_id=uuid4(), tick=NODE_TICK)
    )
    assert result.status is EnumSnapshotStatus.FAILED
    assert result.failure_reason is reason
    assert recorder.recorded() == ()
    ((topic, event),) = publisher.published
    assert topic == overlay().failed_topic
    assert isinstance(event, ModelLandingSnapshotFailedEvent)
    assert event.reason is reason


def test_the_controller_overlay_fields_come_as_a_set() -> None:
    base = overlay().model_dump()
    fields = {
        "controller_facts_path": "$HOME/f.json",
        "controller_ticks_path": "$HOME/t.jsonl",
        "controller_decisions_path": "$HOME/d.jsonl",
        "controller_max_age_seconds": 900,
        "controller_sidecar_path": "$HOME/s.json",
        "external_blocker_kinds": ("upstream_open",),
        "external_blocker_unbounded_kinds": ("upstream_open",),
        "external_gate_hours": 1.0,
    }
    ModelSnapshotOverlay.model_validate({**base, **fields})
    for name in fields:
        partial = {k: v for k, v in fields.items() if k != name}
        with pytest.raises(ValueError, match="come as a set"):
            ModelSnapshotOverlay.model_validate({**base, **partial})


def test_the_shipped_cutover_example_overlay_validates_and_names_the_controller_state() -> (
    None
):
    path = NODE_DIR / "snapshot.overlay.cutover.example.yaml"
    loaded = ModelSnapshotOverlay.model_validate(
        yaml.safe_load(path.read_text(encoding="utf-8"))
    )
    assert loaded.controller_facts_path == loaded.facts_path
    assert loaded.controller_ticks_path
    assert loaded.controller_decisions_path
    assert loaded.controller_max_age_seconds == loaded.watcher_max_age_seconds


def test_the_sidecars_judged_external_blockers_are_read_and_filled_onto_the_matching_head(
    tmp_path: Path,
) -> None:
    """OMN-22921: the controller's worker gate external:<ref> comes from its sidecar; the carry brings it as a fact."""
    facts, ticks, decisions = write_controller_state(tmp_path)
    source = LandingCarryStateDirSource(
        facts,
        ticks,
        decisions,
        sidecar_path=tmp_path / "sidecar.json",
        max_age_seconds=900,
        now=lambda: T + timedelta(seconds=60),
    )
    got = source.read_carry()
    ((row,),) = [(r,) for r in got.judged]
    assert (row.pr, row.judged.ref, row.judged.kind) == (
        "omnimemory#713",
        "omnimemory#725",
        "upstream_open",
    )
    cutover = overlay().model_copy(
        update={
            "external_blocker_kinds": ("upstream_open", "operator_decision"),
            "external_blocker_unbounded_kinds": ("upstream_open",),
            "external_gate_hours": 2.0,
        }
    )
    carried = carry_controller_state(node_facts_bare(), got, cutover)
    by_pr = {p.pr: p for p in carried.prs}
    assert [j.ref for j in by_pr[HELD_PR].external_judged] == ["omnimemory#725"]
    assert by_pr[NEW_RED_PR].external_judged == ()
    assert carried.policy.external_gate_hours == 2.0
    assert carried.policy.external_blocker_kinds == (
        "upstream_open",
        "operator_decision",
    )
    # failing control: a pull request now at another head takes no judgement made at the old one
    moved = node_facts_bare().model_copy(update={"prs": (pr(713, "c"), pr(714, "b"))})
    assert all(
        not p.external_judged for p in carry_controller_state(moved, got, cutover).prs
    )


def test_error_chain_an_unreadable_sidecar_is_an_unread_controller_state_never_an_empty_memory(
    tmp_path: Path,
) -> None:
    facts, ticks, decisions = write_controller_state(tmp_path)
    for text in (None, "not json", "[1]"):
        sidecar = tmp_path / "sidecar.json"
        sidecar.unlink(missing_ok=True)
        if text is not None:
            sidecar.write_text(text, encoding="utf-8")
        source = LandingCarryStateDirSource(
            facts,
            ticks,
            decisions,
            sidecar_path=sidecar,
            max_age_seconds=900,
            now=lambda: T + timedelta(seconds=60),
        )
        with pytest.raises(SnapshotSourceError, match=r"sidecar\.json"):
            source.read_carry()
