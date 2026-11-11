# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Persist plugin policy only after its owned lifecycle callback succeeds."""

from __future__ import annotations

import fcntl
import os
import signal
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

from omnibase_core.cli.cli_user_config import render_user_config, user_config_path

from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_plugins import (
    HandlerSetupPlugins,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plugin import (
    ModelSetupPlugin,
    ModelSetupPluginAction,
    ModelSetupPluginActionResult,
    ModelSetupPluginPreferences,
)


class HandlerSetupPluginLifecycle:
    """Dependency-aware transitions sharing setup's nonblocking lock."""

    def __init__(
        self,
        *,
        onex_home: Path | None = None,
        registry: HandlerSetupPlugins | None = None,
    ) -> None:
        self._home = onex_home or user_config_path().parent
        self._registry = registry or HandlerSetupPlugins(onex_home=self._home)

    def _validate_transition(self, plugin: ModelSetupPlugin, enabled: bool) -> None:
        if not enabled and plugin.automatic:
            raise ValueError(f"Automatic setup plugin cannot be disabled: {plugin.id}")
        if enabled:
            for dependency in plugin.requires:
                if not self._registry.is_enabled(self._registry.plugin(dependency)):
                    raise ValueError(
                        f"Enable required setup plugin first: {dependency}"
                    )
        else:
            dependents = [
                p.id
                for p in self._registry.manifests()
                if plugin.id in p.requires and self._registry.is_enabled(p)
            ]
            if dependents:
                raise ValueError(
                    "Disable dependent setup plugins first: " + ", ".join(dependents)
                )

    def _callback(self, plugin: ModelSetupPlugin, action: str) -> None:
        if getattr(plugin.lifecycle, action) is None:
            return
        # A process group gives cancellation an actual boundary. Plugin output is
        # untrusted and can contain secrets, so it never becomes a CLI diagnostic.
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "omnimarket.nodes.node_onex_setup_effect.cli_plugins",
                "--lifecycle-worker",
                plugin.id,
                action,
                str(self._home),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            status = process.wait(timeout=plugin.lifecycle.timeout_seconds)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
            raise ValueError(
                f"Plugin lifecycle timed out: {plugin.id}; inspect owned resources before retrying"
            ) from None
        if status:
            raise ValueError(
                f"Plugin lifecycle failed: {plugin.id}; inspect owned resources before retrying"
            )

    def handle(self, request: ModelSetupPluginAction) -> ModelSetupPluginActionResult:
        plugin = self._registry.plugin(request.plugin)
        enabled = request.action == "enable"
        self._validate_transition(plugin, enabled)
        if self._registry.is_enabled(plugin) == enabled:
            return ModelSetupPluginActionResult(
                plugin=plugin.id, enabled=enabled, changed=False
            )
        self._home.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(
            self._home / "setup.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("Invalid setup lock")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError(
                    "A concurrent setup or plugin change holds the setup lock"
                ) from None
            self._validate_transition(plugin, enabled)
            if self._registry.is_enabled(plugin) == enabled:
                return ModelSetupPluginActionResult(
                    plugin=plugin.id, enabled=enabled, changed=False
                )
            self._callback(plugin, request.action)
            # Preserve unrelated settings, including edits made since this command
            # began. Configuration holds policy only; setup.json remains the journal.
            configuration = self._registry.configuration()
            preferences = ModelSetupPluginPreferences.model_validate(
                configuration.get("setup_plugins", {})
            )
            configuration["setup_plugins"] = {
                **preferences.model_dump(mode="json"),
                "enabled": {**preferences.enabled, plugin.id: enabled},
            }
            rendered = render_user_config(configuration)
            path = user_config_path(self._home)
            if path.is_symlink():
                raise ValueError("Invalid plugin configuration file")
            target_fd, name = tempfile.mkstemp(prefix=".plugins-", dir=self._home)
            temporary = Path(name)
            try:
                with os.fdopen(target_fd, "w") as target:
                    target.write(rendered)
                    target.flush()
                    os.fsync(target.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
            return ModelSetupPluginActionResult(
                plugin=plugin.id, enabled=enabled, changed=True
            )
        finally:
            os.close(fd)


def stop_dashboard(onex_home: Path) -> None:
    """Dispose only the process authenticated and recorded by the dashboard owner."""
    from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_dashboard import (
        HandlerSetupDashboard,
    )

    HandlerSetupDashboard(onex_home=onex_home).stop()


def start_dashboard(onex_home: Path) -> None:
    from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_dashboard import (
        HandlerSetupDashboard,
    )
    from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_workflow import (
        HandlerSetupWorkflow,
    )

    plan = HandlerSetupWorkflow.saved_plan(onex_home)
    if plan is None:
        raise ValueError("Run onex setup to select dashboard configuration first")
    HandlerSetupDashboard(onex_home=onex_home).apply(plan)


def remove_gateway_hooks(onex_home: Path) -> None:
    """Dispose legacy and auxiliary hooks through their existing settings owners."""
    from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_claude import (
        HandlerSetupClaude,
    )

    del onex_home
    HandlerSetupClaude(name="claude-hooks").remove_hooks()


def install_gateway_hooks(onex_home: Path) -> None:
    from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_claude import (
        HandlerSetupClaude,
    )
    from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_workflow import (
        HandlerSetupWorkflow,
    )

    plan = HandlerSetupWorkflow.saved_plan(onex_home)
    if plan is None:
        raise ValueError("Run onex setup to select Claude configuration first")
    HandlerSetupClaude(name="claude-hooks").apply(plan)


def _switch_claude_plugin(enabled: bool) -> None:
    from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_claude import (
        HandlerSetupClaude,
    )

    HandlerSetupClaude().set_enabled(enabled)


def enable_claude_plugin(onex_home: Path) -> None:
    del onex_home
    _switch_claude_plugin(True)


def disable_claude_plugin(onex_home: Path) -> None:
    del onex_home
    _switch_claude_plugin(False)
