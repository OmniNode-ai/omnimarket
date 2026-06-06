# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for HandlerInsightsToPlanCompute."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_insights_to_plan_compute.handlers.handler_insights_to_plan_compute import (
    HandlerInsightsToPlanCompute,
)
from omnimarket.nodes.node_insights_to_plan_compute.models.model_insights_to_plan_compute_request import (
    ModelInsightsToPlanComputeRequest,
)


@pytest.mark.unit
def test_handler_extracts_plan_from_html(tmp_path: Path) -> None:
    handler = HandlerInsightsToPlanCompute()
    html_path = tmp_path / "insights.html"
    html_path.write_text(
        """
        <html>
          <head><title>Ticketing Insights</title></head>
          <body>
            <h1>Runtime Followups</h1>
            <p>Linear ticket creation is stable after the retry fix.</p>
            <ul>
              <li>Action: Owner: platform fix critical receipt evidence gap</li>
              <li>Todo: low priority docs cleanup</li>
              <li>Observed but not actionable</li>
            </ul>
          </body>
        </html>
        """,
        encoding="utf-8",
    )

    request = ModelInsightsToPlanComputeRequest(
        html_path=str(html_path), source_type="ticketing_insights"
    )

    result = handler.handle(request)

    assert result.status == "ok"
    assert result.error is None
    assert result.plan["title"] == "Ticketing Insights"
    assert result.plan["source_type"] == "ticketing_insights"
    assert result.plan["action_count"] == 2
    assert result.action_items[0].description == (
        "Owner: platform fix critical receipt evidence gap"
    )
    assert result.action_items[0].priority == "high"
    assert result.action_items[0].owner == "platform"
    assert result.action_items[1].priority == "low"


@pytest.mark.unit
def test_handler_rejects_relative_path() -> None:
    handler = HandlerInsightsToPlanCompute()
    request = ModelInsightsToPlanComputeRequest(html_path="insights.html")

    result = handler.handle(request)

    assert result.status == "error"
    assert result.error == "html_path must be absolute"


@pytest.mark.unit
def test_contract_marks_implemented() -> None:
    contract_path = Path(__file__).resolve().parent.parent / "contract.yaml"
    with contract_path.open(encoding="utf-8") as file:
        contract = yaml.safe_load(file)

    assert contract["node_not_implemented"] is False


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
