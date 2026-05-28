# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for HandlerRrhCompute."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_rrh_compute.handlers.handler_rrh_compute import (
    HandlerRrhCompute,
)
from omnimarket.nodes.node_rrh_compute.models.model_rrh_compute_request import (
    ModelRrhComputeRequest,
)


@pytest.mark.unit
def test_handler_passes_registered_checks_by_default() -> None:
    handler = HandlerRrhCompute()
    request = ModelRrhComputeRequest(release_id="v1.2.3")

    result = handler.handle(request)

    assert result.status == "ok"
    assert result.ready is True
    assert [check.name for check in result.results] == [
        "release_id_present",
        "release_id_format",
    ]
    assert result.blocking_checks == []
    assert result.error is None


@pytest.mark.unit
def test_handler_blocks_invalid_release_format() -> None:
    handler = HandlerRrhCompute()
    request = ModelRrhComputeRequest(release_id="feature-branch")

    result = handler.handle(request)

    assert result.status == "ok"
    assert result.ready is False
    assert result.blocking_checks == ["release_id_format"]
    assert (
        result.results[1].detail == "release_id must be semver-like or release/<name>"
    )


@pytest.mark.unit
def test_handler_reports_unknown_check_as_error() -> None:
    handler = HandlerRrhCompute()
    request = ModelRrhComputeRequest(
        release_id="release/2026-05-28", checks=["missing_gate"]
    )

    result = handler.handle(request)

    assert result.status == "error"
    assert result.ready is False
    assert result.blocking_checks == ["missing_gate"]
    assert result.error == "unknown readiness checks: missing_gate"


@pytest.mark.unit
def test_contract_marks_implemented() -> None:
    contract_path = Path(__file__).resolve().parent.parent / "contract.yaml"
    with contract_path.open(encoding="utf-8") as file:
        contract = yaml.safe_load(file)

    assert contract["node_not_implemented"] is False


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
