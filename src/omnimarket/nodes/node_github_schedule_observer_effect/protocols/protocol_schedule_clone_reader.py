# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam that reads a clone for node_github_schedule_observer_effect (OMN-20803)."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from omnimarket.nodes.node_github_schedule_observer_effect.models.model_cloned_repository import (
    ModelClonedRepository,
)


class ScheduleCloneError(RuntimeError):
    """The clone could not be read (absent, not a git repository, no default branch)."""


@runtime_checkable
class ProtocolScheduleCloneReader(Protocol):
    """Read a clone's head, its remote default branch's head and its scheduled workflows."""

    def read_clone(self, clone_dir: Path) -> ModelClonedRepository:
        """Return the clone's facts.

        Raises:
            ScheduleCloneError: when the clone cannot be read.
        """
        ...
