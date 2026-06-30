# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-8722 regression tests for node_quality_scoring_compute.

Guards the stub-elimination guarantee:

1. No production source file in the node package contains the substring
   "stub" (case-insensitive). Reintroducing a `status: 'stub'` path, a
   stub-tracking field, or a `# stub-ok` annotation fails this test
   deterministically.
2. The metadata model carries no stub-tracking field (`tracking_url`).
3. Real temporal-relevance scoring is produced for any non-empty input:
   status is never 'stub', the score is non-null, and the temporal
   dimension reflects staleness markers (TODO/FIXME/deprecated) rather
   than a hardcoded 0.0.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import omnimarket.nodes.node_quality_scoring_compute as _node_pkg
from omnimarket.nodes.node_quality_scoring_compute.handlers.handler_compute import (
    handle_quality_scoring_compute,
)
from omnimarket.nodes.node_quality_scoring_compute.handlers.handler_quality_scoring import (
    _compute_temporal_relevance_score,
)
from omnimarket.nodes.node_quality_scoring_compute.models.model_quality_scoring_input import (
    ModelQualityScoringInput,
)
from omnimarket.nodes.node_quality_scoring_compute.models.model_quality_scoring_metadata import (
    ModelQualityScoringMetadata,
)

_NODE_DIR = Path(_node_pkg.__file__).resolve().parent

_FRESH_CONTENT = """
from __future__ import annotations


def add(a: int, b: int) -> int:
    return a + b
"""

_STALE_CONTENT = """
from __future__ import annotations


def add(a: int, b: int) -> int:
    # TODO: this is broken, FIXME later
    # deprecated path, do not use
    return a + b
"""


def _production_source_files() -> list[Path]:
    """All production source files (py + yaml) under the node package."""
    files: list[Path] = []
    for pattern in ("*.py", "*.yaml"):
        files.extend(
            p for p in _NODE_DIR.rglob(pattern) if "__pycache__" not in p.parts
        )
    return files


def _make_input(content: str) -> ModelQualityScoringInput:
    return ModelQualityScoringInput(
        source_path="probe.py",
        content=content,
        language="python",
    )


@pytest.mark.unit
class TestNoStubFraming:
    """The node must carry no stub framing in production source."""

    def test_no_stub_substring_in_production_sources(self) -> None:
        offenders: list[str] = []
        for path in _production_source_files():
            text = path.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), 1):
                if "stub" in line.lower():
                    rel = path.relative_to(_NODE_DIR)
                    offenders.append(f"{rel}:{lineno}: {line.strip()}")
        assert not offenders, "stub framing reintroduced:\n" + "\n".join(offenders)

    def test_metadata_model_has_no_tracking_url_field(self) -> None:
        assert "tracking_url" not in ModelQualityScoringMetadata.model_fields

    def test_metadata_status_description_has_no_stub(self) -> None:
        desc = ModelQualityScoringMetadata.model_fields["status"].description or ""
        assert "stub" not in desc.lower()


@pytest.mark.unit
class TestTemporalRelevanceReal:
    """Temporal relevance is computed, never a hardcoded stub 0.0."""

    def test_status_never_stub_for_non_empty_input(self) -> None:
        result = handle_quality_scoring_compute(_make_input(_FRESH_CONTENT))
        assert result.metadata is not None
        assert result.metadata.status != "stub"

    def test_score_and_dimensions_non_null_on_success(self) -> None:
        result = handle_quality_scoring_compute(_make_input(_FRESH_CONTENT))
        assert result.success is True
        assert result.quality_score is not None
        assert result.quality_score > 0.0
        assert result.dimensions["temporal_relevance"] is not None

    def test_fresh_code_scores_max_temporal_relevance(self) -> None:
        # No staleness markers -> freshness score is 1.0 (not a stubbed 0.0).
        assert _compute_temporal_relevance_score(_FRESH_CONTENT) == 1.0

    def test_staleness_markers_lower_temporal_relevance(self) -> None:
        fresh = _compute_temporal_relevance_score(_FRESH_CONTENT)
        stale = _compute_temporal_relevance_score(_STALE_CONTENT)
        assert stale < fresh

    def test_temporal_relevance_feeds_aggregate_for_stale_input(self) -> None:
        result = handle_quality_scoring_compute(_make_input(_STALE_CONTENT))
        assert result.success is True
        assert result.metadata is not None
        assert result.metadata.status != "stub"
        assert 0.0 <= result.dimensions["temporal_relevance"] < 1.0
