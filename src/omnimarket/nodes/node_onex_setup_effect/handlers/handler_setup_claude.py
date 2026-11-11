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
import tempfile
import time
from importlib import metadata
from pathlib import Path
from typing import Literal

from omnimarket.handlers.claude_gateway_settings import (
    gateway_hook_enabled,
    manage_gateway_hooks,
)
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
    observe_legacy_hooks,
)


class HandlerSetupClaude:
    """Observe or reconcile the selected plugin profile and gateway hook rows."""

    def __init__(
        self,
        *,
        name: Literal["claude-plugin", "claude-hooks"] = "claude-plugin",
        claude_home: Path | None = None,
        executable: Path | None = None,
    ) -> None:
        self.name = name
        self._claude = claude_home or Path(
            os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))
        )
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

    def _run(self, argv: tuple[str, ...], *, network: bool = False) -> str:
        result = None
        for attempt in range(3 if network else 1):
            result = self._commands.handle(
                ModelSetupCommandRequest(
                    argv=argv,
                    cwd=Path.home(),
                    environment={
                        "CLAUDE_CONFIG_DIR": str(self._claude),
                        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                    },
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
                except (OSError, ValueError, RuntimeError):
                    return ModelSetupObservation(
                        step=self.name,
                        status="action_required",
                        fix="Install or repair the packaged delegation hook assets with onex install omninode-claude and check the selected Claude settings, then retry setup.",
                    )
                if legacy.status != "success":
                    return legacy
                state = manage_gateway_hooks("status", claude_home=self._claude)
                if (
                    state.status == "success"
                    and state.registered
                    and not gateway_hook_enabled()
                ):
                    return ModelSetupObservation(
                        step=self.name,
                        status="unavailable",
                        fix="Enable POST_SKILL_DELEGATION_ENFORCER with onex hooks enable POST_SKILL_DELEGATION_ENFORCER and remove any conflicting ONEX_HOOKS_MASK override.",
                    )
                return ModelSetupObservation(
                    step=self.name,
                    status="success"
                    if state.status == "success" and state.registered
                    else "unavailable"
                    if state.status == "success"
                    else "failure",
                    evidence={
                        "switch": "POST_SKILL_DELEGATION_ENFORCER",
                        "restart_sessions": "required after registration changes",
                    },
                    fix=state.fix
                    or (
                        None
                        if state.registered
                        else "Run onex setup --action repair with the Claude plugin and hooks components selected."
                    ),
                )
            if not self._binary.is_file() or not os.access(self._binary, os.X_OK):
                return ModelSetupObservation(
                    step=self.name,
                    status="unavailable",
                    fix="Install Claude Code, then repeat this component setup.",
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
            auth = self._commands.handle(
                ModelSetupCommandRequest(
                    argv=(str(self._binary), "auth", "status", "--json"),
                    timeout_seconds=10,
                    environment={
                        "CLAUDE_CONFIG_DIR": str(self._claude),
                        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                    },
                )
            )
            status = json.loads(auth.stdout)
            if not isinstance(status, dict) or status.get("loggedIn") is not True:
                return ModelSetupObservation(
                    step=self.name,
                    status="action_required",
                    evidence={"plugin": wanted},
                    fix="Sign in to Claude Code yourself, then repeat onex setup --action repair. Existing login settings were preserved.",
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

    def remove_hooks(self) -> None:
        """Dispose through the two existing owners, preserving edited user state."""
        installer = self._legacy_installer()
        state = observe_legacy_hooks(installer, self._claude)
        if state.status == "action_required":
            raise ValueError(
                "Installed delegation files or settings changed; preserve them before removal"
            )
        if (
            Path.home() / ".omninode" / "delegation" / "hooks-delegation.json"
        ).exists():
            self._run(
                ("/bin/bash", str(installer), "--uninstall", "--python", sys.executable)
            )
        result = manage_gateway_hooks("uninstall", claude_home=self._claude)
        if result.status != "success":
            raise ValueError("Owned auxiliary gateway hook removal was refused")

    def apply(self, plan: ModelSetupPlan) -> ModelSetupObservation | None:
        if self.name == "claude-hooks":
            try:
                installer = self._legacy_installer()
                legacy = observe_legacy_hooks(installer, self._claude)
            except (OSError, ValueError, RuntimeError):
                return ModelSetupObservation(
                    step=self.name,
                    status="action_required",
                    fix="Install or repair the packaged delegation hook assets with onex install omninode-claude, then retry setup.",
                )
            if legacy.status == "action_required":
                return legacy
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
            if self.observe(plan).status == "success":
                return None
            result = manage_gateway_hooks("install", claude_home=self._claude)
            if result.status != "success" or not result.registered:
                raise ValueError(
                    "The canonical gateway hook installer refused the settings change"
                )
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
        if not self._binary.is_file():
            with tempfile.TemporaryDirectory(
                prefix="onex-claude-install-"
            ) as directory:
                script = Path(directory) / "install.sh"
                self._run(
                    (
                        "curl",
                        "-fsSL",
                        "--retry",
                        "3",
                        "https://claude.ai/install.sh",
                        "-o",
                        str(script),
                    ),
                    network=True,
                )
                self._run(("/bin/bash", str(script)), network=True)
            selected = shutil.which("claude")
            self._binary = (
                Path(selected)
                if selected
                else Path.home() / ".local" / "bin" / "claude"
            )
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
