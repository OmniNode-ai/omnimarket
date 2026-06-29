# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Multi-parameter integration proof for node_design_to_plan (OMN-13680, WS5 Wave 6).

The design_to_plan handler is a pure deterministic FSM (no external I/O, no bus
round-trip surface — ``handle()`` returns ``(state, events, completed_event)``
synchronously), so this is a Variant A direct in-process drive. Plan-markdown is
mocked as a ``plan_path`` string on the command; phase outcomes are injected via
``phase_results``.

Each case parametrizes a distinct phase-result map / command-flag combination and
asserts the typed terminal state + phase-transition event payloads. The two
failing-phase cases are the negative controls: an injected phase failure must
halt progression short of DONE and surface ``error_message``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.nodes.node_design_to_plan.handlers.handler_design_to_plan import (
    HandlerDesignToPlan,
)
from omnimarket.nodes.node_design_to_plan.models.model_design_to_plan_command import (
    ModelDesignToPlanCommand,
)
from omnimarket.nodes.node_design_to_plan.models.model_design_to_plan_state import (
    EnumDesignToPlanPhase,
)


def _command(**overrides: Any) -> ModelDesignToPlanCommand:
    base: dict[str, Any] = {
        "correlation_id": uuid4(),
        "topic": "improve delegation routing",
        "requested_at": datetime(2026, 6, 1, tzinfo=UTC),
    }
    base.update(overrides)
    return ModelDesignToPlanCommand(**base)


_P = EnumDesignToPlanPhase


@pytest.mark.integration
@pytest.mark.parametrize(
    ("command_kwargs", "phase_results", "expect"),
    [
        pytest.param(
            {},
            None,
            {
                "final_phase": _P.DONE,
                "event_count": 6,
                "all_success": True,
                "error": None,
            },
            id="all-phases-succeed",
        ),
        pytest.param(
            {"plan_path": "/tmp/plan.md", "no_launch": True},
            None,
            {
                "final_phase": _P.DONE,
                "event_count": 6,
                "all_success": True,
                "error": None,
            },
            id="plan-path-and-no-launch",
        ),
        pytest.param(
            {"plan_only": True},
            None,
            {
                "final_phase": _P.DONE,
                "event_count": 6,
                "all_success": True,
                "error": None,
            },
            id="plan-only",
        ),
        pytest.param(
            {},
            {_P.REVIEW: False},
            {
                "final_phase": _P.STRUCTURE,
                "event_count": 3,
                "all_success": False,
                "error": "Phase review failed",
            },
            id="negative-control-review-fails",
        ),
        pytest.param(
            {},
            {_P.BRAINSTORM: False},
            {
                "final_phase": _P.IDLE,
                "event_count": 1,
                "all_success": False,
                "error": "Phase brainstorm failed",
            },
            id="negative-control-brainstorm-fails",
        ),
    ],
)
def test_design_to_plan_multiparam(
    command_kwargs: dict[str, Any],
    phase_results: dict[EnumDesignToPlanPhase, bool] | None,
    expect: dict[str, Any],
) -> None:
    handler = HandlerDesignToPlan()
    command = _command(**command_kwargs)

    state, events, completed = handler.handle(command, phase_results=phase_results)

    assert state.current_phase == expect["final_phase"]
    assert completed.final_phase == expect["final_phase"]
    assert completed.correlation_id == command.correlation_id
    assert len(events) == expect["event_count"]
    assert all(e.success for e in events) is expect["all_success"]
    assert completed.error_message == expect["error"]


@pytest.mark.integration
def test_design_to_plan_happy_path_event_sequence() -> None:
    """The all-success run walks the canonical phase sequence in order."""
    _state, events, completed = HandlerDesignToPlan().handle(_command())

    transitions = [(e.from_phase, e.to_phase) for e in events]
    assert transitions == [
        (_P.IDLE, _P.BRAINSTORM),
        (_P.BRAINSTORM, _P.STRUCTURE),
        (_P.STRUCTURE, _P.REVIEW),
        (_P.REVIEW, _P.FINALIZE),
        (_P.FINALIZE, _P.LAUNCH),
        (_P.LAUNCH, _P.DONE),
    ]
    assert completed.final_phase == _P.DONE
