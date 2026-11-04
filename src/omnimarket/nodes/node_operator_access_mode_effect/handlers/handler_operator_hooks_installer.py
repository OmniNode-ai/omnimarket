# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Install the operator-supervision hooks into a Claude Code settings file (OMN-20892).

Copies this node package and its overlay-declared dependency closure into ``<hooks dir>/operator-access-mode/`` (under two stub package
files, so it imports without the repository). Stop uses core and Pydantic from the
runtime Python supplied to the installer; prompt and drain keep their standard-library
bootstrap. Registers one command per event, each ``PYTHONPATH=<copy> <python> -B -m <entry module> <event>``:

* SessionStart (startup, resume, clear, compact): ``session-start``;
* UserPromptSubmit: ``prompt``;
* PreToolUse on Bash, WebFetch, WebSearch, PushNotification, SendUserMessage, Workflow and Agent:
  ``pre-tool``;
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

from ....models.model_operator_message_spool import (
    ModelOperatorSpoolUnitRequest,
    ModelOperatorSpoolUnitResult,
)

MARKER = "operator-access-mode"
RETIRED_MARKERS = ("foreground-bash-shape-guard.py",)
NODE_DIR = Path(__file__).resolve().parents[1]
ENTRY_MODULE = f"omnimarket.nodes.{NODE_DIR.name}.runtime_operator_hook_cli"
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
        "^(Bash|WebFetch|WebSearch|PushNotification|SendUserMessage|Workflow|Agent)$",
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
    # The isolated copy is an import closure, not just the hook's node. Node
    # shells are not executed here; stub their package exports to avoid loading
    # a runtime simply to import a pure dependency handler.
    import json

    policy = json.loads((NODE_DIR / "unanswered_guard.overlay.json").read_text())
    package = NODE_DIR.parents[1]
    for relative in policy["dependency_packages"]:
        destination = root / "omnimarket" / relative
        shutil.copytree(
            package / relative,
            destination,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        (destination / "__init__.py").write_text(
            '"""Installed hook dependency; contract resolves its handler."""\n',
            encoding="utf-8",
        )
    for relative in policy["dependency_files"]:
        destination = root / "omnimarket" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        (destination.parent / "__init__.py").touch()
        shutil.copy2(package / relative, destination)
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


class HandlerOperatorSpoolDrainUnit:
    """Render the independent host unit; the host installer owns loading it."""

    def handle(
        self, request: ModelOperatorSpoolUnitRequest
    ) -> ModelOperatorSpoolUnitResult:
        import plistlib

        from ....handlers.operator_message_spool import load_arrival_policy

        env = dict(request.environment)
        env["PYTHONPATH"] = request.copy_root
        if request.overlay_path:
            env["ONEX_OPERATOR_MESSAGE_OVERLAY"] = request.overlay_path
        policy = load_arrival_policy(env)
        argv = [request.python, "-B", "-m", ENTRY_MODULE, "drain"]
        if request.platform == "launchd":
            unit = {
                "Label": policy.drain_label,
                "ProgramArguments": argv,
                "StartInterval": policy.drain_interval_seconds,
                "RunAtLoad": True,
                "EnvironmentVariables": env,
                "ProcessType": "Background",
            }
            return ModelOperatorSpoolUnitResult(
                files={
                    f"{policy.drain_label}.plist": plistlib.dumps(unit).decode("utf-8")
                }
            )
        service = "[Unit]\nDescription=Drain durable operator-message rows\n[Service]\nType=oneshot\n"
        service += "ExecStart=" + " ".join(shlex.quote(arg) for arg in argv) + "\n"
        service += "".join(
            "Environment=" + shlex.quote(f"{key}={value}") + "\n"
            for key, value in sorted(env.items())
        )
        timer = f"[Unit]\nDescription=Drain durable operator-message rows\n[Timer]\nOnBootSec={policy.drain_interval_seconds}s\nOnUnitActiveSec={policy.drain_interval_seconds}s\nUnit={policy.drain_label}.service\n[Install]\nWantedBy=timers.target\n"
        return ModelOperatorSpoolUnitResult(
            files={
                f"{policy.drain_label}.service": service,
                f"{policy.drain_label}.timer": timer,
            }
        )
