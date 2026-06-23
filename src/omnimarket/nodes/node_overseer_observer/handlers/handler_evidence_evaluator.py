# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Stub handler for EvidenceEvaluator.

Phase-0 stub only. Checks dod_evidence fields against observed outputs.
Wiring happens in Wave 3+.

OMN-12951: EvidenceEvaluator is an abstract base class (ABC), NOT a
typing.Protocol. The runtime handler resolver instantiates handler_cls
zero-arg; typing.Protocol raises "TypeError: Protocols cannot be
instantiated" and crash-loops bootstrap on infra builds predating the
OMN-12501 quarantine guard (OMN-12956). Concrete implementations inherit
from EvidenceEvaluator and implement the abstract methods.

Related:
    - OMN-8506: stub side-effect observer + evidence evaluator interfaces
    - OMN-8025: Overseer seam integration epic
    - OMN-12951: crash-loop root cause — Protocol handler instantiation
"""

from __future__ import annotations

import abc
from typing import Any

from omnimarket.nodes.node_overseer_observer.models.model_overseer_observation_request import (
    ModelOverseerObservationRequest,
)
from omnimarket.nodes.node_overseer_observer.models.model_overseer_observation_result import (
    ModelOverseerObservationResult,
)


class EvidenceEvaluator(abc.ABC):
    """Abstract base class for evidence evaluators.

    Checks dod_evidence fields against observed side-effect outputs.
    Phase-0 stub — no wiring yet.

    Concrete implementations inherit from this class and implement
    ``evaluate()``. The runtime handler resolver instantiates the
    concrete Null* implementation; it never instantiates this base class
    directly (which would raise TypeError from ABC).
    """

    @abc.abstractmethod
    def evaluate(
        self,
        *,
        dod_evidence: list[dict[str, Any]],
        observed: list[dict[str, Any]],
    ) -> bool:
        """Return True if all dod_evidence requirements are satisfied."""


class NullEvidenceEvaluator(EvidenceEvaluator):
    """No-op implementation — always passes until wiring is active."""

    def evaluate(
        self,
        *,
        dod_evidence: list[dict[str, Any]],
        observed: list[dict[str, Any]],
    ) -> bool:
        return True

    def handle(
        self,
        request: ModelOverseerObservationRequest,
    ) -> ModelOverseerObservationResult:
        passed = self.evaluate(
            dod_evidence=request.dod_evidence,
            observed=request.observed,
        )
        return ModelOverseerObservationResult(
            passed=passed,
            observed_count=len(request.observed),
            evidence_count=len(request.dod_evidence),
        )


__all__: list[str] = ["EvidenceEvaluator", "NullEvidenceEvaluator"]
