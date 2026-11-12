# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The host down reading: when a lab host stops taking work (OMN-19998).

One ssh probe that fails is noise; a host that stops answering for a sustained
window is out of the fleet until it comes back. So a host is read

- DOWN once its probes have failed for at least ``down_bound_s`` seconds with
  no capacity advertisement since — ``down_since`` anchors the outage at the
  start of the failed run, so the reading survives the tick that confirms it;
- SUSPECT before the bound is reached, or when the host advertised capacity
  within the bound — an advertisement says the host is alive even while its
  probe fails;
- UP when the probe answers;
- UNKNOWN when the probe could not run, or — when
  ``unknown_when_no_host_answers`` is set and at least two hosts are read and
  not one probe answered — for every host: a fleet-wide silence is far likelier
  the observer's own link than every host failing at once.

Every bound arrives in the request. No clock, no I/O, no literal bound.
"""

from datetime import datetime, timedelta

from ..models.model_host_down import (
    EnumHostState,
    ModelHostDownReading,
    ModelHostDownRequest,
    ModelHostDownRules,
    ModelHostObservation,
    ModelHostReading,
)


def _seconds_text(value: float) -> str:
    """Seconds as a plain number: ``300.0`` prints ``300``, ``90.5`` stays ``90.5``."""
    return f"{value:g}"


class HandlerHostDown:
    def handle(self, request: ModelHostDownRequest) -> ModelHostDownReading:
        observations = request.observations
        if _observer_sees_no_host(request.rules, observations):
            readings = tuple(
                ModelHostReading(
                    host_name=observation.host_name,
                    state=EnumHostState.UNKNOWN,
                    first_failed_at=observation.first_failed_at,
                    down_since=None,
                    silent_seconds=None,
                    reason="observer sees no host: its own link is suspect",
                )
                for observation in observations
            )
            return ModelHostDownReading(
                evaluated_at=request.evaluated_at,
                readings=_sorted_by_host(readings),
            )
        readings = tuple(
            _read_one(request.evaluated_at, request.rules, observation)
            for observation in observations
        )
        return ModelHostDownReading(
            evaluated_at=request.evaluated_at,
            readings=_sorted_by_host(readings),
        )


def _observer_sees_no_host(
    rules: ModelHostDownRules,
    observations: tuple[ModelHostObservation, ...],
) -> bool:
    """Rule 1: the rule is on, at least two hosts are read, and no probe answered."""
    return (
        rules.unknown_when_no_host_answers
        and len(observations) >= 2
        and all(observation.probe_ok is not True for observation in observations)
    )


def _read_one(
    evaluated_at: datetime,
    rules: ModelHostDownRules,
    observation: ModelHostObservation,
) -> ModelHostReading:
    """Rules 2-4, evaluated for one observation in order."""
    if observation.probe_ok is None:
        return ModelHostReading(
            host_name=observation.host_name,
            state=EnumHostState.UNKNOWN,
            first_failed_at=observation.first_failed_at,
            down_since=None,
            silent_seconds=None,
            reason="probe could not run",
        )
    if observation.probe_ok is True:
        return ModelHostReading(
            host_name=observation.host_name,
            state=EnumHostState.UP,
            first_failed_at=None,
            down_since=None,
            silent_seconds=0.0,
            reason="probe answered",
        )
    bound = timedelta(seconds=rules.down_bound_s)
    failed_since = observation.first_failed_at or evaluated_at
    if (
        observation.advertised_at is not None
        and (evaluated_at - observation.advertised_at) < bound
    ):
        return ModelHostReading(
            host_name=observation.host_name,
            state=EnumHostState.SUSPECT,
            first_failed_at=failed_since,
            down_since=None,
            silent_seconds=(evaluated_at - observation.advertised_at).total_seconds(),
            reason="probe fails but the host advertised capacity within the down bound",
        )
    silent_seconds = (evaluated_at - failed_since).total_seconds()
    if silent_seconds >= rules.down_bound_s:
        return ModelHostReading(
            host_name=observation.host_name,
            state=EnumHostState.DOWN,
            first_failed_at=failed_since,
            down_since=failed_since,
            silent_seconds=silent_seconds,
            reason=(
                f"no probe answer and no advertisement for {_seconds_text(silent_seconds)} s "
                f"(bound {rules.down_bound_s} s)"
            ),
        )
    return ModelHostReading(
        host_name=observation.host_name,
        state=EnumHostState.SUSPECT,
        first_failed_at=failed_since,
        down_since=None,
        silent_seconds=silent_seconds,
        reason=(
            f"probe failing for {_seconds_text(silent_seconds)} s, "
            f"under the bound {rules.down_bound_s} s"
        ),
    )


def _sorted_by_host(
    readings: tuple[ModelHostReading, ...],
) -> tuple[ModelHostReading, ...]:
    return tuple(sorted(readings, key=lambda reading: reading.host_name))
