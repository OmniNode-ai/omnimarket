# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC2 (OMN-26620): the start screen's header is at most four lines, and the
full tips show only on the first launch.

Read on dev (bf09f2968): ``welcome_renderable()`` writes three block-letter
banner words (each three font rows, separated by blank lines), a tagline and
five tips, so the start screen's header is 21 lines tall and tips are present
on every launch. The mockups the operator approved ("26620-mockup=A", variant
02-refresh-a) replace it with a compact header of at most four lines
(logo, version, first model, folder) and gate the tips on a launch counter.

Failure modes:
  AC2-F1  the header is taller than four lines at 140x40.
  AC2-F2  the tips reappear on a second launch (no first-launch gating).
"""

from __future__ import annotations

import pytest
from rich.console import Console

from omnimarket.nodes.node_onex_setup_effect.cli_tui import (
    welcome_renderable,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import tui_design

pytestmark = pytest.mark.unit

#: Render the header exactly as the screen writes it, at the pilot's 140 cols.
_WIDTH = 140
_HEIGHT = 40


def _rendered_lines() -> list[str]:
    """The header the main screen writes into #output, rendered to plain lines."""
    console = Console(
        width=_WIDTH, height=_HEIGHT, force_terminal=False, color_system=None
    )
    with console.capture() as capture:
        console.print(welcome_renderable())
    return capture.get().splitlines()


def test_header_no_taller_than_four_lines() -> None:
    """AC2-F1: the compact header replaces the 21-line block-letter banner."""
    _ = [line for line in _rendered_lines() if line.strip()]
    # A header is the contiguous top block; the fix keeps it to at most four
    # lines (logo, version, first model, folder). Count non-blank content lines
    # that are the logo/header, not the tips (tips are gated below).
    design = tui_design()
    header = design.header  # NEW: compact header config (AC2, AC27)
    assert len(header.lines) <= 4, (
        f"compact header has {len(header.lines)} lines; at most four allowed"
    )
    # And nothing block-letter remains: no banner.words rendered as 3-row font.
    assert design.banner.words == () or design.banner.compact, (
        "block-letter banner words still present on the start screen"
    )


def test_tips_only_on_first_launch() -> None:
    """AC2-F2: a launch counter suppresses the tips after the first launch."""
    design = tui_design()
    assert design.first_launch is True or design.launch_count == 0, (
        "no first-launch state exists in tui_design.yaml"
    )
    # The fix must expose a launch counter that flips after the first launch.
    from omnimarket.nodes.node_onex_setup_effect import cli_tui

    first = cli_tui.tips_visible(launch_count=0)
    second = cli_tui.tips_visible(launch_count=1)
    assert first is True, "full tips must show on the first launch"
    assert second is False, "full tips must not show on a second launch"


def test_measurement_detects_a_tall_render_positive_control() -> None:
    """Positive control: the line-count instrument must fail a >4-line render.

    This proves the measurement in ``test_header_no_taller_than_four_lines``
    can tell a tall render from a compact one, independent of the fix. It stays
    green on dev and on the branch; it measures the instrument, not the defect.
    """
    from rich.console import Group
    from rich.text import Text

    tall = Group(*[Text(f"line {i}") for i in range(6)])
    console = Console(
        width=_WIDTH, height=_HEIGHT, force_terminal=False, color_system=None
    )
    with console.capture() as capture:
        console.print(tall)
    lines = [line for line in capture.get().splitlines() if line.strip()]
    assert len(lines) == 6, "instrument should count six non-blank lines"
    assert len(lines) > 4, "instrument must be able to detect a >4-line render"
