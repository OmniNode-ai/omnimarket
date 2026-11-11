# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerTestSelection: the repository's CI test-marker selection, as a pure function.

Definition-B compute: ``handle(request: ModelTestSelectionRequest) -> ModelTestSelectionResult``.
No I/O, no clock. The rule set is the overlay's ``test_markers`` row for the
request's repository: when the pool does not provide the broker, the repository's
own CI selector (its nightly and merge-gating jobs run ``-m "not kafka"``) is
applied and the broker marker is held back; a pool that provides the broker, or a
repository with no marker row, selects exactly as before (no marker).
"""

from __future__ import annotations

from omnimarket.nodes.node_lab_proof_judge_compute.models.model_test_selection import (
    ModelTestSelectionRequest,
    ModelTestSelectionResult,
)
from omnimarket.nodes.node_lab_proof_judge_compute.overlay import (
    ModelLabProofJudgeOverlay,
)


class HandlerTestSelection:
    """Select the test marker for a repository, from the overlay's marker row."""

    def __init__(self, overlay: ModelLabProofJudgeOverlay) -> None:
        self._overlay = overlay

    def handle(self, request: ModelTestSelectionRequest) -> ModelTestSelectionResult:
        """Return the marker expressions for the repository and broker availability."""
        row = next(
            (r for r in self._overlay.test_markers.repos if r.repo == request.repo),
            None,
        )
        if row is None or request.broker_available:
            return ModelTestSelectionResult(
                marker_expression="",
                not_run_marker_expression="",
                not_run_reason="",
                source="",
            )
        return ModelTestSelectionResult(
            marker_expression=row.selector_without_broker,
            not_run_marker_expression=row.broker_marker,
            not_run_reason=row.not_run_reason,
            source=row.source,
        )
