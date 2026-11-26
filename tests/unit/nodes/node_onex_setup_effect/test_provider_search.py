# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26620 AC14: /config's providers are a searchable A-Z list, set-up first.

Operator 2026-11-26: "go alphabetically ... config page needs to redesign -
check how others doin it?", then chose "Search + A-Z, set-up first". The list
stays radio buttons (operator 2026-11-13: "in tui no dropdown at any places").
It is read from setup_provider_traits.yaml, so the providers OMN-26688 adds
join it with no TUI change.

Failure modes:
  S1  the list keeps the file's order, so a provider is hard to find and the
      ones already set up are buried.
  S2  there is no way to narrow the list by typing, which hundreds of
      providers will need.
  S3  Enter in the search box does nothing, so the user has to reach for the
      arrows to pick the one match.
  S4  a provider added to the data does not show, or shows out of order,
      until code changes.
  S5  a long list pushes the key box and the models off the screen, at
      80x24 or 140x40.
  S6  narrowing the list silently switches the provider the user picked.
  S7  the search line cannot be reached by keyboard: Tab changes tab, and
      the arrow keys inside the list only move between providers (found in
      a real terminal on 2026-11-26).
  S8  the arrow keys cannot leave the search line for the list.
  S9  once a server's models load, the model list holds the keyboard and Up
      never leaves it, so the providers cannot be reached (real terminal,
      80x24, 2026-11-26: eight Up presses, focus stayed on the list).
  S10 a brand-new user (verify pass 2, 2026-11-26, real onex, empty home):
      /config opens on the search line, but Up there moved the keyboard
      past the top to the Guided setup button, so the name typed next went
      nowhere and Enter left the TUI for the full terminal onboarding.
  S11 when guided setup stops early the TUI said "Guided setup exited with
      code 1", which tells the user nothing they can act on.
"""

from __future__ import annotations

import json

import pytest
from textual.widgets import Input, RadioButton

from omnimarket.nodes.node_onex_setup_effect import cli_tui
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_provider_traits import (
    ModelSetupPickerProvider,
    setup_provider_traits,
)
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui import (
    _app,
    _open_config,
    _Runner,
    _selected_rows,
    _type,
    _wait_for_models,
)

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


async def _config(pilot, app) -> None:
    await _type(pilot, "/config")
    await pilot.press("enter")
    for _ in range(6):
        await pilot.pause()


def _shown(app) -> list[str]:
    """The provider ids the list shows, top to bottom."""
    return [
        b.name
        for b in app.screen.query_one("#provider").query(RadioButton)
        if b.display
    ]


def _a_to_z(entries) -> list[str]:
    return [e.provider for e in sorted(entries, key=lambda e: e.label.casefold())]


def _with_providers(monkeypatch: pytest.MonkeyPatch, extra) -> tuple:
    base = setup_provider_traits()
    traits = base.model_copy(update={"picker": (*base.picker, *extra)})
    monkeypatch.setattr(cli_tui, "setup_provider_traits", lambda: traits)
    return traits.picker


async def test_set_up_providers_come_first_then_a_to_z() -> None:
    """S1: openrouter is set up in the fixture, so it leads; the rest are A-Z."""
    app = _app(_Runner([], by_command=_selected_rows(connection=False)))
    async with app.run_test(size=(160, 46)) as pilot:
        await _config(pilot, app)
        rest = [p for p in _a_to_z(setup_provider_traits().picker) if p != "openrouter"]
        assert _shown(app) == ["openrouter", *rest]


async def test_typing_narrows_the_providers() -> None:
    """S2: typing keeps only the providers whose name holds the text."""
    app = _app(_Runner([], by_command=_selected_rows(connection=False)))
    async with app.run_test(size=(160, 46)) as pilot:
        await _config(pilot, app)
        app.screen.query_one("#provider-search", Input).focus()
        await _type(pilot, "open")
        await pilot.pause()
        assert sorted(_shown(app)) == ["openai", "openrouter"]
        for _ in range(4):
            await pilot.press("backspace")
        await pilot.pause()
        assert len(_shown(app)) == len(setup_provider_traits().picker)


async def test_enter_in_the_search_picks_the_first_match() -> None:
    """S3: Enter marks the first match and moves to its key box."""
    app = _app(_Runner([], by_command=_selected_rows(connection=False)))
    async with app.run_test(size=(160, 46)) as pilot:
        await _config(pilot, app)
        app.screen.query_one("#provider-search", Input).focus()
        await _type(pilot, "anthro")
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen.query_one("#provider").value == "anthropic"
        assert app.focused is app.screen.query_one("#key", Input)


async def test_an_added_provider_row_shows_with_no_code_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S4: a row added to the provider data takes its A-Z place."""
    picker = _with_providers(
        monkeypatch,
        [ModelSetupPickerProvider(provider="groq", label="Groq", hint="Create a key.")],
    )
    app = _app(_Runner([], by_command=_selected_rows(connection=False)))
    async with app.run_test(size=(160, 46)) as pilot:
        await _config(pilot, app)
        rest = [p for p in _a_to_z(picker) if p != "openrouter"]
        assert _shown(app) == ["openrouter", *rest]
        assert "groq" in _shown(app)


@pytest.mark.parametrize("size", [(80, 24), (140, 40)])
async def test_a_long_list_keeps_the_key_box_on_screen(
    monkeypatch: pytest.MonkeyPatch, size: tuple[int, int]
) -> None:
    """S5: forty providers scroll inside their box; the key box and search stay."""
    _with_providers(
        monkeypatch,
        [
            ModelSetupPickerProvider(provider=f"p{i:02d}", label=f"Provider {i:02d}")
            for i in range(40)
        ],
    )
    app = _app(_Runner([], by_command=_selected_rows(connection=False)))
    async with app.run_test(size=size) as pilot:
        await _config(pilot, app)
        screen = app.screen
        height = size[1]
        for widget_id in ("#provider-search", "#keybox", "#cfg-foot"):
            region = screen.query_one(widget_id).region
            assert region.height > 0, widget_id
            assert region.bottom <= height, widget_id
        box = screen.query_one("#provider-box").region
        assert box.height < height // 2


async def test_narrowing_never_switches_the_marked_provider() -> None:
    """S6: a filter that hides the marked provider leaves it marked."""
    app = _app(_Runner([], by_command=_selected_rows(connection=False)))
    async with app.run_test(size=(160, 46)) as pilot:
        await _config(pilot, app)
        screen = app.screen
        screen.query_one("#provider").value = "gemini"
        await pilot.pause()
        screen.query_one("#provider-search", Input).focus()
        await _type(pilot, "open")
        await pilot.pause()
        assert screen.query_one("#provider").value == "gemini"


async def _focus_by_keys(pilot, app, widget_id: str, key: str = "up") -> None:
    """Press ``key`` until ``widget_id`` has focus, as a user would."""
    for _ in range(20):
        if app.focused is not None and app.focused.id == widget_id:
            return
        await pilot.press(key)
    raise AssertionError(f"#{widget_id} is not reachable with {key}")


@pytest.mark.parametrize("size", [(80, 24), (140, 40)])
async def test_typing_on_the_list_moves_to_the_search(size: tuple[int, int]) -> None:
    """S7: reach the list with the arrows, type, and the search line has it.
    /config opens on the search line, so Down is the way into the list."""
    app = _app(_Runner([], by_command=_selected_rows(connection=False)))
    async with app.run_test(size=size) as pilot:
        await _config(pilot, app)
        await _focus_by_keys(pilot, app, "provider", key="down")
        await _type(pilot, "anthro")
        await pilot.pause()
        screen = app.screen
        search = screen.query_one("#provider-search", Input)
        assert app.focused is search
        assert search.value == "anthro"
        assert _shown(app) == ["anthropic"]
        await pilot.press("enter")
        await pilot.pause()
        assert screen.query_one("#provider").value == "anthropic"
        assert app.focused is screen.query_one("#key", Input)


async def test_down_from_the_search_goes_into_the_list() -> None:
    """S8: Down leaves the search line for the provider list."""
    app = _app(_Runner([], by_command=_selected_rows(connection=False)))
    async with app.run_test(size=(80, 24)) as pilot:
        await _config(pilot, app)
        app.screen.query_one("#provider-search", Input).focus()
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == "provider"


@pytest.mark.parametrize("size", [(80, 24), (140, 40)])
async def test_up_from_the_loaded_models_reaches_the_providers(
    size: tuple[int, int],
) -> None:
    """S9: Up leaves the model list from its first row; typing then searches."""
    found = json.dumps({"status": "ready", "models": ["llama3.2", "qwen3:8b"]})
    app = _app(_Runner([], by_command={("models", "discover"): found}))
    async with app.run_test(size=size) as pilot:
        await _open_config(pilot, app)
        await _wait_for_models(pilot)
        assert app.focused is not None
        assert app.focused.id == "mt-rows"  # the list takes the keyboard on load
        await _focus_by_keys(pilot, app, "provider")
        await _type(pilot, "anthro")
        await pilot.pause()
        assert app.screen.query_one("#provider-search", Input).value == "anthro"
        assert _shown(app) == ["anthropic"]


class _NewUser(_Runner):
    """No model is set up; guided setup stops early when started."""

    def __init__(self, setup_code: int = 0) -> None:
        super().__init__([])
        self._setup_code = setup_code

    def in_terminal(self, argv: tuple[str, ...]) -> int:
        self.terminal_calls.append(argv)
        return self._setup_code


@pytest.mark.parametrize("size", [(80, 24), (140, 40)])
async def test_a_new_user_types_a_provider_name_as_config_opens(
    size: tuple[int, int],
) -> None:
    """S10."""
    runner = _NewUser()
    app = _app(runner)
    async with app.run_test(size=size) as pilot:
        await _config(pilot, app)
        assert app.focused is not None
        assert app.focused.id == "provider-search", app.focused
        for _ in range(4):  # as the real run pressed it
            await pilot.press("up")
            await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == "provider-search", app.focused
        await _type(pilot, "anthro")
        await pilot.pause()
        assert app.screen.query_one("#provider-search", Input).value == "anthro"
        await pilot.press("enter")
        await pilot.pause()
        assert runner.terminal_calls == []  # Enter did not start guided setup
        assert app.screen.query_one("#provider").value == "anthropic"
        assert app.focused is app.screen.query_one("#key", Input)


async def test_guided_setup_that_stops_early_says_so_plainly() -> None:
    """S11."""
    runner = _NewUser(setup_code=1)
    app = _app(runner)
    async with app.run_test(size=(80, 24)) as pilot:
        await _config(pilot, app)
        await pilot.press("ctrl+g")
        for _ in range(6):
            await pilot.pause()
        assert runner.terminal_calls == [("setup",)]
        said = str(app.screen.query_one("#note").render())
        assert "did not finish" in said, said
        assert "code" not in said, said
