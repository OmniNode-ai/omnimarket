# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""EnumPrMergeState: GitHub's ``mergeable_state`` for a pull request."""

from __future__ import annotations

from enum import StrEnum


class EnumPrMergeState(StrEnum):
    """The REST ``mergeable_state`` value, lower case as GitHub returns it.

    ``UNKNOWN`` is what GitHub returns before it has computed the state, and
    for a PR that has already merged or closed.
    """

    BEHIND = "behind"
    BLOCKED = "blocked"
    CLEAN = "clean"
    DIRTY = "dirty"
    DRAFT = "draft"
    HAS_HOOKS = "has_hooks"
    UNKNOWN = "unknown"
    UNSTABLE = "unstable"


__all__: list[str] = ["EnumPrMergeState"]
