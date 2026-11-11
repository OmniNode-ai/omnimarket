# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Interactive catalog failure controls before any setup effects.

Refuse wrong/duplicate IDs and empty, unauthorized or unreachable catalogs.
Only unsupported listing permits a manual ID. Authentication is read once,
bound to this request, and never included in the returned selection or output.
"""

import json
from types import SimpleNamespace

import click
import pytest
from click.testing import CliRunner
from pydantic import SecretStr

from omnimarket.nodes.node_onex_setup_effect import cli as setup

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("status", "models", "answer", "expected"),
    [
        (
            "ready",
            ("server/custom-a", "server/new-b"),
            "server/new-b,server/custom-a\n",
            ["server/new-b", "server/custom-a"],
        ),
        ("ready", ("a",), "invented\n", None),
        ("ready", ("a",), "a,a\n", None),
        ("ready", (), "invented\n", None),
        ("rejected", (), "invented\n", None),
        ("unavailable", (), "invented\n", None),
        ("invalid", (), "invented\n", None),
        ("unsupported", (), "explicit/manual\n", ["explicit/manual"]),
    ],
)
def test_endpoint_picker_uses_catalog_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    models: tuple[str, ...],
    answer: str,
    expected: list[str] | None,
) -> None:
    calls: list[tuple[str, SecretStr | None]] = []

    def discover_models(*, url: str, key: SecretStr | None = None) -> SimpleNamespace:
        calls.append((url, key))
        return SimpleNamespace(
            status=status, models=models, message="Catalog unavailable"
        )

    monkeypatch.setattr(setup, "discover_models", discover_models, raising=False)
    monkeypatch.setattr(
        setup,
        "probe_models",
        lambda **_: SimpleNamespace(models=models, probes=(), all_throttled=False),
        raising=False,
    )

    @click.command()
    def choose() -> None:
        _, selected, _ = setup._ask_choices("ollama", None, "http://server.test:11434")
        click.echo(json.dumps((selected or "").split(",")))

    result = CliRunner().invoke(
        choose, input=("y\n" if status == "ready" and models else "") + answer
    )
    assert calls == [("http://server.test:11434", None)]
    if expected is None:
        assert result.exit_code != 0
    else:
        assert result.exit_code == 0, result.output
        assert json.loads(result.output.splitlines()[-1]) == expected
        if status == "ready":
            for name in models:
                assert name in result.output


def test_authenticated_picker_reuses_one_hidden_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "positive-control-key-should-never-print"
    reads: list[str] = []
    calls: list[SecretStr | None] = []
    credentials: dict[str, SecretStr] = {}

    def hidden(prompt: str) -> str:
        reads.append(prompt)
        return secret

    def discover_models(*, url: str, key: SecretStr | None = None) -> SimpleNamespace:
        calls.append(key)
        return SimpleNamespace(status="ready", models=("private-model",), message=None)

    monkeypatch.setattr(setup, "getpass", hidden)
    monkeypatch.setattr(setup, "discover_models", discover_models, raising=False)
    monkeypatch.setattr(
        setup,
        "probe_models",
        lambda **_: SimpleNamespace(
            models=("private-model",), probes=(), all_throttled=False
        ),
        raising=False,
    )

    @click.command()
    def choose() -> None:
        monkeypatch.setattr(setup.sys.stdin, "isatty", lambda: True)
        setup._ask_choices(
            "ollama",
            None,
            "https://private.test",
            endpoint_auth="api-key",
            credentials=credentials,
        )

    result = CliRunner().invoke(choose, input="y\nprivate-model\n")
    assert result.exit_code == 0, result.output
    assert len(reads) == 1
    assert calls[0] is credentials["ollama"]
    assert calls[0].get_secret_value() == secret
    assert secret not in result.output
