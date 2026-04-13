# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain tests for node_persona_lifecycle_orchestrator.

Verifies request model validation for on_tick and on_demand operations,
and that the node is importable from omnimarket.nodes.

Related: OMN-8301 (Wave 5 migration), OMN-7305
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_persona_lifecycle_orchestrator import (
    ModelPersonaLifecycleRequest,
    ModelPersonaLifecycleResponse,
)


@pytest.mark.unit
class TestPersonaLifecycleOrchestratorGoldenChain:
    """Golden chain: persona lifecycle orchestrator model contracts."""

    def test_on_tick_request_valid(self) -> None:
        """on_tick operation requires no user_id."""
        req = ModelPersonaLifecycleRequest(operation="on_tick")
        assert req.operation == "on_tick"
        assert req.user_id is None

    def test_on_demand_request_with_user_id(self) -> None:
        """on_demand operation accepts user_id."""
        req = ModelPersonaLifecycleRequest(operation="on_demand", user_id="user_abc")
        assert req.operation == "on_demand"
        assert req.user_id == "user_abc"

    def test_on_demand_request_without_user_id(self) -> None:
        """on_demand operation accepts None user_id (validation is handler-side)."""
        req = ModelPersonaLifecycleRequest(operation="on_demand")
        assert req.operation == "on_demand"
        assert req.user_id is None

    def test_request_frozen(self) -> None:
        """ModelPersonaLifecycleRequest is immutable."""
        from pydantic import ValidationError

        req = ModelPersonaLifecycleRequest(operation="on_tick")
        with pytest.raises(ValidationError):
            req.operation = "on_demand"  # type: ignore[misc]

    def test_invalid_operation_rejected(self) -> None:
        """Invalid operation value raises ValidationError."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            ModelPersonaLifecycleRequest(operation="invalid_op")  # type: ignore[arg-type]

    def test_response_importable(self) -> None:
        """ModelPersonaLifecycleResponse is importable from omnimarket.nodes."""
        assert ModelPersonaLifecycleResponse is not None

    def test_node_importable(self) -> None:
        """node_persona_lifecycle_orchestrator is importable from omnimarket.nodes."""
        import omnimarket.nodes.node_persona_lifecycle_orchestrator as node

        assert node is not None
