# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain tests for node_intelligence_reducer.

Verifies the canonical PATTERN_LIFECYCLE reducer path end-to-end:
  input (candidate → validated) → handler → ModelReducerOutput with intent

This exercises the live-path handler (HandlerIntelligenceReducer) and the FSM
transition logic (handle_pattern_lifecycle_process) so that any regression in
the pattern lifecycle state machine is caught at the golden-chain level.

Related:
    - OMN-13735: FSM handler binding for node_intelligence_reducer
    - OMN-1805: Pattern lifecycle state machine
    - OMN-11759: HandlerIntelligenceReducer class
"""

from __future__ import annotations

from uuid import UUID

import pytest

from omnimarket.nodes.node_intelligence_reducer.handlers.handler_intelligence_reducer import (
    HandlerIntelligenceReducer,
)
from omnimarket.nodes.node_intelligence_reducer.models.model_intelligence_state import (
    ModelIntelligenceState,
)
from omnimarket.nodes.node_intelligence_reducer.models.model_pattern_lifecycle_reducer_input import (
    ModelPatternLifecycleReducerInput,
)
from omnimarket.nodes.node_intelligence_reducer.models.model_reducer_input import (
    ModelReducerInputPatternLifecycle,
)

_PATTERN_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_CORRELATION_ID = UUID("12345678-1234-5678-1234-567812345678")
_REQUEST_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


def _make_input(
    *,
    from_status: str = "candidate",
    to_status: str = "validated",
    trigger: str = "promote_direct",
) -> ModelReducerInputPatternLifecycle:
    return ModelReducerInputPatternLifecycle(
        fsm_type="PATTERN_LIFECYCLE",
        entity_id=_PATTERN_ID,
        action=trigger,
        payload=ModelPatternLifecycleReducerInput(
            pattern_id=_PATTERN_ID,
            from_status=from_status,
            to_status=to_status,
            trigger=trigger,
            actor="test",
        ),
        correlation_id=_CORRELATION_ID,
        request_id=_REQUEST_ID,
    )


@pytest.mark.unit
def test_intelligence_reducer_golden_chain_candidate_to_validated() -> None:
    """candidate → promote_direct → validated emits one intent and returns success state."""
    handler = HandlerIntelligenceReducer()
    result = handler.handle(_make_input())

    assert isinstance(result.result, ModelIntelligenceState)
    assert result.result.success is True
    assert result.result.fsm_type == "PATTERN_LIFECYCLE"
    assert result.result.entity_id == _PATTERN_ID
    assert result.result.from_status == "candidate"
    assert result.result.to_status == "validated"
    assert len(result.intents) == 1
    assert result.items_processed == 1


@pytest.mark.unit
def test_intelligence_reducer_golden_chain_candidate_to_deprecated() -> None:
    """candidate → deprecate → deprecated emits one intent and returns success state."""
    handler = HandlerIntelligenceReducer()
    result = handler.handle(
        _make_input(
            from_status="candidate",
            to_status="deprecated",
            trigger="deprecate",
        )
    )

    assert isinstance(result.result, ModelIntelligenceState)
    assert result.result.success is True
    assert result.result.to_status == "deprecated"
    assert len(result.intents) == 1


@pytest.mark.unit
def test_intelligence_reducer_golden_chain_invalid_transition_no_intents() -> None:
    """An invalid transition (validated → promote_direct) returns failure with zero intents."""
    handler = HandlerIntelligenceReducer()
    result = handler.handle(
        _make_input(
            from_status="validated",
            to_status="validated",
            trigger="promote_direct",
        )
    )

    assert isinstance(result.result, ModelIntelligenceState)
    assert result.result.success is False
    assert len(result.intents) == 0
    assert result.result.error_code is not None
