# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""uv configuration outcome and login environment conflicts."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModelLabUvCacheColocateResult:
    """Environment overrides remain visible even when the file is configured."""

    status: str
    reason: str = ""
    cache_dir: str = ""
    env_overrides: tuple[str, ...] = ()

    def line(self) -> str:
        line = f"uv_cache={self.status}"
        if self.env_overrides:
            line += f" uv_env_overrides={len(self.env_overrides)}"
        return line
