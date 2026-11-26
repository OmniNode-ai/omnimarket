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
from textual.widgets import Select

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
