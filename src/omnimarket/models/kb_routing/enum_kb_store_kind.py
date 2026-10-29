# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The kinds of knowledge store the routing rules can declare."""

from __future__ import annotations

from enum import StrEnum


class EnumKbStoreKind(StrEnum):
    """Store kinds. Which stores exist, and of which kind, is data in the rules."""

    GIT_REPOSITORY = "git_repository"
    DRIVE = "drive"
    IN_MEMORY = "in_memory"
    LOCAL_DIRECTORY = "local_directory"

    @property
    def is_external(self) -> bool:
        """An external store leaves the process and so must declare required checks."""
        return self in (EnumKbStoreKind.GIT_REPOSITORY, EnumKbStoreKind.DRIVE)


__all__ = ["EnumKbStoreKind"]
