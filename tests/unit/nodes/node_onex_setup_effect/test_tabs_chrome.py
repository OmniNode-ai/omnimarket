# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tab bar and chrome shared by the four tabs (OMN-26620 AC12, mockups pages 4 to 7).

Failure modes this suite is written against (enumerated before the code):
  A1  a tab label keeps padding spaces, so the bar reads "  Config    Model".
  A2  the heavy rule runs the whole width, or sits under a tab that is not open.
  A3  the rule is under the right tab but wider or narrower than its label.
  A4  a tab's footer line shows the old keycap list (capital keys, many
      actions) instead of the mockup's lowercase hints with middle dots.
  A5  the keys in the footer are not bold, so the key does not stand out.
  A6  the footer wraps onto a second line at 80x24.
  A7  the "N models" summary line, absent from the mockups, is still drawn.
"""

from __future__ import annotations

import re

import pytest

from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import tui_design
from tests.unit.nodes.node_onex_setup_effect import tabs_redesign_support as sup

pytestmark = pytest.mark.unit

_TABS = ["config", "model", "delegation", "dashboard"]
_BAR = "Config   Model   Delegation   Dashboard"
#: The footer line of each tab, exactly as the approved mockups print it.
_FOOTER = {
    "config": "tab next · space tick · ctrl+s save · esc close",
    "model": "↑↓ move · enter make first · d details · esc close",
    "delegation": "space switch · ctrl+s save · esc close",
    "dashboard": "s start · esc close",
}


def _lines(app: object) -> list[str]:
    return sup.screen_text(app).splitlines()


def _bar_row(lines: list[str]) -> int:
    return next(
        i for i, line in enumerate(lines) if "Config" in line and "Model" in line
    )


def _inner(line: str) -> str:
    """The line without the borders of the form and of the screen behind it."""
    return line.replace("│", " ")


@pytest.mark.parametrize("size", sup.SIZES)
@pytest.mark.parametrize("tab", _TABS)
async def test_tab_labels_have_no_padding(size: tuple[int, int], tab: str) -> None:
    """A1: three plain spaces between labels, labels without padding of their own."""
    app = sup.app()
    async with app.run_test(size=size) as pilot:
        await sup.open_tab(pilot, app, tab)
        lines = _lines(app)
    assert _BAR in _inner(lines[_bar_row(lines)]), lines[_bar_row(lines)]


@pytest.mark.parametrize("size", sup.SIZES)
@pytest.mark.parametrize("tab", _TABS)
async def test_heavy_rule_is_under_the_open_tab_only(
    size: tuple[int, int], tab: str
) -> None:
    """A2, A3: the rule is as wide as the open tab's label and under it."""
    app = sup.app()
    async with app.run_test(size=size) as pilot:
        await sup.open_tab(pilot, app, tab)
        await sup.settle(pilot, 20)  # the rule slides to the tab
        lines = _lines(app)
    row = _bar_row(lines)
    bar, rule = _inner(lines[row]), _inner(lines[row + 1])
    label = next(s.label.strip() for s in tui_design().tabs if s.id == tab)
    start = bar.index(label)
    runs = re.findall("━+", rule)
    assert runs == ["━" * len(label)], rule  # one run, as wide as the label
    assert rule.index("━") == start, (bar, rule)


@pytest.mark.parametrize("size", sup.SIZES)
@pytest.mark.parametrize("tab", _TABS)
async def test_footer_is_the_mockups_hint_line(size: tuple[int, int], tab: str) -> None:
    """A4, A6: one line, lowercase keys, middle-dot separators."""
    app = sup.app()
    async with app.run_test(size=size) as pilot:
        await sup.open_tab(pilot, app, tab)
        lines = [_inner(line) for line in _lines(app)]
    hits = [line for line in lines if _FOOTER[tab] in line]
    assert len(hits) == 1, (_FOOTER[tab], lines)


@pytest.mark.parametrize("tab", _TABS)
async def test_footer_keys_are_bold(tab: str) -> None:
    """A5: each key is drawn bold; the words after it are not."""
    app = sup.app()
    async with app.run_test(size=(140, 40)) as pilot:
        await sup.open_tab(pilot, app, tab)
        bold = sup.bold_words(app)
    keys = [part.strip().split(" ")[0] for part in _FOOTER[tab].split(" · ")]
    for key in keys:
        assert key in bold, (key, sorted(bold))


@pytest.mark.parametrize("tab", _TABS)
async def test_no_models_summary_line_is_drawn(tab: str) -> None:
    """A7: the mockups have no "3 models · first" line under the tabs."""
    app = sup.app()
    async with app.run_test(size=(140, 40)) as pilot:
        await sup.open_tab(pilot, app, tab)
        text = sup.screen_text(app)
    assert "3 models ·" not in text, text
