# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Merge the shipped registration into a Codex user hooks file (OMN-18759).

The Codex marketplace repoint is owned by Codex itself
(``codex plugin marketplace add <clone>/plugins``) and hook trust by Codex
(``/hooks`` in the TUI, or the app-server's ``hooks/list`` +
``config/batchWrite`` of ``hooks.state.<key>.trusted_hash``), so this node does
only the hooks.json merge. It never touches config.toml.

The registration is the four bus-mirror emitters declaring ``--actor codex``
(OMN-18704). Existing user hooks keep their groups, order and settings; only
missing commands from the shipped registration are appended.

The registration file is read at run time from the omniclaude plugin root passed in.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omnimarket.nodes.node_codex_hook_registration_effect.enums import (
    EnumCodexHookRegistrationStatus,
)
from omnimarket.nodes.node_codex_hook_registration_effect.models import (
    ModelCodexHookRegistrationRequest,
    ModelCodexHookRegistrationResult,
)


def merge_registration(
    registration: dict[str, Any], existing: dict[str, Any], plugin_root: Path
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Copy the user config and append only commands missing from each event."""
    merged = deepcopy(existing)
    events = merged.setdefault("hooks", {})
    added: list[str] = []
    for event, groups in registration["hooks"].items():
        target_groups = events.setdefault(event, [])
        present = {
            hook["command"]
            for group in target_groups
            for hook in group.get("hooks", [])
            if "command" in hook
        }
        for group in groups:
            rewritten = deepcopy(group)
            missing = []
            for hook in rewritten["hooks"]:
                command = f"CLAUDE_PLUGIN_ROOT={plugin_root} " + hook[
                    "command"
                ].replace("${CLAUDE_PLUGIN_ROOT}", str(plugin_root))
                if command not in present:
                    hook["command"] = command
                    missing.append(hook)
                    added.append(command)
                    present.add(command)
            if missing:
                rewritten["hooks"] = missing
                target_groups.append(rewritten)
    return merged, tuple(added)


def _read_object(path: Path) -> dict[str, Any]:
    """Read a JSON object without silently accepting another top-level shape."""
    document: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        message = f"{path}: expected a JSON object"
        raise ValueError(message)
    return document


class HandlerCodexHookRegistration:
    """Merge missing commands, optionally backing up and replacing the user file."""

    def handle(
        self, request: ModelCodexHookRegistrationRequest
    ) -> ModelCodexHookRegistrationResult:
        hooks_file = request.codex_home / "hooks.json"
        backup_path = ""

        def failed(exc: Exception) -> ModelCodexHookRegistrationResult:
            return ModelCodexHookRegistrationResult(
                status=EnumCodexHookRegistrationStatus.FAILED,
                exit_code=1,
                hooks_file=str(hooks_file),
                backup_path=backup_path,
                messages=(str(exc),),
            )

        try:
            registration = _read_object(
                request.plugin_root / "hooks" / "codex" / "hooks.json"
            )
            if not isinstance(registration.get("hooks"), dict):
                message = "registration must contain a hooks object"
                raise ValueError(message)
            try:
                existing = _read_object(hooks_file)
            except FileNotFoundError:
                existing = {}
            merged, added = merge_registration(
                registration, existing, request.plugin_root
            )
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            return failed(exc)

        if not added:
            status = EnumCodexHookRegistrationStatus.UNCHANGED
        elif not request.apply:
            status = EnumCodexHookRegistrationStatus.WOULD_ADD
        else:
            temporary: Path | None = None
            try:
                request.codex_home.mkdir(parents=True, exist_ok=True)
                if hooks_file.exists():
                    backup = (
                        request.codex_home
                        / f"hooks.json.bak.{request.now:%Y%m%dT%H%M%SZ}"
                    )
                    shutil.copy2(hooks_file, backup)
                    backup_path = str(backup)
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=request.codex_home,
                    prefix=".hooks.json.",
                    suffix=".tmp",
                    delete=False,
                ) as stream:
                    temporary = Path(stream.name)
                    stream.write(json.dumps(merged, indent=2) + "\n")
                os.replace(temporary, hooks_file)  # Atomic replacement.
            except OSError as exc:
                return failed(exc)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            status = EnumCodexHookRegistrationStatus.ADDED

        return ModelCodexHookRegistrationResult(
            status=status,
            exit_code=0,
            hooks_file=str(hooks_file),
            added_commands=added,
            backup_path=backup_path,
        )


def main(argv: list[str] | None = None) -> int:
    """Parse the filesystem operands and apply flag, then print the result as JSON."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin-root", required=True, type=Path)
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser(),
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    result = HandlerCodexHookRegistration().handle(
        ModelCodexHookRegistrationRequest(
            plugin_root=args.plugin_root,
            codex_home=args.codex_home,
            apply=args.apply,
            now=datetime.now(UTC),
        )
    )
    sys.stdout.write(result.model_dump_json() + "\n")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
