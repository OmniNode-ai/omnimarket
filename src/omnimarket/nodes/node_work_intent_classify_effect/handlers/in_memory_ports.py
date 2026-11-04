# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory ports of the work-intent fallback: injected only by overlay, never a default (OMN-22263)."""

from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass

from omnimarket.models.work_classification.model_work_fallback import (
    EnumStoreStatus,
    ModelFallbackKey,
    ModelFallbackResult,
    ModelRouteAnswer,
    ModelStoreOutcome,
)
from omnimarket.protocols.protocol_work_intent_route import RouteUnavailableError


@dataclass(frozen=True)
class LoggedRouteRequest:
    """One route call as logged: the policy and hosts it was given and a prompt length, never the prompt."""

    policy: str
    hosts: tuple[str, ...]
    timeout_s: float
    prompt_chars: int


class ScriptedWorkIntentRoute:
    """A fake delegation route that answers from a script. Selected by overlay, not by default."""

    def __init__(
        self,
        answers: Sequence[ModelRouteAnswer],
        *,
        barrier: threading.Barrier | None = None,
    ) -> None:
        self._answers = list(answers)
        self._barrier = barrier
        self._lock = threading.Lock()
        self.requests: list[LoggedRouteRequest] = []

    def submit(
        self,
        prompt: str,
        *,
        policy: str,
        hosts: tuple[str, ...],
        timeout_s: float,
    ) -> ModelRouteAnswer:
        with self._lock:
            self.requests.append(
                LoggedRouteRequest(policy, hosts, timeout_s, len(prompt))
            )
            if not self._answers:
                raise RouteUnavailableError("the scripted route has no answer left")
            answer = self._answers.pop(0)
        if self._barrier is not None:
            self._barrier.wait(timeout=timeout_s)
        return answer


class InMemoryFallbackStore:
    """The fallback table with one atomic put-if-absent. A lookup then a write is not offered."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: dict[ModelFallbackKey, ModelFallbackResult] = {}
        self._refused: dict[ModelFallbackKey, int] = {}

    def get(self, key: ModelFallbackKey) -> ModelFallbackResult | None:
        with self._lock:
            return self._rows.get(key)

    def put_if_absent(
        self, key: ModelFallbackKey, result: ModelFallbackResult
    ) -> ModelStoreOutcome:
        with self._lock:
            held = self._rows.get(key)
            if held is None:
                self._rows[key] = result
                return ModelStoreOutcome(status=EnumStoreStatus.STORED, stored=result)
            if held.work_type_id == result.work_type_id:
                return ModelStoreOutcome(status=EnumStoreStatus.SAME, stored=held)
            self._refused[key] = self._refused.get(key, 0) + 1
            return ModelStoreOutcome(status=EnumStoreStatus.REFUSED, stored=held)

    def refused_count(self, key: ModelFallbackKey) -> int:
        with self._lock:
            return self._refused.get(key, 0)


__all__ = ["InMemoryFallbackStore", "LoggedRouteRequest", "ScriptedWorkIntentRoute"]
