# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains for the compaction verdict compute (OMN-21973)."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_compaction_verdict_compute.handlers.handler_compaction_verdict import (
    HandlerCompactionVerdict,
)
from omnimarket.nodes.node_compaction_verdict_compute.models import (
    CompactionVerdictRefusedError,
    EnumCompactionVerdict,
    EnumRunLabel,
    ModelArmCell,
    ModelCompactionVerdictRequest,
    ModelMetricCount,
    ModelPairedBoundary,
)

pytestmark = pytest.mark.unit

_FIXTURE = (
    Path(__file__).parent
    / "nodes"
    / "node_compaction_verdict_compute"
    / "fixtures"
    / "thresholds.yaml"
)
_METRICS = ("ledger_recall", "acted_share", "contradiction", "planted_recall")


def _cell(session: int, boundary: int) -> ModelPairedBoundary:
    metrics = {m: ModelMetricCount(successes=0, trials=5) for m in _METRICS}
    return ModelPairedBoundary(
        session_id=f"s{session:03d}",
        boundary_id=f"s{session:03d}-b{boundary}",
        mod=ModelArmCell(metrics=metrics, cost=1.0),
        stock=ModelArmCell(metrics=metrics, cost=1.0),
    )


def _request(
    boundaries: list[ModelPairedBoundary], *, confirmatory: bool
) -> ModelCompactionVerdictRequest:
    return ModelCompactionVerdictRequest(
        run_id="golden-chain",
        thresholds_path=str(_FIXTURE),
        thresholds_text=_FIXTURE.read_text(encoding="utf-8"),
        confirmatory=confirmatory,
        boundaries=tuple(boundaries),
    )


def test_golden_chain_small_identical_arms_is_inconclusive() -> None:
    """Six identical sessions run exploratory give INCONCLUSIVE, never NO_DIFFERENCE."""
    corpus = [_cell(s, b) for s in range(6) for b in range(2)]
    report = HandlerCompactionVerdict().handle(_request(corpus, confirmatory=False))
    assert report.verdict is EnumCompactionVerdict.INCONCLUSIVE
    assert report.run_label is EnumRunLabel.EXPLORATORY
    assert report.sessions == 6
    assert report.boundaries_used == 12


def test_error_chain_duplicate_boundary_is_refused() -> None:
    """A boundary that appears twice is refused before any statistic is computed."""
    cell = _cell(0, 0)
    with pytest.raises(CompactionVerdictRefusedError, match="more than once"):
        HandlerCompactionVerdict().handle(
            _request([cell, cell.model_copy()], confirmatory=False)
        )
