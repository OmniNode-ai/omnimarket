"""OMN-26620: welcome boxes show their whole text and line up.

Failure modes this guards (each seen in a demo screenshot):
- a guide box too small for its text cuts the last words off ("on or" without "off.");
- the outcome labels are centred, so their icons sit at different columns.
"""

from __future__ import annotations

from omnimarket.nodes.node_onex_setup_effect.cli_tui import (
    ModelTuiStatus,
    _draw_diagram,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import tui_design


async def _ready() -> ModelTuiStatus:
    return ModelTuiStatus(model="Qwen3.8-27B", delegation="off", models=5)


def test_outcome_labels_start_at_the_same_column() -> None:
    words = tui_design().welcome.diagram
    for tick in (0, 7, 30):
        plain = _draw_diagram(tui_design(), tick).plain
        rows = plain.split("\n")
        columns = set()
        for outcome in words.outcomes:
            row = next(r for r in rows if outcome in r)
            columns.add(row.index(outcome))
        assert len(columns) == 1, columns
