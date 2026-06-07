# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Unit tests for the local delegation request wire model."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from omnimarket.models.delegation.wire.model_delegation_request import (
    DELEGATION_DEFAULT_MAX_TOKENS,
    DELEGATION_MAX_TOKENS_HARD_LIMIT,
    ModelDelegationRequest,
)


def _request(**overrides: object) -> ModelDelegationRequest:
    payload: dict[str, object] = {
        "prompt": "Explain the routing failure",
        "task_type": "reasoning",
        "correlation_id": uuid4(),
        "emitted_at": datetime.now(UTC),
    }
    payload.update(overrides)
    return ModelDelegationRequest.model_validate(payload)


@pytest.mark.unit
def test_omitted_max_tokens_defaults_to_local_hard_limit() -> None:
    request = _request()

    assert request.max_tokens == DELEGATION_DEFAULT_MAX_TOKENS


@pytest.mark.unit
def test_explicit_max_tokens_hard_limit_accepted() -> None:
    request = _request(max_tokens=DELEGATION_MAX_TOKENS_HARD_LIMIT)

    assert request.max_tokens == 8192


@pytest.mark.unit
@pytest.mark.parametrize("max_tokens", [8193, 16384])
def test_max_tokens_above_hard_limit_rejected(max_tokens: int) -> None:
    with pytest.raises(ValidationError):
        _request(max_tokens=max_tokens)


@pytest.mark.unit
def test_overlay_accepts_quality_gate_dod_checks() -> None:
    request = _request(
        quality_contract_mode="replace_task_class",
        acceptance_criteria=(
            "compiles_without_errors",
            "final_artifact_only",
            "no_refusal",
            "uses_pytest_mark_unit",
            "covers_edge_cases",
            "covers_error_paths",
            "follows_codebase_conventions",
            "no_obvious_regressions",
        ),
    )

    assert request.quality_contract_mode == "replace_task_class"
    assert request.acceptance_criteria == (
        "compiles_without_errors",
        "final_artifact_only",
        "no_refusal",
        "uses_pytest_mark_unit",
        "covers_edge_cases",
        "covers_error_paths",
        "follows_codebase_conventions",
        "no_obvious_regressions",
    )


@pytest.mark.unit
def test_unknown_overlay_acceptance_criterion_rejected() -> None:
    with pytest.raises(ValidationError, match="unsupported acceptance criteria"):
        _request(acceptance_criteria=("semantic_magic",))
