# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for HandlerRrhCompute stub."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_rrh_compute.handlers.handler_rrh_compute import (
    HandlerRrhCompute,
)
from omnimarket.nodes.node_rrh_compute.models.model_rrh_compute_request import (
    ModelRrhComputeRequest,
)


@pytest.mark.unit
def test_handler_raises_not_implemented() -> None:
    """Stub handler must raise NotImplementedError."""
    handler = HandlerRrhCompute()
    request = ModelRrhComputeRequest(release_id="v1.2.3")
    with pytest.raises(NotImplementedError):
        handler.handle(request)


@pytest.mark.unit
def test_request_model_frozen() -> None:
    """Request model must be frozen (immutable)."""
    request = ModelRrhComputeRequest(release_id="v1.2.3")
    with pytest.raises(ValidationError):
        request.release_id = "v2.0.0"  # type: ignore[misc]


@pytest.mark.unit
def test_request_default_checks() -> None:
    """checks defaults to empty list (meaning all registered checks)."""
    request = ModelRrhComputeRequest(release_id="v1.2.3")
    assert request.checks == []
