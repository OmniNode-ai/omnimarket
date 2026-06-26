# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Focused node-local unit tests for HandlerEntropyExperimentOrchestrator (OMN-13614).

Node-local coverage (under ``src/.../tests/``) so the contract-aware dependency
health gate (which scans ``--repo-roots src``) sees the handler as tested. The
broader golden-chain lives under ``tests/nodes/node_entropy_experiment_orchestrator/``.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest
from omnibase_core.enums.enum_experiment_status import EnumExperimentStatus
from omnibase_core.enums.enum_experiment_type import EnumExperimentType
from omnibase_core.models.experiment.model_experiment_result import (
    ModelExperimentResult,
)

from omnimarket.nodes.node_entropy_experiment_orchestrator.handlers.handler_entropy_experiment_orchestrator import (
    HandlerEntropyExperimentOrchestrator,
)
from omnimarket.nodes.node_entropy_experiment_orchestrator.models.model_entropy_experiment_request import (
    ModelEntropyExperimentRequest,
    ModelEntropyTrackInput,
)
from omnimarket.nodes.node_entropy_experiment_orchestrator.models.model_entropy_failure import (
    EntropyFailureClass,
)

_EXPERIMENT_ID = UUID("11111111-1111-1111-1111-111111111111")
_RUN_ID = UUID("22222222-2222-2222-2222-222222222222")
_CORRELATION_ID = UUID("33333333-3333-3333-3333-333333333333")
_EVIDENCE_ID = UUID("44444444-4444-4444-4444-444444444444")


def _request(*tracks: ModelEntropyTrackInput) -> ModelEntropyExperimentRequest:
    return ModelEntropyExperimentRequest(
        experiment_id=_EXPERIMENT_ID,
        run_id=_RUN_ID,
        correlation_id=_CORRELATION_ID,
        runtime_identity="dev/runtime-local",
        evidence_id=_EVIDENCE_ID,
        tracks=tracks,
    )


@pytest.fixture
def handler() -> HandlerEntropyExperimentOrchestrator:
    return HandlerEntropyExperimentOrchestrator()


def test_emits_canonical_core_result(
    handler: HandlerEntropyExperimentOrchestrator,
) -> None:
    result = handler.handle(
        _request(
            ModelEntropyTrackInput(
                track_id="omninode:0",
                framework="omninode",
                succeeded=True,
                total_cost_usd=Decimal("0.0021"),
            )
        )
    )
    assert isinstance(result, ModelExperimentResult)
    assert result.experiment_type == EnumExperimentType.ENTROPY
    assert result.status == EnumExperimentStatus.COMPLETED
    assert result.score.value == pytest.approx(1.0)
    assert result.cost.cost_usd == Decimal("0.0021")
    assert result.evidence_ref.evidence_id == _EVIDENCE_ID


def test_all_failed_is_failed_status(
    handler: HandlerEntropyExperimentOrchestrator,
) -> None:
    result = handler.handle(
        _request(
            ModelEntropyTrackInput(
                track_id="langchain:0",
                framework="langchain",
                succeeded=False,
                total_cost_usd=Decimal("0.0034"),
                failure_classes=(EntropyFailureClass.TIMEOUT,),
            )
        )
    )
    assert result.status == EnumExperimentStatus.FAILED
    assert result.score.value == pytest.approx(0.0)


def test_deterministic(handler: HandlerEntropyExperimentOrchestrator) -> None:
    req = _request(
        ModelEntropyTrackInput(
            track_id="plain_python:0",
            framework="plain_python",
            succeeded=True,
            total_cost_usd=Decimal("0.0009"),
        )
    )
    assert handler.handle(req) == handler.handle(req)
