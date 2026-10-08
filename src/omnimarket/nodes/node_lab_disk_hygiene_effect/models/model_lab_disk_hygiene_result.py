# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Steps and totals of the lab disk hygiene handler (OMN-17427)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ModelLabDiskHygieneStep:
    """One Docker call: its step name, argv, exit code and the last line it printed."""

    step: str
    argv: tuple[str, ...]
    exit_code: int
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ModelLabDiskHygieneResult:
    """What the pass did. docker is absent, unreachable, dry-run or ran."""

    docker: str
    free_bytes_before: int
    free_bytes_after: int
    protected_projects: tuple[str, ...] = ()
    removed_containers: tuple[str, ...] = ()
    kept_containers: tuple[str, ...] = ()
    removed_tmp: tuple[str, ...] = ()
    removed_tmp_bytes: int = 0
    steps: tuple[ModelLabDiskHygieneStep, ...] = ()
    errors: tuple[str, ...] = field(default_factory=tuple)

    @property
    def freed_bytes(self) -> int:
        return max(0, self.free_bytes_after - self.free_bytes_before)

    def line(self) -> str:
        gb = self.freed_bytes / 1e9
        parts = [
            f"docker={self.docker}",
            f"docker_freed={gb:.1f}GB",
            f"containers_removed={len(self.removed_containers)}",
            f"tmp_removed={len(self.removed_tmp)}",
        ]
        if self.errors:
            parts.append(f"docker_errors={len(self.errors)}")
        return " ".join(parts)
