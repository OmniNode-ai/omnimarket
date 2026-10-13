# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The classifier echoes the correlation id and validation context it was given.

A bus requester pairs the classification with its request by correlation id, and
the validator orchestrator reads the file under validation back off the context.
These sit under tests/ because CI does not collect tests inside a node directory.
"""

from __future__ import annotations

import pytest

from omnimarket.models.antipattern import ModelAntipatternValidationContext
from omnimarket.nodes.node_semantic_antipattern_classifier_compute.handlers.handler_antipattern_classifier import (
    HandlerAntipatternClassifier,
)
from omnimarket.nodes.node_semantic_antipattern_classifier_compute.models.model_antipattern_classify_request import (
    ModelAntipatternClassifyRequest,
)


@pytest.mark.unit
def test_correlation_id_and_context_are_echoed_unchanged() -> None:
    context = ModelAntipatternValidationContext(
        file_path="src/foo.py",
        line_count=14,
        similarity_threshold=0.8,
        enforcement_mode="blocking",
        match_error="Qdrant search failed.",
    )

    result = HandlerAntipatternClassifier().handle(
        ModelAntipatternClassifyRequest(
            correlation_id="corr-7", validation_context=context
        )
    )

    assert result.correlation_id == "corr-7"
    assert result.validation_context == context
    assert result.violations == ()


@pytest.mark.unit
def test_request_without_chain_fields_still_classifies() -> None:
    """A direct caller that sends only matches and config is unaffected."""
    result = HandlerAntipatternClassifier().handle(ModelAntipatternClassifyRequest())

    assert result.correlation_id == ""
    assert result.validation_context is None
