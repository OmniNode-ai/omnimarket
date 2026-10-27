# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and result for a refused lane pin's fallback decision."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelLanePinFallbackRequest:
    pin: str | None  # the host the lane was pinned to, None for an unpinned placement
    required: bool  # the brief itself requires the host (node_brief_prerequisites_compute ``required``)
    verdict: (
        str | None
    )  # the placement node record's verdict: ``acted`` when it answered
    holdback: bool  # the priority holdback refused the placement, not the host
    pin_read: bool  # the pool read measured the pinned host
    pin_refusal: (
        str | None
    )  # why the pinned host takes no lane, as the placement reader judged it


@dataclass(frozen=True)
class ModelLanePinFallbackResult:
    refusal: (
        str | None
    )  # place the lane again unpinned, citing this; None keeps the no-host outcome
