# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26688: /config offers OpenCode, written from OpenCode's own docs.

Operator 2026-11-26: "add https://opencode.ai/ as a provider". The shipped
models.dev list already carries it as ``opencode`` (pay as you go, base
https://opencode.ai/inference/openai/v1, which the docs' Base URLs section
gives as /inference plus /openai/v1/chat/completions), so `onex models add
opencode` already stored a key; /config offered it nowhere.

Failure modes, each with a test:
  O1 /config does not offer it, offers it under another name, or puts it after
     the Local Model (the server the user runs stays last).
  O2 the key window for it does not say where the key goes or where to make
     one, or opening it runs a command.
  O3 OpenCode Go is folded into it rather than offered as its own choice
     (operator 2026-11-26: "add both").
"""

from __future__ import annotations

from textual.widgets import Input

from omnimarket.nodes.node_onex_setup_effect import cli_tui
from omnimarket.nodes.node_onex_setup_effect.cli_tui import KeyScreen
from tests.unit.nodes.node_onex_setup_effect.test_config_providers_table import (
    _actions,
    _box_text,
    _config,
    _go_to,
    _make,
    _runner,
    _settle,
)


def test_config_offers_opencode_before_the_local_model() -> None:
    """O1."""
    choices = [e.provider for e in cli_tui._picker()]
    assert "opencode" in choices
    labels = {e.provider: e.label for e in cli_tui._picker()}
    assert labels["opencode"] == "OpenCode"
    assert cli_tui._hosted("opencode")
    assert choices[-1] == "local"


async def test_its_key_window_says_where_the_key_goes_and_where_to_make_one() -> None:
    """O2."""
    runner = _runner()
    app, seen = _make(runner)
    async with app.run_test(size=(160, 40)) as pilot:
        screen = await _config(pilot, app)
        await _go_to(pilot, screen, "opencode")
        await pilot.press("enter")
        await _settle(pilot)
        assert isinstance(app.screen, KeyScreen)
        assert "OpenCode" in str(app.screen.query_one("#ks-box").border_title)
        assert app.focused is app.screen.query_one("#ks-field", Input)
        said = _box_text(app.screen.query_one("#ks-box"))
        assert "opencode.ai" in said, said
        assert "opencode.ai/console" in said, said
        await pilot.press("escape")
        await _settle(pilot)
        assert app.screen is screen
    assert _actions(runner) == []
    assert seen == []


def test_opencode_go_is_its_own_choice() -> None:
    """O3: Go is offered beside it, as its own provider (test_opencode_go)."""
    choices = [e.provider for e in cli_tui._picker()]
    assert choices.index("opencode") < choices.index("opencode-go")
