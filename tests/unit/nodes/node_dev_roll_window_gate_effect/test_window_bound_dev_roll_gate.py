# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The two-hour bound: with a dev merge every four minutes the gate rolls at most once per interval.

A minute-by-minute simulation over the real handlers: one dev command every four minutes, a runtime
tick every minute, and a simulated deploy agent that completes each release five minutes after it.
The number of roll_now decisions must stay within the overlay's interval, every merge must be carried
by exactly one release (none lost, none repeated), and no release may start while the previous roll has
no completion.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

import pytest
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.events.runtime_deployment import ModelDeployPublishCommand
from omnimarket.nodes.node_dev_roll_window_gate_effect.models import (
    ModelDevRollGateDecision,
    ModelDevRollWindowGatePolicy,
)
from tests.unit.nodes.node_dev_roll_window_gate_effect.chain_support import tick_payload
from tests.unit.nodes.node_dev_roll_window_gate_effect.conftest import (
    MIN_INTERVAL,
    NOW,
    POLICY,
    Clock,
    Gate,
    command,
    completed,
)

pytestmark = pytest.mark.unit

HORIZON_SECONDS = 7200
MERGE_EVERY_SECONDS = 240
COMPLETES_AFTER_SECONDS = 300


def simulate(
    policy: ModelDevRollWindowGatePolicy,
) -> tuple[list[list[UUID]], list[UUID], int]:
    """Return the releases (each a list of command ids), every merged id, and the commands still held."""
    gate = Gate(policy, Clock(NOW))
    releases: list[list[UUID]] = []
    merged: list[UUID] = []
    running_since: list[object] = []
    pending_completion: list[tuple[object, UUID]] = []
    for minute in range(HORIZON_SECONDS // 60 + 1):
        if (minute * 60) % MERGE_EVERY_SECONDS == 0:
            item = command(number=len(merged) + 1)
            item = item.model_copy(update={"requested_at": gate.clock.now})
            gate.hold.handle(item)
            merged.append(item.correlation_id)
        for due, command_id in [
            p for p in pending_completion if p[0] <= gate.clock.now
        ]:
            gate.completed.handle(completed(command_id, at=gate.clock.now))
            pending_completion.remove((due, command_id))
            running_since.clear()
        out = gate.tick.handle(
            ModelRuntimeTick.model_validate(tick_payload(gate.clock.now))
        )
        sent = [m for m in (out or []) if isinstance(m, ModelDeployPublishCommand)]
        if sent:
            assert running_since == [], (
                "a release started while the previous roll had no completion"
            )
            releases.append([c.correlation_id for c in sent])
            running_since.append(gate.clock.now)
            pending_completion.append(
                (
                    gate.clock.now + timedelta(seconds=COMPLETES_AFTER_SECONDS),
                    sent[-1].correlation_id,
                )
            )
        assert all(
            isinstance(m, ModelDeployPublishCommand | ModelDevRollGateDecision)
            for m in (out or [])
        )
        gate.clock.advance(60)
    return releases, merged, len(gate.store.load().held)


def test_two_hours_of_merges_roll_at_most_once_per_interval_and_lose_nothing() -> None:
    releases, merged, still_held = simulate(POLICY)
    assert 1 <= len(releases) <= HORIZON_SECONDS // MIN_INTERVAL
    carried = [command_id for release in releases for command_id in release]
    assert len(carried) == len(set(carried)), "a command was released twice"
    assert set(carried) <= set(merged)
    assert len(carried) + still_held == len(merged)
    assert all(len(release) >= 1 for release in releases)
    assert max(len(release) for release in releases) > 1, "no release carried a batch"


def test_failing_control_with_a_one_second_window_the_bound_is_broken() -> None:
    """The bound is real: with the window and interval set to one second the same simulation rolls far more often."""
    loose = POLICY.model_copy(
        update={
            "batch_window_seconds": 1,
            "min_start_interval_seconds": 1,
            "healthy_for_seconds": 1,
        }
    )
    releases, _merged, _held = simulate(loose)
    assert len(releases) > HORIZON_SECONDS // MIN_INTERVAL
    with pytest.raises(AssertionError):
        assert len(releases) <= HORIZON_SECONDS // MIN_INTERVAL
