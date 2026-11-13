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
  FM8  two servers of one kind on one port got one gateway connection name.
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
    connection_name,
)
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui import (
    _root,
    _Runner,
    _status,
    _type,
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
            ("Local model · vLLM", _MODEL, _VLLM, "ready"),
            ("Local model · llama.cpp", _MODEL, _LLAMA, "untested"),
            ("Local model · Ollama", "saluki:latest", _OLLAMA, "untested"),
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


def test_two_servers_of_one_kind_on_one_port_get_two_connection_names() -> None:
    """FM8."""
    first = connection_name("vLLM", "http://gpu-a.test:8000")
    second = connection_name("vLLM", "http://gpu-b.test:8000")
    assert first != second
    assert all(name[:1].isalpha() and len(name) <= 64 for name in (first, second))


# The screens must say only what is true (OMN-23413, operator 2026-11-13):
#   S1  the status bar named a model while Auto picks per task, and said "model
#       not set" before it had read anything.
#   S2  /config said "Selected: none yet" with three models set up, and offered
#       127.0.0.1:11434 instead of the user's own server.
#   S3  the /delegation gateway note named a model two servers run without
#       saying which server.
#   S4  a delegated task's answering model replaced the model shown as first.


async def test_the_status_bar_says_auto_and_how_many_models_when_none_is_first() -> (
    None
):
    """S1."""
    from omnimarket.nodes.node_onex_setup_effect.cli_tui import ModelTuiStatus

    async def auto() -> ModelTuiStatus:
        return ModelTuiStatus(workspace="~/ws", models=3)

    app = OnexTui(root=_root, runner=_Runner([]), load_status=auto)
    async with app.run_test() as pilot:
        await pilot.pause()
        status = str(app.query_one("#status").render())
        assert "Auto · 3 models" in status
        assert "first" not in status


async def test_the_status_bar_says_reading_until_the_setup_is_read() -> None:
    """S1: before the first read, nothing about a model is claimed."""
    import asyncio

    from omnimarket.nodes.node_onex_setup_effect.cli_tui import ModelTuiStatus

    gate = asyncio.Event()

    async def slow() -> ModelTuiStatus:
        await gate.wait()
        return ModelTuiStatus(workspace="~/ws", models=0)

    app = OnexTui(root=_root, runner=_Runner([]), load_status=slow)
    async with app.run_test() as pilot:
        await pilot.pause()
        status = str(app.query_one("#status").render())
        assert "reading your setup" in status
        assert "not set" not in status
        gate.set()
        await pilot.pause()
        await pilot.pause()
        assert "no model set up" in str(app.query_one("#status").render())


async def test_the_status_bar_names_the_server_of_the_first_model() -> None:
    """S1 with a first model on a server."""
    from omnimarket.nodes.node_onex_setup_effect.cli_tui import ModelTuiStatus

    async def first() -> ModelTuiStatus:
        return ModelTuiStatus(
            workspace="~/ws", model=_MODEL, first_url=_LLAMA, models=3
        )

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
        basket = str(app.screen.query_one("#basket", Static).render())
        assert "none yet" not in basket
        assert f"{_MODEL} on vllm.test:8000" in basket
        assert f"{_MODEL} on llama.test:8000" in basket
        app.screen.query_one("#provider").value = "local"
        await pilot.pause()
        assert app.screen.query_one("#url", Input).value == _VLLM
        summary = str(app.screen.query_one(".summary", Static).render())
        assert "3 models · 3 servers" in summary


async def test_the_gateway_note_names_each_server() -> None:
    """S3."""
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
        note = str(app.screen.query_one("#dnote", Static).render())
        assert f"{_MODEL} on vllm.test:8000" in note
        assert f"{_MODEL} on llama.test:8000" in note
        assert "saluki" not in note


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
#   P1  the provider list was sorted by label, so Local model came second, and
#       /config opened on it; it is the yaml's order, opening on the first.
#   P2  the URL field was filled with a made-up 127.0.0.1:11434.
#   R1  the TUI could not remove a model; one R must remove nothing.
#   R2  R twice removes only the highlighted server's copy of the model.
#   R3  R, a move away and back, then R must ask again, not remove.


async def test_config_lists_providers_in_yaml_order_and_fills_no_url() -> None:
    """P1, P2."""
    from textual.widgets import Input, Select

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
        select = app.screen.query_one("#provider", Select)
        assert select.value == picker[0].provider
        assert [value for _, value in select._options if value is not Select.BLANK] == [
            entry.provider for entry in picker
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
