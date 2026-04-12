# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Stub handler for SideEffectObserver.

Phase-0 stub only. Records which Kafka side effects a node emitted.
Wiring happens in Wave 3+.

Related:
    - OMN-8506: stub side-effect observer + evidence evaluator interfaces
    - OMN-8025: Overseer seam integration epic
"""

from __future__ import annotations

import copy
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class SideEffectObserver(Protocol):
    """Observer that records Kafka side effects emitted by a node.

    Phase-0 stub — no wiring yet.
    Implementations should accumulate emitted topic/payload pairs for
    downstream inspection by EvidenceEvaluator.
    """

    def record_emission(self, *, topic: str, payload: dict[str, Any]) -> None:
        """Record a single Kafka emission from a node."""
        ...

    def get_emissions(self) -> list[dict[str, Any]]:
        """Return all recorded emissions in order."""
        ...


class NullSideEffectObserver:
    """No-op implementation used as default until wiring is active."""

    def __init__(self) -> None:
        self._emissions: list[dict[str, Any]] = []

    def record_emission(self, *, topic: str, payload: dict[str, Any]) -> None:
        self._emissions.append({"topic": topic, "payload": copy.deepcopy(payload)})

    def get_emissions(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._emissions)


__all__: list[str] = ["NullSideEffectObserver", "SideEffectObserver"]
