# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""TDD tests for node_semantic_antipattern_validator_orchestrator.

Verifies:
- Handler is importable and instantiatable
- handle() returns the typed ModelAntipatternMatchCommand directly (OMN-14242
  thin canonical shape -- no ModelHandlerOutput envelope, no coercion in the
  handler; the runtime wraps this for bus publication toward
  node_antipattern_match_effect)
- Similarity threshold and correlation_id flow through to the emitted command
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_semantic_antipattern_validator_orchestrator.handlers.handler_antipattern_validator_orchestrator import (
    HandlerAntipatternValidatorOrchestrator,
)
from omnimarket.nodes.node_semantic_antipattern_validator_orchestrator.models.model_antipattern_match_command import (
    ModelAntipatternMatchCommand,
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
def test_handle_returns_match_command_directly() -> None:
    """Thin canonical shape (OMN-14242): handle() returns the typed
    ModelAntipatternMatchCommand directly -- no ModelHandlerOutput envelope."""
    handler = HandlerAntipatternValidatorOrchestrator()
    req = _make_request()
    result = handler.handle(req)

    assert isinstance(result, ModelAntipatternMatchCommand)


@pytest.mark.unit
def test_emitted_command_targets_antipattern_match_effect() -> None:
    """The returned command carries the file identity the match effect needs."""
    handler = HandlerAntipatternValidatorOrchestrator()
    req = _make_request(file_path="src/foo.py")
    result = handler.handle(req)

    assert result.file_path == "src/foo.py"


@pytest.mark.unit
def test_similarity_threshold_passed_through() -> None:
    """Threshold from request must appear on the emitted command."""
    handler = HandlerAntipatternValidatorOrchestrator()
    req = _make_request(similarity_threshold=0.92)
    result = handler.handle(req)

    assert result.similarity_threshold == pytest.approx(0.92)


@pytest.mark.unit
def test_correlation_id_propagated() -> None:
    """Correlation ID from request must appear verbatim on the emitted command."""
    handler = HandlerAntipatternValidatorOrchestrator()
    correlation_id = "test-corr-id-12345"
    req = _make_request(correlation_id=correlation_id)
    result = handler.handle(req)

    assert result.correlation_id == correlation_id


@pytest.mark.unit
def test_invalid_correlation_id_forwarded_verbatim() -> None:
    """A non-UUID correlation_id is forwarded as-is (OMN-14242: the UUID-parsing
    fallback previously fed only the discarded ModelHandlerOutput envelope
    correlation_id -- it is dead code once the envelope is gone)."""
    handler = HandlerAntipatternValidatorOrchestrator()
    req = _make_request(correlation_id="not-a-uuid")
    result = handler.handle(req)

    assert result.correlation_id == "not-a-uuid"


@pytest.mark.unit
def test_enforcement_mode_passed_through() -> None:
    handler = HandlerAntipatternValidatorOrchestrator()
    req = _make_request(enforcement_mode="advisory")
    result = handler.handle(req)

    assert result.enforcement_mode == "advisory"


@pytest.mark.unit
def test_file_content_passed_through() -> None:
    handler = HandlerAntipatternValidatorOrchestrator()
    content = "def god_function():\n    return 1\n"
    req = _make_request(file_content=content)
    result = handler.handle(req)

    assert result.file_content == content
