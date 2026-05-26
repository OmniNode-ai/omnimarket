# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for HandlerInsightsToPlanCompute stub."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_insights_to_plan_compute.handlers.handler_insights_to_plan_compute import (
    HandlerInsightsToPlanCompute,
)
from omnimarket.nodes.node_insights_to_plan_compute.models.model_insights_to_plan_compute_request import (
    ModelInsightsToPlanComputeRequest,
)


@pytest.mark.unit
def test_handler_raises_not_implemented() -> None:
    """Stub handler must raise NotImplementedError."""
    handler = HandlerInsightsToPlanCompute()
    request = ModelInsightsToPlanComputeRequest(html_path="/tmp/insights.html")
    with pytest.raises(NotImplementedError):
        handler.handle(request)


@pytest.mark.unit
def test_request_model_frozen() -> None:
    """Request model must be frozen (immutable)."""
    request = ModelInsightsToPlanComputeRequest(html_path="/tmp/insights.html")
    with pytest.raises(ValidationError):
        request.html_path = "/other/path"  # type: ignore[misc]


@pytest.mark.unit
def test_request_default_source_type() -> None:
    """source_type defaults to 'generic'."""
    request = ModelInsightsToPlanComputeRequest(html_path="/tmp/insights.html")
    assert request.source_type == "generic"
