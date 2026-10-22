# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input and output of the remote setup check (OMN-20895)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ModelRemoteSetupRequest:
    """The session's permission mode, the user settings, the user config and the environment."""

    permission_mode: str = ""
    settings: dict[str, Any] = field(default_factory=dict)
    user_config: dict[str, Any] = field(default_factory=dict)
    cwd: str = ""
    environ: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ModelRemoteSetupResult:
    """``missing`` lists each gap in words; ``line`` is the one-line report."""

    missing: tuple[str, ...]
    line: str

    @property
    def ok(self) -> bool:
        return not self.missing
