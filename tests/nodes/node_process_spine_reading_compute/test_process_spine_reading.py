# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Piece 7's reading from the hourly drill and the replay (OMN-22943 AC1 and AC2).

AC1: the reading computes WORKING, NOT WORKING or UNKNOWN from the drill and the replay, with probed_at, published_at
and max_age. AC2: an injected acceptance of the drill's lane flips the reading to NOT WORKING within one probe.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from omnimarket.models.model_gate_store import (
    EnumGateStoreOperation,
    EnumGateStoreOutcome,
    ModelGateStoreRequest,
    ModelGateStoreResult,
)
from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_gate_store_effect.handlers.handler_gate_store_join_j6 import (
    HandlerGateStoreJoinJ6,
)
from omnimarket.nodes.node_process_spine_reading_compute.handlers import (
    HandlerProcessSpineReading,
)
from omnimarket.nodes.node_process_spine_reading_compute.models import (
    EnumDrillSend,
    ModelDrillObservation,
    ModelProcessSpineReading,
    ModelProcessSpineReadingRequest,
    ModelReplayObservation,
)
from omnimarket.nodes.node_stop_drill_effect.adapters import (
    AcceptingGateStore,
    build_test_instance,
)
from omnimarket.nodes.node_stop_drill_effect.handlers import HandlerStopDrill
from omnimarket.nodes.node_stop_drill_effect.models import (
    ModelStopDrillOverlay,
    ModelStopDrillRequest,
)
from tests.nodes.node_stop_drill_effect.support import (
    MAX_AGE,
    NOW,
    RecordingAppender,
    minutes_ago,
    observation,
    overlay,
)

pytestmark = pytest.mark.unit


def read(
    *,
    drill: ModelDrillObservation | None = None,
    replay: ModelReplayObservation | None = None,
    replay_required: bool = False,
) -> ModelProcessSpineReading:
    return HandlerProcessSpineReading().handle(
        ModelProcessSpineReadingRequest(
            drill=drill,
            replay=replay,
            replay_required=replay_required,
            now=NOW,
            max_age_seconds=MAX_AGE,
        )
    )


def test_reading_working_when_the_stop_commits_the_lane_is_refused_and_the_control_is_admitted() -> (
    None
):
    got = read(drill=observation())
    assert got.verdict is EnumReadingVerdict.WORKING
    assert got.reasons == ("ok",)
    assert got.probed_at == NOW
    assert got.published_at == NOW
    assert got.max_age_seconds == MAX_AGE
    assert got.drill_id == "drill-1"


def test_reading_unknown_when_the_drill_did_not_run() -> None:
    got = read(drill=None)
    assert got.verdict is EnumReadingVerdict.UNKNOWN
    assert got.reasons == ("drill-unread",)
    assert got.probed_at is None


@pytest.mark.parametrize("minutes", [121, 600])
def test_reading_not_working_when_the_drill_is_older_than_max_age(
    minutes: int,
) -> None:
    got = read(drill=observation(at=minutes_ago(minutes)))
    assert got.verdict is EnumReadingVerdict.NOT_WORKING
    assert got.reasons == ("stale-drill",)


def test_reading_working_at_exactly_max_age() -> None:
    assert (
        read(drill=observation(at=NOW - timedelta(seconds=MAX_AGE))).verdict
        is EnumReadingVerdict.WORKING
    )


def test_reading_not_working_when_the_drill_is_dated_after_the_read_time() -> None:
    got = read(drill=observation(at=NOW + timedelta(minutes=5)))
    assert got.verdict is EnumReadingVerdict.NOT_WORKING
    assert got.reasons == ("future-dated-drill",)


def test_reading_not_working_when_the_stop_did_not_commit() -> None:
    got = read(drill=observation(stop_committed=False))
    assert got.verdict is EnumReadingVerdict.NOT_WORKING
    assert got.reasons == ("stop-not-committed",)


def test_reading_not_working_when_the_instance_refuses_the_control_lane() -> None:
    got = read(drill=observation(control_send=EnumDrillSend.REFUSED))
    assert got.verdict is EnumReadingVerdict.NOT_WORKING
    assert got.reasons == ("control-lane-refused",)


@pytest.mark.parametrize(
    "errored",
    [
        observation(drill_send=EnumDrillSend.ERRORED),
        observation(control_send=EnumDrillSend.ERRORED),
    ],
)
def test_reading_unknown_when_a_send_errored(errored: ModelDrillObservation) -> None:
    got = read(drill=errored)
    assert got.verdict is EnumReadingVerdict.UNKNOWN
    assert got.reasons == ("drill-send-errored",)


def test_reading_unknown_when_the_required_replay_is_absent() -> None:
    got = read(drill=observation(), replay_required=True)
    assert got.verdict is EnumReadingVerdict.UNKNOWN
    assert got.reasons == ("replay-unread",)


def test_reading_ignores_an_absent_replay_when_it_is_not_required() -> None:
    assert (
        read(drill=observation(), replay_required=False).verdict
        is EnumReadingVerdict.WORKING
    )


def test_reading_not_working_on_a_replay_mismatch_and_working_on_a_clean_replay() -> (
    None
):
    mismatch = ModelReplayObservation(probed_at=NOW, dispatches=800, mismatches=2)
    clean = ModelReplayObservation(probed_at=NOW, dispatches=800, mismatches=0)
    bad = read(drill=observation(), replay=mismatch, replay_required=True)
    assert bad.verdict is EnumReadingVerdict.NOT_WORKING
    assert bad.reasons == ("replay-mismatch",)
    assert (
        read(drill=observation(), replay=clean, replay_required=True).verdict
        is EnumReadingVerdict.WORKING
    )


def test_reading_not_working_beats_unknown_when_both_apply() -> None:
    got = read(
        drill=observation(stop_committed=False, drill_send=EnumDrillSend.ERRORED)
    )
    assert got.verdict is EnumReadingVerdict.NOT_WORKING
    assert "stop-not-committed" in got.reasons


def test_fault_injection_an_accepted_drill_lane_reads_not_working() -> None:
    got = read(drill=observation(drill_send=EnumDrillSend.ADMITTED))
    assert got.verdict is EnumReadingVerdict.NOT_WORKING
    assert got.reasons == ("instance-accepted-drill-lane",)


def _drill(accepting_instance: bool) -> HandlerStopDrill:
    def factory(
        ov: ModelStopDrillOverlay, *, accepting: bool
    ) -> HandlerGateStoreJoinJ6:
        return build_test_instance(ov, accepting=accepting or accepting_instance)

    return HandlerStopDrill(
        overlay(),
        appender=RecordingAppender(),
        host="h053",
        instance_factory=factory,
        now=lambda: NOW,
    )


def test_fault_injection_flips_the_reading_within_one_probe() -> None:
    request = ModelStopDrillRequest(run_id="probe-1", requested_at=NOW, trigger="test")
    before = _drill(False).handle(request)
    after = _drill(True).handle(request)
    assert before.reading.verdict is EnumReadingVerdict.WORKING
    assert after.reading.verdict is EnumReadingVerdict.NOT_WORKING
    assert after.reading.reasons == ("instance-accepted-drill-lane",)
    assert after.verdict == "NOT_WORKING"


def test_fault_injection_the_accepting_store_admits_a_lane_above_a_stop() -> None:
    class Refusing:
        def transact(self, request: ModelGateStoreRequest) -> ModelGateStoreResult:
            raise AssertionError("the accepting store must not ask the inner store")

    got = AcceptingGateStore(Refusing()).transact(
        ModelGateStoreRequest(
            correlation_id="c",
            operation=EnumGateStoreOperation.RESERVE,
            piece="p",
            reservation_id="r",
        )
    )
    assert got.outcome is EnumGateStoreOutcome.OK
