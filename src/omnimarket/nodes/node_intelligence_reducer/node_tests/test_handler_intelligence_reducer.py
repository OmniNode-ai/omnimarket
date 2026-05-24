# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2025 OmniNode Team
"""Unit tests for HandlerIntelligenceReducer class.

Verifies that the handler class:
    - Is instantiable with no arguments
    - Routes ModelReducerInputPatternLifecycle to handle_pattern_lifecycle_process
    - Returns correct ModelReducerOutput on valid transition
    - Returns error ModelReducerOutput on invalid transition
    - Raises TypeError for non-PATTERN_LIFECYCLE inputs

Ticket: OMN-11759
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_intelligence_reducer.handlers.handler_intelligence_reducer import (
    HandlerIntelligenceReducer,
)
from omnimarket.nodes.node_intelligence_reducer.models.model_intelligence_state import (
    ModelIntelligenceState,
)
from omnimarket.nodes.node_intelligence_reducer.models.model_reducer_input import (
    ModelReducerInputIngestion,
    ModelReducerInputPatternLifecycle,
)


@pytest.mark.unit
class TestHandlerIntelligenceReducerInstantiation:
    """HandlerIntelligenceReducer can be instantiated with no arguments."""

    def test_handler_instantiates(self) -> None:
        handler = HandlerIntelligenceReducer()
        assert handler is not None

    def test_handler_has_handle_method(self) -> None:
        handler = HandlerIntelligenceReducer()
        assert callable(handler.handle)


@pytest.mark.unit
class TestHandlerIntelligenceReducerRouting:
    """HandlerIntelligenceReducer routes correctly by FSM type."""

    def test_pattern_lifecycle_input_routes_and_returns_output(
        self,
        make_reducer_input: object,
    ) -> None:
        from collections.abc import Callable

        assert callable(make_reducer_input)
        factory: Callable[..., ModelReducerInputPatternLifecycle] = make_reducer_input
        handler = HandlerIntelligenceReducer()
        input_data = factory(
            from_status="candidate",
            to_status="validated",
            trigger="promote_direct",
        )

        result = handler.handle(input_data)

        assert isinstance(result.result, ModelIntelligenceState)
        assert result.result.success is True
        assert result.result.fsm_type == "PATTERN_LIFECYCLE"

    def test_valid_transition_emits_one_intent(
        self,
        make_reducer_input: object,
    ) -> None:
        from collections.abc import Callable

        factory: Callable[..., ModelReducerInputPatternLifecycle] = make_reducer_input  # type: ignore[assignment]
        handler = HandlerIntelligenceReducer()
        input_data = factory(
            from_status="candidate",
            to_status="deprecated",
            trigger="deprecate",
        )

        result = handler.handle(input_data)

        assert result.result.success is True
        assert len(result.intents) == 1

    def test_invalid_transition_returns_error_output(
        self,
        make_reducer_input: object,
    ) -> None:
        from collections.abc import Callable

        factory: Callable[..., ModelReducerInputPatternLifecycle] = make_reducer_input  # type: ignore[assignment]
        handler = HandlerIntelligenceReducer()
        # validated -> promote_direct is not a valid transition
        input_data = factory(
            from_status="validated",
            to_status="validated",
            trigger="promote_direct",
        )

        result = handler.handle(input_data)

        assert isinstance(result.result, ModelIntelligenceState)
        assert result.result.success is False
        assert len(result.intents) == 0

    def test_non_pattern_lifecycle_input_raises_type_error(self) -> None:
        """Non-PATTERN_LIFECYCLE inputs must raise TypeError — they belong in base class."""
        handler = HandlerIntelligenceReducer()
        # Create a minimal ingestion-type input to trigger the wrong-type branch.
        # We need to pass something that is not ModelReducerInputPatternLifecycle.
        # Use a mock object to avoid constructing a full ModelReducerInputIngestion.
        import unittest.mock as mock

        bad_input = mock.MagicMock(spec=ModelReducerInputIngestion)
        bad_input.fsm_type = "INGESTION"

        with pytest.raises(TypeError, match="unexpected FSM type"):
            handler.handle(bad_input)
