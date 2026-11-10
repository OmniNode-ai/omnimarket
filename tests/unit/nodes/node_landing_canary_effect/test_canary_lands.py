# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1 (OMN-22920): the canary opens one test pull request, the landing tick merges it, the node records the time."""

from __future__ import annotations

import pytest

from omnimarket.models.model_reading import EnumReadingOutcome, EnumReadingVerdict
from omnimarket.nodes.node_landing_canary_effect.models.model_landing_canary import (
    EnumCanaryEnding,
    ModelLandingCanaryCommand,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from tests.unit.nodes.node_landing_canary_effect.canary_support import (
    World,
    operational_overlay,
    overlay,
)

pytestmark = pytest.mark.unit

COMMAND = ModelLandingCanaryCommand(correlation_id="canary-1", write=True)


def test_the_canary_lands_inside_the_bar_and_the_elapsed_time_is_recorded() -> None:
    world, ledger = World(), InMemoryLedger()
    result = world.handler(ledger, operational_overlay()).handle(COMMAND)
    assert result.verdict is EnumReadingVerdict.WORKING
    assert result.outcome is EnumReadingOutcome.READ
    assert result.ending is EnumCanaryEnding.MERGED
    assert result.elapsed_seconds == 300
    assert result.bound_seconds == overlay().target.landing_bound_seconds
    assert result.ledger == "written"
    row = ledger.lines[0]
    assert "verdict=WORKING" in row
    assert "ending=merged" in row
    assert "elapsed_s=300" in row
    assert "bound_s=3600" in row
    assert "piece=9" in row
    assert "tag=operational" in row
    assert "backend=scm" in row


def test_exactly_one_test_pull_request_is_opened_in_the_test_repository() -> None:
    world = World()
    world.handler(InMemoryLedger()).handle(COMMAND)
    assert len(world.repo.opened_requests) == 1
    request = world.repo.opened_requests[0]
    assert request.repo == overlay().target.repo
    assert request.base == overlay().target.base
    assert request.branch.startswith(overlay().target.branch_prefix)
    assert request.title.startswith(overlay().target.title_prefix)


def test_the_wait_polls_at_the_overlay_interval_and_stops_when_it_merges() -> None:
    world = World()
    world.handler(InMemoryLedger()).handle(COMMAND)
    poll = float(overlay().target.poll_seconds)
    assert set(world.clock.sleeps) == {poll}
    assert len(world.clock.sleeps) == 5  # 300 s at 60 s a poll


def test_a_merge_after_the_bar_is_not_working_with_the_late_time_recorded() -> None:
    world, ledger = World(), InMemoryLedger()
    world.break_landing_tick_slow()
    # The host slept through the wait: the first poll after the bar finds the merge, which came late.
    world.clock.oversleep_seconds = 4000
    result = world.handler(ledger).handle(COMMAND)
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert result.ending is EnumCanaryEnding.MERGED_LATE
    assert result.elapsed_seconds == 3720
    assert "elapsed_s=3720" in ledger.lines[0]


def test_a_pull_request_closed_unmerged_is_not_working() -> None:
    world, ledger = World(), InMemoryLedger()
    world.break_pull_request_closed()
    result = world.handler(ledger).handle(COMMAND)
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert result.ending is EnumCanaryEnding.CLOSED_UNMERGED
    assert "reason=closed-unmerged" in ledger.lines[0]


def test_a_pull_request_that_cannot_be_opened_is_unknown_with_a_typed_reason() -> None:
    world, ledger = World(), InMemoryLedger()
    world.break_open_fails()
    result = world.handler(ledger).handle(COMMAND)
    assert result.verdict is EnumReadingVerdict.UNKNOWN
    assert result.outcome is EnumReadingOutcome.SOURCE_UNREADABLE
    assert result.ending is EnumCanaryEnding.OPEN_FAILED
    assert result.pr_number is None
    assert "reason=canary-open-failed-push-refused" in ledger.lines[0]
    assert world.clock.sleeps == []


def test_a_state_that_cannot_be_read_at_the_end_is_unknown() -> None:
    world, ledger = World(), InMemoryLedger()
    world.break_state_unreadable()
    result = world.handler(ledger).handle(COMMAND)
    assert result.verdict is EnumReadingVerdict.UNKNOWN
    assert result.ending is EnumCanaryEnding.STATE_UNREADABLE
    assert "reason=state-unreadable-pr-state-exit-1" in ledger.lines[0]


def test_a_probe_run_appends_nothing() -> None:
    world, ledger = World(), InMemoryLedger()
    result = world.handler(ledger).handle(
        ModelLandingCanaryCommand(correlation_id="canary-probe", write=False)
    )
    assert result.verdict is EnumReadingVerdict.WORKING
    assert ledger.lines == []
    assert result.ledger == "not-written"
