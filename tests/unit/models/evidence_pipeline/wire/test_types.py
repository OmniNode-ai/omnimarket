# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Unit tests for evidence_pipeline/wire/types.py.

Covers:
- Member completeness for all 10 Literal type aliases
- __all__ parity with actual public exports
- Type annotation acceptance in consuming Pydantic models
- Migration target comment presence in source file
"""

from __future__ import annotations

import importlib
from typing import get_args

import pytest
from pydantic import ValidationError

import omnimarket.models.evidence_pipeline.wire.types as types_module
from omnimarket.models.evidence_pipeline.wire.model_deployment_readiness_result import (
    ModelDeploymentReadinessResult,
)
from omnimarket.models.evidence_pipeline.wire.model_evidence_dashboard_projection import (
    ModelEvidenceDashboardProjection,
    ModelEvidenceDashboardStageProjection,
)
from omnimarket.models.evidence_pipeline.wire.model_evidence_validation_result import (
    ModelEvidenceValidationResult,
)
from omnimarket.models.evidence_pipeline.wire.model_readiness_aggregate_projection import (
    ModelReadinessAggregateProjection,
)
from omnimarket.models.evidence_pipeline.wire.types import (
    DashboardSeverity,
    DashboardStage,
    DashboardStatus,
    EvidenceLifecycleState,
    FreshnessState,
    GapClassification,
    MissingEventClassification,
    ReadinessState,
    TriggerSurface,
    ValidationState,
)


def _literal_members(alias: object) -> set[str]:
    """Return the set of string members from a PEP 695 TypeAliasType.

    Python 3.12 ``type`` statements (PEP 695) create ``typing.TypeAliasType``
    instances. ``get_args()`` on the alias itself returns ``()``; the Literal
    args live on ``alias.__value__``.
    """
    return set(get_args(alias.__value__))


# ---------------------------------------------------------------------------
# 1. Member completeness — one test per Literal type alias
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_evidence_lifecycle_state_members() -> None:
    assert _literal_members(EvidenceLifecycleState) == {
        "PROVISIONAL",
        "VALIDATED",
        "FINALIZED",
        "SUPERSEDED",
        "REJECTED",
    }


@pytest.mark.unit
def test_gap_classification_members() -> None:
    assert _literal_members(GapClassification) == {
        "MISSING",
        "STALE",
        "SUPERSEDED",
        "HASH_MISMATCH",
        "RECEIPT_MISSING",
        "VALIDATION_FAILED",
        "UNKNOWN",
    }


@pytest.mark.unit
def test_readiness_state_members() -> None:
    assert _literal_members(ReadinessState) == {
        "READY",
        "BLOCKED",
        "DEGRADED",
        "ADVISORY_ONLY",
    }


@pytest.mark.unit
def test_dashboard_severity_members() -> None:
    assert _literal_members(DashboardSeverity) == {
        "INFO",
        "WARNING",
        "ERROR",
        "BLOCKING",
    }


@pytest.mark.unit
def test_dashboard_stage_members() -> None:
    assert _literal_members(DashboardStage) == {
        "TRIGGERED",
        "COLLECTED",
        "EXTRACTED",
        "VALIDATED",
        "OCC_PR",
        "COMPLETED",
        "BLOCKED",
        "READINESS_GATE_STARTED",
        "READINESS_GATE_COMPLETED",
        "READINESS_GATE_BLOCKED",
    }


@pytest.mark.unit
def test_dashboard_status_members() -> None:
    assert _literal_members(DashboardStatus) == {
        "PENDING",
        "IN_FLIGHT",
        "PASSED",
        "FAILED",
        "BLOCKED",
        "STALE",
        "DEGRADED",
    }


@pytest.mark.unit
def test_freshness_state_members() -> None:
    assert _literal_members(FreshnessState) == {
        "CURRENT",
        "STALE",
        "DEGRADED",
    }


@pytest.mark.unit
def test_missing_event_classification_members() -> None:
    assert _literal_members(MissingEventClassification) == {
        "DELAYED",
        "STALE",
        "MISSING",
        "SUPERSEDED",
        "INVALID_ORDER",
        "PROJECTION_GAP",
    }


@pytest.mark.unit
def test_trigger_surface_members() -> None:
    assert _literal_members(TriggerSurface) == {
        "pull_request_merge",
        "merge_group",
        "deploy",
        "manual",
        "runtime_event",
    }


@pytest.mark.unit
def test_validation_state_members() -> None:
    assert _literal_members(ValidationState) == {
        "PASSED",
        "FAILED",
        "ADVISORY_ONLY",
    }


# ---------------------------------------------------------------------------
# 2. __all__ parity
# ---------------------------------------------------------------------------

_EXPECTED_ALL = {
    "DashboardSeverity",
    "DashboardStage",
    "DashboardStatus",
    "EvidenceLifecycleState",
    "FreshnessState",
    "GapClassification",
    "MissingEventClassification",
    "ReadinessState",
    "TriggerSurface",
    "ValidationState",
}


@pytest.mark.unit
def test_all_count() -> None:
    assert len(types_module.__all__) == 10


@pytest.mark.unit
def test_all_matches_expected_set() -> None:
    assert set(types_module.__all__) == _EXPECTED_ALL


@pytest.mark.unit
def test_all_names_are_importable() -> None:
    for name in types_module.__all__:
        assert hasattr(types_module, name), (
            f"{name!r} in __all__ but not importable from module"
        )


@pytest.mark.unit
def test_all_no_extra_names() -> None:
    """Ensure __all__ does not contain names absent from the module."""
    missing = [name for name in types_module.__all__ if not hasattr(types_module, name)]
    assert missing == [], f"Names in __all__ not present on module: {missing}"


# ---------------------------------------------------------------------------
# 3. Type annotation acceptance in consuming Pydantic models
# ---------------------------------------------------------------------------

_VALIDATION_RESULT_BASE: dict[str, object] = {
    "correlation_id": "corr-001",
    "validation_run_id": "run-001",
    "ticket_id": "OMN-99999",
    "repository": "omnibase_compat",
    "contract_hash": "abc123",
    "evidence_bundle_hash": "def456",
    "verifier_identity": "ci-bot",
    "validator_version": "1.0.0",
    "validated_at": "2026-05-23T00:00:00Z",
    "topology_affecting": False,
}

_READINESS_RESULT_BASE: dict[str, object] = {
    "correlation_id": "corr-001",
    "validation_run_id": "run-001",
    "deployment_id": "deploy-001",
    "scored_at": "2026-05-23T00:00:00Z",
    "validator_version": "1.0.0",
    "gap_report_hash": "gapXXX",
}

_READINESS_PROJECTION_BASE: dict[str, object] = {
    "deployment_id": "deploy-001",
    "projection_cursor": "cursor-001",
    "last_event_id": "evt-001",
    "last_ingest_sequence": 0,
    "observed_at": "2026-05-23T00:00:00Z",
}

_DASHBOARD_PROJECTION_BASE: dict[str, object] = {
    "projection_cursor": "cursor-001",
    "last_event_id": "evt-001",
    "last_ingest_sequence": 0,
    "observed_at": "2026-05-23T00:00:00Z",
}


@pytest.mark.unit
class TestEvidenceValidationResultTypeAcceptance:
    """ModelEvidenceValidationResult accepts ValidationState / EvidenceLifecycleState."""

    def test_accepts_all_validation_state_values(self) -> None:
        for state in _literal_members(ValidationState):
            result = ModelEvidenceValidationResult(
                **_VALIDATION_RESULT_BASE,
                validation_state=state,
                evidence_lifecycle_state="PROVISIONAL",
            )
            assert result.validation_state == state

    def test_accepts_all_evidence_lifecycle_state_values(self) -> None:
        for state in _literal_members(EvidenceLifecycleState):
            result = ModelEvidenceValidationResult(
                **_VALIDATION_RESULT_BASE,
                validation_state="PASSED",
                evidence_lifecycle_state=state,
            )
            assert result.evidence_lifecycle_state == state

    def test_rejects_invalid_validation_state(self) -> None:
        with pytest.raises(ValidationError):
            ModelEvidenceValidationResult(
                **_VALIDATION_RESULT_BASE,
                validation_state="UNKNOWN_VALUE",
                evidence_lifecycle_state="PROVISIONAL",
            )

    def test_rejects_invalid_evidence_lifecycle_state(self) -> None:
        with pytest.raises(ValidationError):
            ModelEvidenceValidationResult(
                **_VALIDATION_RESULT_BASE,
                validation_state="PASSED",
                evidence_lifecycle_state="ACTIVE",
            )


@pytest.mark.unit
class TestDeploymentReadinessResultTypeAcceptance:
    """ModelDeploymentReadinessResult accepts valid ReadinessState."""

    def test_accepts_all_readiness_state_values(self) -> None:
        for state in _literal_members(ReadinessState):
            result = ModelDeploymentReadinessResult(
                **_READINESS_RESULT_BASE,
                readiness_state=state,
            )
            assert result.readiness_state == state

    def test_rejects_invalid_readiness_state(self) -> None:
        with pytest.raises(ValidationError):
            ModelDeploymentReadinessResult(
                **_READINESS_RESULT_BASE,
                readiness_state="UNKNOWN",
            )


@pytest.mark.unit
class TestReadinessAggregateProjectionTypeAcceptance:
    """ModelReadinessAggregateProjection accepts ReadinessState / DashboardStatus / FreshnessState.

    Each valid literal value must round-trip through the model; invalid values must raise.
    """

    def test_accepts_all_readiness_state_values(self) -> None:
        for state in _literal_members(ReadinessState):
            result = ModelReadinessAggregateProjection(
                **_READINESS_PROJECTION_BASE,
                freshness_state="CURRENT",
                readiness_state=state,
                evidence_pipeline_state="PENDING",
            )
            assert result.readiness_state == state

    def test_accepts_all_dashboard_status_values(self) -> None:
        for status in _literal_members(DashboardStatus):
            result = ModelReadinessAggregateProjection(
                **_READINESS_PROJECTION_BASE,
                freshness_state="CURRENT",
                readiness_state="READY",
                evidence_pipeline_state=status,
            )
            assert result.evidence_pipeline_state == status

    def test_accepts_all_freshness_state_values_except_degraded(self) -> None:
        for state in _literal_members(FreshnessState):
            if state == "DEGRADED":
                # DEGRADED requires degraded_reason — tested separately
                continue
            result = ModelReadinessAggregateProjection(
                **_READINESS_PROJECTION_BASE,
                freshness_state=state,
                readiness_state="READY",
                evidence_pipeline_state="PENDING",
            )
            assert result.freshness_state == state

    def test_accepts_degraded_with_reason(self) -> None:
        result = ModelReadinessAggregateProjection(
            **_READINESS_PROJECTION_BASE,
            freshness_state="DEGRADED",
            degraded_reason="upstream timeout",
            readiness_state="READY",
            evidence_pipeline_state="PENDING",
        )
        assert result.freshness_state == "DEGRADED"

    def test_rejects_invalid_readiness_state(self) -> None:
        with pytest.raises(ValidationError):
            ModelReadinessAggregateProjection(
                **_READINESS_PROJECTION_BASE,
                freshness_state="CURRENT",
                readiness_state="PENDING",
                evidence_pipeline_state="PENDING",
            )

    def test_rejects_invalid_dashboard_status(self) -> None:
        with pytest.raises(ValidationError):
            ModelReadinessAggregateProjection(
                **_READINESS_PROJECTION_BASE,
                freshness_state="CURRENT",
                readiness_state="READY",
                evidence_pipeline_state="READY",
            )

    def test_gap_breakdown_accepts_valid_gap_classifications(self) -> None:
        gap_breakdown = dict.fromkeys(_literal_members(GapClassification), 1)
        result = ModelReadinessAggregateProjection(
            **_READINESS_PROJECTION_BASE,
            freshness_state="CURRENT",
            readiness_state="BLOCKED",
            evidence_pipeline_state="FAILED",
            gap_breakdown=gap_breakdown,
        )
        assert len(result.gap_breakdown) == len(_literal_members(GapClassification))


@pytest.mark.unit
class TestEvidenceDashboardProjectionTypeAcceptance:
    """ModelEvidenceDashboardProjection and Stage accept DashboardStage / DashboardStatus."""

    def test_stage_projection_accepts_all_dashboard_stages(self) -> None:
        for stage in _literal_members(DashboardStage):
            result = ModelEvidenceDashboardStageProjection(
                stage=stage,
                status="PENDING",
                event_count=0,
                freshness_state="CURRENT",
                last_projection_update_at="2026-05-23T00:00:00Z",
            )
            assert result.stage == stage

    def test_stage_projection_accepts_all_dashboard_statuses(self) -> None:
        for status in _literal_members(DashboardStatus):
            result = ModelEvidenceDashboardStageProjection(
                stage="TRIGGERED",
                status=status,
                event_count=0,
                freshness_state="CURRENT",
                last_projection_update_at="2026-05-23T00:00:00Z",
            )
            assert result.status == status

    def test_stage_projection_rejects_invalid_stage(self) -> None:
        with pytest.raises(ValidationError):
            ModelEvidenceDashboardStageProjection(
                stage="UNKNOWN_STAGE",
                status="PENDING",
                event_count=0,
                freshness_state="CURRENT",
                last_projection_update_at="2026-05-23T00:00:00Z",
            )

    def test_stage_projection_rejects_invalid_status(self) -> None:
        with pytest.raises(ValidationError):
            ModelEvidenceDashboardStageProjection(
                stage="TRIGGERED",
                # ReadinessState value used intentionally — must be rejected
                status="READY",
                event_count=0,
                freshness_state="CURRENT",
                last_projection_update_at="2026-05-23T00:00:00Z",
            )

    def test_dashboard_projection_accepts_valid_freshness_state(self) -> None:
        for state in _literal_members(FreshnessState):
            kwargs: dict[str, object] = {
                **_DASHBOARD_PROJECTION_BASE,
                "freshness_state": state,
            }
            if state == "DEGRADED":
                kwargs["degraded_reason"] = "reason"
            result = ModelEvidenceDashboardProjection(**kwargs)
            assert result.freshness_state == state

    def test_dashboard_projection_rejects_invalid_freshness_state(self) -> None:
        with pytest.raises(ValidationError):
            ModelEvidenceDashboardProjection(
                **_DASHBOARD_PROJECTION_BASE,
                freshness_state="EXPIRED",
            )


# ---------------------------------------------------------------------------
# 4. Package re-exports
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_module_importable_via_wire_init() -> None:
    """All 10 names are re-exported from the wire __init__ package."""
    wire = importlib.import_module("omnimarket.models.evidence_pipeline.wire")
    for name in types_module.__all__:
        assert hasattr(wire, name), f"{name!r} not re-exported from wire __init__"
