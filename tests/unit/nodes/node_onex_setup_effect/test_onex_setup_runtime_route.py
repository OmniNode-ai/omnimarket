# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24432: ``onex setup`` runs through the local runtime, never in the CLI process.

AC3  the command is published to the local runtime and node_onex_setup_effect handles it;
     the provider key never travels on that command.

Named failing control: ``test_the_cli_does_not_call_the_handler_in_its_own_process`` fails
when ``cli.py`` calls ``HandlerOnexSetup().handle(request)`` again (the call this change
removed), and ``test_the_key_never_rides_on_the_command`` fails when the key is put in the
payload. The fixtures are the ones the OMN-23207 model-step tests use.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from omnimarket.nodes.node_onex_setup_effect import cli as onex_setup_cli
from omnimarket.nodes.node_onex_setup_effect.handlers.handler_onex_setup import (
    discard_key,
    park_key,
    take_key,
)
from tests.unit.nodes.node_onex_setup_effect.test_onex_setup_model_step import (
    GOOD_KEY,
    MODEL,
    Recorder,
    _setup,
    _unattended,
)

pytestmark = pytest.mark.unit

NODE_NAME = "node_onex_setup_effect"


class _RuntimeSpy:
    """Records what the CLI hands the local runtime, and runs the real runtime."""

    def __init__(self, real: Any) -> None:
        self._real = real
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(
        self, node_id: str, contract_path: Path, payload: dict[str, Any], timeout: int
    ) -> dict[str, Any]:
        self.calls.append((node_id, dict(payload)))
        return self._real(node_id, contract_path, payload, timeout)  # type: ignore[no-any-return]


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> _RuntimeSpy:
    recorded = _RuntimeSpy(onex_setup_cli.run_node_in_process)
    monkeypatch.setattr(onex_setup_cli, "run_node_in_process", recorded)
    return recorded


def test_setup_publishes_its_command_to_the_runtime(
    home: Path, tmp_path: Path, server: str, recorder: Recorder, spy: _RuntimeSpy
) -> None:
    workspace = tmp_path / "ws"
    result = _setup(
        _unattended(
            workspace, "--provider", "local", "--url", server, "--model", MODEL
        ),
        stdin="",
    )
    assert result.exit_code == 0, result.output
    assert [node for node, _ in spy.calls] == [NODE_NAME]
    payload = spy.calls[0][1]
    assert payload["workspace"] == str(workspace)
    assert payload["provider"] == "local"
    assert payload["model"] == MODEL
    assert "[done] workspace:" in result.output, "the typed result came back through it"


def test_the_key_never_rides_on_the_command(
    home: Path, tmp_path: Path, server: str, recorder: Recorder, spy: _RuntimeSpy
) -> None:
    result = _setup(
        _unattended(
            tmp_path / "ws",
            "--provider",
            "openai_compatible",
            "--model",
            MODEL,
            "--url",
            server,
        ),
        stdin=f"{GOOD_KEY}\n",
    )
    assert result.exit_code == 0, result.output
    assert len(spy.calls) == 1
    payload = spy.calls[0][1]
    assert "key" not in payload
    assert GOOD_KEY not in json.dumps(payload, default=str)
    assert isinstance(payload["key_handoff"], str)
    assert payload["key_handoff"]
    assert GOOD_KEY not in result.output
    assert take_key(payload["key_handoff"]) is None, "the handler took the key out"


def test_the_cli_does_not_call_the_handler_in_its_own_process() -> None:
    assert not hasattr(onex_setup_cli, "HandlerOnexSetup"), (
        "cli.py must reach the handler only through the local runtime"
    )


def test_a_parked_key_is_taken_once_and_an_unclaimed_one_is_discarded() -> None:
    token = park_key(SecretStr("sk-parked"))
    taken = take_key(token)
    assert taken is not None
    assert taken.get_secret_value() == "sk-parked"
    assert take_key(token) is None
    unclaimed = park_key(SecretStr("sk-unclaimed"))
    discard_key(unclaimed)
    assert take_key(unclaimed) is None
    assert take_key(None) is None


def test_a_refused_request_goes_through_the_runtime_and_ends_exit_1(
    home: Path, tmp_path: Path, spy: _RuntimeSpy
) -> None:
    result = _setup(["--no-interactive", "--workspace", str(tmp_path / "ws")], stdin="")
    assert result.exit_code == 1, result.output
    assert [node for node, _ in spy.calls] == [NODE_NAME]
    assert "step 'arguments' failed" in result.output
    assert "--provider is missing" in result.output


def test_a_runtime_failure_ends_the_command_and_leaves_no_parked_key(
    home: Path,
    tmp_path: Path,
    server: str,
    recorder: Recorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnibase_core.errors.model_onex_error import ModelOnexError

    parked: list[str] = []
    real_park = onex_setup_cli.park_key

    def park_and_note(key: SecretStr) -> str:
        token = real_park(key)
        parked.append(token)
        return token

    def runtime_down(*_args: object, **_kwargs: object) -> dict[str, Any]:
        raise ModelOnexError(
            message="runtime did not start", error_code=_runtime_error_code()
        )

    monkeypatch.setattr(onex_setup_cli, "park_key", park_and_note)
    monkeypatch.setattr(onex_setup_cli, "run_node_in_process", runtime_down)
    result = _setup(
        _unattended(
            tmp_path / "ws",
            "--provider",
            "openai_compatible",
            "--model",
            MODEL,
            "--url",
            server,
        ),
        stdin=f"{GOOD_KEY}\n",
    )
    assert result.exit_code != 0
    assert "runtime did not start" in result.output
    assert GOOD_KEY not in result.output
    assert len(parked) == 1
    assert take_key(parked[0]) is None


def _runtime_error_code() -> Any:
    from omnibase_core.enums.enum_core_error_code import EnumCoreErrorCode

    return EnumCoreErrorCode.RUNTIME_ERROR
