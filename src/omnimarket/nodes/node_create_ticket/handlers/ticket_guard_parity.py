# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compare the node's installed ticket guard with the registered MCP hook's guard."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict

_TOOL_NAME = "mcp__linear-server__save_issue"
_HOOK_SCRIPT = "pre_tool_use_ticket_creation_gate.sh"
_GUARD_ASSIGNMENT = re.compile(
    r'^[ \t]*GUARD_PY="\$\{SCRIPT_DIR\}/([^"\r\n]+)"[ \t]*(?:#.*)?$',
    re.MULTILINE,
)
_GUARD_FILES = (
    "ticket_creation_guard.py",
    "ticket_creation_policy.json",
    "gate_binding_grammar.json",
)


class ModelGuardParity(BaseModel):
    """Paths and node-side digests, with every missing or differing file named."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    guard_path: str
    hook_guard_path: str
    files: dict[str, str]
    differing: list[str]

    @property
    def same(self) -> bool:
        """Whether all three files exist on both sides and have identical bytes."""
        return not self.differing


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(f"cannot read {path}: {exc}") from exc


def hook_guard_path(plugin_root: Path) -> Path:
    """Resolve the guard assigned by the registered ticket-creation hook script."""
    hooks_path = plugin_root / "hooks/hooks.json"
    try:
        document = json.loads(_read_text(hooks_path))
    except ValueError as exc:
        raise RuntimeError(f"invalid hook registration in {hooks_path}: {exc}") from exc
    hooks = document.get("hooks") if isinstance(document, dict) else None
    entries = hooks.get("PreToolUse") if isinstance(hooks, dict) else None
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        matcher = entry.get("matcher")
        if not isinstance(matcher, str):
            continue
        try:
            matched = re.search(matcher, _TOOL_NAME)
        except re.error as exc:
            raise RuntimeError(
                f"invalid PreToolUse matcher in {hooks_path}: {exc}"
            ) from exc
        if not matched:
            continue
        commands = entry.get("hooks")
        for hook in commands if isinstance(commands, list) else []:
            if not isinstance(hook, dict) or hook.get("type") != "command":
                continue
            command = hook.get("command")
            if not isinstance(command, str) or not command.endswith(_HOOK_SCRIPT):
                continue
            script = plugin_root / "hooks/scripts" / _HOOK_SCRIPT
            assignment = _GUARD_ASSIGNMENT.search(_read_text(script))
            if assignment is None:
                raise RuntimeError(f"no GUARD_PY assignment in {script}")
            return (script.resolve().parent / assignment.group(1)).resolve()
    raise RuntimeError(
        f"no PreToolUse hook registered for {_TOOL_NAME} ending in {_HOOK_SCRIPT} "
        f"in {hooks_path}"
    )


def _digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def guard_parity(node_guard: Path, hook_plugin_root: Path) -> ModelGuardParity:
    """Hash the guard, policy and grammar used by the node and the MCP hook."""
    node_guard = node_guard.resolve()
    hook_guard = hook_guard_path(hook_plugin_root)
    files: dict[str, str] = {}
    differing: list[str] = []
    for name in _GUARD_FILES:
        node_file = (
            node_guard
            if name == _GUARD_FILES[0]
            else node_guard.parent.parent / "config" / name
        )
        hook_file = (
            hook_guard
            if name == _GUARD_FILES[0]
            else hook_guard.parent.parent / "config" / name
        )
        node_digest, hook_digest = _digest(node_file), _digest(hook_file)
        if node_digest is not None:
            files[name] = node_digest
        if node_digest is None or hook_digest is None or node_digest != hook_digest:
            differing.append(name)
    return ModelGuardParity(
        guard_path=str(node_guard),
        hook_guard_path=str(hook_guard),
        files=files,
        differing=differing,
    )


def assert_guard_parity(node_guard: Path, hook_plugin_root: Path) -> ModelGuardParity:
    """Return parity evidence, or refuse with the names of differing files."""
    parity = guard_parity(node_guard, hook_plugin_root)
    if not parity.same:
        raise RuntimeError(
            f"ticket guard parity differs: {', '.join(parity.differing)}"
        )
    return parity
