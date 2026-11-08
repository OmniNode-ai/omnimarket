# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Review findings on the floor fill orchestrator pull request (OMN-22855): one test per finding."""

from __future__ import annotations

import ast
import threading
from pathlib import Path

import pytest

from omnimarket.nodes.node_floor_fill_orchestrator.handlers.handler_floor_fill_command import (
    HandlerFloorFillCommand,
)
from omnimarket.nodes.node_floor_fill_orchestrator.models import (
    EnumFloorFillCommand,
    FloorFillConfigError,
    ModelFloorFillCommandRequest,
    ModelFloorFillStoredAnswer,
    load_floor_fill_config,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.ports import (
    ModelRunOutcome,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.scripted_ports import (
    ScriptedCommandForward,
    ScriptedFacts,
)
from omnimarket.nodes.node_floor_fill_orchestrator.tests.support import (
    OVERLAY,
    harness,
    overlay,
)

pytestmark = pytest.mark.unit

NODE_ROOT = Path(__file__).resolve().parents[1]
ANSWER_KEY = OVERLAY["scope"] + ":answer"


# --------------------------------------------------------------------------- 1: a timed-out command is fenced


class _BlockedFloor(ScriptedFacts):
    """A fact source whose floor read waits until the test releases it."""

    def __init__(self, floor: str | None, release: threading.Event) -> None:
        super().__init__(floor)
        self.release = release

    def read_floor(self, url: str) -> str:
        assert self.release.wait(30)
        return super().read_floor(url)


def test_timed_out_command_is_fenced_and_writes_nothing() -> None:
    release = threading.Event()
    plain = harness()
    h = harness(
        facts=_BlockedFloor(plain.facts.floor, release),
        config=overlay(command_timeout_seconds=1),
    )
    handler = HandlerFloorFillCommand(
        config=h.config,
        orchestrator=h.orchestrator(),
        runner=ScriptedCommandForward(ModelRunOutcome(state="failed", cause="x")),
    )
    result = handler.handle(
        ModelFloorFillCommandRequest(
            command=EnumFloorFillCommand.TICK, host_id=h.config.host_id
        )
    )
    assert result.timed_out
    release.set()
    for worker in threading.enumerate():
        if worker.name == "floor-fill-command":
            worker.join(30)
    # the command was reported unreachable, so it must not go on to reserve, start or publish
    assert h.starter.starts == []
    assert h.store.load(h.config.scope).version == 0
    assert h.store.load(ANSWER_KEY).version == 0


# --------------------------------------------------------------------------- 2: an older observation never replaces a newer one


def test_publish_keeps_the_newer_answer_at_equal_state_seq() -> None:
    h = harness()
    h.clock.advance(minutes=10)
    h.orchestrator().run(EnumFloorFillCommand.TICK)  # starts lanes: the state moves
    h.orchestrator().run(
        EnumFloorFillCommand.TICK
    )  # nothing more to send: same state seq
    newer = ModelFloorFillStoredAnswer.model_validate_json(
        h.store.load(ANSWER_KEY).payload or ""
    )
    h.clock.advance(
        minutes=-10
    )  # a slower command that observed earlier, at the same state seq
    answer = h.orchestrator().run(EnumFloorFillCommand.TICK)
    stored = ModelFloorFillStoredAnswer.model_validate_json(
        h.store.load(ANSWER_KEY).payload or ""
    )
    assert stored.state_seq >= newer.state_seq
    assert stored.observed_at == newer.observed_at
    assert answer.lines == ("STALE-STATE",)


# --------------------------------------------------------------------------- 4: no other node's private handler or model package


def test_orchestration_core_imports_no_private_module_of_the_reducer_node() -> None:
    tree = ast.parse((NODE_ROOT / "orchestration" / "core.py").read_text("utf-8"))
    private = [
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module is not None
        and node.module.startswith("omnimarket.nodes.node_floor_fill_reducer.")
        and node.module.rsplit(".", 1)[-1].startswith(("handler_", "model_"))
    ]
    assert private == []


# --------------------------------------------------------------------------- 6: a field constraint names its overlay key


def test_field_constraint_error_names_the_overlay_key() -> None:
    with pytest.raises(FloorFillConfigError) as raised:
        load_floor_fill_config(overlay(command_timeout_seconds=0))
    assert raised.value.keys == ("floor_fill.command_timeout_seconds",)
