# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor-line start brief (OMN-23663): the golden start, the error chain and the failing controls.

A floor line with a startable task is a floor-line start, not a stopped-lane continuation. When the fill's
``floor_fill.runner_command`` carries a ``{brief}`` element, the fill writes a brief file for the floor-line start
through the briefs seam (``ScriptedBriefs.save_brief``) from the config key ``line_brief_text`` and fills ``{brief}``
with the path the seam returned. A ``runner_command`` with no ``{brief}`` element writes no brief (the old behaviour).
"""

from __future__ import annotations

import pytest

from omnimarket.models.floor_fill import (
    EnumFloorFillEntryState,
    ModelFloorFillState,
)
from omnimarket.nodes.node_floor_fill_orchestrator.models import (
    EnumFloorFillCommand,
    FloorFillConfigError,
    load_floor_fill_config,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.scripted_ports import (
    ScriptedBriefs,
)
from omnimarket.nodes.node_floor_fill_orchestrator.tests.support import (
    OVERLAY,
    Harness,
    harness,
    overlay,
)

pytestmark = pytest.mark.unit

TICK = EnumFloorFillCommand.TICK

LINE_BRIEF_TEXT = "LINE-BRIEF line={line_id} task={task_id} ticket={ticket} lane={lane} plans={plans_root}"


def _config() -> dict[str, object]:
    """The override: the fill's runner command carries the brief element and the line brief text."""
    return overlay(
        runner_command=[
            "node-entry",
            "--overlay",
            "/scripted/overlay.yaml",
            "--ledger",
            "/scripted/ledger.md",
            "--pool-snapshot",
            "/scripted/pool.json",
            "--model",
            "scripted-model",
            "--lane",
            "{lane}",
            "--key",
            "{key}",
            "--ticket",
            "{ticket}",
            "--attempt",
            "{attempt}",
            "--brief",
            "{brief}",
            "--run-id",
            "{lane}",
        ],
        line_brief_text=LINE_BRIEF_TEXT,
    )


def _stored_state(h: Harness) -> ModelFloorFillState:
    read = h.store.load(h.config.scope)
    assert read.payload is not None
    return ModelFloorFillState.model_validate_json(read.payload)


def _line_task(key: str) -> tuple[str, str]:
    """The line id and task id of a floor-line key, ``floor:<line_id>:<task_id>``."""
    _, line_id, task_id = key.split(":")
    return line_id, task_id


def _expected_brief(key: str, ticket: str, lane: str) -> str:
    line_id, task_id = _line_task(key)
    return LINE_BRIEF_TEXT.format(
        line_id=line_id,
        task_id=task_id,
        ticket=ticket,
        lane=lane,
        plans_root=OVERLAY["plans_root"],
    )


# --------------------------------------------------------------------------- the golden behaviour


def test_a_floor_line_start_hands_the_entry_its_brief() -> None:
    h = harness(config=_config())
    h.orchestrator().run(TICK)
    assert h.starter.starts
    start = h.starter.starts[0]
    argv = start.argv
    assert argv[argv.index("--brief") + 1] == f"scripted://briefs/{start.lane}.md"
    assert argv[argv.index("--run-id") + 1] == start.lane
    assert argv[argv.index("--model") + 1] == "scripted-model"
    assert h.briefs.saved[start.lane] == _expected_brief(
        start.key, start.ticket, start.lane
    )


def test_every_started_floor_line_gets_its_own_brief() -> None:
    h = harness(config=_config())
    h.orchestrator().run(TICK)
    assert h.starter.starts
    texts = []
    for start in h.starter.starts:
        assert start.lane in h.briefs.saved
        texts.append(h.briefs.saved[start.lane])
    assert len(set(texts)) == len(texts)


# --------------------------------------------------------------------------- the failing controls


def test_control_without_the_brief_placeholder_no_brief_is_written() -> None:
    h = harness(config=overlay())
    h.orchestrator().run(TICK)
    assert h.starter.starts
    assert h.briefs.saved == {}
    for start in h.starter.starts:
        assert "--brief" not in start.argv


def test_an_unwritable_line_brief_clears_the_reservation_and_reports_failed() -> None:
    h = harness(config=_config(), briefs=ScriptedBriefs(fail_save=True))
    answer = h.orchestrator().run(TICK)
    assert h.starter.starts == []
    assert answer.key_results
    for result in answer.key_results:
        assert result.kind == "failed"
        assert result.cause == "brief-unwritable:brief-unwritable"
    state = _stored_state(h)
    for result in answer.key_results:
        assert state.entries[result.key].state is EnumFloorFillEntryState.NONE


def test_control_the_line_brief_is_not_the_continuation_brief() -> None:
    h = harness(config=_config())
    h.orchestrator().run(TICK)
    for start in h.starter.starts:
        assert OVERLAY["stopped_continue_rule"] not in h.briefs.saved[start.lane]


def test_the_line_brief_text_must_be_fillable() -> None:
    with pytest.raises(FloorFillConfigError) as refused:
        load_floor_fill_config(overlay(line_brief_text="bad {nope}"))
    assert "line_brief_text" in str(refused.value)
