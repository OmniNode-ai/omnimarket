# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The host down rule table, one branch at a time (OMN-19998).

Every datetime is fixed at construction; the node owns no clock, so each branch
is reachable from the request alone. Every bound arrives in the request too, so
the numbers below are the tests' own, not the node's.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_host_down_compute.handlers.handler_host_down import (
    HandlerHostDown,
)
from omnimarket.nodes.node_host_down_compute.models.model_host_down import (
    EnumHostState,
    ModelHostDownReading,
    ModelHostDownRequest,
    ModelHostDownRules,
    ModelHostObservation,
)

pytestmark = [pytest.mark.unit]

EVALUATED_AT = datetime(2026, 11, 12, 12, 10, 0, tzinfo=UTC)
BOUND_S = 300
_FAILED_RUN_START = datetime(
    2026, 11, 12, 12, 0, 0, tzinfo=UTC
)  # 600 s before EVALUATED_AT


def _rules(
    unknown_when_no_host_answers: bool = False,
    down_bound_s: int = BOUND_S,
) -> ModelHostDownRules:
    return ModelHostDownRules(
        down_bound_s=down_bound_s,
        unknown_when_no_host_answers=unknown_when_no_host_answers,
    )


def _observation(
    host_name: str,
    probe_ok: bool | None,
    first_failed_at: datetime | None = None,
    advertised_at: datetime | None = None,
) -> ModelHostObservation:
    return ModelHostObservation(
        host_name=host_name,
        probe_ok=probe_ok,
        first_failed_at=first_failed_at,
        advertised_at=advertised_at,
    )


def _reading_for(
    observations: tuple[ModelHostObservation, ...],
    rules: ModelHostDownRules | None = None,
) -> tuple[ModelHostDownRequest, ModelHostDownReading]:
    request = ModelHostDownRequest(
        evaluated_at=EVALUATED_AT,
        rules=rules or _rules(),
        observations=observations,
    )
    return request, HandlerHostDown().handle(request)


def test_an_answered_probe_reads_up() -> None:
    _, reading = _reading_for(
        (_observation("h1", True, first_failed_at=_FAILED_RUN_START),),
    )
    (host,) = reading.readings
    assert host.host_name == "h1"
    assert host.state is EnumHostState.UP
    assert host.first_failed_at is None  # a answered probe clears the failed run
    assert host.down_since is None
    assert host.silent_seconds == 0.0
    assert host.reason == "probe answered"


def test_a_probe_that_could_not_run_reads_unknown() -> None:
    _, reading = _reading_for(
        (_observation("h1", None, first_failed_at=_FAILED_RUN_START),)
    )
    (host,) = reading.readings
    assert host.state is EnumHostState.UNKNOWN
    assert host.first_failed_at == _FAILED_RUN_START  # carried unchanged
    assert host.down_since is None
    assert host.reason == "probe could not run"


def test_a_probe_failing_under_the_bound_reads_suspect() -> None:
    # 12:08:30 is 90 s before the evaluation, under the 300 s bound
    failed_at = datetime(2026, 11, 12, 12, 8, 30, tzinfo=UTC)
    _, reading = _reading_for((_observation("h1", False, first_failed_at=failed_at),))
    (host,) = reading.readings
    assert host.state is EnumHostState.SUSPECT
    assert host.first_failed_at == failed_at
    assert host.down_since is None
    assert host.silent_seconds == 90.0
    assert host.reason == "probe failing for 90 s, under the bound 300 s"


def test_a_fresh_advertisement_keeps_a_failing_host_suspect() -> None:
    # probes failed 600 s ago (over the bound), but the host advertised 60 s ago
    _, reading = _reading_for(
        (
            _observation(
                "h1",
                False,
                first_failed_at=_FAILED_RUN_START,
                advertised_at=datetime(2026, 11, 12, 12, 9, 0, tzinfo=UTC),
            ),
        ),
    )
    (host,) = reading.readings
    assert host.state is EnumHostState.SUSPECT
    assert host.first_failed_at == _FAILED_RUN_START
    assert host.down_since is None
    assert host.silent_seconds == 60.0  # silence measured from the advertisement
    assert (
        host.reason
        == "probe fails but the host advertised capacity within the down bound"
    )


def test_an_advertisement_at_exactly_the_bound_is_not_fresh() -> None:
    # 300 s since the advertisement is not "within" the bound, so the failed
    # run's own silence decides: 600 s of it, over the bound, reads DOWN
    _, reading = _reading_for(
        (
            _observation(
                "h1",
                False,
                first_failed_at=_FAILED_RUN_START,
                advertised_at=datetime(2026, 11, 12, 12, 5, 0, tzinfo=UTC),
            ),
        ),
    )
    (host,) = reading.readings
    assert host.state is EnumHostState.DOWN


def test_silence_at_exactly_the_bound_reads_down() -> None:
    # 12:05:00 is exactly the 300 s bound before the evaluation
    failed_at = datetime(2026, 11, 12, 12, 5, 0, tzinfo=UTC)
    _, reading = _reading_for((_observation("h1", False, first_failed_at=failed_at),))
    (host,) = reading.readings
    assert host.state is EnumHostState.DOWN
    assert host.silent_seconds == 300.0
    assert host.reason == "no probe answer and no advertisement for 300 s (bound 300 s)"


def test_down_anchors_at_the_failed_run_start_not_the_evaluation() -> None:
    request, reading = _reading_for(
        (_observation("h1", False, first_failed_at=_FAILED_RUN_START),),
        rules=_rules(down_bound_s=60),
    )
    (host,) = reading.readings
    assert host.state is EnumHostState.DOWN
    assert host.down_since == _FAILED_RUN_START
    assert host.first_failed_at == _FAILED_RUN_START
    assert reading.evaluated_at == request.evaluated_at  # down_since is not this


def test_the_first_failed_tick_anchors_at_the_evaluation() -> None:
    # no prior reading to carry: the failed run starts now, so nothing is down yet
    _, reading = _reading_for((_observation("h1", False),))
    (host,) = reading.readings
    assert host.state is EnumHostState.SUSPECT
    assert host.first_failed_at == EVALUATED_AT
    assert host.down_since is None
    assert host.reason == "probe failing for 0 s, under the bound 300 s"


def test_fleet_wide_silence_blames_the_observer_and_not_the_hosts() -> None:
    observations = (
        _observation("h1", False, first_failed_at=_FAILED_RUN_START),  # alone: DOWN
        _observation("h2", False),  # alone: SUSPECT
    )
    _, reading = _reading_for(
        observations, rules=_rules(unknown_when_no_host_answers=True)
    )
    assert [host.state for host in reading.readings] == [
        EnumHostState.UNKNOWN,
        EnumHostState.UNKNOWN,
    ]
    assert all(
        host.reason == "observer sees no host: its own link is suspect"
        for host in reading.readings
    )
    # each host's failed run is carried unchanged, ready for the next tick
    assert reading.readings[0].first_failed_at == _FAILED_RUN_START
    assert reading.readings[1].first_failed_at is None

    # the same silence with the rule off judges each host on its own probe
    _, per_host = _reading_for(
        observations, rules=_rules(unknown_when_no_host_answers=False)
    )
    assert [host.state for host in per_host.readings] == [
        EnumHostState.DOWN,
        EnumHostState.SUSPECT,
    ]


def test_the_observer_rule_needs_at_least_two_hosts() -> None:
    # one silent host is one host's problem, not the observer's link
    _, reading = _reading_for(
        (_observation("h1", False, first_failed_at=_FAILED_RUN_START),),
        rules=_rules(unknown_when_no_host_answers=True),
    )
    (host,) = reading.readings
    assert host.state is EnumHostState.DOWN


def test_down_hosts_lists_the_down_names_sorted() -> None:
    _, reading = _reading_for(
        (
            _observation("host-b", False, first_failed_at=_FAILED_RUN_START),
            _observation("host-a", False, first_failed_at=_FAILED_RUN_START),
            _observation("host-c", True),
        ),
    )
    assert reading.down_hosts == ("host-a", "host-b")


def test_readings_are_sorted_by_host_name() -> None:
    _, reading = _reading_for(
        (
            _observation("zeta", True),
            _observation("alpha", True),
            _observation("mid", True),
        ),
    )
    assert [host.host_name for host in reading.readings] == ["alpha", "mid", "zeta"]


def test_duplicate_host_names_are_refused() -> None:
    with pytest.raises(ValidationError, match="host_name values must be unique"):
        ModelHostDownRequest(
            evaluated_at=EVALUATED_AT,
            rules=_rules(),
            observations=(
                _observation("h1", True),
                _observation("h1", False),
            ),
        )


def test_the_same_request_reads_the_same_twice() -> None:
    request = ModelHostDownRequest(
        evaluated_at=EVALUATED_AT,
        rules=_rules(unknown_when_no_host_answers=True),
        observations=(
            _observation("h1", False, first_failed_at=_FAILED_RUN_START),
            _observation("h2", None),
            _observation("h3", True),
        ),
    )
    handler = HandlerHostDown()
    assert handler.handle(request) == handler.handle(request)
