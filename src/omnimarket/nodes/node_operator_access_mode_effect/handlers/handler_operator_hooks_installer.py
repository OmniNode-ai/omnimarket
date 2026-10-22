# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Install the operator-supervision hooks into a Claude Code settings file (OMN-20892).

Copies this node package into ``<hooks dir>/operator-access-mode/`` (under two stub package
files, so the copy imports without the rest of omnimarket and without a venv) and registers
one command per event, each ``PYTHONPATH=<copy> <python> -B -m <entry module> <event>``:

* SessionStart (startup, resume, clear, compact): ``session-start``;
* UserPromptSubmit: ``prompt``;
* PreToolUse on Bash, WebFetch, WebSearch, PushNotification and SendUserMessage: ``pre-tool``;
* Stop: ``stop``.

The Bash rules include the shape rules of the host's untracked ``foreground-bash-shape-guard.py``,
so installing retires that file's settings entry (the file itself is left in place). Install is
idempotent: an earlier registration is replaced, never duplicated, and nothing else in the file
changes. Uninstall removes exactly these entries and the copy; it does not restore the retired
entry. Hooks load at session start: a running session sees the change after a restart.
"""

from __future__ import annotations

import shlex
import shutil
from pathlib import Path
from typing import Any

MARKER = "operator-access-mode"
RETIRED_MARKERS = ("foreground-bash-shape-guard.py",)
ENTRY_MODULE = "omnimarket.nodes.node_operator_access_mode_effect.handlers.handler_operator_hook_cli"
NODE_DIR = Path(__file__).resolve().parents[1]
REGISTRATIONS: tuple[tuple[str, str | None, str, str], ...] = (
    (
        "SessionStart",
        "startup|resume|clear|compact",
        "session-start",
        "Reading access mode...",
    ),
    ("UserPromptSubmit", None, "prompt", "Recording operator prompt..."),
    (
        "PreToolUse",
        "^(Bash|WebFetch|WebSearch|PushNotification|SendUserMessage)$",
        "pre-tool",
        "Checking operator supervision rules...",
    ),
    ("Stop", None, "stop", "Checking the reply to the operator..."),
)


def copy_root(hooks_dir: Path) -> Path:
    return hooks_dir / MARKER


def _copy_package(hooks_dir: Path) -> Path:
    root = copy_root(hooks_dir)
    if root.exists():
        shutil.rmtree(root)
    target = root / "omnimarket" / "nodes" / NODE_DIR.name
    shutil.copytree(
        NODE_DIR, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )
    (root / "omnimarket" / "__init__.py").write_text(
        '"""Stub: the installed operator-supervision hooks only."""\n', encoding="utf-8"
    )
    (root / "omnimarket" / "nodes" / "__init__.py").write_text(
        '"""Stub: the installed operator-supervision hooks only."""\n', encoding="utf-8"
    )
    return root


def command_for(root: Path, python: str, event: str) -> str:
    return f"PYTHONPATH={shlex.quote(str(root))} {shlex.quote(python)} -B -m {ENTRY_MODULE} {event}"


def _mentions(entry: object, markers: tuple[str, ...]) -> bool:
    if not isinstance(entry, dict):
        return False
    hooks = entry.get("hooks")
    if not isinstance(hooks, list):
        return False
    return any(
        isinstance(hook, dict)
        and any(m in str(hook.get("command", "")) for m in markers)
        for hook in hooks
    )


def _strip(settings: dict[str, Any], markers: tuple[str, ...]) -> dict[str, Any]:
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return settings
    for event in list(hooks):
        entries = hooks[event]
        if not isinstance(entries, list):
            continue
        kept = [entry for entry in entries if not _mentions(entry, markers)]
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if not hooks:
        del settings["hooks"]
    return settings


def install_operator_hooks(
    settings: dict[str, Any], hooks_dir: Path, python: str
) -> Path:
    """Copy the package and register its hooks in ``settings`` (mutated and returned via root)."""
    _strip(settings, (MARKER, *RETIRED_MARKERS))
    root = _copy_package(hooks_dir)
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("'hooks' is not a JSON object; refusing to rewrite it")
    for event, matcher, argument, status in REGISTRATIONS:
        entries = hooks.setdefault(event, [])
        if not isinstance(entries, list):
            raise ValueError(f"'hooks.{event}' is not a list; refusing to rewrite it")
        entry: dict[str, Any] = {}
        if matcher is not None:
            entry["matcher"] = matcher
        entry["hooks"] = [
            {
                "type": "command",
                "command": command_for(root, python, argument),
                "timeout": 10,
                "statusMessage": status,
            }
        ]
        entries.append(entry)
    return root


def uninstall_operator_hooks(settings: dict[str, Any], hooks_dir: Path) -> None:
    _strip(settings, (MARKER,))
    root = copy_root(hooks_dir)
    if root.exists():
        shutil.rmtree(root)
