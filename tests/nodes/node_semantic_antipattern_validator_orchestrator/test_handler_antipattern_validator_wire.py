# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for the orchestrator's first hop: validate command -> match command.

These sit under tests/ because CI does not collect tests inside a node directory.
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
) -> ModelAntipatternValidatorRequest:
    return ModelAntipatternValidatorRequest(
        file_path=file_path,
        file_content=file_content,
        correlation_id="corr-0",
    )


@pytest.mark.unit
def test_command_is_accepted_by_the_match_effect_request_model() -> None:
    """The effect's request model refuses fields it does not declare; the command
    the orchestrator returns must validate against it as sent on the wire."""
    from omnimarket.nodes.node_antipattern_match_effect.models.model_antipattern_match_request import (
        ModelAntipatternMatchRequest,
    )

    result = HandlerAntipatternValidatorOrchestrator().handle(_make_request())
    request = ModelAntipatternMatchRequest.model_validate(result.model_dump())

    assert request.correlation_id == result.correlation_id
    assert request.code_text == "class Foo:\n    pass\n"
    assert request.validation_context == result.validation_context


@pytest.mark.unit
def test_context_carries_line_count_of_the_file() -> None:
    handler = HandlerAntipatternValidatorOrchestrator()
    content = "\n".join(f"x{i} = {i}" for i in range(12)) + "\n"
    result = handler.handle(_make_request(file_content=content))

    assert result.validation_context.line_count == 12


@pytest.mark.unit
def test_empty_file_queries_by_path_and_has_zero_lines() -> None:
    """An empty file has nothing to embed; the effect refuses a command with
    neither code_text nor description, so the path stands in."""
    handler = HandlerAntipatternValidatorOrchestrator()
    result = handler.handle(_make_request(file_path="src/empty.py", file_content="\n"))

    assert result.code_text is None
    assert result.description == "src/empty.py"
    assert result.validation_context.line_count == 1
