# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ports the ledger-guard refusal actor may ask of the world (OMN-21959).

Kept apart from ``guard_refusal`` so ``recovery`` can name the type without importing a module that imports it.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class GuardPorts:
    """What the actor may ask of the world. None of them raises for an unreachable thing: it answers no."""

    lane_rows: Callable[
        [str], Sequence[str]
    ]  # every ledger row, archive rolls included, naming lane=<lane>
    refused_rows: Callable[
        [str], str | None
    ]  # the rows of the refused request kept at a path; None when unreadable here
    path_exists: Callable[[str], bool]
    append: Callable[
        [str], bool
    ]  # append rows as a new request; True when the ledger accepted them
