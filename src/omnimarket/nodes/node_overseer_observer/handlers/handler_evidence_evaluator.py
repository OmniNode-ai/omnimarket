# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Stub handler for EvidenceEvaluator.

Phase-0 stub only. Checks dod_evidence fields against observed outputs.
Wiring happens in Wave 3+.

Related:
    - OMN-8506: stub side-effect observer + evidence evaluator interfaces
    - OMN-8025: Overseer seam integration epic
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class EvidenceEvaluator(Protocol):
    """Evaluator that checks dod_evidence fields against observed outputs.

    Phase-0 stub — no wiring yet.
    Receives observed side-effect data and a dod_evidence specification,
    returns whether all evidence requirements are satisfied.
    """

    def evaluate(
        self,
        *,
        dod_evidence: list[dict[str, Any]],
        observed: list[dict[str, Any]],
    ) -> bool:
        """Return True if all dod_evidence requirements are satisfied."""
        ...


class NullEvidenceEvaluator:
    """No-op implementation — always passes until wiring is active."""

    def evaluate(
        self,
        *,
        dod_evidence: list[dict[str, Any]],
        observed: list[dict[str, Any]],
    ) -> bool:
        return True


__all__: list[str] = ["EvidenceEvaluator", "NullEvidenceEvaluator"]
