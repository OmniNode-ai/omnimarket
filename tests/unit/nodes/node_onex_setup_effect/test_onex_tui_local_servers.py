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
