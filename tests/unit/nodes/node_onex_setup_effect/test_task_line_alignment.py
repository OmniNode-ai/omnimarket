"""OMN-26620: the working line lines up under the task, with a row of space between.

Operator 2026-11-25 (screenshot): "alignment and space not proper" - the spinner sat one
column left of the task's mark, on the row straight under it. Also "don't use lucid":
the design names no Lucide icon at all.
"""

from __future__ import annotations

from pathlib import Path

from omnimarket.nodes.node_onex_setup_effect import cli_tui
from omnimarket.nodes.node_onex_setup_effect.cli_tui import OnexTui, icon
from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import tui_design
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui import _root
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui_activity import (
    _answer,
    _Answers,
    _running,
)
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui_cancel import _status


async def test_the_spinner_sits_under_the_task_mark_one_row_apart() -> None:
    runner = _Answers(_answer({}))
    app = OnexTui(root=_root, runner=runner, load_status=_status)
    async with app.run_test(size=(140, 40)) as pilot:
        await _running(pilot, app, runner)
        await pilot.pause(0.1)
        lines = [
            "".join(segment.text for segment in strip)
            for strip in app.screen._compositor.render_strips()
        ]
        mark = icon("ask")
        frames = tui_design().activity.frames
        task_row = next(
            i for i, line in enumerate(lines) if f"{mark} what is 2 + 2?" in line
        )
        spin_row = next(
            i
            for i, line in enumerate(lines)
            if line.strip()[:1] in frames and "…" in line
        )
        assert spin_row == task_row + 2, (task_row, spin_row)
        assert not lines[task_row + 1].strip(), lines[task_row + 1]
        spin_col = next(c for c, ch in enumerate(lines[spin_row]) if ch in frames)
        assert spin_col == lines[task_row].index(mark), (spin_col, lines[task_row])
        runner.release.set()


def test_no_icon_is_a_lucide_name() -> None:
    source = Path(cli_tui.__file__).with_name("tui_design.yaml").read_text()
    assert "lucide" not in source.lower()
