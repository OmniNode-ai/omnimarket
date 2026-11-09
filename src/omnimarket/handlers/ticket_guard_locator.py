# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where the installed omniclaude ticket-creation guard lives (OMN-17942, OMN-23138).

Lane intake and the ticket node both need the guard's path. It sits here, beside neither,
so lane intake does not import the ticket node's handler (the dispatch to tickets-plans cut
of the rebuild plan's section 4.1). ``handler_create_ticket`` re-exports both names.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_GUARD_PATH_ENV = "ONEX_TICKET_GUARD_PATH"
_GUARD_RELPATH = Path("hooks/lib/ticket_creation_guard.py")
_ONEX_PLUGIN_PREFIX = "onex@"


class TicketGuardUnavailableError(RuntimeError):
    """No installed ticket-creation guard was found, so no create may run."""


def _claude_config_dir() -> Path:
    override = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    return Path(override) if override else Path.home() / ".claude"


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def locate_ticket_guard() -> Path:
    """The installed omniclaude plugin's ticket-creation guard, or raise.

    ``ONEX_TICKET_GUARD_PATH`` names it explicitly (a lab lane takes it from the
    onex plugin directory its runner shipped). Otherwise the one enabled
    ``onex@<marketplace>`` plugin in Claude's installed-plugin registry. Nothing
    found, an explicit path that does not exist, or two enabled onex plugins
    each raise :class:`TicketGuardUnavailableError`; there is no default.
    """
    explicit = os.environ.get(_GUARD_PATH_ENV, "").strip()
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise TicketGuardUnavailableError(
                f"{_GUARD_PATH_ENV}={explicit} is not a file; refusing to create "
                "a ticket without the ticket-creation guard (OMN-17942)"
            )
        return path

    config = _claude_config_dir()
    settings = _read_json(config / "settings.json")
    enabled = settings.get("enabledPlugins") if isinstance(settings, dict) else None
    registry = _read_json(config / "plugins" / "installed_plugins.json")
    plugins = registry.get("plugins") if isinstance(registry, dict) else None
    candidates: list[Path] = []
    if isinstance(enabled, dict) and isinstance(plugins, dict):
        for key, entries in plugins.items():
            if not key.startswith(_ONEX_PLUGIN_PREFIX) or enabled.get(key) is not True:
                continue
            for entry in entries if isinstance(entries, list) else []:
                install = entry.get("installPath") if isinstance(entry, dict) else None
                if (
                    isinstance(install, str)
                    and (Path(install) / _GUARD_RELPATH).is_file()
                ):
                    candidates.append(Path(install) / _GUARD_RELPATH)
    if len(candidates) > 1:
        raise TicketGuardUnavailableError(
            "more than one enabled onex plugin carries a ticket-creation guard "
            f"({', '.join(map(str, candidates))}); set {_GUARD_PATH_ENV} to the one to use"
        )
    if not candidates:
        raise TicketGuardUnavailableError(
            "no installed ticket-creation guard: set "
            f"{_GUARD_PATH_ENV} to <onex plugin dir>/{_GUARD_RELPATH}, or enable the "
            f"onex plugin under {config}; refusing to create a ticket without it (OMN-17942)"
        )
    return candidates[0]


__all__ = ["TicketGuardUnavailableError", "locate_ticket_guard"]
