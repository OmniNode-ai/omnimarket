# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read-only proof of the dependency closure installed by the legacy owner."""

import json
import shlex
import sys
from pathlib import Path
from typing import Any

from .models.model_setup_observation import ModelSetupObservation


def _files(root: Path) -> dict[Path, Path]:
    return {
        path.relative_to(root): path
        for tree in (root / "hooks", root / "lib")
        for path in tree.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and not path.is_relative_to(root / "hooks" / "tests")
        and path.suffix != ".pyc"
    }


def observe_legacy_hooks(installer: Path, claude_home: Path) -> ModelSetupObservation:
    """Compare files and exact registered rows; never infer success from a receipt."""
    source = installer.parent.parent / "plugins" / "onex"
    root = Path.home() / ".omninode" / "delegation"
    target = root / "plugin" / "plugins" / "onex"
    manifest = json.loads((source / "hooks" / "hooks-delegation.v1.json").read_text())

    def rewrite(value: Any, key: str = "") -> Any:
        if isinstance(value, str):
            value = value.replace("${CLAUDE_PLUGIN_ROOT}", str(target))
            return (
                f"PLUGIN_PYTHON_BIN={shlex.quote(sys.executable)} {value}"
                if key == "command"
                else value
            )
        if isinstance(value, list):
            return [rewrite(item) for item in value]
        if isinstance(value, dict):
            return {name: rewrite(item, name) for name, item in value.items()}
        return value

    expected = rewrite(manifest)
    rows = expected.get("hooks")
    if not isinstance(rows, dict) or not rows:
        raise ValueError(
            "The installed customer bundle has no delegation hook manifest"
        )
    expected_files = _files(source)
    actual_files = _files(target)
    missing = set(expected_files) - set(actual_files)
    changed = set(actual_files) - set(expected_files)
    for name in set(expected_files) & set(actual_files):
        actual = actual_files[name]
        if (
            actual.is_symlink()
            or actual.read_bytes() != expected_files[name].read_bytes()
        ):
            changed.add(name)
    config = root / "hooks-delegation.json"
    settings_path = claude_home / "settings.json"
    if config.exists() and json.loads(config.read_text()) != expected:
        changed.add(Path("hooks-delegation.json"))
    settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    installed_rows = settings.get("hooks", {})
    wanted_commands = {
        hook["command"]
        for groups in rows.values()
        for group in groups
        for hook in group["hooks"]
    }
    current_commands = [
        hook.get("command", "")
        for groups in installed_rows.values()
        for group in groups
        for hook in group.get("hooks", [])
    ]
    if any(
        str(root) + "/" in command and command not in wanted_commands
        for command in current_commands
    ):
        changed.add(Path("settings.json"))
    if any(current_commands.count(command) > 1 for command in wanted_commands):
        changed.add(Path("settings.json"))
    registered = True
    for event, groups in rows.items():
        for group in groups:
            metadata = {key: value for key, value in group.items() if key != "hooks"}
            for wanted in group["hooks"]:
                matches = [
                    (actual_event, actual_group, actual)
                    for actual_event, actual_groups in installed_rows.items()
                    for actual_group in actual_groups
                    for actual in actual_group.get("hooks", [])
                    if actual.get("command") == wanted["command"]
                ]
                registered = registered and bool(matches)
                for actual_event, actual_group, actual in matches:
                    actual_metadata = {
                        key: value
                        for key, value in actual_group.items()
                        if key != "hooks"
                    }
                    if (
                        actual_event != event
                        or actual_metadata != metadata
                        or actual != wanted
                    ):
                        changed.add(Path("settings.json"))
    if changed:
        return ModelSetupObservation(
            step="claude-hooks",
            status="action_required",
            fix="Installed delegation files or hook rows changed after installation. Preserve and review those changes before repair or removal.",
        )
    complete = bool(expected_files) and not missing and config.is_file() and registered
    return ModelSetupObservation(
        step="claude-hooks",
        status="success" if complete else "unavailable",
        evidence={
            "legacy_installer": str(installer),
            "verified_files": str(len(actual_files)),
        },
        fix=None
        if complete
        else "Run onex setup --action repair with claude-hooks selected to install the bundled delegation hook dependency closure.",
    )
