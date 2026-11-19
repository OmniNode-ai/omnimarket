# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A named connection shown in the table must not disappear from the footer.

Observed on the installed 2c33a00833 candidate: /delegation shows one selected
Qwen model, but the footer says no model is set up. Failure modes: named-only
selection omitted; mixed legacy/named count differs; an unavailable connection
read is presented as an authoritative empty selection. Empty and legacy-first
selections are positive compatibility controls.
"""

import json
from collections.abc import Callable

import pytest

from omnimarket.nodes.node_onex_setup_effect import cli_tui as tui
from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import tui_design

pytestmark = pytest.mark.unit


class _StoredModelsCli:
    def __init__(
        self,
        rows: list[dict[str, object]],
        connections: list[dict[str, object]],
        connection_exit: int,
        model_exit: int = 0,
        model_output: str | None = None,
        connection_output: str | None = None,
    ) -> None:
        self.rows = rows
        self.connections = connections
        self.connection_exit = connection_exit
        self.model_exit = model_exit
        self.model_output = model_output
        self.connection_output = connection_output

    async def stream(self, args: tuple[str, ...], emit: Callable[[str], object]) -> int:
        if args == ("models", "list", "--json"):
            if self.model_output is not None:
                emit(self.model_output)
                return self.model_exit
            for row in self.rows:
                emit(json.dumps(row))
            return self.model_exit
        if args == ("models", "connections", "--json"):
            if self.connection_output is not None:
                emit(self.connection_output)
                return self.connection_exit
            emit(json.dumps({"status": "ready", "connections": self.connections}))
            return self.connection_exit
        assert args == tui_design().delegation.claude_check
        emit(json.dumps({"status": "off", "observations": []}))
        return 0


_LEGACY: dict[str, object] = {
    "provider": "local",
    "set_up": True,
    "models": ["legacy-model"],
    "local_models": [
        {
            "model": "legacy-model",
            "base_url": "http://legacy.test",
            "backend_id": "legacy-backend",
        }
    ],
    "tried_first": True,
    "first_model": "legacy-model",
    "first_url": "http://legacy.test",
}
_NAMED: dict[str, object] = {
    "connection_id": "named-server",
    "provider": "local",
    "url": "http://named.test",
    "models": ["named-model"],
}


@pytest.mark.parametrize(
    ("rows", "connections", "expected"),
    [([], [_NAMED], 1), ([_LEGACY], [_NAMED], 2), ([_LEGACY], [], 1), ([], [], 0)],
)
async def test_footer_counts_the_same_selected_models_as_the_table(
    monkeypatch: pytest.MonkeyPatch,
    rows: list[dict[str, object]],
    connections: list[dict[str, object]],
    expected: int,
) -> None:
    runner = _StoredModelsCli(rows, connections, 0)
    monkeypatch.setattr(tui, "HandlerOnexCommand", lambda: runner)
    status = await tui.load_status()
    assert status.models == expected
    if rows:
        assert status.model == "legacy-model"
        assert status.first_url == "http://legacy.test"
    else:
        assert status.model is None


@pytest.mark.parametrize(
    ("model_exit", "connection_exit", "command"),
    [(1, 0, "models list"), (0, 1, "connections")],
)
async def test_an_unavailable_model_read_cannot_claim_no_models(
    monkeypatch: pytest.MonkeyPatch,
    model_exit: int,
    connection_exit: int,
    command: str,
) -> None:
    runner = _StoredModelsCli([], [], connection_exit, model_exit)
    monkeypatch.setattr(tui, "HandlerOnexCommand", lambda: runner)
    with pytest.raises(RuntimeError, match=command):
        await tui.load_status()


@pytest.mark.parametrize(
    ("model_output", "connection_output", "command"),
    [
        ("not-json", None, "models list"),
        ('{"unrelated":true}', None, "models list"),
        (None, '{"status":"ready","connections":[{}]}', "connections"),
        (None, '{"status":"ready","connections":{}}', "connections"),
        (None, '{"status":"failed"}', "connections"),
    ],
)
async def test_malformed_success_output_cannot_claim_an_empty_selection(
    monkeypatch: pytest.MonkeyPatch,
    model_output: str | None,
    connection_output: str | None,
    command: str,
) -> None:
    runner = _StoredModelsCli(
        [], [], 0, model_output=model_output, connection_output=connection_output
    )
    monkeypatch.setattr(tui, "HandlerOnexCommand", lambda: runner)
    with pytest.raises(RuntimeError, match=command):
        await tui.load_status()
