# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure per-host transitions, including failed checks and missed runs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omnimarket.nodes.node_launchd_drift_effect.handlers.handler_host_drift_fold import (
    HandlerHostDriftFold,
)
from omnimarket.nodes.node_launchd_drift_effect.models.enum_host_drift_resolution import (
    EnumHostDriftResolution,
)
from omnimarket.nodes.node_launchd_drift_effect.models.enum_host_drift_verdict import (
    EnumHostDriftVerdict as Verdict,
)
from omnimarket.nodes.node_launchd_drift_effect.models.model_host_drift_fold_request import (
    ModelHostDriftFoldRequest,
)
from omnimarket.nodes.node_launchd_drift_effect.models.model_host_drift_fold_result import (
    ModelHostDriftFoldResult,
)
from omnimarket.nodes.node_launchd_drift_effect.models.model_host_drift_item import (
    ModelHostDriftItem,
)
from omnimarket.nodes.node_launchd_drift_effect.models.model_host_drift_observation import (
    ModelHostDriftObservation,
)
from omnimarket.nodes.node_launchd_drift_effect.models.model_host_drift_state import (
    ModelHostDriftState,
)

pytestmark = pytest.mark.unit
NOW = datetime(2026, 10, 7, tzinfo=UTC)


def fold(
    previous: ModelHostDriftState, verdict: Verdict | None, now: datetime = NOW
) -> ModelHostDriftFoldResult:
    items = (
        ()
        if verdict is None
        else (ModelHostDriftItem("clone:x", "clone", verdict, "dev", "behind", 7200),)
    )
    return HandlerHostDriftFold().handle(
        ModelHostDriftFoldRequest(
            previous,
            ModelHostDriftObservation("h201", "linux-lab", now, items),
            "check",
            900,
        )
    )


def test_detected_then_resolved() -> None:
    first = fold(ModelHostDriftState("h201", None, ()), Verdict.DRIFT)
    assert len(first.detected) == 1
    assert first.heartbeat.items_drift == 1
    second = fold(first.state, Verdict.OK, NOW + timedelta(seconds=900))
    assert not second.state.open
    assert second.resolved[0].duration_seconds == 900
    assert second.resolved[0].resolution == EnumHostDriftResolution.OBSERVED_CONVERGED


def test_first_seen_kept_and_failed_check_stays_open() -> None:
    first = fold(ModelHostDriftState("h201", None, ()), Verdict.CHECK_FAILED)
    second = fold(first.state, Verdict.CHECK_FAILED, NOW + timedelta(seconds=900))
    assert not second.detected
    assert second.state.open[0].first_seen_at == NOW
    assert second.heartbeat.items_failed == 1
    assert second.heartbeat.open == second.state.open


def test_disappearing_key_resolves() -> None:
    first = fold(ModelHostDriftState("h201", None, ()), Verdict.DRIFT)
    assert len(fold(first.state, None, NOW + timedelta(seconds=900)).resolved) == 1


def test_heartbeat_gap_detected_once_then_resolves() -> None:
    first = fold(
        ModelHostDriftState("h201", NOW - timedelta(seconds=2700), ()), Verdict.OK
    )
    assert first.detected[0].key == "heartbeat:gap"
    assert first.heartbeat.gap_seconds == 2700
    assert first.state.open[0].observed == "no check for 45m"
    second = fold(first.state, Verdict.OK, NOW + timedelta(seconds=900))
    assert second.resolved[0].key == "heartbeat:gap"
    assert not second.state.open


def test_state_and_events_round_trip() -> None:
    result = fold(ModelHostDriftState("h201", None, ()), Verdict.DRIFT)
    assert ModelHostDriftState.from_payload(result.state.to_payload()) == result.state
    assert (
        type(result.heartbeat).from_payload(result.heartbeat.to_payload())
        == result.heartbeat
    )


def test_exact_two_intervals_has_no_gap_and_observed_is_updated() -> None:
    previous = ModelHostDriftState("h201", NOW - timedelta(seconds=1800), ())
    assert not fold(previous, Verdict.OK).state.open
    first = fold(ModelHostDriftState("h201", None, ()), Verdict.DRIFT)
    item = ModelHostDriftItem(
        "clone:x", "clone", Verdict.CHECK_FAILED, "dev", "timeout", 7200
    )
    result = HandlerHostDriftFold().handle(
        ModelHostDriftFoldRequest(
            first.state,
            ModelHostDriftObservation(
                "h201", "linux-lab", NOW + timedelta(seconds=900), (item,)
            ),
            "next",
            900,
        )
    )
    assert result.state.open[0].observed == "timeout"
    assert result.state.open[0].first_seen_at == NOW
    assert not result.detected
    assert not result.resolved


def test_unchecked_keys_stay_open() -> None:
    first = fold(ModelHostDriftState("h201", None, ()), Verdict.DRIFT)
    ignored = ModelHostDriftItem(
        "clone:y", "clone", Verdict.DRIFT, "dev", "behind", 7200
    )
    result = HandlerHostDriftFold().handle(
        ModelHostDriftFoldRequest(
            first.state,
            ModelHostDriftObservation("h201", "linux-lab", NOW, (ignored,)),
            "next",
            900,
            checked_keys=(),
        )
    )
    assert result.state.open == first.state.open
    assert not result.detected
    assert not result.resolved
    assert result.heartbeat.items_total == 0
    checked = HandlerHostDriftFold().handle(
        ModelHostDriftFoldRequest(
            first.state,
            ModelHostDriftObservation("h201", "linux-lab", NOW, (ignored,)),
            "next",
            900,
            checked_keys=("clone:x", "clone:y"),
        )
    )
    assert checked.resolved[0].key == "clone:x"
    assert checked.detected[0].key == "clone:y"
