# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19998: a lab daemon whose installed revision lags its source branch for more than two hours
is a MISSING `deploy-drift:<daemon>@<host>` line on every throughput tick, so the orchestrator sees
a merged fix that is not running. A younger lag, or one held by an unexpired branch install, is a
NOTE. The facts are the drift rows node_lab_daemon_sync_effect writes on each host.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.nodes.node_throughput_tick_decision_compute.handlers.handler_throughput_tick_decision import (
    HandlerThroughputTickDecision,
)
from omnimarket.nodes.node_throughput_tick_decision_compute.models.model_throughput_tick_decision import (
    ModelThroughputTickRequest,
)

pytestmark = pytest.mark.unit

HANDLER = HandlerThroughputTickDecision()
NOW = "2026-10-17T03:00:00Z"


def run(**overrides: Any) -> Any:
    payload: dict[str, Any] = {"now": NOW, "launchctl": "not_loaded", **overrides}
    return HANDLER.handle(ModelThroughputTickRequest.model_validate(payload))


def drift(since: str, **extra: Any) -> dict[str, Any]:
    return {
        "daemon": "landing-controller",
        "host": "h101",
        "installed_revision": "4c8a15a0" + "0" * 32,
        "source_revision": "a" * 40,
        "lagging_since": since,
        **extra,
    }


def lines(result: Any) -> list[str]:
    return [line for line in result.lines if "deploy-drift:" in line]


def test_lag_past_two_hours_is_missing() -> None:
    result = run(deploy_drifts=[drift("2026-10-16T11:42:00Z")])
    [line] = lines(result)
    assert line.startswith(
        "MISSING deploy-drift:landing-controller@h101 | installed 4c8a15a0 lags source aaaaaaaa "
        "since 2026-10-16T11:42:00Z (15.3h, > 2h)"
    ), line
    assert " | FIX: " in line
    assert "deploy-drift:landing-controller@h101" in result.missing
    assert result.exit_code == 1
    assert "deploy-drift" in result.checked


def test_lag_within_two_hours_is_a_note() -> None:
    result = run(deploy_drifts=[drift("2026-10-17T01:30:00Z")])
    [line] = lines(result)
    assert line.startswith("NOTE deploy-drift:landing-controller@h101"), line
    assert not [m for m in result.missing if m.startswith("deploy-drift")]


def test_unexpired_branch_install_is_a_note_and_an_expired_one_is_missing() -> None:
    held = run(
        deploy_drifts=[
            drift("2026-10-16T20:00:00Z", branch_until="2026-10-17T04:00:00Z")
        ]
    )
    assert lines(held)[0].startswith("NOTE deploy-drift:landing-controller@h101")
    expired = run(
        deploy_drifts=[
            drift("2026-10-16T20:00:00Z", branch_until="2026-10-17T02:00:00Z")
        ]
    )
    assert lines(expired)[0].startswith("MISSING deploy-drift:landing-controller@h101")


def test_the_bound_is_a_request_field_and_no_facts_skip_the_finding() -> None:
    assert run(
        deploy_drifts=[drift("2026-10-16T11:42:00Z")], deploy_drift_max_hours=24
    ).missing == ["controller"]
    assert "deploy-drift" in run(deploy_drifts=[]).checked
    assert "deploy-drift" not in run().checked
