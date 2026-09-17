# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lane liveness and drop detection over the cloud hook ledger (OMN-18609).

The fixtures below are not invented. Every timestamp comes from the live
2026-09-17 window read out of ``public.hook_events`` through the ``onex-dev``
projection pod: the relay's last event before the outage at
``15:20:54.543159Z``, its first event after at ``16:01:46.740871Z``, and the
crash of the orchestrator session at about ``15:25Z`` in between.

That window is the reason this node exists in the shape it does. During those
40 minutes 52 seconds every lane on the fleet was working normally and the
table was empty, so a naive last-event-age rule calls the entire fleet dropped.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from omnimarket.nodes.node_lane_liveness_compute.handlers.handler_lane_liveness import (
    HandlerLaneLiveness,
)
from omnimarket.nodes.node_lane_liveness_compute.models.model_lane_liveness import (
    EnumEvidenceBasis,
    EnumLaneVerdict,
    EnumRelayState,
    ModelLaneLivenessRequest,
    ModelLaneObservation,
)

pytestmark = pytest.mark.unit

# The measured relay outage, from the live table.
RELAY_LAST_BEFORE = datetime(2026, 9, 17, 15, 20, 54, 543159, tzinfo=UTC)
RELAY_FIRST_AFTER = datetime(2026, 9, 17, 16, 1, 46, 740871, tzinfo=UTC)
OUTAGE_START = datetime(2026, 9, 17, 15, 21, tzinfo=UTC)
OUTAGE_END = datetime(2026, 9, 17, 16, 1, tzinfo=UTC)


def _handle(request: ModelLaneLivenessRequest):
    return HandlerLaneLiveness().handle(request)


def _by_lane(report) -> dict[str, object]:
    return {v.lane: v for v in report.verdicts}


# ---------------------------------------------------------------------------
# AC6 -- the labelled fail-closed criterion
# ---------------------------------------------------------------------------


def test_no_lane_is_dropped_while_the_relay_is_silent() -> None:
    """The measured outage, replayed. Nothing may be called dropped here.

    Zero events of any kind exist between 15:21Z and 16:01Z. Every lane named
    below held a live CLAIM row with no TERMINAL row, and every one of them was
    working. This is the exact input a naive detector gets it wrong on.
    """
    lanes = (
        "release-trains-build-1440",
        "omninode-infra-main-advance-1415",
        "m4-stalled-carriers-triage-1415",
        "prod-keycloak-roster-reconcile-build-1505",
    )
    request = ModelLaneLivenessRequest(
        window_start=OUTAGE_START,
        window_end=OUTAGE_END,
        observations=tuple(
            ModelLaneObservation(
                lane=lane,
                claimed_at=datetime(2026, 9, 17, 14, 40, tzinfo=UTC),
                terminal_at=None,
                last_hook_event_at=None,
                hook_event_count=0,
            )
            for lane in lanes
        ),
        relay_last_event_at=None,
        relay_event_count=0,
        lane_attribution_available=False,
    )

    report = _handle(request)

    assert report.relay_state is EnumRelayState.SILENT
    assert report.dropped == ()
    assert all(
        v.verdict is EnumLaneVerdict.UNKNOWN_RELAY_SILENT for v in report.verdicts
    )
    assert report.counts()["dropped"] == 0
    assert report.counts()["unknown_relay_silent"] == len(lanes)


def test_a_naive_last_event_age_rule_would_have_dropped_the_whole_fleet() -> None:
    """The positive control for the test above.

    Without the relay guard the same input satisfies every condition for a
    drop: claim row, no terminal row, no events. This asserts the guard is what
    changes the answer, so the test above cannot pass vacuously.
    """
    observation = ModelLaneObservation(
        lane="release-trains-build-1440",
        claimed_at=datetime(2026, 9, 17, 14, 40, tzinfo=UTC),
        last_hook_event_at=None,
        hook_event_count=0,
    )
    naive_conditions_met = (
        observation.claimed_at is not None
        and observation.terminal_at is None
        and observation.hook_event_count == 0
    )
    assert naive_conditions_met

    guarded = _handle(
        ModelLaneLivenessRequest(
            window_start=OUTAGE_START,
            window_end=OUTAGE_END,
            observations=(observation,),
            relay_last_event_at=None,
            relay_event_count=0,
        )
    )
    assert guarded.verdicts[0].verdict is EnumLaneVerdict.UNKNOWN_RELAY_SILENT


def test_the_relay_threshold_trips_before_the_lane_threshold() -> None:
    """A guard that arrives after the verdict it guards is not a guard.

    With the relay stale by 400s, the relay threshold (300s) is breached while
    the lane threshold (900s) is not yet. The relay state must already be
    silent at that point.
    """
    report = _handle(
        ModelLaneLivenessRequest(
            window_start=OUTAGE_START,
            window_end=datetime(2026, 9, 17, 15, 30, tzinfo=UTC),
            observations=(
                ModelLaneObservation(
                    lane="some-lane-1440",
                    claimed_at=OUTAGE_START,
                    last_hook_event_at=datetime(2026, 9, 17, 15, 23, 20, tzinfo=UTC),
                    hook_event_count=5,
                ),
            ),
            relay_last_event_at=datetime(2026, 9, 17, 15, 23, 20, tzinfo=UTC),
            relay_event_count=5,
            lane_attribution_available=True,
            silence_threshold_seconds=900,
            relay_silence_threshold_seconds=300,
        )
    )

    assert report.relay_silent_seconds == 400
    assert report.relay_state is EnumRelayState.SILENT
    assert report.verdicts[0].verdict is EnumLaneVerdict.UNKNOWN_RELAY_SILENT


def test_a_terminal_row_survives_relay_silence() -> None:
    """Ledger evidence is not hook evidence and the relay cannot unmake it.

    Deliberate asymmetry: the relay guard exists because SILENCE stops being
    informative, and a terminal row is not silence. A lane that recorded that
    it finished did not drop, whatever the relay was doing.
    """
    report = _handle(
        ModelLaneLivenessRequest(
            window_start=OUTAGE_START,
            window_end=OUTAGE_END,
            observations=(
                ModelLaneObservation(
                    lane="finished-lane-1430",
                    claimed_at=datetime(2026, 9, 17, 14, 30, tzinfo=UTC),
                    terminal_at=datetime(2026, 9, 17, 15, 22, tzinfo=UTC),
                ),
            ),
            relay_last_event_at=None,
            relay_event_count=0,
        )
    )

    verdict = report.verdicts[0]
    assert verdict.verdict is EnumLaneVerdict.TERMINATED
    assert verdict.evidence_basis is EnumEvidenceBasis.LEDGER_ONLY


def test_an_empty_window_is_silent_regardless_of_threshold() -> None:
    """ "Never delivered" must not read as "delivered long ago".

    Otherwise the answer depends on how wide a window somebody chose.
    """
    report = _handle(
        ModelLaneLivenessRequest(
            window_start=OUTAGE_START,
            window_end=OUTAGE_END,
            relay_last_event_at=None,
            relay_event_count=0,
            relay_silence_threshold_seconds=100_000,
        )
    )
    assert report.relay_state is EnumRelayState.SILENT
    assert report.relay_silent_seconds is None


# ---------------------------------------------------------------------------
# AC5 -- alive and dropped, when the relay is demonstrably carrying
# ---------------------------------------------------------------------------


def test_a_lane_with_recent_events_is_alive_and_a_silent_one_is_dropped() -> None:
    now = datetime(2026, 9, 17, 17, 0, tzinfo=UTC)
    report = _handle(
        ModelLaneLivenessRequest(
            window_start=datetime(2026, 9, 17, 16, 0, tzinfo=UTC),
            window_end=now,
            observations=(
                ModelLaneObservation(
                    lane="alive-lane-1552",
                    claimed_at=datetime(2026, 9, 17, 16, 0, tzinfo=UTC),
                    last_hook_event_at=datetime(2026, 9, 17, 16, 59, tzinfo=UTC),
                    hook_event_count=120,
                ),
                ModelLaneObservation(
                    lane="silent-lane-1552",
                    claimed_at=datetime(2026, 9, 17, 16, 0, tzinfo=UTC),
                    last_hook_event_at=datetime(2026, 9, 17, 16, 10, tzinfo=UTC),
                    hook_event_count=8,
                ),
            ),
            relay_last_event_at=datetime(2026, 9, 17, 16, 59, 50, tzinfo=UTC),
            relay_event_count=400,
            lane_attribution_available=True,
        )
    )

    verdicts = _by_lane(report)
    assert report.relay_state is EnumRelayState.CARRYING
    assert verdicts["alive-lane-1552"].verdict is EnumLaneVerdict.ALIVE
    assert verdicts["alive-lane-1552"].evidence_basis is EnumEvidenceBasis.HOOK_EVENTS
    assert verdicts["silent-lane-1552"].verdict is EnumLaneVerdict.DROPPED
    assert verdicts["silent-lane-1552"].silent_seconds == 3000


def test_a_lane_with_a_claim_and_no_events_at_all_is_dropped() -> None:
    """The shape a lane that died at dispatch leaves behind."""
    report = _handle(
        ModelLaneLivenessRequest(
            window_start=datetime(2026, 9, 17, 16, 0, tzinfo=UTC),
            window_end=datetime(2026, 9, 17, 17, 0, tzinfo=UTC),
            observations=(
                ModelLaneObservation(
                    lane="died-at-dispatch-1600",
                    claimed_at=datetime(2026, 9, 17, 16, 1, tzinfo=UTC),
                ),
            ),
            relay_last_event_at=datetime(2026, 9, 17, 16, 59, 50, tzinfo=UTC),
            relay_event_count=400,
            lane_attribution_available=True,
        )
    )
    verdict = report.verdicts[0]
    assert verdict.verdict is EnumLaneVerdict.DROPPED
    assert verdict.evidence_basis is EnumEvidenceBasis.HOOK_EVENTS


def test_a_live_lane_with_no_claim_row_is_alive_not_unobservable() -> None:
    """The hook ledger is the primary record; a missing claim row is its gap.

    The standing ruling makes the event ledger the record of what happened. A
    lane visibly working with no CLAIM row is a hand-ledger omission, and
    reporting it as unobservable would let the weaker surface veto the stronger.
    """
    report = _handle(
        ModelLaneLivenessRequest(
            window_start=datetime(2026, 9, 17, 16, 0, tzinfo=UTC),
            window_end=datetime(2026, 9, 17, 17, 0, tzinfo=UTC),
            observations=(
                ModelLaneObservation(
                    lane="unclaimed-but-working-1600",
                    claimed_at=None,
                    last_hook_event_at=datetime(2026, 9, 17, 16, 59, tzinfo=UTC),
                    hook_event_count=50,
                ),
            ),
            relay_last_event_at=datetime(2026, 9, 17, 16, 59, 50, tzinfo=UTC),
            relay_event_count=400,
            lane_attribution_available=True,
        )
    )
    assert report.verdicts[0].verdict is EnumLaneVerdict.ALIVE


# ---------------------------------------------------------------------------
# AC4 -- a window with no lane attribution cannot be read by lane
# ---------------------------------------------------------------------------


def test_without_attribution_an_unterminated_claim_is_not_a_drop() -> None:
    """Pre-emitter rows carry no lane, so their silence proves nothing.

    A claim with no terminal row is the PRE-EXISTING un-terminated-claim
    signal. Calling it a drop is what produced the 3,144 `died_no_terminal` and
    969 unattributed verdicts the lane-registry reconcile is called noise for,
    and this reader exists to replace that signal rather than restate it. The
    information is preserved in the reason and the basis; the overclaim is not.
    """
    report = _handle(
        ModelLaneLivenessRequest(
            window_start=datetime(2026, 9, 17, 15, 12, tzinfo=UTC),
            window_end=datetime(2026, 9, 17, 15, 20, tzinfo=UTC),
            observations=(
                ModelLaneObservation(
                    lane="hook-cloud-relay-chain-build-1425",
                    claimed_at=datetime(2026, 9, 17, 14, 25, tzinfo=UTC),
                ),
            ),
            relay_last_event_at=datetime(2026, 9, 17, 15, 19, 50, tzinfo=UTC),
            relay_event_count=38,
            lane_attribution_available=False,
        )
    )

    verdict = report.verdicts[0]
    assert verdict.verdict is EnumLaneVerdict.UNOBSERVABLE
    assert verdict.evidence_basis is EnumEvidenceBasis.LEDGER_ONLY
    assert "not a drop" in verdict.reason
    assert "carries a lane attribution" in verdict.reason


def test_without_attribution_and_without_a_claim_row_nothing_is_concluded() -> None:
    report = _handle(
        ModelLaneLivenessRequest(
            window_start=datetime(2026, 9, 17, 15, 12, tzinfo=UTC),
            window_end=datetime(2026, 9, 17, 15, 20, tzinfo=UTC),
            observations=(ModelLaneObservation(lane="nobody-claimed-me"),),
            relay_last_event_at=datetime(2026, 9, 17, 15, 19, 50, tzinfo=UTC),
            relay_event_count=38,
            lane_attribution_available=False,
        )
    )
    verdict = report.verdicts[0]
    assert verdict.verdict is EnumLaneVerdict.UNOBSERVABLE
    assert verdict.evidence_basis is EnumEvidenceBasis.NONE


def test_counts_include_every_verdict_class_even_at_zero() -> None:
    """A class missing from a summary reads as not-applicable, not as none.

    That is how a zero-drop report and an unrun report come to look alike.
    """
    report = _handle(
        ModelLaneLivenessRequest(
            window_start=datetime(2026, 9, 17, 16, 0, tzinfo=UTC),
            window_end=datetime(2026, 9, 17, 17, 0, tzinfo=UTC),
            relay_last_event_at=datetime(2026, 9, 17, 16, 59, tzinfo=UTC),
            relay_event_count=10,
        )
    )
    assert set(report.counts()) == {m.value for m in EnumLaneVerdict}
    assert all(count == 0 for count in report.counts().values())


def test_a_clock_skewed_future_event_does_not_become_negative_silence() -> None:
    """A future-stamped event must not read as fresher than fresh."""
    report = _handle(
        ModelLaneLivenessRequest(
            window_start=datetime(2026, 9, 17, 16, 0, tzinfo=UTC),
            window_end=datetime(2026, 9, 17, 17, 0, tzinfo=UTC),
            observations=(
                ModelLaneObservation(
                    lane="skewed-1600",
                    claimed_at=datetime(2026, 9, 17, 16, 0, tzinfo=UTC),
                    last_hook_event_at=datetime(2026, 9, 17, 17, 5, tzinfo=UTC),
                    hook_event_count=3,
                ),
            ),
            relay_last_event_at=datetime(2026, 9, 17, 17, 5, tzinfo=UTC),
            relay_event_count=3,
            lane_attribution_available=True,
        )
    )
    assert report.verdicts[0].silent_seconds == 0
    assert report.verdicts[0].verdict is EnumLaneVerdict.ALIVE


# ---------------------------------------------------------------------------
# The gatherer's pure halves -- ledger parsing and request assembly
# ---------------------------------------------------------------------------


def _reader():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[3] / "scripts" / "lane_liveness_reader.py"
    spec = importlib.util.spec_from_file_location("lane_liveness_reader", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LEDGER = """\
2026-09-17T14:25:00Z | CLAIM | lane=hook-cloud-relay-chain-build-1425 | scope=x
2026-09-17T14:40:00Z | CLAIM | lane=release-trains-build-1440 | scope=y
2026-09-17T15:00:00Z | PROGRESS | lane=release-trains-build-1440 | note=z
2026-09-17T16:13:00Z | TERMINAL | lane=hook-cloud-relay-chain-build-1552 | done
2026-09-17T18:00:00Z | CLAIM | lane=a-lane-from-the-future-1800 | scope=later
not a ledger row at all
"""


def test_the_ledger_parser_reads_claims_and_terminals_and_ignores_the_rest() -> None:
    claims, terminals = _reader().parse_ledger(
        LEDGER, datetime(2026, 9, 17, 17, 0, tzinfo=UTC)
    )

    assert set(claims) == {
        "hook-cloud-relay-chain-build-1425",
        "release-trains-build-1440",
    }
    assert set(terminals) == {"hook-cloud-relay-chain-build-1552"}
    # A PROGRESS row is neither a claim nor a terminal.
    assert claims["release-trains-build-1440"] == datetime(
        2026, 9, 17, 14, 40, tzinfo=UTC
    )


def test_rows_after_the_window_end_cannot_contaminate_a_historical_window() -> None:
    """A reader that changes its mind about the past is not a record.

    Without this the same historical window yields different answers every time
    the ledger grows.
    """
    claims, _ = _reader().parse_ledger(LEDGER, datetime(2026, 9, 17, 17, 0, tzinfo=UTC))
    assert "a-lane-from-the-future-1800" not in claims


def test_the_observation_set_is_the_union_of_both_surfaces() -> None:
    """A lane that appears only in the ledger is what drop detection is FOR.

    Building the set from the wire alone makes the detector blind to its own
    subject: a lane that died produces no events, so it would never be asked
    about.
    """
    reader = _reader()
    request = reader.build_request(
        window_start=datetime(2026, 9, 17, 16, 0, tzinfo=UTC),
        window_end=datetime(2026, 9, 17, 17, 0, tzinfo=UTC),
        lane_rows=[
            {
                "lane": "on-the-wire-only-1600",
                "last_event_at": datetime(2026, 9, 17, 16, 59, tzinfo=UTC),
                "event_count": 9,
            }
        ],
        relay_last_event_at=datetime(2026, 9, 17, 16, 59, tzinfo=UTC),
        relay_event_count=9,
        attributed_event_count=9,
        claims={"in-the-ledger-only-1600": datetime(2026, 9, 17, 16, 1, tzinfo=UTC)},
        terminals={},
        silence_threshold_seconds=900,
        relay_silence_threshold_seconds=300,
    )

    lanes = {o.lane for o in request.observations}
    assert lanes == {"on-the-wire-only-1600", "in-the-ledger-only-1600"}
    assert request.lane_attribution_available is True


def test_zero_attributed_events_turns_attribution_off() -> None:
    """The flag is derived from the data, never asserted by the caller."""
    reader = _reader()
    request = reader.build_request(
        window_start=datetime(2026, 9, 17, 15, 12, tzinfo=UTC),
        window_end=datetime(2026, 9, 17, 15, 20, tzinfo=UTC),
        lane_rows=[],
        relay_last_event_at=datetime(2026, 9, 17, 15, 19, tzinfo=UTC),
        relay_event_count=38,
        attributed_event_count=0,
        claims={"pre-emitter-lane-1425": datetime(2026, 9, 17, 14, 25, tzinfo=UTC)},
        terminals={},
        silence_threshold_seconds=900,
        relay_silence_threshold_seconds=300,
    )
    assert request.lane_attribution_available is False


def test_the_reader_never_groups_or_filters_on_a_session_key() -> None:
    """AC4, mechanically.

    session_id is not a unit of work: one value covered 79,343 of 83,067 rows
    across nine days. correlation_id, run_id and entity_id carry that same
    value, so none of the four may appear as a key.
    """
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[3] / "scripts" / "lane_liveness_reader.py"
    ).read_text()
    statements = source[source.index("async def gather") :]
    for forbidden in ("session_id", "correlation_id", "run_id", "entity_id"):
        assert forbidden not in statements, f"{forbidden} is used as a query key"
    assert "payload->>'lane'" in statements


def test_dropped_is_only_ever_reachable_with_hook_evidence() -> None:
    """The invariant behind the correction above, stated once.

    DROPPED is the narrow, actionable conclusion. It must never rest on the
    hand ledger alone, or this reader becomes a second source of the signal it
    was built to replace.
    """
    windows = (
        # relay silent, claim, no terminal
        ModelLaneLivenessRequest(
            window_start=OUTAGE_START,
            window_end=OUTAGE_END,
            observations=(ModelLaneObservation(lane="a-1", claimed_at=OUTAGE_START),),
            relay_last_event_at=None,
            relay_event_count=0,
        ),
        # relay carrying, claim, no terminal, but no attribution on the wire
        ModelLaneLivenessRequest(
            window_start=OUTAGE_START,
            window_end=datetime(2026, 9, 17, 15, 25, tzinfo=UTC),
            observations=(ModelLaneObservation(lane="a-2", claimed_at=OUTAGE_START),),
            relay_last_event_at=datetime(2026, 9, 17, 15, 24, 50, tzinfo=UTC),
            relay_event_count=200,
            lane_attribution_available=False,
        ),
    )
    for request in windows:
        report = _handle(request)
        for verdict in report.verdicts:
            if verdict.verdict is EnumLaneVerdict.DROPPED:
                assert verdict.evidence_basis is EnumEvidenceBasis.HOOK_EVENTS
        assert report.dropped == ()


def test_the_contract_declares_its_terminal_event_and_activity_classes() -> None:
    """The node's declared surface, pinned.

    The terminal event is named literally here because a declared output state
    with no test naming it is how a contract and its tests drift apart: the
    state-coverage gate reads exactly this reference.
    """
    from pathlib import Path

    import yaml

    contract = yaml.safe_load(
        (
            Path(__file__).resolve().parents[3]
            / "src"
            / "omnimarket"
            / "nodes"
            / "node_lane_liveness_compute"
            / "contract.yaml"
        ).read_text(encoding="utf-8")
    )

    assert (
        contract["terminal_event"] == "onex.evt.omnimarket.lane-liveness-evaluated.v1"
    )
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.lane-liveness-evaluated.v1"
    ]
    assert contract["event_bus"]["subscribe_topics"] == [
        "onex.cmd.omnimarket.lane-liveness-requested.v1"
    ]
    # Session lifecycle classes are deliberately absent: they belong to the
    # harness, not to a lane, so counting them would keep a dead lane's session
    # looking busy.
    assert contract["lane_activity_event_types"] == [
        "onex.evt.omniclaude.tool-executed.v1",
        "onex.evt.omniclaude.prompt-submitted.v1",
    ]


def test_the_reader_resolves_activity_classes_from_the_contract() -> None:
    """Not from a second copy in Python, which is a second thing to forget."""
    assert _reader().activity_event_types() == (
        "onex.evt.omniclaude.tool-executed.v1",
        "onex.evt.omniclaude.prompt-submitted.v1",
    )


def test_an_empty_activity_class_list_is_refused_rather_than_queried(
    tmp_path,
) -> None:
    """A query with no event classes returns nothing, and nothing reads as calm."""
    from pathlib import Path

    import yaml

    src = (
        Path(__file__).resolve().parents[3]
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_lane_liveness_compute"
        / "contract.yaml"
    )
    contract = yaml.safe_load(src.read_text(encoding="utf-8"))
    contract["lane_activity_event_types"] = []
    emptied = tmp_path / "contract.yaml"
    emptied.write_text(yaml.safe_dump(contract), encoding="utf-8")

    with pytest.raises(ValueError, match="false zero"):
        _reader().activity_event_types(emptied)
