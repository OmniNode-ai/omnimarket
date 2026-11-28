# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The start screen's animation draws every frame (OMN-26688).

Seen on a real run, 2026-11-26: `onex` stopped at start with a KeyError from
_draw_diagram, after its dot was read as a design *word* when tui_design.yaml
keeps it under glyphs (dot_on). The suite's app tests open the TUI at frame 0
only on some paths, so every frame is drawn here.

Failure modes:
  G1  a frame of the welcome diagram, or of the activity flow, raises.
  G2  the moving dot is not the design's own dot_on glyph.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_onex_setup_effect.cli_tui import (
    _draw_diagram,
    flow_renderable,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import tui_design

pytestmark = pytest.mark.unit

#: Longer than one full cycle of either animation.
_FRAMES = range(120)


@pytest.mark.parametrize("pulse", [True, False])
def test_every_frame_of_the_welcome_diagram_draws_the_designs_dot(pulse: bool) -> None:
    """G1, G2: both the pulsing and the still diagram."""
    design = tui_design()
    diagram = design.welcome.diagram.model_copy(update={"pulse": pulse})
    welcome = design.welcome.model_copy(update={"diagram": diagram})
    shown = design.model_copy(update={"welcome": welcome})
    frames = [_draw_diagram(shown, tick).plain for tick in _FRAMES]
    assert any(design.glyphs.dot_on in frame for frame in frames)


def test_every_frame_of_the_activity_flow_draws_the_designs_dot() -> None:
    """G1, G2."""
    dot = tui_design().glyphs.dot_on
    frames = [
        flow_renderable(tick, ["task", "onex", "model"]).plain for tick in _FRAMES
    ]
    assert any(dot in frame for frame in frames)
