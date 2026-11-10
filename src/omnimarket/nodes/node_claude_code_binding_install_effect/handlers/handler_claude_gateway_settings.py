# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Own only ONEX's gateway binding; never replace the user's Claude settings."""

from __future__ import annotations

import errno
import fcntl
import json
import os
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import cast
from uuid import uuid4

from omnibase_core.cli.cli_user_config import user_config_path
from pydantic import JsonValue

from omnimarket.nodes.node_claude_code_binding_install_effect.handlers.gateway_probe import (
    probe_gateway,
)
from omnimarket.nodes.node_claude_code_binding_install_effect.models.model_claude_gateway_settings import (
    ModelClaudeGatewayOwnership,
    ModelClaudeGatewaySettingsRequest,
    ModelClaudeGatewaySettingsResult,
)

_LIMIT = 1024 * 1024
_MODEL_KEYS = (
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
)
_AUTH_KEYS = frozenset(
    ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")
)


def _read(path: Path) -> bytes | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size > _LIMIT:
        raise ValueError("Expected a bounded regular configuration file")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Expected a regular configuration file")
        data = stream.read(_LIMIT + 1)
    if len(data) > _LIMIT:
        raise ValueError("Configuration too large")
    return data


def _object(raw: bytes | None) -> dict[str, JsonValue]:
    data = json.loads(raw) if raw is not None else {}
    if not isinstance(data, dict):
        raise ValueError("Expected settings object")
    env = data.get("env", {})
    if not isinstance(env, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in env.items()
    ):
        raise ValueError("Expected string environment")
    return cast("dict[str, JsonValue]", data)


def _atomic(path: Path, raw: bytes) -> None:
    _read(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".onex-gateway-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            try:
                os.fsync(directory)
            except OSError as exc:
                # macOS does not support fsync on every directory filesystem.
                if exc.errno not in (errno.EINVAL, errno.ENOTSUP):
                    raise
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _json_bytes(data: dict[str, JsonValue]) -> bytes:
    return (json.dumps(data, indent=2, ensure_ascii=True) + "\n").encode()


def _owned(request: ModelClaudeGatewaySettingsRequest) -> dict[str, str]:
    assert request.gateway_url is not None
    assert request.model is not None
    assert request.credential_helper is not None
    return {
        "env.ANTHROPIC_BASE_URL": request.gateway_url,
        **{f"env.{key}": request.model for key in _MODEL_KEYS},
        "apiKeyHelper": request.credential_helper,
    }


def _lookup(settings: dict[str, JsonValue], key: str) -> JsonValue:
    if key.startswith("env."):
        return cast("dict[str, JsonValue]", settings.get("env", {})).get(key[4:])
    return settings.get(key)


def _conflicts(
    settings: dict[str, JsonValue], environment: dict[str, str], owned: dict[str, str]
) -> tuple[str, ...]:
    result: set[str] = set()
    configured = cast("dict[str, JsonValue]", settings.get("env", {}))
    for prefix, env in (("env.", configured), ("environment.", environment)):
        for name, value in env.items():
            active = str(value).lower() not in ("", "0", "false")
            if active and (name in _AUTH_KEYS or name.startswith("CLAUDE_CODE_USE_")):
                result.add(prefix + name)
            key = "env." + name
            if name in ("ANTHROPIC_BASE_URL", *_MODEL_KEYS) and (
                key not in owned or value != owned[key]
            ):
                result.add(prefix + name)
    for key in (
        "apiKeyHelper",
        "env.ANTHROPIC_BASE_URL",
        *(f"env.{name}" for name in _MODEL_KEYS),
    ):
        value = _lookup(settings, key)
        if value is not None and (key not in owned or value != owned[key]):
            result.add(key)
    return tuple(sorted(result))


class HandlerClaudeGatewaySettings:
    """The existing Claude binding effect's gateway configuration operation."""

    def __init__(
        self,
        *,
        onex_home: Path | None = None,
        claude_home: Path | None = None,
        probe: Callable[[ModelClaudeGatewaySettingsRequest], bool] | None = None,
        working_directory: Path | None = None,
    ) -> None:
        self._home = (onex_home or user_config_path().parent).expanduser().absolute()
        claude_root = claude_home or Path(
            os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))
        )
        self._settings = claude_root.expanduser().absolute() / "settings.json"
        self._journal = self._home / "claude_gateway.json"
        self._lock = self._home / "claude_gateway.lock"
        self._probe = probe or probe_gateway
        self._cwd = (
            working_directory.expanduser().resolve()
            if working_directory is not None
            else None
        )

    def _scope_conflicts(self, owned: dict[str, str]) -> tuple[str, ...]:
        if self._cwd is None:
            return ()
        conflicts: list[str] = []
        for directory in (self._cwd, *self._cwd.parents):
            for name in ("settings.json", "settings.local.json"):
                path = directory / ".claude" / name
                if path == self._settings:
                    continue
                raw = _read(path)
                if raw is not None:
                    conflicts.extend(
                        f"{path}:{key}" for key in _conflicts(_object(raw), {}, owned)
                    )
            if (directory / ".git").exists():
                break
        return tuple(conflicts)

    def _snapshot(
        self,
    ) -> tuple[bytes | None, dict[str, JsonValue], ModelClaudeGatewayOwnership | None]:
        raw = _read(self._settings)
        journal = _read(self._journal)
        _read(self._lock)
        _read(self._home / "claude_gateway.settings.backup")
        owner = (
            ModelClaudeGatewayOwnership.model_validate_json(journal)
            if journal is not None
            else None
        )
        if owner is not None and _read(self._home / owner.backup_name) is None:
            raise ValueError("Missing original settings backup")
        return raw, _object(raw), owner

    @staticmethod
    def _refused(conflicts: tuple[str, ...]) -> ModelClaudeGatewaySettingsResult:
        return ModelClaudeGatewaySettingsResult(
            status="action_required",
            conflicts=conflicts,
            fix="Resolve the named Claude configuration conflicts, then retry onex claude enable.",
        )

    def _save_owner(self, owner: ModelClaudeGatewayOwnership) -> None:
        _atomic(self._journal, (owner.model_dump_json() + "\n").encode())

    def configured(self) -> bool:
        """Whether this install owns an active binding; this never probes or writes."""
        _, _, owner = self._snapshot()
        return owner is not None and owner.phase != "disabled"

    def handle(
        self, request: ModelClaudeGatewaySettingsRequest
    ) -> ModelClaudeGatewaySettingsResult:
        mutated = False
        try:
            _, settings, owner = self._snapshot()
            active = owner is not None and owner.phase != "disabled"
            previous = (
                _owned(owner.enable_request(request.environment))
                if active and owner is not None
                else {}
            )
            conflicts = _conflicts(settings, request.environment, previous)
            if request.action != "disable":
                expected = _owned(request) if request.action == "enable" else previous
                scoped = self._scope_conflicts(expected)
                if scoped:
                    return self._refused(scoped)
            if request.action == "status":
                if not active or owner is None:
                    return ModelClaudeGatewaySettingsResult(status="unavailable")
                drift = tuple(
                    key
                    for key, value in previous.items()
                    if _lookup(settings, key) != value
                )
                if conflicts or drift:
                    return self._refused(tuple(sorted({*conflicts, *drift})))
                available = self._probe(owner.enable_request(request.environment))
                return ModelClaudeGatewaySettingsResult(
                    status="success" if available else "unavailable",
                    enabled=True,
                    gateway_url=owner.gateway_url,
                    model=owner.model,
                    fix=None
                    if available
                    else "Restore gateway connectivity; no direct-provider fallback is enabled.",
                )
            if request.action == "enable":
                if conflicts:
                    return self._refused(conflicts)
                if active and previous != _owned(request):
                    return self._refused(("gateway.selection",))
                if not self._probe(request):
                    return ModelClaudeGatewaySettingsResult(
                        status="unavailable",
                        fix="Check the gateway URL, credential and model; then retry onex claude enable.",
                    )
            elif not active:
                return ModelClaudeGatewaySettingsResult(status="success")

            self._home.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self._lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise ValueError("Unsafe lock")
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                raw, settings, current = self._snapshot()
                if current != owner:
                    return self._refused(("gateway.concurrent_change",))
                if request.action == "disable":
                    assert owner is not None
                    conflicts = tuple(
                        key
                        for key, value in previous.items()
                        if _lookup(settings, key) is not None
                        and _lookup(settings, key) != value
                    )
                    env = cast("dict[str, JsonValue]", settings.get("env", {}))
                    for key, value in previous.items():
                        if _lookup(settings, key) == value:
                            if key.startswith("env."):
                                env.pop(key[4:], None)
                            else:
                                settings.pop(key, None)
                    if not env and not owner.original_env_present:
                        settings.pop("env", None)
                    original = _read(self._home / owner.backup_name)
                    restored = (
                        original
                        if _object(original) == settings
                        else _json_bytes(settings)
                    )
                    if raw != restored:
                        _atomic(
                            self._settings,
                            restored if restored is not None else b"{}\n",
                        )
                        mutated = True
                    self._save_owner(owner.model_copy(update={"phase": "disabled"}))
                    return ModelClaudeGatewaySettingsResult(
                        status="action_required" if conflicts else "success",
                        changed=True,
                        conflicts=conflicts,
                        fix="Review the edited fields retained in Claude settings."
                        if conflicts
                        else None,
                    )
                conflicts = _conflicts(settings, request.environment, previous)
                if conflicts:
                    return self._refused(conflicts)
                desired = _owned(request)
                if active and all(
                    _lookup(settings, key) == value for key, value in desired.items()
                ):
                    return ModelClaudeGatewaySettingsResult(
                        status="success",
                        enabled=True,
                        gateway_url=request.gateway_url,
                        model=request.model,
                    )
                if owner is None or owner.phase == "disabled":
                    name = (
                        "claude_gateway.settings.backup"
                        if owner is None
                        else f"claude_gateway.settings.{uuid4().hex}.backup"
                    )
                    if _read(self._home / name) is not None:
                        return self._refused(("gateway.unowned_backup",))
                    _atomic(self._home / name, raw if raw is not None else b"{}\n")
                    mutated = True
                    assert request.gateway_url is not None
                    assert request.model is not None
                    assert request.credential_ref is not None
                    assert request.credential_helper is not None
                    owner = ModelClaudeGatewayOwnership(
                        phase="enabling",
                        gateway_url=request.gateway_url,
                        model=request.model,
                        credential_ref=request.credential_ref,
                        credential_helper=request.credential_helper,
                        original_env_present="env" in settings,
                        backup_name=name,
                    )
                    self._save_owner(owner)
                env = cast("dict[str, JsonValue]", settings.setdefault("env", {}))
                for key, value in desired.items():
                    if key.startswith("env."):
                        env[key[4:]] = value
                    else:
                        settings[key] = value
                _atomic(self._settings, _json_bytes(settings))
                mutated = True
                self._save_owner(owner.model_copy(update={"phase": "enabled"}))
                return ModelClaudeGatewaySettingsResult(
                    status="success",
                    changed=True,
                    enabled=True,
                    gateway_url=request.gateway_url,
                    model=request.model,
                )
            finally:
                os.close(fd)
        except Exception:
            return ModelClaudeGatewaySettingsResult(
                status="failure",
                changed=mutated,
                fix="Inspect Claude settings and the ONEX gateway backup; repair unsafe or invalid files before retrying.",
            )
