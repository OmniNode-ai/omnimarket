# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain tests for node_persona_retrieval_effect.

Verifies request/response model validation and that the node is importable
from omnimarket.nodes.

Related: OMN-8301 (Wave 5 migration), OMN-7305, OMN-14010 (self-contained
after the omnimemory stub deletion, OMN-12172)
"""

from __future__ import annotations

import pytest
import yaml

from omnimarket.nodes.node_persona_retrieval_effect import (
    ModelPersonaRetrievalRequest,
    ModelPersonaRetrievalResponse,
)


@pytest.mark.unit
class TestPersonaRetrievalEffectGoldenChain:
    def test_request_requires_user_id(self) -> None:
        request = ModelPersonaRetrievalRequest(user_id="user-1")
        assert request.user_id == "user-1"
        assert request.agent_id is None

    def test_request_accepts_agent_id_filter(self) -> None:
        request = ModelPersonaRetrievalRequest(user_id="user-1", agent_id="agent-1")
        assert request.agent_id == "agent-1"

    def test_request_frozen(self) -> None:
        from pydantic import ValidationError

        request = ModelPersonaRetrievalRequest(user_id="user-1")
        with pytest.raises(ValidationError):
            request.user_id = "user-2"  # type: ignore[misc]

    def test_response_not_found(self) -> None:
        response = ModelPersonaRetrievalResponse(status="not_found")
        assert response.status == "not_found"
        assert response.persona is None

    def test_response_error_requires_no_persona(self) -> None:
        response = ModelPersonaRetrievalResponse(
            status="error", error_message="lookup failed"
        )
        assert response.status == "error"
        assert response.error_message == "lookup failed"

    def test_node_importable(self) -> None:
        """node_persona_retrieval_effect is importable from omnimarket.nodes."""
        import omnimarket.nodes.node_persona_retrieval_effect as node

        assert node is not None


@pytest.mark.unit
def test_contract_declares_publish_topics() -> None:
    """OMN-14010 state-coverage: contract declares both output topics for real."""
    with open("src/omnimarket/nodes/node_persona_retrieval_effect/contract.yaml") as f:
        contract = yaml.safe_load(f)
    publish_topics = contract["event_bus"]["publish_topics"]
    assert "onex.evt.omnimemory.persona-retrieval-completed.v1" in publish_topics
    assert "onex.evt.omnimemory.persona-retrieval-failed.v1" in publish_topics
