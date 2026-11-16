# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Claude components use the vendor marketplace and canonical ONEX hook installer.

Verification reads the installed-plugin registry and manifest directly because
`claude plugin list` initializes config files even when called as a query.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from importlib import metadata
from pathlib import Path
from typing import Literal

from omnimarket.handlers.delegation_hook_switch import delegation_switch_enabled
from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_command import (
    HandlerSetupCommand,
)
from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_environment import (
    _read,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_claude_request import (
    ModelSetupClaudeRequest,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_command_request import (
    ModelSetupCommandRequest,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_observation import (
    ModelSetupObservation,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plan import (
    ModelSetupPlan,
)
from omnimarket.nodes.node_onex_setup_effect.setup_legacy_hooks import (
    ClaudeSettingsError,
    hook_rows,
    keep_installed_hooks,
    observe_legacy_hooks,
    read_claude_settings,
    record_installed_hooks,
)

#: The shell's ONEX_HOOKS_MASK wins over the switch onex saves (the hooks read it first).
SWITCH_OVERRIDE_FIX = (
    "Your shell sets ONEX_HOOKS_MASK without the delegation bit, and it overrides "
    "the switch onex saves, so delegation stays off. Unset ONEX_HOOKS_MASK (or add "
    "POST_SKILL_DELEGATION_ENFORCER to it), then run onex delegation on again."
)


def _settings_problem(step: str, error: ClaudeSettingsError) -> ModelSetupObservation:
    return ModelSetupObservation(
        step=step,
        status="action_required",
        fix=f"Claude Code's settings cannot be used: {error}. Fix that, then run onex delegation on again.",
    )


class HandlerSetupClaude:
    """Observe or reconcile the selected plugin profile and delegation hook rows."""

    def __init__(
        self,
        *,
        name: Literal["claude-plugin", "claude-hooks"] = "claude-plugin",
        claude_home: Path | None = None,
        executable: Path | None = None,
        replace: bool = False,
    ) -> None:
        self.name = name
        # --replace: hooks onex has no record of writing are copied aside, then
        # reinstalled; without it they are never overwritten (OMN-23413).
        self._replace = replace
        own = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
        self._claude = claude_home or own
        # Claude Code keys its sign-in by CLAUDE_CONFIG_DIR: naming even the
        # default ~/.claude reads another keychain entry, and a signed-in user
        # read as signed out (OMN-23413, 2026-11-13). So the variable is set only
        # for a folder Claude Code would not use on its own; a user's own value
        # is inherited unchanged.
        self._environment = {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
        if self._claude.expanduser().resolve() != own.expanduser().resolve():
            self._environment["CLAUDE_CONFIG_DIR"] = str(self._claude)
        selected = shutil.which("claude")
        self._binary = executable or (
            Path(selected) if selected else Path.home() / ".local" / "bin" / "claude"
        )
        self._commands = HandlerSetupCommand()

    def handle(self, request: ModelSetupClaudeRequest) -> ModelSetupObservation:
        adapter = HandlerSetupClaude(
            name=request.component, claude_home=self._claude, executable=self._binary
        )
        if request.operation == "apply":
            applied = adapter.apply(request.plan)
            if applied is not None:
                return applied
        return adapter.observe(request.plan)

    def _claude_code_installed(self) -> bool:
        return self._binary.is_file() and os.access(self._binary, os.X_OK)

    def _not_installed(self) -> ModelSetupObservation:
        return ModelSetupObservation(
            step=self.name,
            status="unavailable",
            evidence={"claude_code": "not_installed"},
            fix="Claude Code is not installed. Install it yourself (claude.com/claude-code), then repeat this component setup.",
        )

    def _run(self, argv: tuple[str, ...], *, network: bool = False) -> str:
        result = None
        for attempt in range(3 if network else 1):
            result = self._commands.handle(
                ModelSetupCommandRequest(
                    argv=argv,
                    cwd=Path.home(),
                    environment=self._environment,
                    timeout_seconds=180 if network else 10,
                )
            )
            if result.returncode == 0:
                return result.stdout
            if network and attempt < 2:
                time.sleep(5)
        raise RuntimeError(
            "The Claude command failed; inspect the selected component before retrying"
        )

    def _registrations(self) -> dict[str, list[dict[str, object]]]:
        raw = _read(self._claude / "plugins" / "installed_plugins.json")
        if raw is None:
            return {}
        data = json.loads(raw)
        if (
            not isinstance(data, dict)
            or data.get("version") != 2
            or not isinstance(data.get("plugins"), dict)
        ):
            raise ValueError("Unsupported Claude plugin registry")
        selected: dict[str, list[dict[str, object]]] = {}
        for name in ("onex@omninode-tools", "onex@omninode-tools-dev"):
            entries = data["plugins"].get(name, [])
            if not isinstance(entries, list) or any(
                not isinstance(entry, dict) for entry in entries
            ):
                raise ValueError("Invalid plugin entries")
            selected[name] = [
                entry for entry in entries if entry.get("scope") == "user"
            ]
        return selected

    def developer_registered(self) -> bool:
        """Whether the developer ONEX plugin profile is registered for the user.

        A command with no workspace (onex delegation) cannot tell a developer from
        a customer by folder, so it keeps the profile already there rather than
        adding the customer one beside it (OMN-23413, 2026-11-13).
        """
        return bool(self._registrations().get("onex@omninode-tools-dev"))

    def set_enabled(self, enabled: bool) -> None:
        """Toggle the single registered ONEX user profile through Claude's owner."""
        installed = self._registrations()
        names = [name for name, rows in installed.items() if rows]
        if len(names) > 1 or any(len(rows) > 1 for rows in installed.values()):
            raise ValueError(
                "Resolve conflicting Claude plugin registrations before changing their state"
            )
        if not names:
            if enabled:
                raise ValueError(
                    "Run onex setup to install the selected Claude plugin first"
                )
            return
        self._run(
            (
                str(self._binary),
                "plugin",
                "enable" if enabled else "disable",
                names[0],
                "--scope",
                "user",
            )
        )

    def observe(self, plan: ModelSetupPlan) -> ModelSetupObservation:
        try:
            if self.name == "claude-hooks":
                try:
                    legacy = observe_legacy_hooks(
                        self._legacy_installer(), self._claude
                    )
                except ClaudeSettingsError as error:
                    return _settings_problem(self.name, error)
                except (OSError, ValueError, RuntimeError):
                    return ModelSetupObservation(
                        step=self.name,
                        status="action_required",
                        fix="Install or repair the packaged delegation hook assets with onex install omninode-claude and check the selected Claude settings, then retry setup.",
                    )
                if legacy.status != "success":
                    return legacy
                # Each hook's installed state reaches status and the TUI (OMN-23413).
                hooks = {
                    key: legacy.evidence[key]
                    for key in ("prompt_hook", "subagent_hook")
                    if key in legacy.evidence
                }
                if not delegation_switch_enabled():
                    # A shell's ONEX_HOOKS_MASK decided this, not the saved switch.
                    if "ONEX_HOOKS_MASK" in os.environ:
                        return ModelSetupObservation(
                            step=self.name,
                            status="action_required",
                            evidence={"switch_override": "ONEX_HOOKS_MASK", **hooks},
                            fix=SWITCH_OVERRIDE_FIX,
                        )
                    return ModelSetupObservation(
                        step=self.name,
                        status="unavailable",
                        evidence=hooks,
                        fix="Enable POST_SKILL_DELEGATION_ENFORCER with onex hooks enable POST_SKILL_DELEGATION_ENFORCER and remove any conflicting ONEX_HOOKS_MASK override.",
                    )
                return ModelSetupObservation(
                    step=self.name,
                    status="success",
                    evidence={
                        "switch": "POST_SKILL_DELEGATION_ENFORCER",
                        "restart_sessions": "required after registration changes",
                        **hooks,
                    },
                )
            if not self._claude_code_installed():
                return self._not_installed()
            # OMN-23413: signing in is the user's step, so it is reported before
            # the plugin that setup adds itself; the /delegation tab then says
            # "not signed in" before a Save rather than after it.
            auth = self._commands.handle(
                ModelSetupCommandRequest(
                    argv=(str(self._binary), "auth", "status", "--json"),
                    timeout_seconds=10,
                    environment=self._environment,
                )
            )
            status = json.loads(auth.stdout)
            if not isinstance(status, dict) or status.get("loggedIn") is not True:
                return ModelSetupObservation(
                    step=self.name,
                    status="action_required",
                    evidence={"claude_code": "signed_out"},
                    fix="Sign in to Claude Code yourself, then repeat onex setup --action repair. Existing login settings were preserved.",
                )
            installed = self._registrations()
            customer, developer = (
                installed.get("onex@omninode-tools", []),
                installed.get("onex@omninode-tools-dev", []),
            )
            if customer and developer:
                return ModelSetupObservation(
                    step=self.name,
                    status="action_required",
                    fix="Both customer and developer ONEX plugins are registered. Remove the unwanted profile before retrying; unrelated plugins are preserved.",
                )
            wanted = (
                "onex@omninode-tools-dev" if plan.developer else "onex@omninode-tools"
            )
            rows = installed.get(wanted, [])
            if len(rows) > 1:
                return ModelSetupObservation(
                    step=self.name,
                    status="action_required",
                    fix="Resolve duplicate user registrations of the selected ONEX plugin.",
                )
            if not rows:
                return ModelSetupObservation(
                    step=self.name,
                    status="unavailable",
                    fix=f"Install the selected plugin profile: {wanted}.",
                )
            location = rows[0].get("installPath")
            if not isinstance(location, str) or not Path(location).is_absolute():
                raise ValueError("Invalid plugin installation path")
            raw = _read(Path(location) / ".claude-plugin" / "plugin.json")
            manifest = json.loads(raw) if raw is not None else {}
            if (
                not isinstance(manifest, dict)
                or manifest.get("name") != "onex"
                or (
                    rows[0].get("version") is not None
                    and rows[0].get("version") != manifest.get("version")
                )
            ):
                return ModelSetupObservation(
                    step=self.name,
                    status="unavailable",
                    fix="Repair the stale ONEX plugin manifest through its marketplace installer.",
                )
            return ModelSetupObservation(
                step=self.name,
                status="success",
                evidence={
                    "plugin": wanted,
                    "version": str(manifest.get("version", "unknown")),
                },
            )
        except (OSError, ValueError, RuntimeError):
            return ModelSetupObservation(
                step=self.name,
                status="failure",
                fix="The Claude installation or plugin registry could not be verified. Preserve its settings and repair the reported component.",
            )

    @staticmethod
    def _developer_marketplace(workspace: Path) -> Path:
        root = workspace / "omniclaude" / "plugins"
        if not root.is_dir():
            raise ValueError(
                "Prepare the developer workspace before installing its plugin profile"
            )
        found: list[Path] = []
        for directory in root.iterdir():
            if not directory.is_dir() or not directory.resolve().is_relative_to(
                root.resolve()
            ):
                continue
            try:
                raw = _read(directory / ".claude-plugin" / "marketplace.json")
                manifest = json.loads(raw) if raw is not None else {}
            except (OSError, ValueError):
                continue
            if (
                isinstance(manifest, dict)
                and manifest.get("name") == "omninode-tools-dev"
            ):
                found.append(directory)
        if len(found) != 1:
            raise ValueError(
                "The developer marketplace manifest is missing or ambiguous. Review the owning plugin checkout."
            )
        return found[0]

    @staticmethod
    def _customer_assets() -> dict[str, str]:
        entries = tuple(
            metadata.entry_points(group="onex.claude", name="customer_assets")
        )
        if len(entries) != 1:
            raise ValueError(
                "Install the packaged customer assets with onex install omninode-claude "
                "(or a selected local wheel), then repeat setup. The customer_assets "
                "entry point is missing or ambiguous."
            )
        assets = entries[0].load()()
        if not isinstance(assets, dict):
            raise ValueError("The customer asset resolver returned no asset mapping")
        return {
            str(key): value for key, value in assets.items() if isinstance(value, str)
        }

    @classmethod
    def _customer_marketplace(cls) -> Path:
        assets = cls._customer_assets()
        location = assets.get("marketplace") if isinstance(assets, dict) else None
        if not isinstance(location, str) or not Path(location).is_absolute():
            raise ValueError(
                "The installed customer asset resolver returned no local marketplace"
            )
        marketplace = Path(location)
        if not marketplace.is_dir():
            raise ValueError("The installed customer marketplace directory is missing")
        return marketplace

    @classmethod
    def _legacy_installer(cls) -> Path:
        value = cls._customer_assets().get("installer")
        if value is None or not Path(value).is_absolute() or not Path(value).is_file():
            raise ValueError(
                "Install the customer hook bundle with onex install omninode-claude, then retry"
            )
        return Path(value)

    def remove_delegation_hooks(self, *, preserve_history: bool = False) -> None:
        """Remove owned delegation; OFF retains pending context, uninstall does not."""
        root = Path.home() / ".omninode" / "delegation"
        config = root / "hooks-delegation.json"
        settings = self._claude / "settings.json"
        if not config.is_file():
            registered = any(
                str(root) + "/" in str(hook.get("command", ""))
                for groups in hook_rows(read_claude_settings(self._claude)).values()
                for group in groups
                for hook in group.get("hooks") or []
            )
            if registered or (root / "plugin").exists():
                raise ValueError("Installed delegation ownership config is missing")
            return
        manifest = json.loads((root / "rollback-manifest.json").read_text())
        if (
            not _same_path(manifest.get("claude_settings"), settings)
            or not _same_path(manifest.get("installed_hook_config"), config)
            or not settings.is_file()
        ):
            raise ValueError(
                "Delegation belongs to a different or changed Claude profile"
            )
        installer = self._legacy_installer()
        # Removal asks only whether the install changed; a bundle that lacks a
        # hook does not stop removing what it installed (review 2026-11-15).
        state = observe_legacy_hooks(installer, self._claude, require_both=False)
        if state.status == "action_required":
            raise ValueError(
                "Installed delegation files or settings changed; preserve them before removal"
            )
        if preserve_history and state.evidence.get("installed") == "older":
            # A recorded, unchanged old bundle may have no context hook yet.
            # Upgrade through its owner before removing delegation registrations.
            self._run(
                (
                    "/bin/bash",
                    str(installer),
                    "--apply",
                    "--skip-pip",
                    "--python",
                    sys.executable,
                )
            )
            if observe_legacy_hooks(installer, self._claude).status != "success":
                raise ValueError("Delegation hook upgrade could not be verified")
        self._run(
            (
                "/bin/bash",
                str(installer),
                "--pause" if preserve_history else "--uninstall",
                "--python",
                sys.executable,
            )
        )
        if preserve_history:
            record_installed_hooks(self._claude)

    def remove_hooks(self) -> None:
        """Dispose through the delegation hooks' owner, preserving edited user state."""
        self.remove_delegation_hooks()

    def apply(self, plan: ModelSetupPlan) -> ModelSetupObservation | None:
        if self.name == "claude-hooks":
            try:
                installer = self._legacy_installer()
                legacy = observe_legacy_hooks(installer, self._claude)
            except ClaudeSettingsError as error:
                return _settings_problem(self.name, error)
            except (OSError, ValueError, RuntimeError):
                return ModelSetupObservation(
                    step=self.name,
                    status="action_required",
                    fix="Install or repair the packaged delegation hook assets with onex install omninode-claude, then retry setup.",
                )
            if legacy.evidence.get("bundle_missing"):
                return legacy  # AC38: reinstalling this bundle can never reach "on"
            if legacy.status == "action_required":
                if not self._replace:
                    return legacy
                keep_installed_hooks(self._claude)
            if legacy.status != "success":
                self._run(
                    (
                        "/bin/bash",
                        str(installer),
                        "--apply",
                        "--skip-pip",
                        "--python",
                        sys.executable,
                    )
                )
                legacy = observe_legacy_hooks(installer, self._claude)
                if legacy.status != "success":
                    return legacy
            record_installed_hooks(self._claude)
            if self.observe(plan).status == "success":
                return None
            self._run(
                (
                    str(Path(sys.executable).parent / "onex"),
                    "hooks",
                    "enable",
                    "POST_SKILL_DELEGATION_ENFORCER",
                )
            )
            return None
        # Resolve the selected installed assets before any vendor command. This
        # optional plugin package is never imported during non-Claude setup.
        if plan.developer:
            marketplace = self._developer_marketplace(plan.workspace)
        else:
            try:
                marketplace = self._customer_marketplace()
            except Exception:
                return ModelSetupObservation(
                    step=self.name,
                    status="action_required",
                    fix="Install or repair the customer asset bundle with onex install omninode-claude (or the selected local wheel), then repeat setup. The installed customer_assets entry point could not be resolved.",
                )
        if not self._claude_code_installed():
            # OMN-23413, operator 2026-11-13: Claude Code is the user's own
            # install; setup detects and says so, and never downloads it.
            return self._not_installed()
        if plan.developer:
            self._run(
                (str(self._binary), "plugin", "marketplace", "add", str(marketplace)),
                network=True,
            )
            wanted = "onex@omninode-tools-dev"
        else:
            self._run(
                (
                    str(self._binary),
                    "plugin",
                    "marketplace",
                    "add",
                    str(marketplace),
                ),
                network=True,
            )
            wanted = "onex@omninode-tools"
        self._run((str(self._binary), "plugin", "install", wanted), network=True)
        return None


def _same_path(recorded: object, actual: Path) -> bool:
    """The installer records paths as the shell spelled them ("dir//settings.json")."""
    return isinstance(recorded, str) and Path(recorded).resolve() == actual.resolve()
