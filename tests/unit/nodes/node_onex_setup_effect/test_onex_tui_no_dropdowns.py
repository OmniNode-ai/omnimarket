# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""No tab hides its choices in a dropdown (OMN-23413).

Operator 2026-11-13: "in tui no dropdown at any places, instead use radio
buttons for visibility". Failure modes:

  D1  a tab still offers a choice through a dropdown (Select).
  D2  /config's providers are not all shown, set-up first and then A-Z
      (operator 2026-11-26), with the first one marked.
"""

from __future__ import annotations

import pytest
from textual.widgets import RadioButton, Select

from omnimarket.nodes.node_onex_setup_effect.models.model_setup_provider_traits import (
    setup_provider_traits,
)
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui import (
    _app,
    _Runner,
    _selected_rows,
    _type,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("tab", ["/config", "/model", "/delegation", "/dashboard"])
async def test_no_tab_has_a_dropdown(tab: str) -> None:
    """D1."""
    app = _app(_Runner([], by_command=_selected_rows(connection=True)))
    async with app.run_test(size=(160, 46)) as pilot:
        await _type(pilot, tab)
        await pilot.press("enter")
        for _ in range(6):
            await pilot.pause()
        assert list(app.screen.query(Select)) == []


async def test_config_shows_every_provider_with_the_first_marked() -> None:
    """D2."""
    app = _app(_Runner([], by_command=_selected_rows(connection=False)))
    async with app.run_test(size=(160, 46)) as pilot:
        await _type(pilot, "/config")
        await pilot.press("enter")
        for _ in range(6):
            await pilot.pause()
        picker = setup_provider_traits().picker_providers()
        # openrouter is the one set up in _selected_rows, so it leads; then
        # the well-known providers, then the listed ones (K5).
        written = {e.provider for e in setup_provider_traits().picker}
        order = sorted(
            picker,
            key=lambda e: (
                e.provider != "openrouter",
                e.provider not in written,
                e.label.casefold(),
            ),
        )
        buttons = list(app.screen.query_one("#provider").query(RadioButton))
        assert [b.name for b in buttons] == [e.provider for e in order]
        assert [str(b.label) for b in buttons] == [
            f"{e.label} ✓" if e.provider == "openrouter" else e.label for e in order
        ]
        # openrouter is in "Your providers"; the add list opens on the first
        # provider not set up yet.
        assert [b.name for b in buttons if b.value] == [order[1].provider]
