# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A canary row whose first ledger append fails is appended again, as the overlay says (OMN-23077).

Recorded failure: the daily unit's one append on the h053 lab host outlived its 60 second timeout at
2026-11-08 04:14Z, the same command answered in 7.6 seconds a few hours later, and the one miss left the
piece 9 reading with no row for the whole 93600 second bound. The row id is the same on every attempt, so an
append that did land after its caller gave up is answered ``duplicate`` and never written twice.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_landing_canary_effect.overlay import (
    ModelLandingCanaryOverlay,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import ModelAppendOutcome
from tests.unit.nodes.node_landing_canary_effect.canary_support import World, overlay

pytestmark = pytest.mark.unit


class _FailsFirst:
    """Answers ``error`` for the first ``failures`` appends, then writes into a real in-memory ledger."""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls: list[str] = []
        self.ledger = InMemoryLedger()

    def append(self, rows: str, request_id: str) -> ModelAppendOutcome:
        self.calls.append(request_id)
        if len(self.calls) <= self.failures:
            return ModelAppendOutcome(
                status="error", message="append-command-failed:TimeoutExpired"
            )
        return self.ledger.append(rows, request_id)


def _with_retry(attempts: int, wait: int) -> ModelLandingCanaryOverlay:
    base = overlay()
    return base.model_copy(
        update={
            "publish": base.publish.model_copy(
                update={"append_attempts": attempts, "append_retry_wait_seconds": wait}
            )
        }
    )


def _run(world: World, appender: _FailsFirst, settings: ModelLandingCanaryOverlay):  # type: ignore[no-untyped-def]
    from omnimarket.nodes.node_landing_canary_effect.models.model_landing_canary import (
        ModelLandingCanaryCommand,
    )

    return world.handler(appender, settings).handle(  # type: ignore[arg-type]
        ModelLandingCanaryCommand(correlation_id="retry-1", write=True)
    )


def test_golden_chain_a_second_attempt_lands_the_row() -> None:
    world = World()
    appender = _FailsFirst(failures=1)
    result = _run(world, appender, _with_retry(attempts=3, wait=20))
    assert result.ledger == "written"
    assert result.ledger_cause == ""
    assert not result.failed
    assert len(appender.calls) == 2
    assert len(set(appender.calls)) == 1, "every attempt carries the same request id"
    assert 20.0 in world.clock.sleeps


def test_error_chain_every_attempt_failing_keeps_the_last_cause() -> None:
    world = World()
    appender = _FailsFirst(failures=99)
    result = _run(world, appender, _with_retry(attempts=3, wait=20))
    assert result.ledger == "append-failed:error"
    assert result.ledger_cause == "append-command-failed:TimeoutExpired"
    assert result.failed
    assert len(appender.calls) == 3


def test_one_attempt_is_the_old_behaviour() -> None:
    appender = _FailsFirst(failures=1)
    result = _run(World(), appender, _with_retry(attempts=1, wait=0))
    assert result.ledger == "append-failed:error"
    assert len(appender.calls) == 1


def test_a_landed_append_is_not_repeated() -> None:
    appender = _FailsFirst(failures=0)
    result = _run(World(), appender, _with_retry(attempts=3, wait=20))
    assert result.ledger == "written"
    assert len(appender.calls) == 1
