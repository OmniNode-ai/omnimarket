# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26620: /config keeps the providers already set up in their own list.

Operator 2026-11-26, on a /config screenshot: "Added provider should be in
different list and user can change api key, remove it also - redesign it like
a pro", then "give best design so that user feel great". Design: two panes.
Left, "Your providers" above "Add a provider". Right, a panel for the selected
provider of yours, with its status, its models and four actions.

Failure modes:
  Y1  set-up providers sit inside the 260-row add list, so a user cannot see
      at a glance what they have, and a set-up provider is offered for adding.
  Y2  a new user sees an empty "Your providers" box taking room.
  Y3  choosing one of your providers shows nothing about it: no status, no
      models, no way to act on it.
  Y4  changing a key drops the provider's stored models, or puts the key in
      a command line (argv is visible to every process on the machine).
  Y5  there is no way to test a provider again from the TUI.
  Y6  Remove deletes a stored key at once, with no way back from a slip.
  Y7  a confirmed Remove does not run the CLI's own remove.
  Y8  the CLI refusing a remove (the last usable provider) shows a raw error
      or nothing.
  Y9  adding more models to one of your providers is no longer possible
      once it has left the add list.
  Y10 at 80x24 the second list pushes the panel's actions or the footer off
      the screen.
  Y11 the keyboard cannot get from the add list's search up into your
      providers, or back down.
"""

from __future__ import annotations

import json

import pytest
from textual.widgets import Button, Input, RadioButton

from omnimarket.nodes.node_onex_setup_effect.cli_tui import (
    RemoveProviderScreen,
    SetupScreen,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_provider_traits import (
    setup_provider_traits,
)
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui import (
    _app,
    _Runner,
    _selected_rows,
    _type,
)

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

_KEY = "sk-or-v1-new-key-0123456789abcdef"


async def _config(pilot, app) -> SetupScreen:
    await _type(pilot, "/config")
    await pilot.press("enter")
    for _ in range(6):
        await pilot.pause()
    assert isinstance(app.screen, SetupScreen)
    return app.screen


async def _settle(pilot, times: int = 8) -> None:
    for _ in range(times):
        await pilot.pause()


def _mine(screen) -> list[str]:
    """The provider ids "Your providers" lists, top to bottom."""
    listing = screen.query_one("#mine")
    return [str(listing.get_option_at_index(i).id) for i in range(listing.option_count)]


def _addable(screen) -> list[str]:
    return [
        b.name for b in screen.query_one("#provider").query(RadioButton) if b.display
    ]


def _text(widget) -> str:
    return " ".join(str(widget.render()).split())


async def _open_mine(pilot, screen, index: int = 0) -> None:
    listing = screen.query_one("#mine")
    listing.focus()
    listing.highlighted = index
    await _settle(pilot)


def _runner(**extra: str) -> _Runner:
    rows = _selected_rows(connection=False)
    rows.update({tuple(k.split()): v for k, v in extra.items()})
    return _Runner([], by_command=rows)


async def test_your_providers_have_their_own_list() -> None:
    """Y1: openrouter and ollama are set up in the fixture."""
    app = _app(_runner())
    async with app.run_test(size=(140, 40)) as pilot:
        screen = await _config(pilot, app)
        assert _mine(screen) == ["openrouter", "ollama"]
        box = screen.query_one("#mine-box")
        assert box.display
        assert str(box.border_title) == "Your providers"
        assert str(box.border_subtitle) == "2"
        addable = _addable(screen)
        assert "openrouter" not in addable
        picker = [e.provider for e in setup_provider_traits().picker_providers()]
        assert len(addable) == len([p for p in picker if p != "openrouter"])
        assert str(screen.query_one("#provider-box").border_title) == "Add a provider"


async def test_a_new_user_sees_only_the_add_list() -> None:
    """Y2."""
    app = _app(_Runner([]))
    async with app.run_test(size=(140, 40)) as pilot:
        screen = await _config(pilot, app)
        assert not screen.query_one("#mine-box").display
        assert len(_addable(screen)) == len(setup_provider_traits().picker_providers())


async def test_your_provider_shows_its_status_models_and_actions() -> None:
    """Y3."""
    app = _app(_runner())
    async with app.run_test(size=(140, 40)) as pilot:
        screen = await _config(pilot, app)
        await _open_mine(pilot, screen)
        detail = screen.query_one("#detail")
        assert detail.display
        assert not screen.query_one("#keybox").display
        said = " ".join(
            (
                str(detail.border_title),
                _text(screen.query_one("#d-head")),
                _text(screen.query_one("#d-models")),
            )
        )
        assert "OpenRouter" in said, said
        assert "ready" in said, said
        assert "qwen/qwen3.8-27b" in said, said
        labels = [str(b.label) for b in detail.query(Button) if b.display]
        assert labels == ["Change key", "Add models", "Test", "Remove"]


async def test_change_key_keeps_the_models_and_keeps_the_key_off_argv() -> None:
    """Y4."""
    runner = _runner()
    app = _app(runner)
    async with app.run_test(size=(140, 40)) as pilot:
        screen = await _config(pilot, app)
        await _open_mine(pilot, screen)
        screen.query_one("#d-key", Button).press()
        await _settle(pilot)
        field = screen.query_one("#newkey", Input)
        assert field.display
        assert app.focused is field
        await _type(pilot, _KEY)
        await pilot.press("enter")
        await _settle(pilot, 12)
    adds = [c for c in runner.calls if c[:2] == ("models", "add")]
    assert adds == [
        ("models", "add", "openrouter", "--model", "qwen/qwen3.8-27b", "--json")
    ]
    assert f"{_KEY}\n" in runner.stdins
    assert not any(_KEY in part for call in runner.calls for part in call)


async def test_test_runs_one_test_and_says_how_it_went() -> None:
    """Y5."""
    passed = json.dumps(
        {"provider": "openrouter", "status": "passed", "model": "qwen/qwen3.8-27b"}
    )
    runner = _runner(**{"models test": passed})
    app = _app(runner)
    async with app.run_test(size=(140, 40)) as pilot:
        screen = await _config(pilot, app)
        await _open_mine(pilot, screen)
        screen.query_one("#d-test", Button).press()
        await _settle(pilot, 12)
        note = _text(screen.query_one("#note"))
    assert ("models", "test", "openrouter", "--json") in runner.calls
    assert "qwen/qwen3.8-27b ready" in note, note


async def test_remove_asks_first_and_cancel_is_the_default() -> None:
    """Y6: Enter on the question keeps the key."""
    runner = _runner()
    app = _app(runner)
    async with app.run_test(size=(140, 40)) as pilot:
        screen = await _config(pilot, app)
        await _open_mine(pilot, screen)
        screen.query_one("#d-remove", Button).press()
        await _settle(pilot)
        assert isinstance(app.screen, RemoveProviderScreen)
        asked = _text(app.screen.query_one("#rm-question"))
        assert "OpenRouter" in asked, asked
        assert app.focused is not None
        assert app.focused.id == "rm-cancel"
        await pilot.press("enter")
        await _settle(pilot)
        assert app.screen is screen
    assert not [c for c in runner.calls if c[:2] == ("models", "remove")]


async def test_a_confirmed_remove_runs_the_cli_remove() -> None:
    """Y7."""
    runner = _runner()
    app = _app(runner)
    async with app.run_test(size=(140, 40)) as pilot:
        screen = await _config(pilot, app)
        await _open_mine(pilot, screen)
        screen.query_one("#d-remove", Button).press()
        await _settle(pilot)
        app.screen.query_one("#rm-yes", Button).press()
        await _settle(pilot, 12)
        assert app.screen is screen
    assert ("models", "remove", "openrouter") in runner.calls


async def test_a_refused_remove_says_why_in_plain_words() -> None:
    """Y8."""
    refusal = "Error: OpenRouter is your only working provider; add another first"
    runner = _Runner(
        [], code=1, errors=refusal, by_command=_selected_rows(connection=False)
    )
    app = _app(runner)
    async with app.run_test(size=(140, 40)) as pilot:
        screen = await _config(pilot, app)
        await _open_mine(pilot, screen)
        screen.query_one("#d-remove", Button).press()
        await _settle(pilot)
        app.screen.query_one("#rm-yes", Button).press()
        await _settle(pilot, 12)
        note = _text(screen.query_one("#note"))
    assert "only working provider; add another first" in note, note
    assert "Error:" not in note


async def test_add_models_opens_the_add_flow_on_that_provider() -> None:
    """Y9."""
    app = _app(_runner())
    async with app.run_test(size=(140, 40)) as pilot:
        screen = await _config(pilot, app)
        await _open_mine(pilot, screen)
        screen.query_one("#d-add", Button).press()
        await _settle(pilot)
        assert screen.query_one("#provider").value == "openrouter"
        assert screen.query_one("#keybox").display
        assert not screen.query_one("#detail").display
        assert app.focused is screen.query_one("#key", Input)


@pytest.mark.parametrize("size", [(80, 24), (140, 40)])
async def test_both_lists_and_the_actions_fit(size: tuple[int, int]) -> None:
    """Y10."""
    app = _app(_runner())
    async with app.run_test(size=size) as pilot:
        screen = await _config(pilot, app)
        await _open_mine(pilot, screen)
        foot = screen.query_one("#cfg-foot").region
        for widget_id in ("#mine", "#provider", "#d-remove", "#cfg-foot"):
            region = screen.query_one(widget_id).region
            assert region.height > 0, widget_id
            assert region.bottom <= size[1], widget_id
            assert region.right <= size[0], widget_id
        assert screen.query_one("#d-remove").region.bottom <= foot.y
        assert screen.query_one("#provider").region.height >= 2


async def test_the_arrows_move_between_the_two_lists() -> None:
    """Y11: Up from the search reaches your providers' last row; Down leaves it."""
    app = _app(_runner())
    async with app.run_test(size=(140, 40)) as pilot:
        screen = await _config(pilot, app)
        screen.query_one("#provider-search", Input).focus()
        await pilot.pause()
        await pilot.press("up")
        await _settle(pilot, 3)
        listing = screen.query_one("#mine")
        assert app.focused is listing
        await pilot.press("up")
        await pilot.pause()
        assert listing.highlighted == 0
        assert screen.query_one("#detail").display
        await pilot.press("down")
        await pilot.press("down")
        await _settle(pilot, 3)
        assert app.focused is screen.query_one("#provider-search", Input)
        assert screen.query_one("#keybox").display
