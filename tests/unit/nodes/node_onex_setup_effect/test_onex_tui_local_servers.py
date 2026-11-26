# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The TUI shows each local server's model as its own row (OMN-23413).

Two lab hosts serve the same model (vLLM on one, llama.cpp on the other).
Failure modes, each caught below:

  FM1  /model, /config and /delegation crashed on open: both rows had the key
       ``local\\tQwen3.8-27B`` and the table refused the second (DuplicateKey).
  FM10 both rows showed the provider's one test, not their own server's.
  FM11 picking the second server's row sent ``models first local MODEL`` with no
       URL, so the CLI could not tell which server was meant.
  FM12 the "Now:" line could not say which server is tried first.
"""

from __future__ import annotations

import json

import pytest
from textual.widgets import DataTable, Static

from omnimarket.models.model_local_server_details import ModelLocalServerDetails
from omnimarket.nodes.node_onex_setup_effect.cli_model_table import entry_key
from omnimarket.nodes.node_onex_setup_effect.cli_tui import (
    DelegationScreen,
    ModelPickScreen,
    OnexTui,
)
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui import (
    _root,
    _Runner,
    _status,
    _type,
    _wait_for_models,
)

pytestmark = pytest.mark.unit

_MODEL = "Qwen3.8-27B"
_VLLM = "http://vllm.test:8000"
_LLAMA = "http://llama.test:8000"
_OLLAMA = "http://ollama.test:11434"


def _rows(first_url: str | None = None) -> str:
    """`models list --json` for the lab: the model on two servers, one other model."""
    passed = {
        "provider": "local",
        "status": "passed",
        "tested_at": "2026-11-13T00:00:00+00:00",
        "backend_id": "local-url",
        "model": _MODEL,
    }
    return json.dumps(
        {
            "provider": "local",
            "set_up": True,
            "models": [_MODEL, "saluki:latest", _MODEL],
            "tried_first": first_url is not None,
            "first_model": _MODEL if first_url else None,
            "first_url": first_url,
            "last_test": passed,
            "local_models": [
                {
                    "model": _MODEL,
                    "base_url": _VLLM,
                    "backend_id": "local-url",
                    "last_test": passed,
                },
                {
                    "model": "saluki:latest",
                    "base_url": _OLLAMA,
                    "backend_id": "local-url-2",
                },
                {"model": _MODEL, "base_url": _LLAMA, "backend_id": "local-url-3"},
            ],
        }
    )


def _read_server(base: str, models: tuple[str, ...]):
    kind = {_VLLM: "vLLM", _LLAMA: "llama.cpp", _OLLAMA: "Ollama"}[base]
    return {m: ModelLocalServerDetails(server=kind) for m in models}


def _app(runner: _Runner) -> OnexTui:
    return OnexTui(
        root=_root, runner=runner, load_status=_status, read_server=_read_server
    )


async def _open(pilot, command: str) -> None:
    await _type(pilot, command)
    await pilot.press("enter")
    await pilot.pause()
    await pilot.pause()


async def test_model_opens_with_one_row_per_server_and_each_its_own_test() -> None:
    """FM1, FM10."""
    runner = _Runner([], by_command={("models", "list"): _rows()})
    app = _app(runner)
    async with app.run_test(size=(160, 40)) as pilot:
        await _open(pilot, "/model")
        assert isinstance(app.screen, ModelPickScreen)
        table = app.screen.query_one("#models")
        shown = {
            (e.provider_label, e.model_id, e.server_url, e.status)
            for e in table.entries
        }
        assert shown == {
            ("Local Model · vLLM", _MODEL, _VLLM, "ready"),
            ("Local Model · llama.cpp", _MODEL, _LLAMA, "untested"),
            ("Local Model · Ollama", "saluki:latest", _OLLAMA, "untested"),
        }
        assert table.query_one(DataTable).row_count == 3


async def test_config_and_delegation_open_with_two_servers_of_one_model() -> None:
    """FM1 on the other two screens that show the same table."""
    runner = _Runner(
        [],
        by_command={
            ("models", "list"): _rows(),
            ("models", "connections"): json.dumps({"connections": []}),
        },
    )
    app = _app(runner)
    async with app.run_test(size=(160, 44)) as pilot:
        await _open(pilot, "/delegation")
        assert isinstance(app.screen, DelegationScreen)
        assert app.screen.query_one("#selected").query_one(DataTable).row_count == 3


async def test_picking_the_second_server_sends_its_url() -> None:
    """FM11."""
    runner = _Runner([], by_command={("models", "list"): _rows()})
    app = _app(runner)
    async with app.run_test(size=(160, 40)) as pilot:
        await _open(pilot, "/model")
        table = app.screen.query_one("#models")
        llama = next(e for e in table.entries if e.server_url == _LLAMA)
        grid = table.query_one(DataTable)
        grid.move_cursor(row=grid.get_row_index(entry_key(llama)))
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        firsts = [c for c in runner.calls if c[:2] == ("models", "first")]
        assert firsts == [("models", "first", "local", _MODEL, "--url", _LLAMA)]


async def test_the_now_line_names_the_server_tried_first() -> None:
    """FM12."""
    runner = _Runner([], by_command={("models", "list"): _rows(first_url=_LLAMA)})
    app = _app(runner)
    async with app.run_test(size=(160, 40)) as pilot:
        await _open(pilot, "/model")
        now = str(app.screen.query_one("#pnow", Static).render())
        assert _MODEL in now
        assert "llama.test:8000" in now
        assert "vllm.test" not in now


# The screens must say only what is true (OMN-23413, operator 2026-11-13):
#   S1  the status bar named a model while Auto picks per task, and said "model
#       not set" before it had read anything.
#   S2  /config said "Selected: none yet" with three models set up, and offered
#       127.0.0.1:11434 instead of the user's own server.
#   S3  the /delegation table named a model two servers run without saying
#       which server.
#   S4  a delegated task's answering model replaced the model shown as first.


async def test_the_status_bar_says_auto_and_how_many_models_when_none_is_first() -> (
    None
):
    """S1."""
    from omnimarket.nodes.node_onex_setup_effect.cli_tui import ModelTuiStatus

    async def auto() -> ModelTuiStatus:
        return ModelTuiStatus(models=3)

    app = OnexTui(root=_root, runner=_Runner([]), load_status=auto)
    async with app.run_test() as pilot:
        await pilot.pause()
        status = str(app.query_one("#status").render())
        assert "Auto · 3 models" in status
        assert "first" not in status


async def test_the_status_bar_says_reading_until_the_setup_is_read() -> None:
    """S1: before the first read, only the saved state shows (OMN-26620 AC7)."""
    import asyncio

    from omnimarket.nodes.node_onex_setup_effect.cli_tui import ModelTuiStatus
    from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import (
        tui_design,
    )

    gate = asyncio.Event()

    async def slow() -> ModelTuiStatus:
        await gate.wait()
        return ModelTuiStatus(models=0)

    from omnimarket.nodes.node_onex_setup_effect.cli_tui import StatusStore

    design = tui_design()
    StatusStore.save(ModelTuiStatus(model="Saved-Model", delegation="off"))
    saved = {"model": "Saved-Model"}
    app = OnexTui(root=_root, runner=_Runner([]), load_status=slow)
    async with app.run_test() as pilot:
        await pilot.pause()
        status = str(app.query_one("#status").render())
        # AC7: the saved state seeds the bar at once, never "reading your setup…".
        assert design.words["status_reading"] not in status
        assert str(saved.get("model")) in status
        assert (
            design.words["delegation_off"] in status
            or design.words["delegation_hook"] in status
        )
        assert "not set" not in status
        gate.set()
        await pilot.pause()
        await pilot.pause()
        assert "no model set up" in str(app.query_one("#status").render())


async def test_the_status_bar_names_the_server_of_the_first_model() -> None:
    """S1 with a first model on a server."""
    from omnimarket.nodes.node_onex_setup_effect.cli_tui import ModelTuiStatus

    async def first() -> ModelTuiStatus:
        return ModelTuiStatus(model=_MODEL, first_url=_LLAMA, models=3)

    app = OnexTui(root=_root, runner=_Runner([]), load_status=first)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert f"{_MODEL} on llama.test:8000 first" in str(
            app.query_one("#status").render()
        )


async def test_config_shows_what_is_set_up_and_offers_the_users_own_server() -> None:
    """S2."""
    from textual.widgets import Input

    from omnimarket.nodes.node_onex_setup_effect.cli_tui import SetupScreen

    runner = _Runner([], by_command={("models", "list"): _rows()})
    app = _app(runner)
    async with app.run_test(size=(170, 46)) as pilot:
        await _open(pilot, "/config")
        assert isinstance(app.screen, SetupScreen)
        # OMN-26620: the Selected line holds what is ticked this visit; the
        # models already set up are the Model tab's.
        basket = str(app.screen.query_one("#basket", Static).render())
        assert basket == "Selected  none yet"
        app.screen.query_one("#provider").value = "local"
        await pilot.pause()
        assert app.screen.query_one("#url", Input).value == _VLLM
        summary = str(app.screen.query_one(".summary", Static).render())
        assert "3 models · 3 servers" in summary


async def test_delegation_table_names_each_selected_server() -> None:
    """S3: hook delegation includes each selected server."""
    from textual.widgets import DataTable

    runner = _Runner(
        [],
        by_command={
            ("models", "list"): _rows(),
            ("models", "connections"): json.dumps({"connections": []}),
        },
    )

    def native(base: str, models: tuple[str, ...]):
        kind = {_VLLM: "vLLM", _LLAMA: "llama.cpp", _OLLAMA: "Ollama"}[base]
        return {
            m: ModelLocalServerDetails(server=kind, native_messages=base != _OLLAMA)
            for m in models
        }

    app = OnexTui(root=_root, runner=runner, load_status=_status, read_server=native)
    async with app.run_test(size=(170, 46)) as pilot:
        await _open(pilot, "/delegation")
        table = app.screen.query_one("#selected").query_one(DataTable)
        rows = [
            " ".join(str(cell) for cell in table.get_row_at(i))
            for i in range(table.row_count)
        ]
        for model, server in (
            (_MODEL, "vllm.test:8000"),
            (_MODEL, "llama.test:8000"),
            ("saluki", "ollama.test:11434"),
        ):
            assert any(model in row and server in row for row in rows), rows


async def test_a_delegated_task_does_not_change_the_model_shown_as_first() -> None:
    """S4: the answering model is the last run's; the first model stays."""
    answer = json.dumps(
        {
            "run_id": "175dde92-0000-0000-0000-000000000000",
            "result": {
                "response": "done",
                "model_name": "other-model",
                "metrics": {"cost_usd": 0.0},
            },
        }
    )
    runner = _Runner([], captured=answer)
    app = OnexTui(root=_root, runner=runner, load_status=_status)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _type(pilot, "hello")
        await pilot.press("enter")
        for _ in range(10):
            await pilot.pause()
        status = str(app.query_one("#status").render())
        assert "Qwen3.8-27B first" in status
        assert "last run other-model" in status


# /config's providers and /model's remove (OMN-23413, operator 2026-11-13):
#   P1  the provider list was sorted by label, so Local Model came second, and
#       /config opened on it; it is the yaml's order, opening on the first.
#   P2  the URL field was filled with a made-up 127.0.0.1:11434.
#   R1  the TUI could not remove a model; one R must remove nothing.
#   R2  R twice removes only the highlighted server's copy of the model.
#   R3  R, a move away and back, then R must ask again, not remove.


async def test_config_lists_providers_in_yaml_order_and_fills_no_url() -> None:
    """P1, P2."""
    from textual.widgets import Input, RadioButton

    from omnimarket.nodes.node_onex_setup_effect.cli_tui import SetupScreen
    from omnimarket.nodes.node_onex_setup_effect.models.model_setup_provider_traits import (
        setup_provider_traits,
    )

    nothing = json.dumps({"provider": "local", "set_up": False, "models": []})
    app = _app(_Runner([], by_command={("models", "list"): nothing}))
    async with app.run_test(size=(170, 46)) as pilot:
        await _open(pilot, "/config")
        assert isinstance(app.screen, SetupScreen)
        picker = setup_provider_traits().picker
        # Nothing is set up, so the list is A-Z (operator 2026-11-26).
        a_to_z = sorted(picker, key=lambda e: e.label.casefold())
        select = app.screen.query_one("#provider")  # radio options (OMN-23413)
        assert select.value == a_to_z[0].provider
        assert [b.name for b in select.query(RadioButton)] == [
            entry.provider for entry in a_to_z
        ]
        assert picker[-1].provider == "local"
        select.value = "local"
        await pilot.pause()
        assert app.screen.query_one("#url", Input).value == ""


async def _remove_ready(pilot, app: OnexTui, server: str) -> None:
    await _open(pilot, "/model")
    table = app.screen.query_one("#models")
    target = next(e for e in table.entries if e.server_url == server)
    grid = table.query_one(DataTable)
    grid.move_cursor(row=grid.get_row_index(entry_key(target)))
    await pilot.pause()


async def test_one_r_asks_and_a_second_removes_only_that_servers_copy() -> None:
    """R1, R2."""
    runner = _Runner([], by_command={("models", "list"): _rows()})
    app = _app(runner)
    async with app.run_test(size=(170, 46)) as pilot:
        await _remove_ready(pilot, app, _LLAMA)
        await pilot.press("r")
        await pilot.pause()
        assert not [c for c in runner.calls if c[:2] == ("models", "remove")]
        note = str(app.screen.query_one("#pnote", Static).render())
        assert f"{_MODEL} on llama.test:8000" in note
        await pilot.press("r")
        for _ in range(6):
            await pilot.pause()
        removes = [c for c in runner.calls if c[:2] == ("models", "remove")]
        assert removes == [
            ("models", "remove", "local", "--model", _MODEL, "--url", _LLAMA)
        ]


async def test_moving_to_another_row_cancels_a_pending_remove() -> None:
    """R3."""
    runner = _Runner([], by_command={("models", "list"): _rows()})
    app = _app(runner)
    async with app.run_test(size=(170, 46)) as pilot:
        await _remove_ready(pilot, app, _LLAMA)
        await pilot.press("r")
        await pilot.pause()
        await pilot.press("up")  # away: the pending remove is forgotten
        await pilot.pause()
        await pilot.press("down")  # back to the same row
        await pilot.pause()
        await pilot.press("r")  # asks again; it does not remove
        await pilot.pause()
        assert not [c for c in runner.calls if c[:2] == ("models", "remove")]


# A tab stays open until Esc (OMN-23413, operator 2026-11-13):
#   W1  Enter on /model closed the tab; the outcome went to the hidden main log.
#   W2  A (Auto) closed the tab.
#   W3  a confirmed remove closed the tab.
#   W4  Save on /delegation closed the tab.
#   W5  Save on /config closed the tab.
#   T1  a click on another tab closed this one and showed the main screen while
#       the next tab's setup was read.
#   E1  Esc still closes the tab, back to the main screen.


class _Slow(_Runner):
    """Reading the setup takes a while, as real CLI calls and server probes do."""

    async def capture(
        self, argv: tuple[str, ...], stdin: str | None = None
    ) -> tuple[int, str, str]:
        import asyncio

        if argv[:2] == ("models", "list"):
            await asyncio.sleep(0.3)
        return await super().capture(argv, stdin)


async def _settle(pilot, times: int = 8) -> None:
    for _ in range(times):
        await pilot.pause()


async def test_choosing_a_first_model_keeps_model_open_and_shows_the_outcome() -> None:
    """W1."""
    runner = _Runner([], by_command={("models", "list"): _rows()})
    app = _app(runner)
    async with app.run_test(size=(170, 46)) as pilot:
        await _open(pilot, "/model")
        await pilot.press("enter")
        await _settle(pilot)
        assert isinstance(app.screen, ModelPickScreen)
        assert [c for c in runner.calls if c[:2] == ("models", "first")]
        assert "is now tried first" in str(app.screen.query_one("#pnote").render())


async def test_auto_keeps_model_open() -> None:
    """W2."""
    runner = _Runner([], by_command={("models", "list"): _rows(first_url=_LLAMA)})
    app = _app(runner)
    async with app.run_test(size=(170, 46)) as pilot:
        await _open(pilot, "/model")
        await pilot.press("a")
        await _settle(pilot)
        assert isinstance(app.screen, ModelPickScreen)
        assert ("models", "first", "--clear") in runner.calls


async def test_a_confirmed_remove_keeps_model_open_and_shows_the_outcome() -> None:
    """W3."""
    runner = _Runner([], by_command={("models", "list"): _rows()})
    app = _app(runner)
    async with app.run_test(size=(170, 46)) as pilot:
        await _remove_ready(pilot, app, _LLAMA)
        await pilot.press("r")
        await pilot.pause()
        await pilot.press("r")
        await _settle(pilot)
        assert isinstance(app.screen, ModelPickScreen)
        assert "removed" in str(app.screen.query_one("#pnote").render())


async def test_saving_delegation_keeps_delegation_open_and_shows_the_outcome() -> None:
    """W4."""
    runner = _Runner(
        [],
        by_command={
            ("models", "list"): _rows(),
            ("models", "connections"): json.dumps({"connections": []}),
        },
    )
    app = _app(runner)
    async with app.run_test(size=(170, 46)) as pilot:
        await _open(pilot, "/delegation")
        app.screen.set_all(True)
        await pilot.press("ctrl+s")
        await _settle(pilot)
        assert isinstance(app.screen, DelegationScreen)
        assert [c for c in runner.calls if c[:2] == ("delegation", "on")]  # OMN-23413
        assert "delegation is on" in str(app.screen.query_one("#dnote").render())


async def test_saving_config_keeps_config_open_and_shows_the_outcome() -> None:
    """W5."""
    from omnimarket.nodes.node_onex_setup_effect.cli_tui import SetupScreen

    found = json.dumps({"status": "ready", "models": ["llama3.2"]})
    report = json.dumps(
        {"observations": [{"step": "models", "status": "success", "changed": True}]}
    )
    runner = _Runner(
        [],
        by_command={
            ("models", "list"): _rows(),
            ("models", "discover"): found,
            ("setup", "--no-interactive"): report,
        },
    )
    app = _app(runner)
    async with app.run_test(size=(170, 46)) as pilot:
        await _open(pilot, "/config")
        app.screen.query_one("#provider").value = "local"
        await _wait_for_models(pilot)
        table = app.screen.query_one("#found")
        table.set_ticked([entry_key(e) for e in table.entries])
        await pilot.press("ctrl+s")
        await _settle(pilot)
        assert isinstance(app.screen, SetupScreen)
        assert [c for c in runner.calls if c[:2] == ("models", "add")]
        assert "Ready" in str(app.screen.query_one("#note").render())


async def test_clicking_another_tab_never_shows_the_main_screen() -> None:
    """T1: the next tab is read while this one stays up, then swapped in."""
    from textual.widgets import Tabs

    from omnimarket.nodes.node_onex_setup_effect.cli_tui import SetupScreen

    runner = _Slow([], by_command={("models", "list"): _rows()})
    app = _app(runner)
    async with app.run_test(size=(170, 46)) as pilot:
        await _type(pilot, "/model")
        await pilot.press("enter")
        await _settle(pilot, 20)
        assert isinstance(app.screen, ModelPickScreen)
        tabs = app.screen.query_one(Tabs)
        # On a loaded runner the Tab children mount after the screen shows;
        # setting `active` before they exist raises "No Tab with id".
        for _ in range(100):
            if tabs.query("#tabs-list > #tab-config"):
                break
            await pilot.pause(0.05)
        tabs.active = "tab-config"
        for _ in range(40):
            await pilot.pause(0.05)
            assert len(app.screen_stack) >= 2, "the main screen showed"
            if isinstance(app.screen, SetupScreen):
                break
        assert isinstance(app.screen, SetupScreen)
        assert len(app.screen_stack) == 2  # swapped, not stacked


async def test_escape_closes_a_tab_back_to_the_main_screen() -> None:
    """E1."""
    runner = _Runner([], by_command={("models", "list"): _rows()})
    app = _app(runner)
    async with app.run_test(size=(170, 46)) as pilot:
        await _open(pilot, "/model")
        await pilot.press("escape")
        await pilot.pause()
        assert len(app.screen_stack) == 1


# Opening a tab took about 9 s in the operator's lab, measured 2026-11-13: two
# onex CLI reads (2 s) and every server probed again (7 s for llama.cpp):
#   K1  every tab open asked every server again.
#   K2  every tab open waited for the full read before it showed.
#   K3  (control) a server that did not answer must be asked again.


async def test_moving_between_tabs_does_not_ask_the_servers_again() -> None:
    """K1."""
    asked: list[str] = []

    def counting(base: str, models: tuple[str, ...]):
        asked.append(base)
        return _read_server(base, models)

    runner = _Runner([], by_command={("models", "list"): _rows()})
    app = OnexTui(root=_root, runner=runner, load_status=_status, read_server=counting)
    async with app.run_test(size=(170, 46)) as pilot:
        await _open(pilot, "/model")
        first = sorted(asked)
        assert first == sorted([_VLLM, _LLAMA, _OLLAMA])
        await pilot.press("tab")  # to /delegation
        await _settle(pilot, 20)
        assert isinstance(app.screen, DelegationScreen)
        assert sorted(asked) == first  # no server was asked twice


async def test_a_tab_opens_from_the_last_read_setup_before_reading_again() -> None:
    """K2: the next tab shows while its setup is still being read again."""
    runner = _Slow([], by_command={("models", "list"): _rows()})
    app = _app(runner)
    async with app.run_test(size=(170, 46)) as pilot:
        await _type(pilot, "/model")
        await pilot.press("enter")
        await _settle(pilot, 20)
        assert isinstance(app.screen, ModelPickScreen)
        reads = runner.calls.count(("models", "list", "--json"))
        await pilot.press("tab")
        await pilot.pause(0.1)  # well inside the 0.3 s a read takes
        assert isinstance(app.screen, DelegationScreen)
        assert app.screen.query_one("#selected").query_one(DataTable).row_count == 3
        await pilot.pause(0.5)
        assert runner.calls.count(("models", "list", "--json")) == reads + 1


async def test_a_server_that_did_not_answer_is_asked_again() -> None:
    """K3."""
    asked: list[str] = []

    def silent(base: str, models: tuple[str, ...]):
        asked.append(base)
        return {}

    runner = _Runner([], by_command={("models", "list"): _rows()})
    app = OnexTui(root=_root, runner=runner, load_status=_status, read_server=silent)
    async with app.run_test(size=(170, 46)) as pilot:
        await _open(pilot, "/model")
        await pilot.press("tab")
        await _settle(pilot, 20)
        assert asked.count(_VLLM) == 2


# Measured on the real app 2026-11-13T04:01Z: the first /model showed nothing
# for 5.1 s while the setup was read; later tabs showed in under 0.2 s.
#   K4  the first tab open waits for a read that could have run at start-up.
#   K5  the first open reads the setup a second time when one read is running.
#   K6  (control) a start-up read that failed leaves the first open able to read.
#   K7  a tab's re-read counted as unshown, so the next tab did not read again.


class _Broken(_Runner):
    """The first setup read fails, as a CLI that is not installed yet does."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.failed = False

    async def capture(
        self, argv: tuple[str, ...], stdin: str | None = None
    ) -> tuple[int, str, str]:
        if argv[:2] == ("models", "list") and not self.failed:
            self.failed = True
            raise OSError("onex is not on PATH yet")
        return await super().capture(argv, stdin)


async def test_the_setup_is_read_at_start_so_the_first_tab_opens_at_once() -> None:
    """K4."""
    runner = _Slow([], by_command={("models", "list"): _rows()})
    app = _app(runner)
    async with app.run_test(size=(170, 46)) as pilot:
        await pilot.pause(0.6)  # the user is still typing; the 0.3 s read is done
        assert runner.calls.count(("models", "list", "--json")) == 1
        await _type(pilot, "/model")
        await pilot.press("enter")
        await pilot.pause(0.1)  # well inside the 0.3 s a second read would take
        assert isinstance(app.screen, ModelPickScreen)


async def test_a_tab_opened_during_the_start_read_shares_that_read() -> None:
    """K5."""
    runner = _Slow([], by_command={("models", "list"): _rows()})
    app = _app(runner)
    async with app.run_test(size=(170, 46)) as pilot:
        await _type(pilot, "/model")
        await pilot.press("enter")
        await _settle(pilot, 20)
        assert isinstance(app.screen, ModelPickScreen)
        assert runner.calls.count(("models", "list", "--json")) == 1


async def test_every_tab_after_the_first_reads_the_setup_again() -> None:
    """K7: a re-read a tab has shown is not mistaken for the unshown start-up read
    (seen on the real app 2026-11-13T04:07Z: the third tab did not re-read)."""
    runner = _Runner([], by_command={("models", "list"): _rows()})
    app = _app(runner)
    async with app.run_test(size=(170, 46)) as pilot:
        await _settle(pilot, 20)
        await _type(pilot, "/model")
        await pilot.press("enter")
        await _settle(pilot, 20)
        reads = [runner.calls.count(("models", "list", "--json"))]
        for _ in range(3):  # /delegation, /dashboard, /config
            await pilot.press("tab")
            await _settle(pilot, 20)
            reads.append(runner.calls.count(("models", "list", "--json")))
        assert reads == [1, 2, 3, 4]


async def test_a_failed_start_read_does_not_stop_the_first_tab() -> None:
    """K6."""
    runner = _Broken([], by_command={("models", "list"): _rows()})
    app = _app(runner)
    async with app.run_test(size=(170, 46)) as pilot:
        await pilot.pause(0.3)
        await _type(pilot, "/model")
        await pilot.press("enter")
        await _settle(pilot, 20)
        assert isinstance(app.screen, ModelPickScreen)
        assert runner.failed


# Edge cases (OMN-23413, operator 2026-11-13: "cover all edge cases"):
#   G1  Esc while an action runs tore the tab down and hid its outcome.
#   G2  a tab switch while an action runs did the same.
#   G3  removing the model tried first left the "Now:" line naming it.
#   G4  a remove the CLI refuses must show the CLI's reason and keep the tab.
#   G5  a server /config cannot reach must say so, not show an empty table.


class _SlowFirst(_Runner):
    """`models first` takes a while, as a real CLI call does."""

    async def capture(
        self, argv: tuple[str, ...], stdin: str | None = None
    ) -> tuple[int, str, str]:
        import asyncio

        if argv[:2] == ("models", "first"):
            await asyncio.sleep(0.4)
        return await super().capture(argv, stdin)


async def test_escape_while_an_action_runs_keeps_the_tab_until_it_finishes() -> None:
    """G1."""
    runner = _SlowFirst([], by_command={("models", "list"): _rows()})
    app = _app(runner)
    async with app.run_test(size=(170, 46)) as pilot:
        await _open(pilot, "/model")
        await pilot.press("a")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, ModelPickScreen)
        assert "still working" in str(app.screen.query_one("#pnote").render())
        for _ in range(100):  # wait for the outcome, not a fixed time
            await pilot.pause(0.05)
            if "Auto" in str(app.screen.query_one("#pnote").render()):
                break
        assert "Auto" in str(app.screen.query_one("#pnote").render())
        await pilot.press("escape")
        await pilot.pause()
        assert len(app.screen_stack) == 1


async def test_a_tab_switch_while_an_action_runs_waits_for_it() -> None:
    """G2."""
    runner = _SlowFirst([], by_command={("models", "list"): _rows()})
    app = _app(runner)
    async with app.run_test(size=(170, 46)) as pilot:
        await _open(pilot, "/model")
        await pilot.press("a")
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        assert isinstance(app.screen, ModelPickScreen)
        assert "still working" in str(app.screen.query_one("#pnote").render())


class _Removes(_Runner):
    """`models list` reflects a remove, as the real CLI does."""

    def __init__(self, before: str, after: str, refuse: str | None = None) -> None:
        super().__init__([], by_command={("models", "list"): before})
        self._after = after
        self._refuse = refuse

    async def capture(
        self, argv: tuple[str, ...], stdin: str | None = None
    ) -> tuple[int, str, str]:
        if argv[:2] == ("models", "remove"):
            self.calls.append(argv)
            self.stdins.append(stdin)
            if self._refuse:
                return 1, "", f"Error: {self._refuse}\n"
            self._by_command[("models", "list")] = self._after
            return 0, "", ""
        return await super().capture(argv, stdin)


def _only(rows: str, server: str) -> str:
    """`models list --json` with one server's model gone."""
    row = json.loads(rows)
    row["local_models"] = [m for m in row["local_models"] if m["base_url"] != server]
    row["models"] = [m["model"] for m in row["local_models"]]
    row.update(tried_first=False, first_model=None, first_url=None)
    return json.dumps(row)


async def test_removing_the_model_tried_first_shows_auto_again() -> None:
    """G3."""
    runner = _Removes(_rows(first_url=_LLAMA), _only(_rows(), _LLAMA))
    app = _app(runner)
    async with app.run_test(size=(170, 46)) as pilot:
        await _remove_ready(pilot, app, _LLAMA)
        assert "llama.test:8000" in str(app.screen.query_one("#pnow").render())
        await pilot.press("r")
        await pilot.pause()
        await pilot.press("r")
        await _settle(pilot, 12)
        now = str(app.screen.query_one("#pnow").render())
        assert "Auto" in now
        assert "llama.test" not in now
        assert app.screen.query_one("#models").query_one(DataTable).row_count == 2


async def test_a_refused_remove_shows_the_reason_and_keeps_the_tab() -> None:
    """G4."""
    reason = "that would leave your openrouter key with no model"
    runner = _Removes(_rows(), _rows(), refuse=reason)
    app = _app(runner)
    async with app.run_test(size=(170, 46)) as pilot:
        await _remove_ready(pilot, app, _LLAMA)
        await pilot.press("r")
        await pilot.pause()
        await pilot.press("r")
        await _settle(pilot, 12)
        assert isinstance(app.screen, ModelPickScreen)
        assert reason in str(app.screen.query_one("#pnote").render())
        assert app.screen.query_one("#models").query_one(DataTable).row_count == 3


async def test_config_says_when_a_server_cannot_be_read() -> None:
    """G5: the CLI's own message, as `onex models discover` prints it."""
    message = "The endpoint could not be read. Check its URL and availability."
    unread = json.dumps(
        {
            "url": "http://127.0.0.1:9",
            "status": "unavailable",
            "models": [],
            "message": message,
        }
    )
    runner = _Runner(
        [], by_command={("models", "list"): _rows(), ("models", "discover"): unread}
    )
    app = _app(runner)
    async with app.run_test(size=(170, 46)) as pilot:
        await _open(pilot, "/config")
        app.screen.query_one("#provider").value = "local"
        await pilot.pause()
        app.screen.query_one("#url").value = "http://127.0.0.1:9"
        await pilot.pause(1.2)
        await _settle(pilot)
        assert message in str(app.screen.query_one("#note").render())
        assert not app.screen.query_one("#found").display


# A pop-up for every outcome (OMN-23413, operator 2026-11-13):
#   X1  a saved model showed no pop-up; only a line under the table.
#   X2  a failed action showed no pop-up either.
#   X3  Add on /config showed nothing but the note line.


def _record_toasts(
    app: OnexTui, monkeypatch: pytest.MonkeyPatch
) -> list[tuple[str, str, str]]:
    shown: list[tuple[str, str, str]] = []

    def notify(
        message: str, *, title: str = "", severity: str = "information", **_: object
    ) -> None:
        shown.append((title, severity, message))

    monkeypatch.setattr(app, "notify", notify)
    return shown


async def test_saving_a_model_pops_up_done(monkeypatch: pytest.MonkeyPatch) -> None:
    """X1."""
    from omnimarket.nodes.node_onex_setup_effect.cli_tui import SetupScreen

    found = json.dumps({"status": "ready", "models": ["llama3.2"]})
    report = json.dumps(
        {"observations": [{"step": "models", "status": "success", "changed": True}]}
    )
    runner = _Runner(
        [],
        by_command={
            ("models", "list"): _rows(),
            ("models", "discover"): found,
            ("setup", "--no-interactive"): report,
        },
    )
    app = _app(runner)
    toasts = _record_toasts(app, monkeypatch)
    async with app.run_test(size=(170, 46)) as pilot:
        await _open(pilot, "/config")
        app.screen.query_one("#provider").value = "local"
        await _wait_for_models(pilot)
        table = app.screen.query_one("#found")
        table.set_ticked([entry_key(e) for e in table.entries])
        await pilot.press("ctrl+s")
        await _settle(pilot)
        assert isinstance(app.screen, SetupScreen)
        done = [t for t in toasts if t[0] == "Done"]
        assert done
        assert done[-1][1] == "information"
        assert "Ready" in done[-1][2]


async def test_a_failed_remove_pops_up_failed_with_the_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """X2."""
    reason = "that would leave your openrouter key with no model"
    runner = _Removes(_rows(), _rows(), refuse=reason)
    app = _app(runner)
    toasts = _record_toasts(app, monkeypatch)
    async with app.run_test(size=(170, 46)) as pilot:
        await _remove_ready(pilot, app, _LLAMA)
        await pilot.press("r")
        await pilot.pause()
        await pilot.press("r")
        await _settle(pilot, 12)
        failed = [t for t in toasts if t[0] == "Failed"]
        assert failed
        assert failed[-1][1] == "error"
        assert reason in failed[-1][2]


async def test_adding_on_config_pops_up_added(monkeypatch: pytest.MonkeyPatch) -> None:
    """X3."""
    found = json.dumps({"status": "ready", "models": ["llama3.2"]})
    runner = _Runner(
        [], by_command={("models", "list"): _rows(), ("models", "discover"): found}
    )
    app = _app(runner)
    toasts = _record_toasts(app, monkeypatch)
    async with app.run_test(size=(170, 46)) as pilot:
        await _open(pilot, "/config")
        app.screen.query_one("#provider").value = "local"
        await _wait_for_models(pilot)
        table = app.screen.query_one("#found")
        table.set_ticked([entry_key(e) for e in table.entries])
        app.screen.query_one("#add").press()
        await _settle(pilot)
        assert [t for t in toasts if t[0] == "Added"]


async def test_a_failed_save_keeps_the_chosen_models_for_a_retry() -> None:
    """Y1 (operator 2026-11-13): a save that setup refused cleared every tick,
    so the user had to find and tick each model again."""
    found = json.dumps({"status": "ready", "models": ["gemma3:1b", "qwen3.5:0.8b"]})
    _refused = json.dumps(
        {
            "observations": [
                {
                    "step": "shell-safety",
                    "status": "action_required",
                    "fix": "Review named shell conflicts.",
                }
            ]
        }
    )
    runner = _Runner(
        [],
        code=1,
        errors="Error: the server did not answer\n",
        by_command={("models", "list"): _rows(), ("models", "discover"): found},
    )
    app = _app(runner)
    async with app.run_test(size=(170, 46)) as pilot:
        await _open(pilot, "/config")
        app.screen.query_one("#provider").value = "local"
        await _wait_for_models(pilot)
        table = app.screen.query_one("#found")
        table.set_ticked([entry_key(e) for e in table.entries])
        await pilot.press("ctrl+s")
        await _settle(pilot)
        basket = str(app.screen.query_one("#basket").render())
        assert "gemma3:1b" in basket
        assert "qwen3.5:0.8b" in basket
        assert "were not added: the server did not answer" in str(
            app.screen.query_one("#note").render()
        )
        # A retry sends the same choices without ticking again.
        await pilot.press("ctrl+s")
        await _settle(pilot)
        adds = [c for c in runner.calls if c[:2] == ("models", "add")]
        assert len(adds) == 2
        assert adds[0] == adds[1]


# Real run, 2026-11-13 (three Ollama models): `onex models add` stored all three
# and tested each; one test failed, so the command exited 1. The tab said all
# three "were not added", gave a success line as the reason, and kept them
# chosen. Measured in the operator's lab:
#   Z1  stored models whose test failed were reported as not added.
#   Z2  the tab said only "working…" for the 150 s the tests took.


def _results(*rows: tuple[str, str, str | None]) -> str:
    """`onex models add --json`: one test result per model, as the CLI prints them."""
    return "\n".join(
        json.dumps(
            {"provider": "local", "status": status, "model": model, "reason": why}
        )
        for model, status, why in rows
    )


async def test_a_stored_model_whose_test_failed_is_reported_as_added() -> None:
    """Z1, Z2."""
    found = json.dumps({"status": "ready", "models": ["gemma3:1b", "qwen3.5:0.8b"]})
    added = _results(
        ("gemma3:1b", "passed", None),
        ("qwen3.5:0.8b", "failed", "WEAK_OUTPUT: the response was cut off"),
    )
    runner = _Runner(
        [],
        code=1,
        by_command={
            ("models", "list"): _rows(),
            ("models", "discover"): found,
            ("models", "add"): added,
        },
    )
    seen: list[str] = []
    app = _app(runner)
    async with app.run_test(size=(170, 46)) as pilot:
        await _open(pilot, "/config")
        app.screen.query_one("#provider").value = "local"
        await _wait_for_models(pilot)
        screen = app.screen
        real_note = screen._note

        def watch(text, style="muted"):
            seen.append(str(text))
            real_note(text, style)

        screen._note = watch
        table = screen.query_one("#found")
        table.set_ticked([entry_key(e) for e in table.entries])
        await pilot.press("ctrl+s")
        await _settle(pilot)
        note = str(screen.query_one("#note").render())
        assert "gemma3:1b ready" in note
        assert (
            "qwen3.5:0.8b was added, but its test failed: the response was cut off"
            in note
        )
        assert "were not added" not in note
        # Stored, so not chosen again for a retry.
        assert "Selected:" not in str(screen.query_one("#basket").render())
        # Z2: the tab said what it was doing, model by model.
        assert any("each is tested with one real delegation" in s for s in seen)
        assert any("1 of 2 tested" in s for s in seen)
        assert any("2 of 2 tested" in s for s in seen)
