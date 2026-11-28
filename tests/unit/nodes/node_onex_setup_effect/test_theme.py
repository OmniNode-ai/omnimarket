# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC5 (OMN-26620): /theme switches between onex dark, onex light and the
Textual built-in themes, and the choice is kept for the next launch.

Read on dev (bf09f2968): ``OnexTui.__init__`` registers exactly one theme
(``tui_design().theme.name`` = "onex", dark) and there is no ``/theme`` command
or persisted choice. The approved 05-config-tab mockup adds a theme choice.

Failure modes:
  AC5-F1  the /theme command does not apply a selected registered theme.
  AC5-F2  the chosen theme is not restored on the next launch.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_onex_setup_effect import cli_tui
from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import tui_design

pytestmark = pytest.mark.unit


async def test_theme_applies() -> None:
    """AC5-F1: /theme applies a registered theme, including onex light."""
    design = tui_design()
    assert hasattr(design, "themes"), (
        "AC5/AC27: tui_design.yaml has no themes.* registry"
    )
    assert "onex-light" in design.themes, "no onex light theme token"
    app = cli_tui.OnexTui()
    apply = getattr(cli_tui, "apply_theme", None)
    assert apply is not None, "AC5: no /theme action in cli_tui"
    async with app.run_test() as pilot:
        await pilot.pause()
        registered = set(app.available_themes)
        assert {"onex-dark", "onex-light"} <= registered, (
            f"both onex themes must be registered, got {registered}"
        )
        for builtin in ("textual-dark", "textual-light"):
            assert builtin in registered, (builtin, registered)
        await apply(app, "onex-light")
        await pilot.pause()
        assert str(app.theme) == "onex-light"


async def test_theme_restored_on_restart() -> None:
    """AC5-F2: the chosen theme is persisted and re-applied on the next launch."""
    store = getattr(cli_tui, "ThemeStore", None)
    assert store is not None, "AC5: no persisted theme store"
    assert store.save("onex-light") is True
    assert store._path.read_text() == "onex-light"  # the next start reads the file
    app = cli_tui.OnexTui()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert str(app.theme) == "onex-light", "theme not restored on restart"
