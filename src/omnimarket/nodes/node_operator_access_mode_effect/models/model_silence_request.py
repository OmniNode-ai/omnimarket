# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input of the silent-turn guard (OMN-20894)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModelSilenceRequest:
    """Every assistant text block in the turn, and whether the turn is silent."""

    texts: tuple[str, ...]
    silent: bool
