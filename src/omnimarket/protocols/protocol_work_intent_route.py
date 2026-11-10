# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of the work-intent fallback: the delegation route and the store-once result store (OMN-22263)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omnimarket.models.work_classification.model_work_fallback import (
    ModelFallbackKey,
    ModelFallbackResult,
    ModelRouteAnswer,
    ModelStoreOutcome,
)


class RouteUnavailableError(RuntimeError):
    """The delegation route could not answer inside its timeout."""


@runtime_checkable
class ProtocolWorkIntentRoute(Protocol):
    """The delegation route. It answers with a work-type id and a receipt, never the text."""

    def submit(
        self,
        prompt: str,
        *,
        policy: str,
        hosts: tuple[str, ...],
        timeout_s: float,
    ) -> ModelRouteAnswer: ...


@runtime_checkable
class ProtocolFallbackResultStore(Protocol):
    """The reducer's fallback table: one atomic put-if-absent per key, never a lookup then a write."""

    def get(self, key: ModelFallbackKey) -> ModelFallbackResult | None: ...

    def put_if_absent(
        self, key: ModelFallbackKey, result: ModelFallbackResult
    ) -> ModelStoreOutcome: ...

    def refused_count(self, key: ModelFallbackKey) -> int: ...


__all__: list[str] = [
    "ProtocolFallbackResultStore",
    "ProtocolWorkIntentRoute",
    "RouteUnavailableError",
]
