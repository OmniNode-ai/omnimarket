# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for ModelDimensionResultV2 — OMN-8132, OMN-8696."""

from omnimarket.nodes.node_platform_readiness.handlers.handler_platform_readiness import (
    EnumReadinessStatus,
    ModelDimensionResult,
    NodePlatformReadiness,
)
from omnimarket.nodes.node_platform_readiness.models.dimension_result_v2 import (
    ModelDimensionEvidence,
    ModelDimensionResultV2,
)


def test_dimension_result_v2_valid_zero_semantics() -> None:
    """valid_zero=True: zero checks on a green repo is a legitimate PASS."""
    r = ModelDimensionResultV2(
        dimension="ci_health",
        status=EnumReadinessStatus.PASS,
        check_count=0,
        valid_zero=True,
        actionable_items=[],
        evidence_source="github_actions",
    )
    assert r.status == EnumReadinessStatus.PASS


def test_dimension_result_v2_broken_zero_is_warn() -> None:
    """valid_zero=False: zero checks means the sweep didn't run — caller sets WARN."""
    r = ModelDimensionResultV2(
        dimension="runtime_wiring",
        status=EnumReadinessStatus.WARN,
        check_count=0,
        valid_zero=False,
        actionable_items=["runtime sweep returned no nodes — check entry points"],
        evidence_source="onex_runtime_api",
    )
    assert r.status == EnumReadinessStatus.WARN
    assert len(r.actionable_items) == 1


def test_dimension_result_v2_defaults() -> None:
    """Minimal construction uses sensible defaults."""
    r = ModelDimensionResultV2(
        dimension="contract_completeness",
        status=EnumReadinessStatus.PASS,
        check_count=42,
        evidence_source="onex_change_control",
    )
    assert r.valid_zero is False
    assert r.actionable_items == []
    assert r.sweep_names == []
    assert r.freshness_seconds is None
    assert r.raw_detail == ""
    assert r.evidence is None


def test_dimension_result_v2_full_fields() -> None:
    """All fields can be populated."""
    r = ModelDimensionResultV2(
        dimension="golden_chain",
        status=EnumReadinessStatus.WARN,
        check_count=5,
        valid_zero=False,
        actionable_items=["sweep stale — last run 5h ago"],
        evidence_source="golden_chain_sweep_artifact",
        sweep_names=["golden-chain-sweep-2026-04-09"],
        freshness_seconds=18000,
        raw_detail="Last sweep: 2026-04-09T10:00:00Z",
    )
    assert r.dimension == "golden_chain"
    assert r.freshness_seconds == 18000
    assert r.sweep_names == ["golden-chain-sweep-2026-04-09"]


def test_dimension_result_v2_fail_status() -> None:
    """FAIL status with actionable items."""
    r = ModelDimensionResultV2(
        dimension="data_flow",
        status=EnumReadinessStatus.FAIL,
        check_count=3,
        valid_zero=False,
        actionable_items=[
            "2 MAJOR gaps in data flow",
            "check topic onex.evt.data.processed",
        ],
        evidence_source="data_flow_artifact",
    )
    assert r.status == EnumReadinessStatus.FAIL
    assert len(r.actionable_items) == 2


def test_v2_does_not_replace_v1() -> None:
    """ModelDimensionResult (V1) still importable and functional — backward compat."""
    v1 = ModelDimensionResult(
        name="runtime_wiring",
        status=EnumReadinessStatus.PASS,
        critical=True,
        freshness="current",
        details="All nodes registered",
    )
    assert v1.status == EnumReadinessStatus.PASS


def test_v1_handler_still_works() -> None:
    """NodePlatformReadiness (V1 handler) still importable and functional."""
    from datetime import UTC, datetime

    from omnimarket.nodes.node_platform_readiness.handlers.handler_platform_readiness import (
        ModelDimensionInput,
        ModelPlatformReadinessRequest,
    )

    handler = NodePlatformReadiness()
    request = ModelPlatformReadinessRequest(
        dimensions=[
            ModelDimensionInput(
                name="test_dim",
                healthy=True,
                last_checked=datetime.now(UTC),
            )
        ]
    )
    result = handler.handle(request)
    assert result.overall == EnumReadinessStatus.PASS


# ---------------------------------------------------------------------------
# Evidence block tests (OMN-8696)
# ---------------------------------------------------------------------------


def test_evidence_block_defaults() -> None:
    """ModelDimensionEvidence uses empty defaults."""
    e = ModelDimensionEvidence()
    assert e.query == ""
    assert e.row_count == 0
    assert e.sample_rows == []
    assert e.last_verified_at == ""


def test_evidence_block_populated() -> None:
    """ModelDimensionEvidence stores all fields correctly."""
    e = ModelDimensionEvidence(
        query="SELECT COUNT(*) FROM routing_outcomes WHERE quality_score IS NOT NULL",
        row_count=42,
        sample_rows=[{"id": 1, "quality_score": 0.9}],
        last_verified_at="2026-04-13T12:00:00+00:00",
    )
    assert e.row_count == 42
    assert len(e.sample_rows) == 1
    assert e.last_verified_at == "2026-04-13T12:00:00+00:00"


def test_dimension_result_v2_with_evidence() -> None:
    """Evidence block round-trips through ModelDimensionResultV2."""
    evidence = ModelDimensionEvidence(
        query="GET http://runtime/api/nodes",
        row_count=45,
        sample_rows=[{"id": "node_foo"}, {"id": "node_bar"}],
        last_verified_at="2026-04-13T12:00:00+00:00",
    )
    r = ModelDimensionResultV2(
        dimension="runtime_wiring",
        status=EnumReadinessStatus.PASS,
        check_count=45,
        evidence_source="onex_runtime_api",
        evidence=evidence,
    )
    assert r.evidence is not None
    assert r.evidence.row_count == 45
    assert len(r.evidence.sample_rows) == 2
    assert r.evidence.query == "GET http://runtime/api/nodes"


def test_evidence_sample_rows_capped_at_three() -> None:
    """sample_rows field accepts more than 3 items (capping is caller responsibility)."""
    e = ModelDimensionEvidence(
        query="test",
        row_count=5,
        sample_rows=[{"i": i} for i in range(5)],
        last_verified_at="2026-04-13T00:00:00+00:00",
    )
    assert len(e.sample_rows) == 5
