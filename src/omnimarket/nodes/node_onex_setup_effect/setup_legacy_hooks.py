# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read-only proof of the dependency closure installed by the legacy owner."""

import hashlib
import json
import shlex
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from omnibase_core.cli.cli_user_config import user_config_path

from .models.model_setup_observation import ModelSetupObservation

#: The script the delegation hook runs (the prompt hook, on UserPromptSubmit).
DELEGATION_SCRIPT = "/hooks/scripts/pre_tool_use_delegation.sh"


class ClaudeSettingsError(ValueError):
    """Claude Code's settings.json cannot be used as it is; the message says why."""


def read_claude_settings(claude_home: Path) -> dict[str, Any]:
    """Claude Code's user settings, through a symlink as Claude Code reads them;
    an absent file is no settings."""
    path = claude_home / "settings.json"
    try:
        text = path.read_text()
    except FileNotFoundError:
        return {}
    except OSError as error:
        raise ClaudeSettingsError(f"{path} cannot be read ({error.strerror})") from None
    if not text.strip():
        raise ClaudeSettingsError(
            f"{path} is empty, which is not valid JSON; delete it or write {{}} into it"
        )
    try:
        settings = json.loads(text)
    except json.JSONDecodeError as error:
        raise ClaudeSettingsError(
            f"{path} is not valid JSON (line {error.lineno})"
        ) from None
    if not isinstance(settings, dict):
        raise ClaudeSettingsError(f"{path} must hold a JSON object")
    hook_rows(settings, path)
    return settings


def hook_rows(
    settings: dict[str, Any], path: Path | None = None
) -> dict[str, list[dict[str, Any]]]:
    """The hooks block by event; a null block or event is no hooks. An entry
    Claude Code could not use raises, naming ``path``."""
    where = path or Path("settings.json")
    block = settings.get("hooks")
    if block is None:
        return {}
    if not isinstance(block, dict):
        raise ClaudeSettingsError(f"{where}: hooks must be an object")
    rows: dict[str, list[dict[str, Any]]] = {}
    for event, groups in block.items():
        if groups is None:
            continue
        if not isinstance(groups, list) or not all(isinstance(g, dict) for g in groups):
            raise ClaudeSettingsError(
                f"{where}: hooks.{event} must be a list of groups"
            )
        for index, group in enumerate(groups):
            hooks = group.get("hooks")
            if hooks is not None and (
                not isinstance(hooks, list)
                or not all(isinstance(hook, dict) for hook in hooks)
            ):
                raise ClaudeSettingsError(
                    f"{where}: hooks.{event}[{index}].hooks must be a list of hooks"
                )
        rows[event] = groups
    return rows


def _files(root: Path) -> dict[Path, Path]:
    files = {
        path.relative_to(root): path
        for tree in (root / "hooks", root / "lib")
        for path in tree.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and not path.is_relative_to(root / "hooks" / "tests")
        and path.suffix != ".pyc"
    }
    # Wheels contain the vendor .js artifact in the customer closure. A source
    # checkout keeps its JavaScript-compatible TypeScript in the handler tree.
    module = Path("hooks/lib/native_delegation_mod.js")
    source_module = root.parents[1] / "src/handlers/native_delegation.ts"
    if module not in files and source_module.is_file():
        files[module] = source_module
    return files


def _root() -> Path:
    return Path.home() / ".omninode" / "delegation"


def _record() -> Path:
    return user_config_path().parent / "claude_hooks_install.json"


def _installed(claude_home: Path) -> dict[str, Any]:
    """What the delegation installer left: its files, its hook config and the
    settings rows it owns (those naming its folder)."""
    root = _root()
    config = root / "hooks-delegation.json"
    rows = sorted(
        json.dumps([event, {k: v for k, v in group.items() if k != "hooks"}, hook])
        for event, groups in hook_rows(read_claude_settings(claude_home)).items()
        for group in groups
        for hook in group.get("hooks") or []
        if str(root) + "/" in str(hook.get("command", ""))
    )
    return {
        "files": {
            str(name): hashlib.sha256(path.read_bytes()).hexdigest()
            for name, path in sorted(
                _files(root / "plugin" / "plugins" / "onex").items()
            )
        },
        "config": hashlib.sha256(config.read_bytes()).hexdigest()
        if config.is_file()
        else None,
        "rows": rows,
    }


def record_installed_hooks(claude_home: Path) -> None:
    """Record what onex installed, so a later onex can tell an older install it
    may update from one the user changed (OMN-23413)."""
    record = _record()
    record.parent.mkdir(parents=True, exist_ok=True)
    temporary = record.with_suffix(".tmp")
    temporary.write_text(json.dumps(_installed(claude_home), sort_keys=True) + "\n")
    temporary.replace(record)


def _unchanged_since_recorded(claude_home: Path) -> bool:
    try:
        recorded = json.loads(_record().read_text())
    except (OSError, ValueError):
        return False
    return bool(recorded == json.loads(json.dumps(_installed(claude_home))))


def keep_installed_hooks(claude_home: Path) -> Path:
    """Copy the installed hooks, their config and the Claude settings aside
    before --replace reinstalls; returns the copy's folder."""
    root = _root()
    kept = root / "backups" / f"replaced-{datetime.now(UTC):%Y%m%dT%H%M%S%fZ}"
    kept.mkdir(parents=True)
    target = root / "plugin" / "plugins" / "onex"
    if target.is_dir():
        shutil.copytree(target, kept / "onex", symlinks=True)
    for path in (root / "hooks-delegation.json", claude_home / "settings.json"):
        if path.is_file():
            shutil.copy2(path, kept / path.name)
    return kept


def missing_delegation_hooks(rows: dict[str, Any]) -> list[str]:
    """Whether a bundle's manifest or an install lacks the delegation hook (AC38).

    Delegation is one hook, the prompt hook: the delegation script on
    UserPromptSubmit, labelled ONEX_DELEGATION_HOOK=prompts (OMN-23413
    Amendment 14a, operator 2026-11-17: "keep one hook for all claude prompt
    delegation"). Subagents always run on Claude, so no PreToolUse row is
    needed. Any other command, such as the no-op prompt classifier installs
    before 2026-11-11 registered, is not the prompt hook, and a bundle or
    install without it must never read as delegation on.
    """
    # UserPromptSubmit has no matcher; Claude Code ignores one written there.
    prompt = any(
        DELEGATION_SCRIPT in str(hook.get("command") or "")
        for group in rows.get("UserPromptSubmit") or []
        for hook in group.get("hooks") or []
    )
    return [] if prompt else ["prompt (UserPromptSubmit)"]


def _owned_rows(rows: dict[str, Any], root: Path) -> dict[str, Any]:
    """The rows of ``rows`` whose command runs from the install root."""
    return {
        event: [
            {
                **group,
                "hooks": [
                    hook
                    for hook in group.get("hooks") or []
                    if str(root) + "/" in str(hook.get("command", ""))
                ],
            }
            for group in groups
        ]
        for event, groups in rows.items()
    }


def _hook_presence(owned: dict[str, Any]) -> dict[str, str]:
    """The delegation hook as installed or missing, for status and the TUI."""
    return {
        "prompt_hook": "missing" if missing_delegation_hooks(owned) else "installed"
    }


def _observe_native_hooks(source: Path, claude_home: Path) -> ModelSetupObservation:
    """Verify both the copied bundle and the cache Claude actually loads."""
    root = _root()
    target = root / "plugin/plugins/onex"
    plugin_id = "onex-delegation@onex-delegation-local"
    evidence = {"display": "native", "prompt_hook": "missing"}

    def result(
        status: Literal["success", "unavailable", "action_required"], fix: str
    ) -> ModelSetupObservation:
        return ModelSetupObservation(
            step="claude-hooks", status=status, evidence=evidence, fix=fix
        )

    settings = read_claude_settings(claude_home)
    registry_path = claude_home / "plugins/installed_plugins.json"
    registry = json.loads(registry_path.read_text()) if registry_path.is_file() else {}
    rows = [
        row
        for row in registry.get("plugins", {}).get(plugin_id, [])
        if row.get("scope") == "user"
    ]
    if len(rows) > 1:
        return result(
            "action_required",
            "Resolve duplicate ONEX delegation plugin registrations before retrying.",
        )
    expected = {name: path.read_bytes() for name, path in _files(source).items()}
    expected[Path("hooks/hooks.json")] = (
        json.dumps({"modules": ["./lib/native_delegation_mod.js"]}, indent=2).encode()
        + b"\n"
    )
    expected[Path("hooks/delegation-runtime.json")] = (
        json.dumps({"python": sys.executable}, indent=2).encode() + b"\n"
    )

    def matches(folder: Path) -> bool:
        actual = _files(folder)
        return set(actual) == set(expected) and all(
            not path.is_symlink() and path.read_bytes() == expected[name]
            for name, path in actual.items()
        )

    if not matches(target):
        if _unchanged_since_recorded(claude_home):
            evidence["installed"] = "older"
            return result(
                "unavailable",
                "Run onex delegation on to upgrade the recorded delegation bundle to native output.",
            )
        if not target.exists() and not rows:
            return result(
                "unavailable",
                "Run onex delegation on to install native delegation output.",
            )
        return result(
            "action_required",
            "The delegation bundle differs from this onex. Preserve edited files, then run onex delegation on --replace.",
        )
    if not rows:
        return result(
            "unavailable",
            "Run onex delegation on to register native delegation output.",
        )
    cache = Path(rows[0].get("installPath", ""))
    # A relative/foreign profile path is not proof of this profile's installed
    # plugin. The local installer always uses Claude's cache in this profile.
    if (
        not cache.is_absolute()
        or not cache.is_relative_to(claude_home)
        or not matches(cache)
    ):
        return result(
            "action_required",
            "The native delegation plugin cache changed; preserve it and repair the installation.",
        )
    digest = hashlib.sha256()
    for name, content in sorted(expected.items()):
        digest.update(str(name).encode())
        digest.update(content)
    metadata = {
        "name": "onex-delegation",
        "version": "0.0.1-r" + digest.hexdigest()[:16],
    }
    for folder in (target, cache):
        path = folder / ".claude-plugin/plugin.json"
        if not path.is_file() or json.loads(path.read_text()) != metadata:
            return result(
                "action_required",
                "The native delegation plugin revision cannot be verified; repair the installation.",
            )
    owned = _owned_rows(hook_rows(settings), root)
    if any(group.get("hooks") for groups in owned.values() for group in groups):
        return result(
            "action_required",
            "Classic and native delegation registrations coexist; repair to retain one entry point.",
        )
    config = root / "hooks-delegation.json"
    if (
        not config.is_file()
        or json.loads(config.read_text()).get("entrypoint") != "native"
    ):
        return result(
            "action_required",
            "The native delegation ownership record is missing or changed.",
        )
    if settings.get("enabledPlugins", {}).get(plugin_id) is not True:
        return result(
            "unavailable",
            "Run onex delegation on to enable the native delegation plugin.",
        )
    evidence.update(
        prompt_hook="installed",
        plugin_id=plugin_id,
        version=metadata["version"],
        install_path=str(cache),
    )
    return result("success", "")


def observe_legacy_hooks(
    installer: Path, claude_home: Path, *, require_both: bool = True
) -> ModelSetupObservation:
    """Compare files and exact registered rows; never infer success from a receipt.

    ``require_both=False`` is for removal: whether the install changed does not
    depend on the bundle carrying the prompt hook, and the uninstaller works from
    what was installed (OMN-23413 review 2026-11-15)."""
    source = installer.parent.parent / "plugins" / "onex"
    root = _root()
    config = root / "hooks-delegation.json"
    native = Path("hooks/lib/native_delegation_mod.js") in _files(source)
    # Removal of a pre-native installation still uses its classic ownership
    # contract; a newer source bundle must not prevent switching an old one off.
    installed_native = (
        config.is_file()
        and json.loads(config.read_text()).get("entrypoint") == "native"
    )
    if native and (require_both or installed_native):
        return _observe_native_hooks(source, claude_home)
    target = root / "plugin" / "plugins" / "onex"
    manifest = json.loads((source / "hooks" / "hooks-delegation.v1.json").read_text())

    def rewrite(value: Any, key: str = "") -> Any:
        if isinstance(value, str):
            value = value.replace("${CLAUDE_PLUGIN_ROOT}", str(target))
            value = value.replace(
                "${ONEX_DELEGATION_PYTHON}", shlex.quote(sys.executable)
            )
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
    installed_rows = hook_rows(read_claude_settings(claude_home))
    owned = _owned_rows(installed_rows, root)
    presence = _hook_presence(owned)
    rows = expected.get("hooks")
    if not isinstance(rows, dict) or not rows:
        raise ValueError(
            "The installed customer bundle has no delegation hook manifest"
        )
    absent = missing_delegation_hooks(rows) if require_both else []
    if absent:
        return ModelSetupObservation(
            step="claude-hooks",
            status="action_required",
            evidence={"bundle_missing": ", ".join(absent), **presence},
            fix=f"This onex's omninode-claude bundle has no {' or '.join(absent)} delegation hook, so Claude Code delegation cannot be switched on. Install an omninode-claude that carries the prompt hook, then run onex delegation on.",
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
    if config.exists() and json.loads(config.read_text()) != expected:
        changed.add(Path("hooks-delegation.json"))
    wanted_commands = {
        hook["command"]
        for groups in rows.values()
        for group in groups
        for hook in group.get("hooks") or []
    }

    # One adapter legitimately handles two event types. Ownership and duplicate
    # checks therefore include the event and group metadata, not just argv.
    def identities(data: dict[str, Any]) -> list[str]:
        return [
            json.dumps(
                [event, {k: v for k, v in group.items() if k != "hooks"}, hook],
                sort_keys=True,
            )
            for event, groups in data.items()
            for group in groups
            for hook in group.get("hooks") or []
            if hook.get("command") in wanted_commands
            or str(root) + "/" in str(hook.get("command", ""))
        ]

    wanted_rows = set(identities(rows))
    current_rows = identities(installed_rows)
    if set(current_rows) - wanted_rows or len(current_rows) != len(set(current_rows)):
        changed.add(Path("settings.json"))
    registered = wanted_rows <= set(current_rows)
    if changed and _unchanged_since_recorded(claude_home):
        # AC38: an older install may lack the prompt hook; name it so
        # status never reads an install without the prompt hook as delegation on.
        lacking = missing_delegation_hooks(owned)
        if lacking:
            return ModelSetupObservation(
                step="claude-hooks",
                status="unavailable",
                evidence={
                    "installed": "older",
                    "installed_missing": ", ".join(lacking),
                    **presence,
                },
                fix=f"An older ONEX delegation hook is installed without the {' or '.join(lacking)} hook; onex delegation on adds it.",
            )
        return ModelSetupObservation(
            step="claude-hooks",
            status="unavailable",
            evidence={"installed": "older", **presence},
            fix="An older ONEX delegation hook is installed, unchanged since; onex delegation on updates it.",
        )
    if changed:
        return ModelSetupObservation(
            step="claude-hooks",
            status="action_required",
            evidence=presence,
            fix="The installed delegation hooks differ from this onex's and onex has no record of writing them (they were edited, or an older onex installed them). Keep any change you need, then run onex delegation on --replace: it copies them to ~/.omninode/delegation/backups and reinstalls.",
        )
    complete = bool(expected_files) and not missing and config.is_file() and registered
    return ModelSetupObservation(
        step="claude-hooks",
        status="success" if complete else "unavailable",
        evidence={
            "legacy_installer": str(installer),
            "verified_files": str(len(actual_files)),
            **presence,
        },
        fix=None
        if complete
        else "Run onex setup --action repair with claude-hooks selected to install the bundled delegation hook dependency closure.",
    )
