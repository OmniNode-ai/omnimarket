"""Golden chain tests for node_closeout_effect.

Verifies dry-run mode and model construction. Full handler tests require
gh CLI and omnimarket node_close_out, so we focus on contract/model validation.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from omnimarket.nodes.node_closeout_effect.models.model_closeout_input import (
    ModelCloseoutInput,
)
from omnimarket.nodes.node_closeout_effect.models.model_closeout_result import (
    ModelCloseoutResult,
)


@pytest.mark.unit
class TestCloseoutEffectGoldenChain:
    """Golden chain: model construction and validation."""

    def test_input_model_construction(self) -> None:
        """ModelCloseoutInput constructs correctly."""
        cid = uuid4()
        inp = ModelCloseoutInput(correlation_id=cid, dry_run=True)
        assert inp.correlation_id == cid
        assert inp.dry_run is True

    def test_result_model_construction(self) -> None:
        """ModelCloseoutResult constructs with all fields."""
        cid = uuid4()
        result = ModelCloseoutResult(
            correlation_id=cid,
            merge_sweep_completed=True,
            prs_merged=5,
            quality_gates_passed=True,
            release_ready=True,
            warnings=("test warning",),
        )
        assert result.correlation_id == cid
        assert result.prs_merged == 5
        assert result.merge_sweep_completed is True
        assert result.quality_gates_passed is True
        assert result.release_ready is True
        assert len(result.warnings) == 1

    def test_result_defaults(self) -> None:
        """ModelCloseoutResult defaults are sensible."""
        result = ModelCloseoutResult(correlation_id=uuid4())
        assert result.merge_sweep_completed is False
        assert result.prs_merged == 0
        assert result.quality_gates_passed is False
        assert result.release_ready is False
        assert result.warnings == ()
