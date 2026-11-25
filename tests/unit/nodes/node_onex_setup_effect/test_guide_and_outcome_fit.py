"""OMN-26620: welcome boxes show their whole text and line up.

Failure modes this guards (each seen in a demo screenshot):
- a guide box too small for its text cuts the last words off ("on or" without "off.");
- the outcome labels are centred, so their icons sit at different columns.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_onex_setup_effect.cli_tui import (
    ModelTuiStatus,
    OnexTui,
    _draw_diagram,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import tui_design


async def _ready() -> ModelTuiStatus:
    return ModelTuiStatus(model="Qwen3.8-27B", delegation="off", models=5)


@pytest.mark.parametrize("size", [(140, 46), (110, 42)])
async def test_every_guide_box_shows_its_whole_text(size: tuple[int, int]) -> None:
    guide = tui_design().welcome.guide
    app = OnexTui(load_status=_ready)
    async with app.run_test(size=size) as pilot:
        for _ in range(4):
            await pilot.pause()
        lines = [
            "".join(segment.text for segment in strip)
            for strip in app.screen._compositor.render_strips()
        ]
        for step in guide.steps:
            region = app.screen.query_one(f"#guide-{step.id}").region
            assert region.height > 0, step.id
            inside = " ".join(
                lines[y][region.x : region.right]
                for y in range(region.y, region.bottom)
            )
            shown = " ".join(inside.replace("│", " ").replace("─", " ").split())
            assert step.text in shown, (step.id, shown)


def test_outcome_labels_start_at_the_same_column() -> None:
    words = tui_design().welcome.diagram
    for tick in (0, 7, 30):
        plain = _draw_diagram(tui_design(), tick).plain  # type: ignore[attr-defined]
        rows = plain.split("\n")
        columns = set()
        for outcome in words.outcomes:
            row = next(r for r in rows if outcome in r)
            columns.add(row.index(outcome))
        assert len(columns) == 1, columns
