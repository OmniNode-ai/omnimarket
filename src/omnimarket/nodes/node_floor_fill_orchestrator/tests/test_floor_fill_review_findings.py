# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22854 review findings: a stale tick cannot replace a newer answer, a mid-pass store failure keeps what was committed,
an expired command stops authorizing starts, and a refused overlay value names its key.

Named injected breaks: ``OlderTickPublishesLast`` (an overlapping tick that finishes after a newer one),
``StoreFailsAfterFirstStart`` (a state write that raises once a lane has started) and ``FactsBlockedUntilReleased``
(a facts read that resumes after the command timeout).
"""

from __future__ import annotations

import threading
import time
from datetime import timedelta

import pytest

from omnimarket.nodes.node_floor_fill_orchestrator.handlers.handler_floor_fill_command import (
    HandlerFloorFillCommand,
)
from omnimarket.nodes.node_floor_fill_orchestrator.models import (
    EXIT_CODE_BY_STATUS,
    EnumFloorFillCommand,
    EnumFloorFillStatus,
    FloorFillConfigError,
    ModelFloorFillCommandRequest,
    ModelFloorFillStoredAnswer,
    load_floor_fill_config,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.ports import (
    FloorFillUnreadableError,
    ModelStateRead,
    ProtocolFloorStateStore,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.scripted_ports import (
    ScriptedFacts,
    ScriptedStarter,
    floor_record,
)
from omnimarket.nodes.node_floor_fill_orchestrator.runtime_floor_fill_orchestrator import (
    run_entry,
)
from omnimarket.nodes.node_floor_fill_orchestrator.tests.support import (
    LINES4,
    OVERLAY,
    T0,
    harness,
    overlay,
)

pytestmark = pytest.mark.unit

TICK = EnumFloorFillCommand.TICK
ANSWER_ROW = "floor-fill-test:answer"


def test_error_an_older_tick_with_an_equal_state_sequence_cannot_replace_a_newer_answer() -> (
    None
):
    """OlderTickPublishesLast: two ticks leave the state row untouched (equal sequence); the older one publishes last."""
    first = harness(free=0)
    newer = first.orchestrator()
    first.clock.advance(seconds=120)
    assert newer.run(TICK).status is not EnumFloorFillStatus.FAILED
    stored = ModelFloorFillStoredAnswer.model_validate_json(
        first.store.load(ANSWER_ROW).payload or ""
    )
    assert stored.observed_at == T0 + timedelta(seconds=120)

    older_harness = harness(free=0, store=first.store)
    older = (
        older_harness.orchestrator()
    )  # its clock reads T0, two minutes before the stored answer
    answer = older.run(TICK)
    assert answer.status is EnumFloorFillStatus.FAILED
    assert answer.lines == ("STALE-STATE",)
    after = ModelFloorFillStoredAnswer.model_validate_json(
        first.store.load(ANSWER_ROW).payload or ""
    )
    assert after.observed_at == T0 + timedelta(seconds=120)


def test_golden_a_later_tick_with_an_equal_state_sequence_still_publishes() -> None:
    first = harness(free=0)
    first.orchestrator().run(TICK)
    first.clock.advance(seconds=120)
    assert first.orchestrator().run(TICK).status is not EnumFloorFillStatus.FAILED
    stored = ModelFloorFillStoredAnswer.model_validate_json(
        first.store.load(ANSWER_ROW).payload or ""
    )
    assert stored.observed_at == T0 + timedelta(seconds=120)


class StoreFailsAfterFirstStart:
    """A store whose state writes raise once the starter has started a lane: the SQLite store's write error."""

    def __init__(
        self, inner: ProtocolFloorStateStore, starter: ScriptedStarter
    ) -> None:
        self.inner = inner
        self.starter = starter

    def load(self, key: str) -> ModelStateRead:
        return self.inner.load(key)

    def compare_and_set(
        self, key: str, expected_version: int, new_version: int, payload: str
    ) -> bool:
        if self.starter.starts and not key.endswith(":answer"):
            raise FloorFillUnreadableError("write-failed")
        return self.inner.compare_and_set(key, expected_version, new_version, payload)


def test_error_a_store_failure_after_a_committed_start_keeps_the_start_in_the_answer() -> (
    None
):
    starter = ScriptedStarter()
    base = harness(free=20, starter=starter)
    base.store = StoreFailsAfterFirstStart(base.store, starter)
    answer = base.orchestrator().run(TICK)
    assert answer.status is EnumFloorFillStatus.FAILED
    assert answer.lines == ("UNKNOWN fill-state:write-failed",)
    assert len(starter.starts) >= 1, "setup: a lane started before the failure"
    started_keys = {r.key for r in answer.key_results if r.kind == "started"}
    assert started_keys == {s.key for s in starter.starts}
    assert answer.state_seq >= 1
    assert answer.observed_at == T0


class FactsBlockedUntilReleased(ScriptedFacts):
    """The floor read resumes only when released: after the command timeout it continues the pass."""

    def __init__(self, floor: str, release: threading.Event) -> None:
        super().__init__(floor)
        self.release = release

    def read_floor(self, url: str) -> str:
        self.release.wait(timeout=10)
        return super().read_floor(url)


def test_error_a_command_that_timed_out_authorizes_no_further_start() -> None:
    release = threading.Event()
    starter = ScriptedStarter()
    base = harness(
        free=20,
        starter=starter,
        config=overlay(command_timeout_seconds=1),
        facts=FactsBlockedUntilReleased(floor_record(LINES4, generated_at=T0), release),
    )
    handler = HandlerFloorFillCommand(
        config=base.config, orchestrator=base.orchestrator(), runner=starter
    )
    result = handler.handle(
        ModelFloorFillCommandRequest(command=TICK, host_id=base.config.host_id)
    )
    assert result.timed_out
    release.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and any(
        t.name == "floor-fill-command" and t.is_alive() for t in threading.enumerate()
    ):
        time.sleep(0.05)
    assert starter.starts == [], "the expired command went on to start lanes"


def test_error_an_invalid_field_value_names_its_overlay_key() -> None:
    with pytest.raises(FloorFillConfigError) as refused:
        load_floor_fill_config({**OVERLAY, "max_age_minutes": 0})
    assert "floor_fill.max_age_minutes" in refused.value.keys


@pytest.mark.parametrize(
    "hosts",
    [[], [("other", True)], [("fill-host", False)]],
    ids=["no-row", "other-host-only", "row-unavailable"],
)
def test_error_the_entry_refuses_a_host_the_lab_table_does_not_list_as_available(
    hosts: list[tuple[str, bool]],
) -> None:
    def never_built(config: object) -> HandlerFloorFillCommand:
        raise AssertionError("no handler may be built for an ineligible host")

    result = run_entry(
        ["tick"],
        {"floor_fill": dict(OVERLAY)},
        host_id=str(OVERLAY["host_id"]),
        hosts=hosts,
        build_handler=never_built,
    )
    assert result.exit_code == EXIT_CODE_BY_STATUS[EnumFloorFillStatus.FAILED]
    assert result.stdout == "UNKNOWN fill-state:overlay:floor_fill.host_id\n"


SEND_KEY = EnumFloorFillCommand.SEND_KEY


def test_error_a_reservation_of_an_earlier_command_still_counts_against_the_pool_slots() -> (
    None
):
    """One free slot is reported; alpha is reserved and started, its claim is not yet visible, so the pool still says one."""
    base = harness(free=1, lines={"alpha": ["a1"], "bravo": ["b1"]})
    first = base.orchestrator().run(SEND_KEY, "floor:alpha:a1")
    second = base.orchestrator().run(SEND_KEY, "floor:bravo:b1")
    assert [r.kind for r in first.key_results] == ["started"]
    assert [s.key for s in base.starter.starts] == ["floor:alpha:a1"], (
        "the second command started a lane on the one slot the first already took"
    )
    assert second.status is EnumFloorFillStatus.FAILED


def test_golden_a_reservation_whose_claim_is_visible_does_not_count_twice() -> None:
    """Once alpha's CLAIM is on the ledger it is a SENT lane and the pool's own count carries it: bravo may take the next slot."""
    base = harness(free=1, lines={"alpha": ["a1"], "bravo": ["b1"]})
    base.orchestrator().run(SEND_KEY, "floor:alpha:a1")
    (alpha,) = base.starter.starts
    base.ledger.claim(alpha.lane, alpha.key)
    base.orchestrator().run(SEND_KEY, "floor:bravo:b1")
    assert [s.key for s in base.starter.starts] == ["floor:alpha:a1", "floor:bravo:b1"]
