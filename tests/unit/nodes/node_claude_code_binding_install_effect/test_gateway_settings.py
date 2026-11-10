# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Gateway configuration failures: collisions, drift, unsafe files and failed probes."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_claude_code_binding_install_effect.handlers.handler_claude_gateway_settings import (
    HandlerClaudeGatewaySettings,
)
from omnimarket.nodes.node_claude_code_binding_install_effect.models.model_claude_gateway_settings import (
    ModelClaudeGatewaySettingsRequest,
)

pytestmark = pytest.mark.unit


def request(**changes: object) -> ModelClaudeGatewaySettingsRequest:
    values: dict[str, object] = {
        "action": "enable",
        "gateway_url": "http://127.0.0.1:8181",
        "model": "claude-fixture",
        "credential_ref": "gateway.fixture",
        "credential_helper": "'/safe/onex' claude credential --ref gateway.fixture",
    }
    values.update(changes)
    return ModelClaudeGatewaySettingsRequest.model_validate(values)


def handler(tmp_path: Path, *, available: bool = True) -> HandlerClaudeGatewaySettings:
    return HandlerClaudeGatewaySettings(
        onex_home=tmp_path / ".onex",
        claude_home=tmp_path / ".claude",
        probe=lambda _: available,
    )


def settings(tmp_path: Path) -> Path:
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir(exist_ok=True)
    return path


def test_enable_preserves_unrelated_and_has_no_literal_credential(
    tmp_path: Path,
) -> None:
    path = settings(tmp_path)
    original = {
        "hooks": {"Stop": []},
        "permissions": {"deny": ["Bash(rm *)"]},
        "env": {"KEEP": "yes"},
    }
    path.write_text(json.dumps(original))
    result = handler(tmp_path).handle(request())
    assert result.status == "success"
    assert result.changed
    assert result.enabled
    actual = json.loads(path.read_text())
    assert actual["hooks"] == original["hooks"]
    assert actual["permissions"] == original["permissions"]
    assert actual["env"]["KEEP"] == "yes"
    assert actual["env"]["ANTHROPIC_BASE_URL"] == request().gateway_url
    for name in (
        "ANTHROPIC_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
    ):
        assert actual["env"][name] == "claude-fixture"
    assert actual["apiKeyHelper"] == request().credential_helper
    assert "ANTHROPIC_AUTH_TOKEN" not in actual["env"]
    for owned in (
        path,
        tmp_path / ".onex" / "claude_gateway.json",
        tmp_path / ".onex" / "claude_gateway.settings.backup",
    ):
        assert stat.S_IMODE(owned.stat().st_mode) == 0o600


def test_failed_probe_writes_nothing(tmp_path: Path) -> None:
    result = handler(tmp_path, available=False).handle(request())
    assert result.status == "unavailable"
    assert not result.changed
    assert list(tmp_path.iterdir()) == []


def test_rerun_is_byte_identical_but_rechecks_gateway(tmp_path: Path) -> None:
    assert handler(tmp_path).handle(request()).enabled
    before = {
        p: (p.read_bytes(), p.stat().st_mtime_ns)
        for p in tmp_path.rglob("*")
        if p.is_file()
    }
    result = handler(tmp_path).handle(request())
    assert result.status == "success"
    assert not result.changed
    assert before == {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before}
    result = handler(tmp_path, available=False).handle(request())
    assert result.status == "unavailable"
    assert not result.changed
    assert before == {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before}


@pytest.mark.parametrize("location", ["settings", "environment"])
@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("ANTHROPIC_API_KEY", "SENTINEL-private"),
        ("ANTHROPIC_AUTH_TOKEN", "SENTINEL-private"),
        ("CLAUDE_CODE_USE_BEDROCK", "1"),
        ("ANTHROPIC_BASE_URL", "https://other.example"),
    ],
)
def test_collision_never_overwrites_or_echoes_values(
    tmp_path: Path, location: str, name: str, value: str
) -> None:
    inherited = {name: value} if location == "environment" else {}
    if location == "settings":
        path = settings(tmp_path)
        path.write_text(json.dumps({"env": {name: value}}))
        before = path.read_bytes()
    result = handler(tmp_path).handle(request(environment=inherited))
    assert result.status == "action_required"
    assert not result.changed
    assert not result.enabled
    assert name in " ".join(result.conflicts)
    assert value not in result.model_dump_json()
    assert not (tmp_path / ".onex").exists()
    if location == "settings":
        assert path.read_bytes() == before


def test_disable_restores_original_bytes(tmp_path: Path) -> None:
    path = settings(tmp_path)
    original = b'{ "hooks" : {}, "env": {"KEEP": "yes"} }\n'
    path.write_bytes(original)
    owner = handler(tmp_path)
    assert owner.handle(request()).enabled
    result = owner.handle(request(action="disable"))
    assert result.status == "success"
    assert result.changed
    assert not result.enabled
    assert path.read_bytes() == original
    assert not owner.handle(request(action="disable")).changed


def test_disable_preserves_later_edits_and_removes_only_owned_values(
    tmp_path: Path,
) -> None:
    owner = handler(tmp_path)
    assert owner.handle(request()).enabled
    path = settings(tmp_path)
    data = json.loads(path.read_text())
    data["env"]["ANTHROPIC_BASE_URL"] = "https://user-edited.example"
    data["env"]["NEW"] = "keep"
    data["extra"] = ["keep"]
    path.write_text(json.dumps(data))
    result = owner.handle(request(action="disable"))
    assert result.status == "action_required"
    assert result.changed
    assert not result.enabled
    assert result.conflicts == ("env.ANTHROPIC_BASE_URL",)
    actual = json.loads(path.read_text())
    assert actual == {
        "env": {"ANTHROPIC_BASE_URL": "https://user-edited.example", "NEW": "keep"},
        "extra": ["keep"],
    }


def test_status_detects_settings_drift_without_writes(tmp_path: Path) -> None:
    owner = handler(tmp_path)
    assert owner.handle(request()).enabled
    path = settings(tmp_path)
    path.write_text('{"apiKeyHelper": "user helper"}')
    before = path.read_bytes()
    result = owner.handle(request(action="status"))
    assert result.status == "action_required"
    assert not result.enabled
    assert not result.changed
    assert path.read_bytes() == before


@pytest.mark.parametrize("target", ["settings", "journal", "backup"])
def test_symlink_refusal_preserves_target(tmp_path: Path, target: str) -> None:
    outsider = tmp_path / "outside"
    outsider.write_text("{}")
    paths = {
        "settings": settings(tmp_path),
        "journal": tmp_path / ".onex" / "claude_gateway.json",
        "backup": tmp_path / ".onex" / "claude_gateway.settings.backup",
    }
    path = paths[target]
    path.parent.mkdir(exist_ok=True)
    path.symlink_to(outsider)
    result = handler(tmp_path).handle(request())
    assert result.status == "failure"
    assert not result.changed
    assert outsider.read_text() == "{}"
    assert path.is_symlink()


def test_invalid_settings_do_not_echo_the_input(tmp_path: Path) -> None:
    path = settings(tmp_path)
    path.write_text('{"SENTINEL-private":')
    result = handler(tmp_path).handle(request())
    assert result.status == "failure"
    assert not result.changed
    assert "SENTINEL" not in result.model_dump_json()


@pytest.mark.parametrize(
    "url",
    [
        "ftp://localhost",
        "http://remote.example",
        "https://u:secret@host",
        "https://host/?token=secret",
        "https://host/#secret",
        "https://host/\nsecret",
    ],
)
def test_invalid_gateway_url_is_refused(url: str) -> None:
    with pytest.raises(ValidationError):
        request(gateway_url=url)


def test_enable_requires_complete_choices() -> None:
    with pytest.raises(ValidationError):
        ModelClaudeGatewaySettingsRequest(action="enable")
