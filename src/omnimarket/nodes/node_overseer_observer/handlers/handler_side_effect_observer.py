# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Stub handler for SideEffectObserver.

Phase-0 stub only. Records which Kafka side effects a node emitted.
Wiring happens in Wave 3+.

OMN-12951: SideEffectObserver is an abstract base class (ABC), NOT a
typing.Protocol. The runtime handler resolver instantiates handler_cls
zero-arg; typing.Protocol raises "TypeError: Protocols cannot be
instantiated" and crash-loops bootstrap on infra builds predating the
OMN-12501 quarantine guard (OMN-12956). Concrete implementations inherit
from SideEffectObserver and implement the abstract methods.

Related:
    - OMN-8506: stub side-effect observer + evidence evaluator interfaces
    - OMN-8025: Overseer seam integration epic
    - OMN-12951: crash-loop root cause — Protocol handler instantiation
"""

from __future__ import annotations

import abc
import copy
from typing import Any


class SideEffectObserver(abc.ABC):
    """Abstract base class for side-effect observers.

    Records Kafka side effects emitted by a node for downstream inspection.
    Phase-0 stub — no wiring yet.

    Concrete implementations inherit from this class and implement
    ``record_emission()`` and ``get_emissions()``. The runtime handler
    resolver instantiates the concrete Null* implementation; it never
    instantiates this base class directly (which would raise TypeError from ABC).
    """

    @abc.abstractmethod
    def record_emission(self, *, topic: str, payload: dict[str, Any]) -> None:
        """Record a single Kafka emission from a node."""

    @abc.abstractmethod
    def get_emissions(self) -> list[dict[str, Any]]:
        """Return all recorded emissions in order."""


class NullSideEffectObserver(SideEffectObserver):
    """No-op implementation used as default until wiring is active."""

    def __init__(self) -> None:
        self._emissions: list[dict[str, Any]] = []

    def record_emission(self, *, topic: str, payload: dict[str, Any]) -> None:
        self._emissions.append({"topic": topic, "payload": copy.deepcopy(payload)})

    def get_emissions(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._emissions)


__all__: list[str] = ["NullSideEffectObserver", "SideEffectObserver"]
