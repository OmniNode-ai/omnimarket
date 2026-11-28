"""OMN-26620: while a task runs, the working line shows its path as a moving flow.

Operator 2026-11-25: "add animation flow". The line reads, after the spinner and time,
``Your task ──●──▶ ONEX ─────▶ <model>``, and the dot travels the path.

Failure modes:
  W1  no flow: the working line names neither ONEX nor the model the task goes to.
  W2  the flow is drawn but the dot never moves, or two dots show at once.
  W3  the dot skips a stretch of the path, so it never reaches the model.
  W4  a narrow terminal wraps the working line onto a second row.
"""

from __future__ import annotations

from textual.widgets import Static

from omnimarket.nodes.node_onex_setup_effect.cli_tui import (
    ModelTuiStatus,
    OnexTui,
    flow_renderable,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import tui_design
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui import _root
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui_activity import (
    _answer,
    _Answers,
    _running,
)


async def _ready() -> ModelTuiStatus:
    return ModelTuiStatus(model="Qwen3.8-27B", delegation="off", models=5)


def _line(app: OnexTui) -> str:
    return str(app.query_one("#activity", Static).render())


async def test_the_working_line_shows_the_path_to_the_model() -> None:
    """W1."""
    runner = _Answers(_answer({}))
    app = OnexTui(root=_root, runner=runner, load_status=_ready)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)  # the status read lands first
        await _running(pilot, app, runner)
        line = _line(app)
        assert "ONEX" in line, line
        assert "Qwen3.8-27B" in line, line
        assert line.index("ONEX") < line.index("Qwen3.8-27B")
        runner.release.set()


async def test_the_dot_moves_along_the_running_line() -> None:
    """W2."""
    runner = _Answers(_answer({}))
    app = OnexTui(root=_root, runner=runner, load_status=_ready)
    async with app.run_test(size=(140, 40)) as pilot:
        await _running(pilot, app, runner)
        places = set()
        for _ in range(10):
            line = _line(app)
            assert line.count("●") == 1, line
            places.add(line.index("●"))
            await pilot.pause(0.15)
        assert len(places) > 2, places
        runner.release.set()


def test_the_dot_covers_the_whole_path_and_lights_each_stop() -> None:
    """W3."""
    stops = ("Your task", "ONEX", "Qwen3.8-27B")
    gap = tui_design().activity.flow_gap
    seen: set[int] = set()
    for tick in range(gap * len(stops) * 2):
        text = flow_renderable(tick, stops)
        plain = text.plain
        assert plain.count("●") == 1, plain
        seen.add(plain.index("●"))
        assert all(stop in plain for stop in stops)
    # every place on both stretches of the path, before each arrow
    track = [i for i, ch in enumerate(flow_renderable(0, stops).plain) if ch in "─●"]
    assert seen == set(track), (sorted(seen), track)


async def test_a_narrow_terminal_keeps_the_working_line_on_one_row() -> None:
    """W4."""
    runner = _Answers(_answer({}))
    app = OnexTui(root=_root, runner=runner, load_status=_ready)
    async with app.run_test(size=(60, 30)) as pilot:
        await _running(pilot, app, runner)
        assert "\n" not in _line(app)
        assert len(_line(app)) <= 60
        runner.release.set()
