# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC2 (OMN-26620): the start screen's header is at most four lines and the
full tips show only on the first launch. The drawn screen is checked at 80x24
and 140x40 in ``test_redesign_start.py``; this file checks the design and the
launch counter they rely on.

Failure modes:
  AC2-F1  the header is declared taller than four lines.
  AC2-F2  the tips reappear on a second launch (no first-launch gating).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from rich.console import Console

from omnimarket.nodes.node_onex_setup_effect import cli_tui
from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import tui_design

pytestmark = pytest.mark.unit


def test_header_no_taller_than_four_lines() -> None:
    """AC2-F1: wordmark and details fit in the declared lines, and render in them."""
    design = tui_design()
    assert len(design.header.lines) <= 4
    console = Console(width=80, force_terminal=False, color_system=None)
    with console.capture() as capture:
        console.print(cli_tui.header_renderable("Last-Model", 78))
    rendered = capture.get().splitlines()
    assert len(rendered) == len(design.header.lines)
    assert all(len(line) <= 80 for line in rendered)


def test_tips_only_on_first_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2-F2: the launch counter flips after the first launch and persists."""
    monkeypatch.setattr(cli_tui.LaunchStore, "_path", tmp_path / "launches")
    first = cli_tui.LaunchStore.bump()
    second = cli_tui.LaunchStore.bump()
    assert cli_tui.tips_visible(first) is True
    assert cli_tui.tips_visible(second) is False


def test_measurement_detects_a_tall_render_positive_control() -> None:
    """Positive control: the line count can tell a tall render from a compact one."""
    from rich.console import Group
    from rich.text import Text

    console = Console(width=140, force_terminal=False, color_system=None)
    with console.capture() as capture:
        console.print(Group(*[Text(f"line {i}") for i in range(6)]))
    assert len([x for x in capture.get().splitlines() if x.strip()]) > 4
