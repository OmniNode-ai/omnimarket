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
