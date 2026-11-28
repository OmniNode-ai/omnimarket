# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26620 AC2 (D5): the start screen is the approved compact one at every
terminal size: a header of at most four lines, no large welcome, no Setup Guide
or How it works, and the full tips only on the first launch.

Failure modes:
  D5-F1  at a roomy size (120x40, 140x40) the large welcome fills the screen.
  D5-F2  the Setup Guide or the How it works diagram is drawn on the start screen.
  D5-F3  the first-launch tips show again on a second launch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_onex_setup_effect import cli_tui
from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import tui_design
from tests.unit.nodes.node_onex_setup_effect import tabs_redesign_support as sup
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui import _Runner

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _launches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_tui.LaunchStore, "_path", tmp_path / "launches")


async def _start(size: tuple[int, int]) -> tuple[str, int]:
    app = sup.app(_Runner([]))
    async with app.run_test(size=size) as pilot:
        await sup.settle(pilot)
        shown = sup.screen_text(app)
        header = app.query_one("#header")
        summary = app.query_one("#summary")
        assert header.display
        assert summary.display
        assert not app.query_one("#welcome").display
        rows = header.region.height + summary.region.height
        return shown, rows


@pytest.mark.parametrize("size", [(120, 40), (140, 40)])
async def test_the_start_screen_is_the_compact_header(size: tuple[int, int]) -> None:
    shown, rows = await _start(size)
    design = tui_design()
    assert rows <= 4, (rows, shown)
    for words in (
        design.welcome.guide.title,
        design.welcome.diagram.heading,
        design.welcome.headline,
    ):
        assert words not in shown, (words, shown)


@pytest.mark.parametrize("size", [(120, 40), (140, 40)])
async def test_the_tips_show_on_the_first_launch_only(size: tuple[int, int]) -> None:
    tips = tui_design().words["tips_first_launch"]
    first, _ = await _start(size)
    second, _ = await _start(size)
    assert tips in first, first
    assert tips not in second, second
