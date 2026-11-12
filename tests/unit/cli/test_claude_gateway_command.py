# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI falsifiers: consent, JSON isolation, credential output and bypass attempts."""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from omnimarket.nodes.node_claude_code_binding_install_effect import cli_gateway as cli
from omnimarket.nodes.node_claude_code_binding_install_effect.models.model_claude_gateway_settings import (
    ModelClaudeGatewaySettingsRequest,
    ModelClaudeGatewaySettingsResult,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import (
    handler_claude_launch as claude_launch,
)

pytestmark = pytest.mark.unit
ENABLE = [
    "enable",
    "--gateway-url",
    "http://127.0.0.1:8181",
    "--model",
    "claude-fixture",
    "--credential-ref",
    "gateway.fixture",
]


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[ModelClaudeGatewaySettingsRequest]:
    requests: list[ModelClaudeGatewaySettingsRequest] = []

    def handle(
        request: ModelClaudeGatewaySettingsRequest,
    ) -> ModelClaudeGatewaySettingsResult:
        requests.append(request)
        return ModelClaudeGatewaySettingsResult(
            status="success",
            enabled=True,
            changed=request.action != "status",
            gateway_url="http://127.0.0.1:8181",
            model="claude-fixture",
        )

    monkeypatch.setattr(
        cli, "build_gateway_settings_handler", lambda: SimpleNamespace(handle=handle)
    )
    return requests


def test_enable_needs_explicit_consent(
    calls: list[ModelClaudeGatewaySettingsRequest],
) -> None:
    result = CliRunner().invoke(cli.claude_group, ENABLE, input="n\n")
    assert result.exit_code != 0
    assert calls == []
    assert "http://127.0.0.1:8181" in result.output
    assert "claude-fixture" in result.output
    assert "unknown" in result.output.lower()
    assert "billing" in result.output.lower()


def test_enable_confirmation_precedes_write(
    calls: list[ModelClaudeGatewaySettingsRequest],
) -> None:
    result = CliRunner().invoke(cli.claude_group, ENABLE, input="y\n")
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    argv = shlex.split(calls[0].credential_helper or "")
    assert Path(argv[0]).is_absolute()
    assert argv[-4:] == ["claude", "credential", "--ref", "gateway.fixture"] or argv[
        -3:
    ] == ["credential", "--ref", "gateway.fixture"]


def test_json_enable_contains_only_result(
    calls: list[ModelClaudeGatewaySettingsRequest],
) -> None:
    result = CliRunner().invoke(cli.claude_group, [*ENABLE, "--yes", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["enabled"] is True
    assert "Welcome" not in result.stdout
    assert len(calls) == 1


def test_json_without_consent_cannot_prompt_or_write(
    calls: list[ModelClaudeGatewaySettingsRequest],
) -> None:
    result = CliRunner().invoke(cli.claude_group, [*ENABLE, "--json"], input="y\n")
    assert result.exit_code != 0
    assert json.loads(result.stdout)["status"] == "action_required"
    assert calls == []


def test_invalid_url_does_not_echo_secret_or_reach_handler(
    calls: list[ModelClaudeGatewaySettingsRequest],
) -> None:
    args = [
        "https://user:SENTINEL-private@host"
        if value == "http://127.0.0.1:8181"
        else value
        for value in ENABLE
    ]
    result = CliRunner().invoke(cli.claude_group, [*args, "--yes", "--json"])
    assert result.exit_code != 0
    assert "SENTINEL" not in result.output
    assert json.loads(result.stdout)["status"] == "failure"
    assert calls == []


def test_disable_requires_consent(
    calls: list[ModelClaudeGatewaySettingsRequest],
) -> None:
    result = CliRunner().invoke(cli.claude_group, ["disable"], input="n\n")
    assert result.exit_code != 0
    assert calls == []


def test_status_json_is_readable(
    calls: list[ModelClaudeGatewaySettingsRequest],
) -> None:
    result = CliRunner().invoke(cli.claude_group, ["status", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["enabled"] is True
    assert calls[0].action == "status"


def test_credential_helper_prints_only_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    class Store:
        async def get_secret(self, key: str) -> str | None:
            return "SENTINEL-private" if key == "gateway.fixture" else None

    monkeypatch.setattr(cli, "gateway_store", Store)
    result = CliRunner().invoke(
        cli.claude_group, ["credential", "--ref", "gateway.fixture"]
    )
    assert result.exit_code == 0
    assert result.stdout == "SENTINEL-private\n"
    assert result.stderr == ""
    missing = CliRunner().invoke(cli.claude_group, ["credential", "--ref", "missing"])
    assert missing.exit_code != 0
    assert missing.stdout == ""
    assert "SENTINEL" not in missing.stderr


@pytest.mark.parametrize(
    "override", ["--settings", "--settings={}", "--setting-sources", "--env"]
)
def test_start_rejects_routing_override_arguments(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[ModelClaudeGatewaySettingsRequest],
    override: str,
) -> None:
    launches: list[object] = []
    monkeypatch.setattr(
        claude_launch.subprocess, "run", lambda *a, **_kw: launches.append(a)
    )
    result = CliRunner().invoke(cli.claude_group, ["start", "--", override])
    assert result.exit_code != 0
    assert launches == []


def test_start_refuses_unavailable_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli,
        "build_gateway_settings_handler",
        lambda: SimpleNamespace(
            handle=lambda _: ModelClaudeGatewaySettingsResult(status="unavailable")
        ),
    )
    launches: list[object] = []
    monkeypatch.setattr(
        claude_launch.subprocess, "run", lambda *a, **_kw: launches.append(a)
    )
    result = CliRunner().invoke(cli.claude_group, ["start"])
    assert result.exit_code != 0
    assert launches == []


def test_start_uses_claude_after_successful_status(
    monkeypatch: pytest.MonkeyPatch, calls: list[ModelClaudeGatewaySettingsRequest]
) -> None:
    launches: list[list[str]] = []

    def run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        assert calls[0].action == "status"
        launches.append(argv)
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr(claude_launch.subprocess, "run", run)
    result = CliRunner().invoke(cli.claude_group, ["start", "--", "--version"])
    assert result.exit_code == 7
    assert launches == [["claude", "--version"]]


def test_gateway_serves_the_ordered_fallback_ladder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OMN-23413: repeated --fallback-backend-id reaches the served config in the
    order given; a repeated backend is refused before anything is served."""
    import omnimarket.handlers.claude_gateway as composition

    served: list[object] = []
    monkeypatch.setattr(composition, "serve_local_gateway", served.append)
    base = [
        "gateway",
        "--backend-id",
        "native-a",
        "--model",
        "claude-fixture",
        "--credential-ref",
        "gateway.fixture",
    ]
    ok = CliRunner().invoke(
        cli.claude_group,
        [
            *base,
            "--fallback-backend-id",
            "native-b",
            "--fallback-backend-id",
            "native-c",
        ],
    )
    assert ok.exit_code == 0, ok.output
    assert [getattr(c, "fallback_backend_ids", None) for c in served] == [
        ("native-b", "native-c")
    ]
    assert "Fallbacks: native-b, native-c" in ok.output
    repeated = CliRunner().invoke(
        cli.claude_group, [*base, "--fallback-backend-id", "native-a"]
    )
    assert repeated.exit_code != 0
    assert len(served) == 1


def _setup_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An ONEX home whose user config sits where onex looks for it."""
    home = tmp_path / ".onex"
    home.mkdir()
    monkeypatch.setattr(cli, "user_config_path", lambda: home / "config.yaml")
    return home


def test_enable_with_no_flags_uses_the_gateway_setup_started(
    calls: list[ModelClaudeGatewaySettingsRequest],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing is typed: the URL, model and credential come from setup's record."""
    home = _setup_home(tmp_path, monkeypatch)
    (home / "gateway-process.json").write_text(
        json.dumps(
            {
                "url": "http://127.0.0.1:8282",
                "credential_ref": "setup.gateway.fixture",
                "selection": {"connection_id": "lab", "model": "qwen-fixture"},
            }
        )
    )
    result = CliRunner().invoke(cli.claude_group, ["enable", "--yes", "--json"])
    assert result.exit_code == 0, result.output
    assert [(c.gateway_url, c.model, c.credential_ref) for c in calls] == [
        ("http://127.0.0.1:8282", "qwen-fixture", "setup.gateway.fixture")
    ]


def test_enable_with_no_flags_and_no_setup_refuses_and_writes_nothing(
    calls: list[ModelClaudeGatewaySettingsRequest],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a selected model there is no gateway, so Claude Code is not bound."""
    _setup_home(tmp_path, monkeypatch)
    result = CliRunner().invoke(cli.claude_group, ["enable", "--yes"])
    assert result.exit_code != 0
    assert "select a model" in result.output
    assert calls == []


def test_enable_refuses_some_flags_without_the_others(
    calls: list[ModelClaudeGatewaySettingsRequest],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _setup_home(tmp_path, monkeypatch)
    result = CliRunner().invoke(
        cli.claude_group, ["enable", "--model", "claude-fixture", "--yes"]
    )
    assert result.exit_code != 0
    assert "or none of them" in result.output
    assert calls == []
