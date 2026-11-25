# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The workspace roots the forwarder finds the hosted lane's declaration from (OMN-25564).

The lane declaration is read from two roots: the workspace root (``OMNIBASE_PATH``, else ``OMNI_HOME``) and the
workspace config root (``ONEX_WORKSPACE_CONFIG_ROOT``). The request carries the environment the forwarder was
given; the result carries each root the host's own declaration supplied and, for each root that stayed
unresolved, every place that was looked at.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

OMNIBASE_PATH_ENV: Final[str] = "OMNIBASE_PATH"
OMNI_HOME_ENV: Final[str] = "OMNI_HOME"
WORKSPACE_CONFIG_ROOT_ENV: Final[str] = "ONEX_WORKSPACE_CONFIG_ROOT"
_HOST_CONFIG_SUBDIR: Final[str] = "onex"
_HOST_CONFIG_PATTERN: Final[str] = "*.env"
_ASSIGNMENT: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?:export\s+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>.*?)\s*$"
)


def _unquote(raw: str) -> str:
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        return raw[1:-1]
    return raw


class ModelWorkspaceRoots(BaseModel):
    """What the host declared for the roots the environment left unset.

    ``declared`` maps a variable name to the value the host's declaration gave it; a root the environment
    already set is never in it. ``looked`` holds one line per root that is still unresolved, naming it and each
    place that was looked at.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    declared: Mapping[str, str] = Field(default_factory=dict)
    looked: tuple[str, ...] = ()

    @classmethod
    def resolve(cls, environment: Mapping[str, str]) -> ModelWorkspaceRoots:
        """The roots the environment left unset, as the host's declaration supplies them."""
        env = environment
        config_dir = cls._host_config_dir(env)
        declared: dict[str, str] = {}
        looked: list[str] = []
        wanted = (
            (OMNIBASE_PATH_ENV, (OMNIBASE_PATH_ENV, OMNI_HOME_ENV)),
            (WORKSPACE_CONFIG_ROOT_ENV, (WORKSPACE_CONFIG_ROOT_ENV,)),
        )
        for name, env_names in wanted:
            if any(env.get(candidate, "").strip() for candidate in env_names):
                continue
            value, trace = cls._from_host_units(name, env_names, config_dir)
            if value is None:
                looked.append(trace)
            else:
                declared[name] = value
        return cls(declared=declared, looked=tuple(looked))

    @staticmethod
    def _host_config_dir(env: Mapping[str, str]) -> Path | None:
        xdg = env.get("XDG_CONFIG_HOME", "").strip()
        if xdg:
            return Path(xdg) / _HOST_CONFIG_SUBDIR
        home = env.get("HOME", "").strip()
        return Path(home) / ".config" / _HOST_CONFIG_SUBDIR if home else None

    @staticmethod
    def _from_host_units(
        name: str, env_names: tuple[str, ...], config_dir: Path | None
    ) -> tuple[str | None, str]:
        where = [" or ".join(f"${n}" for n in env_names) + " (unset or blank)"]
        if config_dir is None:
            where.append("the host config directory (no XDG_CONFIG_HOME and no HOME)")
            return None, f"{name}: looked at " + "; ".join(where)
        declared_by: dict[str, list[str]] = {}
        for path in sorted(config_dir.glob(_HOST_CONFIG_PATTERN)):
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                where.append(f"{path} (unreadable)")
                continue
            for line in text.splitlines():
                match = _ASSIGNMENT.match(line)
                if match is None or match.group("name") != name:
                    continue
                value = _unquote(match.group("value"))
                if not value.startswith("/") or "$" in value:
                    where.append(f"{path} ({name} is not an absolute literal path)")
                    continue
                declared_by.setdefault(os.path.normpath(value), []).append(path.name)
        if len(declared_by) == 1:
            return next(iter(declared_by)), ""
        pattern = f"{config_dir}/{_HOST_CONFIG_PATTERN}"
        if declared_by:
            files = "; ".join(
                f"{', '.join(sorted(set(names)))} declare {value}"
                for value, names in sorted(declared_by.items())
            )
            where.append(f"{pattern} (the files disagree: {files})")
        else:
            where.append(f"{pattern} (no file declares {name})")
        return None, f"{name}: looked at " + "; ".join(where)

    def describe_unresolved(self) -> str | None:
        """The unresolved roots and where each was looked for; ``None`` when none is unresolved."""
        return " | ".join(self.looked) if self.looked else None


__all__ = ["ModelWorkspaceRoots"]
