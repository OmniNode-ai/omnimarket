# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Install the contract's gateway guards through the existing settings owner.

The original settings are backed up, writes share the gateway lock, and removal
touches only the exact command this install registered. Prompt data never enters
this operation. The companion shell installer dispatches here as well.
"""

from __future__ import annotations

import fcntl
import os
import shlex
import stat
import sys
from pathlib import Path
from typing import cast
from uuid import uuid4

import yaml
from omnibase_core.cli.cli_user_config import user_config_path
from pydantic import JsonValue

from omnimarket.nodes.node_claude_code_binding_install_effect.handlers.handler_claude_gateway_settings import (
    _atomic,
    _json_bytes,
    _object,
    _read,
)
from omnimarket.nodes.node_claude_code_binding_install_effect.models.model_gateway_hook_install import (
    ModelGatewayHookInstallRequest,
    ModelGatewayHookInstallResult,
    ModelGatewayHookOwnership,
    ModelGatewayHookProfileRow,
)

_MODULE = "omnimarket.nodes.node_claude_code_binding_install_effect.cli_gateway"


def _profile(command: str) -> dict[str, list[dict[str, JsonValue]]]:
    contract = yaml.safe_load((Path(__file__).parents[1] / "contract.yaml").read_text())
    rows = [
        ModelGatewayHookProfileRow.model_validate(row)
        for row in contract["metadata"]["gateway_hook_profile"]
    ]
    if len(rows) != 2 or {row.event for row in rows} != {
        "UserPromptSubmit",
        "PreToolUse",
    }:
        raise ValueError("Incomplete gateway hook profile")
    groups: dict[str, list[dict[str, JsonValue]]] = {}
    for row in rows:
        group: dict[str, JsonValue] = {
            "hooks": [
                {
                    "type": "command",
                    "command": command,
                    "timeout": row.timeout_seconds,
                    "onFailure": row.on_failure,
                }
            ]
        }
        if row.matcher is not None:
            group["matcher"] = row.matcher
        groups[row.event] = [group]
    return groups


def _hooks(settings: dict[str, JsonValue]) -> dict[str, list[dict[str, JsonValue]]]:
    value = settings.get("hooks", {})
    if not isinstance(value, dict):
        raise ValueError("Invalid hooks object")
    for groups in value.values():
        if not isinstance(groups, list):
            raise ValueError("Invalid hook groups")
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise ValueError("Invalid hook group")
            if any(
                not isinstance(hook, dict)
                for hook in cast("list[JsonValue]", group["hooks"])
            ):
                raise ValueError("Invalid hook")
    return cast("dict[str, list[dict[str, JsonValue]]]", value)


def _own_groups(
    settings: dict[str, JsonValue], command: str
) -> dict[str, list[dict[str, JsonValue]]]:
    result: dict[str, list[dict[str, JsonValue]]] = {}
    for event, groups in _hooks(settings).items():
        for group in groups:
            owned = [
                hook
                for hook in cast("list[dict[str, JsonValue]]", group["hooks"])
                if hook.get("command") == command
            ]
            if owned:
                result.setdefault(event, []).append(
                    {**group, "hooks": cast("JsonValue", owned)}
                )
    return result


def _remove(settings: dict[str, JsonValue], command: str) -> None:
    hooks = _hooks(settings)
    for event, groups in list(hooks.items()):
        kept = []
        for group in groups:
            original = cast("list[dict[str, JsonValue]]", group["hooks"])
            remaining = [hook for hook in original if hook.get("command") != command]
            if len(remaining) == len(original):
                kept.append(group)
            elif remaining:
                kept.append({**group, "hooks": cast("JsonValue", remaining)})
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if hooks:
        settings["hooks"] = cast("JsonValue", hooks)
    else:
        settings.pop("hooks", None)


class HandlerGatewayHookInstall:
    """One lock and atomic per-operation state for this install's two guard rows."""

    def __init__(
        self, *, onex_home: Path | None = None, claude_home: Path | None = None
    ) -> None:
        self._home = onex_home or user_config_path().parent
        self._claude = claude_home or Path(
            os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))
        )
        self._settings = self._claude / "settings.json"
        self._journal = self._home / "claude_gateway_hooks.json"
        self._command = shlex.join(
            [str(Path(sys.executable).absolute()), "-m", _MODULE, "hook"]
        )

    def _snapshot(
        self,
    ) -> tuple[bytes | None, dict[str, JsonValue], ModelGatewayHookOwnership | None]:
        raw = _read(self._settings)
        state = _read(self._journal)
        owner = (
            ModelGatewayHookOwnership.model_validate_json(state)
            if state is not None
            else None
        )
        if owner is not None:
            argv = shlex.split(owner.command)
            if (
                len(argv) != 4
                or argv[1:] != ["-m", _MODULE, "hook"]
                or not Path(argv[0]).is_absolute()
                or _read(self._home / owner.backup_name) is None
            ):
                raise ValueError("Invalid hook ownership")
        settings = _object(raw)
        _hooks(settings)
        return raw, settings, owner

    def _save(self, owner: ModelGatewayHookOwnership) -> None:
        _atomic(self._journal, (owner.model_dump_json() + "\n").encode())

    def handle(
        self, request: ModelGatewayHookInstallRequest
    ) -> ModelGatewayHookInstallResult:
        mutated = False
        try:
            raw, settings, owner = self._snapshot()
            if request.action == "status":
                registered = (
                    owner is not None
                    and owner.phase != "disabled"
                    and _own_groups(settings, owner.command) == _profile(owner.command)
                )
                return ModelGatewayHookInstallResult(
                    status="success", registered=registered
                )
            self._home.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(
                self._home / "claude_gateway.lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                0o600,
            )
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise ValueError("Invalid gateway lock")
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                raw, settings, owner = self._snapshot()
                if request.action == "uninstall":
                    if owner is None or owner.phase == "disabled":
                        return ModelGatewayHookInstallResult(status="success")
                    _remove(settings, owner.command)
                    output = _json_bytes(settings)
                    if output != raw:
                        _atomic(self._settings, output)
                        mutated = True
                    self._save(owner.model_copy(update={"phase": "disabled"}))
                    return ModelGatewayHookInstallResult(status="success", changed=True)
                expected = _profile(self._command)
                if (
                    owner is not None
                    and owner.phase == "enabled"
                    and owner.command == self._command
                    and _own_groups(settings, self._command) == expected
                ):
                    return ModelGatewayHookInstallResult(
                        status="success", registered=True
                    )
                if owner is None or owner.phase == "disabled":
                    if _own_groups(settings, self._command):
                        return ModelGatewayHookInstallResult(
                            status="action_required",
                            fix="An unowned gateway hook is already registered. Choose one owner before installing it again.",
                        )
                    name = f"claude_gateway_hooks.settings.{uuid4().hex}.backup"
                    _atomic(self._home / name, raw if raw is not None else b"{}\n")
                    owner = ModelGatewayHookOwnership(
                        phase="enabling", command=self._command, backup_name=name
                    )
                    self._save(owner)
                    mutated = True
                _remove(settings, owner.command)
                hooks = _hooks(settings)
                for event, groups in expected.items():
                    hooks.setdefault(event, []).extend(groups)
                settings["hooks"] = cast("JsonValue", hooks)
                output = _json_bytes(settings)
                if raw != output:
                    _atomic(self._settings, output)
                    mutated = True
                self._save(
                    owner.model_copy(
                        update={"phase": "enabled", "command": self._command}
                    )
                )
                return ModelGatewayHookInstallResult(
                    status="success", changed=True, registered=True
                )
            finally:
                os.close(fd)
        except BlockingIOError:
            return ModelGatewayHookInstallResult(
                status="action_required",
                changed=mutated,
                fix="Another gateway operation holds the settings lock; retry after it finishes.",
            )
        except (OSError, ValueError, KeyError, yaml.YAMLError):
            return ModelGatewayHookInstallResult(
                status="failure",
                changed=mutated,
                fix="The hook settings or ownership record could not be verified. Preserve the backups and repair the named configuration before retrying.",
            )
