# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26688: /config is one table of providers; models are added on the Model tab.

Operator 2026-11-26: "can you do in config page just Providers and when we add
api key models fetch in model tab?", then "in config just one row and it
should has all the columns", then "when we select model and if that is not
configured then api key enter pop window open".

Failure modes:
  T1  /config shows a model list or more than Provider and Status, or mixes
      yours with the rest instead of two tabs, Configured and Available
      (operator 2026-11-26: "just keep Provider, Status and inside config two
      tabs Configured Available").
  T2  a new user opens on an empty Configured tab, the keys cannot switch
      tabs, or a search with no match in the open tab hides one in the other.
  T3  Enter on a provider with no key does not ask for one in a window, or Esc
      on the window runs something anyway.
  T4  the window does not say whether the key works before it is saved.
  T5  the key is stored with no model named (onex refuses that outside a
      terminal), or the Model tab does not read the provider's models with
      it, or the key reaches a command line.
  T6  one of your providers asks for its key again, or its models are not read
      with the saved key, or the saved key reaches a command line.
  T7  changing a key drops the provider's models or puts the key on argv.
  T8  Test and Remove are gone, run on a provider you do not have, or Remove
      runs without asking.
  T9  on the Model tab, choosing a provider with no key opens no window, or
      choosing one of yours opens one needlessly.
  T10 at 80x24 the table or the footer is cut off.
  T11 the keyboard cannot reach the search from the table, or typing a name
      on the table does not search.
  T13 /config opened before the setup is read stays on Available once it
      lands, though providers are configured (seen on a real run, 2026-11-26).
"""

from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import SecretStr
from textual.widgets import Button, DataTable, Input

from omnimarket.handlers.model_catalog import ModelsDevIndex
from omnimarket.nodes.node_onex_setup_effect import cli_tui
from omnimarket.nodes.node_onex_setup_effect.cli_tui import (
    KeyScreen,
    ModelPickScreen,
    OnexTui,
    ProvidersScreen,
    RemoveProviderScreen,
    SetupScreen,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_provider_traits import (
    setup_provider_traits,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import tui_design
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui import (
    _actions,
    _hosted_listing,
    _root,
    _Runner,
    _selected_rows,
    _status,
    _tick,
    _type,
)

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

_KEY = "sk-test-new-key-0123456789abcdef"
_SAVED = "sk-or-v1-saved-key-0123456789abcd"

_MODELS = {
    ("deepseek", "deepseek-chat"): {
        "name": "DeepSeek Chat",
        "release_date": "2026-03-01",
        "cost": {"input": 0.27, "output": 1.1},
    },
    ("deepseek", "deepseek-reasoner"): {
        "name": "DeepSeek Reasoner",
        "release_date": "2026-09-15",
        "cost": {"input": 0.55, "output": 2.19},
    },
    ("deepseek", "deepseek-coder"): {
        "name": "DeepSeek Coder",
        "release_date": "2025-06-01",
        "cost": {"input": 0.14, "output": 0.28},
    },
}


@pytest.fixture(autouse=True)
def _index(monkeypatch: pytest.MonkeyPatch) -> None:
    index = ModelsDevIndex(
        models=_MODELS, fetched_at="2026-11-26T00:00:00+00:00", stale=False, error=None
    )
    monkeypatch.setattr(cli_tui, "_default_index", lambda: index)
    monkeypatch.setattr(cli_tui, "_default_read_server", lambda _base, _models: {})


def _runner(**extra: str) -> _Runner:
    rows = _selected_rows(connection=False)
    rows.update({tuple(k.split()): v for k, v in extra.items()})
    return _Runner([], by_command=rows)


def _saved_key(provider: str) -> SecretStr | None:
    """openrouter is set up in _selected_rows, so its key is saved."""
    return SecretStr(_SAVED) if provider == "openrouter" else None


def _make(runner: _Runner, list_models=None):
    seen: list[tuple[str, str]] = []
    if list_models is None:
        list_models, seen = _hosted_listing()
    app = OnexTui(
        root=_root,
        runner=runner,
        load_status=_status,
        list_models=list_models,
        stored_key=_saved_key,
    )
    return app, seen


async def _settle(pilot, times: int = 10) -> None:
    for _ in range(times):
        await pilot.pause()


async def _wait(pilot) -> None:
    """A key or URL is read once it stops changing."""
    await pilot.pause(tui_design().auto_find_seconds + 0.4)
    await _settle(pilot)


async def _config(pilot, app) -> ProvidersScreen:
    await _type(pilot, "/config")
    await pilot.press("enter")
    await _settle(pilot)
    assert isinstance(app.screen, ProvidersScreen)
    return app.screen


def _table(screen) -> DataTable:
    return screen.query_one("#providers", DataTable)


def _rows(screen) -> list[str]:
    return [str(key.value) for key in _table(screen).rows]


def _cells(screen, provider: str) -> list[str]:
    return [str(cell) for cell in _table(screen).get_row(provider)]


def _text(widget) -> str:
    return " ".join(str(widget.render()).split())


async def _go_to(pilot, screen, provider: str) -> None:
    table = _table(screen)
    table.focus()
    if provider not in _rows(screen):  # it is in the other tab
        await pilot.press("right" if screen.view == "configured" else "left")
        await _settle(pilot, 3)
    table.move_cursor(row=table.get_row_index(provider))
    await _settle(pilot, 3)


def _adds(runner: _Runner) -> list[tuple[str, ...]]:
    return [
        c for c in runner.calls if c[:2] in (("models", "add"), ("models", "add-model"))
    ]


async def test_config_is_two_tabs_of_provider_and_status() -> None:
    """T1: yours under Configured, the rest under Available."""
    app, _ = _make(_runner())
    async with app.run_test(size=(160, 40)) as pilot:
        screen = await _config(pilot, app)
        table = _table(screen)
        labels = [str(column.label) for column in table.columns.values()]
        assert labels == ["Provider", "Status"]
        assert screen.view == "configured"
        assert _rows(screen) == ["openrouter", "ollama"]
        assert "ready" in _cells(screen, "openrouter")[1]
        picker = {e.provider for e in setup_provider_traits().picker_providers()}
        tabs = [str(t.label) for t in screen.query("#views Tab")]
        assert tabs == ["Configured 2", f"Available {len(picker - {'openrouter'})}"]
        table.focus()
        await pilot.press("right")
        await _settle(pilot, 3)
        assert screen.view == "available"
        rows = _rows(screen)
        assert set(rows) == picker - {"openrouter", "ollama"}
        assert "not set up" in _cells(screen, "deepseek")[1]
        assert not screen.query("#found")
        assert not screen.query("RadioSet")
        await pilot.press("left")
        await _settle(pilot, 3)
        assert screen.view == "configured"


async def test_a_new_user_opens_on_available_and_search_finds_either_tab() -> None:
    """T2."""
    app, _ = _make(_Runner([]))
    async with app.run_test(size=(160, 40)) as pilot:
        screen = await _config(pilot, app)
        assert screen.view == "available"
        assert "deepseek" in _rows(screen)
    app, _ = _make(_runner())
    async with app.run_test(size=(160, 40)) as pilot:
        screen = await _config(pilot, app)
        assert screen.view == "configured"
        screen.query_one("#provider-search", Input).value = "deeps"
        await _settle(pilot, 3)
        assert screen.view == "available"
        assert _rows(screen) == ["deepseek"]


async def test_enter_on_a_provider_with_no_key_asks_in_a_window() -> None:
    """T3."""
    runner = _runner()
    app, seen = _make(runner)
    async with app.run_test(size=(160, 40)) as pilot:
        screen = await _config(pilot, app)
        await _go_to(pilot, screen, "deepseek")
        await pilot.press("enter")
        await _settle(pilot)
        assert isinstance(app.screen, KeyScreen)
        assert "DeepSeek" in str(app.screen.query_one("#ks-box").border_title)
        assert app.focused is app.screen.query_one("#ks-field", Input)
        said = _text(app.screen.query_one("#ks-models"))
        assert "3 models" in said, said
        await pilot.press("escape")
        await _settle(pilot)
        assert app.screen is screen
    assert _actions(runner) == []
    assert seen == []


async def test_the_window_says_whether_the_key_works() -> None:
    """T4: valid with a count; a refused key says why."""
    app, _ = _make(_runner())
    async with app.run_test(size=(160, 40)) as pilot:
        screen = await _config(pilot, app)
        await _go_to(pilot, screen, "deepseek")
        await pilot.press("enter")
        await _settle(pilot)
        await _type(pilot, _KEY)
        await _wait(pilot)
        window = app.screen
        assert "valid" in _text(window.query_one("#ks-mark"))
        assert "2 models" in _text(window.query_one("#ks-check"))
    refused, _ = _hosted_listing(
        outcome="rejected", message="DeepSeek refused this key"
    )
    app, _ = _make(_runner(), list_models=refused)
    async with app.run_test(size=(160, 40)) as pilot:
        screen = await _config(pilot, app)
        await _go_to(pilot, screen, "deepseek")
        await pilot.press("enter")
        await _settle(pilot)
        await _type(pilot, _KEY)
        await _wait(pilot)
        window = app.screen
        assert "not accepted" in _text(window.query_one("#ks-mark"))
        assert "DeepSeek refused this key" in _text(window.query_one("#ks-check"))


async def test_a_saved_key_opens_its_models_on_the_model_tab() -> None:
    """T5: nothing is stored until models are ticked; the key goes on stdin."""
    runner = _runner()
    app, seen = _make(runner)
    async with app.run_test(size=(160, 40)) as pilot:
        screen = await _config(pilot, app)
        await _go_to(pilot, screen, "deepseek")
        await pilot.press("enter")
        await _settle(pilot)
        await _type(pilot, _KEY)
        await pilot.press("enter")
        await _settle(pilot)
        adding = app.screen
        assert isinstance(adding, SetupScreen)
        assert adding.TAB == "model"
        assert adding.query_one("#provider").value == "deepseek"
        before = len(seen)
        await _wait(pilot)
        assert seen[before:] == [("deepseek", _KEY)]
        assert _adds(runner) == []
        _tick(app, "qwen/qwen3.8-27b")
        await pilot.press("ctrl+s")
        await _settle(pilot, 14)
    assert _adds(runner) == [
        ("models", "add", "deepseek", "--model", "qwen/qwen3.8-27b", "--json")
    ]
    assert f"{_KEY}\n" in runner.stdins
    assert not any(_KEY in part for call in runner.calls for part in call)


async def test_your_provider_reads_its_models_with_the_saved_key() -> None:
    """T6: no window; the saved key lists the models and stays off argv."""
    runner = _runner()
    app, seen = _make(runner)
    async with app.run_test(size=(160, 40)) as pilot:
        screen = await _config(pilot, app)
        await _go_to(pilot, screen, "openrouter")
        await pilot.press("enter")
        await _settle(pilot)
        assert not isinstance(app.screen, KeyScreen)
        assert isinstance(app.screen, SetupScreen)
        await _wait(pilot)
        assert seen == [("openrouter", _SAVED)]
        _tick(app, "openrouter/free")
        await pilot.press("ctrl+s")
        await _settle(pilot, 14)
    assert _adds(runner) == [
        ("models", "add-model", "openrouter", "--model", "openrouter/free")
    ]
    assert not any(_SAVED in (stdin or "") for stdin in runner.stdins)
    assert not any(_SAVED in part for call in runner.calls for part in call)


async def test_changing_a_key_keeps_the_models_and_keeps_the_key_off_argv() -> None:
    """T7."""
    runner = _runner()
    app, _ = _make(runner)
    async with app.run_test(size=(160, 40)) as pilot:
        screen = await _config(pilot, app)
        await _go_to(pilot, screen, "openrouter")
        await pilot.press("k")
        await _settle(pilot)
        assert isinstance(app.screen, KeyScreen)
        await _type(pilot, _KEY)
        await pilot.press("enter")
        await _settle(pilot, 14)
        assert app.screen is screen
    assert _adds(runner) == [
        ("models", "add", "openrouter", "--model", "qwen/qwen3.8-27b", "--json")
    ]
    assert f"{_KEY}\n" in runner.stdins
    assert not any(_KEY in part for call in runner.calls for part in call)


async def test_test_and_remove_act_on_your_provider_and_remove_asks() -> None:
    """T8: t and x do nothing on a provider you do not have."""
    passed = json.dumps(
        {"provider": "openrouter", "status": "passed", "model": "qwen/qwen3.8-27b"}
    )
    runner = _runner(**{"models test": passed})
    app, _ = _make(runner)
    async with app.run_test(size=(160, 40)) as pilot:
        screen = await _config(pilot, app)
        await _go_to(pilot, screen, "deepseek")
        await pilot.press("t")
        await pilot.press("x")
        await _settle(pilot)
        assert app.screen is screen
        assert _actions(runner) == []
        await _go_to(pilot, screen, "openrouter")
        await pilot.press("t")
        await _settle(pilot, 12)
        assert ("models", "test", "openrouter", "--json") in runner.calls
        assert "qwen/qwen3.8-27b ready" in _text(screen.query_one("#note"))
        await pilot.press("x")
        await _settle(pilot)
        assert isinstance(app.screen, RemoveProviderScreen)
        assert app.focused is not None
        assert app.focused.id == "rm-cancel"
        await pilot.press("enter")
        await _settle(pilot)
        assert app.screen is screen
        assert not [c for c in runner.calls if c[:2] == ("models", "remove")]
        await pilot.press("x")
        await _settle(pilot)
        app.screen.query_one("#rm-yes", Button).press()
        await _settle(pilot, 12)
    assert ("models", "remove", "openrouter") in runner.calls


async def test_plus_on_the_model_tab_opens_the_providers() -> None:
    """T9: providers are chosen on Config; + takes the Model tab there."""
    app, _ = _make(_runner())
    async with app.run_test(size=(160, 40)) as pilot:
        await _type(pilot, "/model")
        await pilot.press("enter")
        await _settle(pilot)
        await pilot.press("plus")
        await _settle(pilot)
        assert isinstance(app.screen, ProvidersScreen)


async def test_the_models_screen_is_one_providers_models_only() -> None:
    """T12 (operator 2026-11-26, "after enter api why this"): after the key,
    the Model tab shows that provider's models and nothing else; its top line
    names the provider, the key's state and the count; Esc goes back to the
    providers storing nothing; Save goes on to your model list."""
    runner = _runner()
    app, _ = _make(runner)
    async with app.run_test(size=(160, 40)) as pilot:
        screen = await _config(pilot, app)
        await _go_to(pilot, screen, "deepseek")
        await pilot.press("enter")
        await _settle(pilot)
        await _type(pilot, _KEY)
        await pilot.press("enter")
        await _settle(pilot)
        await _wait(pilot)
        adding = app.screen
        assert isinstance(adding, SetupScreen)
        for hidden in ("#provider", "#key", "#url"):
            assert adding.query_one(hidden).region.area == 0, hidden
        assert not adding.query("#provider-search")
        title = _text(adding.query_one("#cfg-title"))
        assert "DeepSeek" in title, title
        assert "valid" in title, title
        assert "2 models" in title, title
        assert "step" not in _text(adding.query_one("#cfg-step"))
        await pilot.press("escape")
        await _settle(pilot)
        assert isinstance(app.screen, ProvidersScreen)
        assert _adds(runner) == []
        await _go_to(pilot, app.screen, "deepseek")
        await pilot.press("enter")
        await _settle(pilot)
        await _type(pilot, _KEY)
        await pilot.press("enter")
        await _settle(pilot)
        await _wait(pilot)
        await pilot.press("space")  # the list has the keyboard once it loads
        await _settle(pilot, 3)
        assert "1 ticked" in _text(app.screen.query_one("#cfg-step"))
        await pilot.press("ctrl+s")
        await _settle(pilot, 20)
        assert isinstance(app.screen, ModelPickScreen)
    assert len(_adds(runner)) == 1


@pytest.mark.parametrize("size", [(80, 24), (160, 40)])
async def test_the_table_and_footer_fit(size: tuple[int, int]) -> None:
    """T10."""
    app, _ = _make(_runner())
    async with app.run_test(size=size) as pilot:
        screen = await _config(pilot, app)
        table = _table(screen).region
        foot = screen.query_one("#cfg-foot").region
        assert table.height >= 3
        assert table.bottom <= foot.y
        assert foot.bottom <= size[1]
        assert foot.right <= size[0]


async def test_typing_on_the_table_searches_and_up_reaches_the_search() -> None:
    """T11."""
    app, _ = _make(_runner())
    async with app.run_test(size=(160, 40)) as pilot:
        screen = await _config(pilot, app)
        _table(screen).focus()
        await pilot.press("d", "e", "e", "p")
        await _settle(pilot, 3)
        search = screen.query_one("#provider-search", Input)
        assert app.focused is search
        assert search.value == "deep"
        rows = _rows(screen)
        assert "deepseek" in rows
        assert "openrouter" not in rows
        search.value = ""
        await _settle(pilot, 3)
        table = _table(screen)
        table.focus()
        table.move_cursor(row=0)
        await pilot.press("up")
        await _settle(pilot, 3)
        assert app.focused is search


class _SlowSetupRead(_Runner):
    """The setup read answers only when the test lets it, as a real CLI that
    takes seconds to start does."""

    def __init__(self) -> None:
        super().__init__([], by_command=_selected_rows(connection=False))
        self.release = asyncio.Event()

    async def capture(self, argv, stdin=None):
        if argv[:2] == ("models", "list"):
            await self.release.wait()
        return await super().capture(argv, stdin)


async def test_config_opened_before_the_setup_read_lands_on_configured() -> None:
    """T13."""
    runner = _SlowSetupRead()
    app, _ = _make(runner)
    async with app.run_test(size=(160, 40)) as pilot:
        screen = await _config(pilot, app)
        assert screen.view == "available"  # nothing read yet
        runner.release.set()
        await _settle(pilot, 20)
        assert screen.view == "configured"
        assert _rows(screen) == ["openrouter", "ollama"]
