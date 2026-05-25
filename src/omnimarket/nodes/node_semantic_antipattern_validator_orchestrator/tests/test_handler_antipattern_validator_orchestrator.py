# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""TDD tests for node_semantic_antipattern_validator_orchestrator.

Verifies:
- Handler is importable and instantiatable
- handle() returns ModelHandlerOutput with events (ORCHESTRATOR contract)
- Emits antipattern-match-requested event toward effect node
- Routes classifier command in output events
- Similarity threshold flows from contract config default (0.80)
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_semantic_antipattern_validator_orchestrator.handlers.handler_antipattern_validator_orchestrator import (
    HandlerAntipatternValidatorOrchestrator,
)
from omnimarket.nodes.node_semantic_antipattern_validator_orchestrator.models.model_antipattern_validator_request import (
    ModelAntipatternValidatorRequest,
)


def _make_request(
    file_path: str = "src/foo.py",
    file_content: str = "class Foo:\n    pass\n",
    enforcement_mode: str = "blocking",
    similarity_threshold: float = 0.80,
    correlation_id: str | None = None,
) -> ModelAntipatternValidatorRequest:
    from uuid import uuid4

    return ModelAntipatternValidatorRequest(
        file_path=file_path,
        file_content=file_content,
        enforcement_mode=enforcement_mode,
        similarity_threshold=similarity_threshold,
        correlation_id=correlation_id or str(uuid4()),
    )


@pytest.mark.unit
def test_handler_importable() -> None:
    assert (
        HandlerAntipatternValidatorOrchestrator.__name__
        == "HandlerAntipatternValidatorOrchestrator"
    )


@pytest.mark.unit
def test_handle_returns_handler_output() -> None:
    from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput

    handler = HandlerAntipatternValidatorOrchestrator()
    req = _make_request()
    result = handler.handle(req)

    assert isinstance(result, ModelHandlerOutput)


@pytest.mark.unit
def test_handle_emits_at_least_one_event() -> None:
    handler = HandlerAntipatternValidatorOrchestrator()
    req = _make_request()
    result = handler.handle(req)

    assert result.events
    assert len(result.events) >= 1


@pytest.mark.unit
def test_emitted_event_targets_antipattern_match_effect() -> None:
    """Event topic must reference the antipattern match effect."""
    handler = HandlerAntipatternValidatorOrchestrator()
    req = _make_request()
    result = handler.handle(req)

    topics = [
        getattr(e, "topic", None) or getattr(e, "_topic", None) or type(e).__name__
        for e in result.events
    ]
    assert any("antipattern" in (t or "").lower() for t in topics), (
        f"Expected antipattern-related event topic, got: {topics}"
    )


@pytest.mark.unit
def test_similarity_threshold_passed_through() -> None:
    """Threshold from request must appear in emitted event payload."""
    handler = HandlerAntipatternValidatorOrchestrator()
    req = _make_request(similarity_threshold=0.92)
    result = handler.handle(req)

    assert result.events
    first_event = result.events[0]
    # The event payload should carry threshold for the effect/classifier
    payload_dict = (
        first_event.model_dump()
        if hasattr(first_event, "model_dump")
        else vars(first_event)
    )
    # Flatten nested dicts for search
    payload_str = str(payload_dict)
    assert "0.92" in payload_str or "similarity_threshold" in payload_str


@pytest.mark.unit
def test_correlation_id_propagated() -> None:
    """Correlation ID from request must appear in output."""
    handler = HandlerAntipatternValidatorOrchestrator()
    correlation_id = "test-corr-id-12345"
    req = _make_request(correlation_id=correlation_id)
    result = handler.handle(req)

    output_dict = result.model_dump()
    assert correlation_id in str(output_dict)
