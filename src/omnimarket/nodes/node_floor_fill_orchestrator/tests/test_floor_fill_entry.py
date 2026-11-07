# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The stored answer, the forward and the deadline on the fill host (plan T13 acceptance criteria 10 and 10b)."""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time
from datetime import timedelta
from typing import Any

import pytest

from omnimarket.nodes.node_floor_fill_orchestrator.handlers.handler_floor_fill_command import (
    HandlerFloorFillCommand,
    forward_argv,
)
from omnimarket.nodes.node_floor_fill_orchestrator.models import (
    LINE_REFUSED_TICK,
    EnumFloorFillCommand,
    EnumFloorFillStatus,
    FloorFillConfigError,
    ModelFloorFillCommandRequest,
    ModelFloorFillStoredAnswer,
    load_floor_fill_config,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.ports import (
    ModelRunOutcome,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.scripted_ports import (
    ScriptedCommandRunner,
)
from omnimarket.nodes.node_floor_fill_orchestrator.tests.support import (
    OVERLAY,
    T0,
    Harness,
    harness,
    overlay,
)

pytestmark = pytest.mark.unit

TICK = EnumFloorFillCommand.TICK
LINES = EnumFloorFillCommand.LINES
ANSWER_KEY = OVERLAY["scope"] + ":answer"


def _handler(
    runner: ScriptedCommandRunner, **scripted: Any
) -> tuple[HandlerFloorFillCommand, Harness]:
    h = harness(**scripted)
    return (
        HandlerFloorFillCommand(
            config=h.config, orchestrator=h.orchestrator(), runner=runner
        ),
        h,
    )


def _request(
    command: EnumFloorFillCommand, host: str, key: str | None = None
) -> ModelFloorFillCommandRequest:
    return ModelFloorFillCommandRequest(command=command, key=key, host_id=host)


# --------------------------------------------------------------------------- the stored answer


def test_lines_prints_stored_answer() -> None:
    h = harness(config=overlay(max_fill_lanes_per_line=1))
    tick = h.orchestrator().run(TICK)
    printed = h.orchestrator().run(LINES)
    assert printed.lines == tick.lines
    assert printed.status is tick.status


def test_lines_stale_and_absent() -> None:
    h = harness()
    absent = h.orchestrator().run(LINES)
    assert absent.lines == ("UNKNOWN floor-fill-answer-absent",)
    h.orchestrator().run(TICK)
    h.clock.advance(minutes=h.config.max_age_minutes + 1)
    stale = h.orchestrator().run(LINES)
    assert stale.lines == (
        f"UNKNOWN floor-fill-answer-stale:{h.config.max_age_minutes + 1}m",
    )
    assert not any("MISSING" in line for line in stale.lines)
    assert stale.exit_code != 0


def test_older_snapshot_cannot_publish() -> None:
    h = harness()
    h.orchestrator().run(TICK)
    stored = ModelFloorFillStoredAnswer(
        lines=("NOTE newer",),
        status=EnumFloorFillStatus.OK,
        observed_at=T0,
        seq=2,
        state_seq=999,
    )
    version = h.store.load(ANSWER_KEY).version
    assert h.store.compare_and_set(
        ANSWER_KEY, version, version + 1, stored.model_dump_json()
    )
    h.clock.advance(minutes=1)
    answer = h.orchestrator().run(TICK)
    assert answer.lines == ("STALE-STATE",)
    assert answer.status is EnumFloorFillStatus.FAILED
    assert h.orchestrator().run(LINES).lines == ("NOTE newer",)


# --------------------------------------------------------------------------- the forward


def test_tick_wrong_host_refused() -> None:
    runner = ScriptedCommandRunner(
        ModelRunOutcome(state="exited", exit_code=0, stdout="x\n")
    )
    handler, h = _handler(runner)
    result = handler.handle(_request(TICK, "orchestrator-mac"))
    assert result.stdout == LINE_REFUSED_TICK + "\n"
    assert result.exit_code == 4
    assert runner.calls == []
    assert h.starter.starts == []


@pytest.mark.parametrize(
    "command",
    [
        EnumFloorFillCommand.INSPECT,
        EnumFloorFillCommand.LINES,
        EnumFloorFillCommand.SEND_KEY,
    ],
)
def test_forward_to_fill_host(command: EnumFloorFillCommand) -> None:
    runner = ScriptedCommandRunner(
        ModelRunOutcome(state="exited", exit_code=3, stdout="remote line\n")
    )
    handler, h = _handler(runner)
    key = "floor:alpha:a1" if command is EnumFloorFillCommand.SEND_KEY else None
    result = handler.handle(_request(command, "orchestrator-mac", key))
    assert (result.stdout, result.exit_code, result.forwarded) == (
        "remote line\n",
        3,
        True,
    )
    ((argv, timeout),) = runner.calls
    assert argv[0] == "remote-shell"
    assert argv[1] == command.value
    assert timeout == h.config.command_timeout_seconds
    assert h.starter.starts == []  # nothing ran on this host


def test_forward_send_key_preserves_argument() -> None:
    key = "floor:alpha-b:a1"
    runner = ScriptedCommandRunner(
        ModelRunOutcome(state="exited", exit_code=0, stdout="")
    )
    handler, _h = _handler(runner)
    handler.handle(_request(EnumFloorFillCommand.SEND_KEY, "orchestrator-mac", key))
    ((argv, _),) = runner.calls
    assert argv == ("remote-shell", "send_key", key)  # the key is one element, intact
    # The remote entry, given exactly that, reserves and dispatches that key.
    remote = harness()
    remote_handler = HandlerFloorFillCommand(
        config=remote.config, orchestrator=remote.orchestrator(), runner=runner
    )
    out = remote_handler.handle(
        _request(EnumFloorFillCommand.SEND_KEY, remote.config.host_id, "floor:alpha:a1")
    )
    assert [s.key for s in remote.starter.starts] == ["floor:alpha:a1"]
    assert "FLOOR-SENT key=floor:alpha:a1" in out.stdout
    assert forward_argv(("{argv}",), _request(LINES, "x")) == ("lines",)


@pytest.mark.parametrize(
    "outcome",
    [
        ModelRunOutcome(state="failed", cause="ssh: no route"),
        ModelRunOutcome(state="timed_out"),
    ],
)
def test_forward_failure_unreachable(outcome: ModelRunOutcome) -> None:
    handler, _ = _handler(ScriptedCommandRunner(outcome))
    result = handler.handle(_request(EnumFloorFillCommand.INSPECT, "orchestrator-mac"))
    assert result.stdout == "UNKNOWN floor-fill-unreachable\n"
    assert result.exit_code == 75


# --------------------------------------------------------------------------- overlay orders and the host table


def test_interval_order_refused() -> None:
    with pytest.raises(FloorFillConfigError) as error:
        load_floor_fill_config(overlay(tick_interval_seconds=15 * 60))
    assert set(error.value.keys) == {
        "floor_fill.tick_interval_seconds",
        "floor_fill.max_age_minutes",
    }


def test_fill_host_must_be_available_lab_host() -> None:
    config = load_floor_fill_config(OVERLAY)
    config.check_fill_host([("fill-host", True), ("other", True)])
    for table in ([("fill-host", False)], [("other", True)]):
        with pytest.raises(FloorFillConfigError) as error:
            config.check_fill_host(table)
        assert error.value.keys == ("floor_fill.host_id",)


def test_runner_command_never_names_the_script() -> None:
    for key in ("runner_command", "remote_argv"):
        broken = overlay(**{key: ["python", "onex_remote_lane.py", "{argv}"]})
        with pytest.raises(FloorFillConfigError):
            load_floor_fill_config(broken)


# --------------------------------------------------------------------------- the deadline ends the process


def test_entry_exits_process_at_timeout() -> None:
    """A command that stalls is ended by its own entry at ``command_timeout_seconds``; the process exits."""
    code = textwrap.dedent(
        """
        import sys, time
        from omnimarket.nodes.node_floor_fill_orchestrator.runtime_floor_fill_orchestrator import run_entry
        from omnimarket.nodes.node_floor_fill_orchestrator.handlers.handler_floor_fill_command import HandlerFloorFillCommand
        from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.core import FloorFillOrchestrator
        from omnimarket.nodes.node_floor_fill_candidates_compute.handlers.handler_floor_fill_candidates import HandlerFloorFillCandidates
        from omnimarket.nodes.node_floor_fill_lines_compute.handlers import HandlerFloorFillLines
        from omnimarket.nodes.node_floor_fill_reducer.handlers import HandlerFloorFillReducer
        from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.scripted_ports import *
        from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.store import InMemoryFloorStateStore
        from omnimarket.nodes.node_floor_fill_orchestrator.models import ModelFloorFillConfig
        from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.ports import ModelRunOutcome
        from omnimarket.nodes.node_floor_fill_orchestrator.tests.support import OVERLAY, T0
        import os

        class Stalling(ScriptedFacts):
            def read_floor(self, url):
                time.sleep(120)
                return ""

        def build(config):
            orch = FloorFillOrchestrator(config=config, store=InMemoryFloorStateStore(), facts=Stalling(None),
                pool=ScriptedPool(1), ledger=ScriptedLedger(), tickets=ScriptedTickets(), runner=ScriptedStarter(),
                clock=ScriptedClock(T0),
                candidates=HandlerFloorFillCandidates(), reducer=HandlerFloorFillReducer(), lines=HandlerFloorFillLines())
            return HandlerFloorFillCommand(config=config, orchestrator=orch,
                runner=ScriptedCommandRunner(ModelRunOutcome(state="failed", cause="x")))

        result = run_entry(["tick"], {"floor_fill": dict(OVERLAY, command_timeout_seconds=1, reserved_timeout_seconds=5)},
                           host_id=OVERLAY["host_id"], build_handler=build)
        sys.stdout.write(result.stdout); sys.stdout.flush()
        if result.timed_out:
            os._exit(result.exit_code)
        sys.exit(result.exit_code)
        """
    )
    started = time.monotonic()
    child = subprocess.Popen(
        [sys.executable, "-I", "-c", code],
        stdout=subprocess.PIPE,
        text=True,
        env={"PYTHONPATH": ":".join(p for p in sys.path if p)},
    )
    out, _ = child.communicate(timeout=60)
    elapsed = time.monotonic() - started
    assert child.returncode == 75
    assert out == "UNKNOWN floor-fill-unreachable\n"
    assert (
        elapsed < timedelta(seconds=30).total_seconds()
    )  # ended at the 1 s deadline, not the 120 s stall
    assert child.poll() == 75  # the process is gone: it was reaped with that code
