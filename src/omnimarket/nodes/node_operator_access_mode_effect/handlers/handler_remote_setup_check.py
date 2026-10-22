# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""At session start in REMOTE mode, report in one line what a phone session is missing (OMN-20895).

Checks, each read from local files and the environment, never from the network:

* permissions: the session's permission mode is one that never prompts (``bypassPermissions``,
  ``auto`` or ``dontAsk``); any other stalls a phone session on the first prompt;
* push: ``agentPushNotifEnabled`` is on in the user settings, so an attention item can reach the
  phone;
* plugins: the internal ``omni`` plugin and an ``onex`` plugin are enabled;
* connectors: the ``linear-server`` and ``github`` MCP servers are configured (user config, user
  settings, or the project entry for the session's directory);
* environment: ``OMNI_HOME`` and ``ONEX_LEDGER_PATH`` are set.

The report never blocks the session.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..models.model_remote_setup_request import (
    ModelRemoteSetupRequest,
    ModelRemoteSetupResult,
)

QUIET_PERMISSION_MODES = frozenset({"bypassPermissions", "auto", "dontAsk"})
REQUIRED_PLUGIN_PREFIXES = ("omni@", "onex@")
REQUIRED_CONNECTORS = ("linear-server", "github")
REQUIRED_ENV = ("OMNI_HOME", "ONEX_LEDGER_PATH")


def _servers(block: Any) -> set[str]:
    return set(block) if isinstance(block, dict) else set()


def _project_servers(user_config: dict[str, Any], cwd: str) -> set[str]:
    projects = user_config.get("projects")
    if not isinstance(projects, dict) or not cwd:
        return set()
    found: set[str] = set()
    here = Path(cwd)
    for key, entry in projects.items():
        if not isinstance(entry, dict):
            continue
        try:
            applies = here == Path(key) or here.is_relative_to(Path(key))
        except ValueError:
            applies = False
        if applies:
            found |= _servers(entry.get("mcpServers"))
    return found


class HandlerRemoteSetupCheck:
    """List what a remote session lacks; an empty list is a ready session."""

    def handle(self, request: ModelRemoteSetupRequest) -> ModelRemoteSetupResult:
        missing: list[str] = []
        settings = request.settings
        permissions = settings.get("permissions")
        mode = request.permission_mode or (
            str(permissions.get("defaultMode", ""))
            if isinstance(permissions, dict)
            else ""
        )
        if mode not in QUIET_PERMISSION_MODES:
            missing.append(
                f"permission mode {mode or 'default'} prompts, which stalls a phone session"
            )
        if settings.get("agentPushNotifEnabled") is not True:
            missing.append("push notifications are off (agentPushNotifEnabled)")
        enabled = settings.get("enabledPlugins")
        enabled = enabled if isinstance(enabled, dict) else {}
        for prefix in REQUIRED_PLUGIN_PREFIXES:
            if not any(
                str(k).startswith(prefix) and v is True for k, v in enabled.items()
            ):
                missing.append(f"no {prefix.rstrip('@')} plugin enabled")
        servers = (
            _servers(request.user_config.get("mcpServers"))
            | _servers(settings.get("mcpServers"))
            | _project_servers(request.user_config, request.cwd)
        )
        for name in REQUIRED_CONNECTORS:
            if name not in servers:
                missing.append(f"connector {name} is not configured")
        for name in REQUIRED_ENV:
            if not request.environ.get(name, "").strip():
                missing.append(f"{name} is not set")
        if missing:
            line = "Remote setup: MISSING " + "; ".join(missing) + "."
        else:
            line = (
                f"Remote setup: OK (permission mode {mode}, push on, omni and onex plugins,"
                " linear-server and github connectors, OMNI_HOME and ONEX_LEDGER_PATH set)."
            )
        return ModelRemoteSetupResult(tuple(missing), line)
