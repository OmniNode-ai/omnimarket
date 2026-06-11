# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for the FSM terminal-state invariant (OMN-12959).

Covers the typed-watchdog vocabulary in ``omnimarket.events.watchdog`` and the
stranded-workflow check in ``NodeRuntimeSweep`` — the platform invariant that
every workflow FSM reaches a terminal state or trips a typed watchdog.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from omnimarket.events.watchdog import (
    WATCHDOG_TOPIC_BY_TYPE,
    WATCHDOG_TOPICS,
    EnumWatchdogEventType,
    ModelWatchdogEvent,
    watchdog_topic_for,
)
from omnimarket.nodes.node_runtime_sweep.handlers.handler_runtime_sweep import (
    DEFAULT_ARCHETYPE_SLA_MS,
    EnumFindingType,
    ModelWorkflowObservation,
    NodeRuntimeSweep,
    RuntimeSweepRequest,
)

_CID = UUID("a604cd40-84aa-41fa-97c8-a8d3ec320cd6")


# ---------------------------------------------------------------------------
# Watchdog vocabulary
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_every_watchdog_type_has_a_canonical_topic() -> None:
    """Each typed watchdog class maps to exactly one canonical topic."""
    for event_type in EnumWatchdogEventType:
        topic = watchdog_topic_for(event_type)
        assert topic in WATCHDOG_TOPICS
        assert WATCHDOG_TOPIC_BY_TYPE[event_type] == topic
    assert len(WATCHDOG_TOPICS) == len(list(EnumWatchdogEventType))


@pytest.mark.unit
def test_watchdog_types_are_not_generic_failures() -> None:
    """The three classes are the canonical typed vocabulary, not generic."""
    assert {e.value for e in EnumWatchdogEventType} == {
        "workflow-timeout",
        "workflow-unroutable",
        "workflow-stalled",
    }


@pytest.mark.unit
def test_watchdog_event_topic_property_resolves() -> None:
    event = ModelWatchdogEvent(
        event_type=EnumWatchdogEventType.WORKFLOW_UNROUTABLE,
        correlation_id=_CID,
        archetype="delegation",
        workflow_state="ROUTED",
        reason="no higher tier can route document task",
        elapsed_ms=1234,
    )
    assert event.topic == "onex.evt.omnimarket.workflow-unroutable.v1"


@pytest.mark.unit
def test_watchdog_event_rejects_negative_elapsed() -> None:
    with pytest.raises(ValidationError):
        ModelWatchdogEvent(
            event_type=EnumWatchdogEventType.WORKFLOW_TIMEOUT,
            correlation_id=_CID,
            archetype="delegation",
            workflow_state="ROUTED",
            reason="x",
            elapsed_ms=-1,
        )


# ---------------------------------------------------------------------------
# Stranded-workflow sweep
# ---------------------------------------------------------------------------


def _obs(**overrides: object) -> ModelWorkflowObservation:
    base: dict[str, object] = {
        "correlation_id": uuid4(),
        "archetype": "delegation",
        "workflow_state": "ROUTED",
        "elapsed_ms": DEFAULT_ARCHETYPE_SLA_MS + 1,
        "reached_terminal": False,
    }
    base.update(overrides)
    return ModelWorkflowObservation(**base)  # type: ignore[arg-type]


@pytest.mark.unit
def test_terminal_workflow_is_not_stranded() -> None:
    result = NodeRuntimeSweep().handle(
        RuntimeSweepRequest(
            workflow_observations=[_obs(reached_terminal=True)],
        )
    )
    assert result.workflows_checked == 1
    assert result.by_type.get(EnumFindingType.STRANDED_WORKFLOW.value, 0) == 0


@pytest.mark.unit
def test_within_sla_workflow_is_not_stranded() -> None:
    result = NodeRuntimeSweep().handle(
        RuntimeSweepRequest(
            workflow_observations=[
                _obs(reached_terminal=False, elapsed_ms=DEFAULT_ARCHETYPE_SLA_MS - 1),
            ],
        )
    )
    assert result.by_type.get(EnumFindingType.STRANDED_WORKFLOW.value, 0) == 0


@pytest.mark.unit
def test_started_no_terminal_past_sla_is_stranded_timeout() -> None:
    """The generalized OMN-12939 strand: started, no terminal, past SLA."""
    result = NodeRuntimeSweep().handle(
        RuntimeSweepRequest(
            workflow_observations=[_obs()],
        )
    )
    stranded = [
        f
        for f in result.findings
        if f.finding_type == EnumFindingType.STRANDED_WORKFLOW
    ]
    assert len(stranded) == 1
    assert stranded[0].severity == "CRITICAL"
    assert EnumWatchdogEventType.WORKFLOW_TIMEOUT.value in stranded[0].message


@pytest.mark.unit
def test_unroutable_takes_precedence_over_stalled_and_timeout() -> None:
    result = NodeRuntimeSweep().handle(
        RuntimeSweepRequest(
            workflow_observations=[
                _obs(has_routable_next=False, making_progress=False),
            ],
        )
    )
    msg = result.findings[0].message
    assert EnumWatchdogEventType.WORKFLOW_UNROUTABLE.value in msg


@pytest.mark.unit
def test_stalled_classified_when_routable_but_no_progress() -> None:
    result = NodeRuntimeSweep().handle(
        RuntimeSweepRequest(
            workflow_observations=[
                _obs(has_routable_next=True, making_progress=False),
            ],
        )
    )
    msg = result.findings[0].message
    assert EnumWatchdogEventType.WORKFLOW_STALLED.value in msg


@pytest.mark.unit
def test_per_archetype_sla_override() -> None:
    """An archetype with a long SLA is not stranded at the default boundary."""
    obs = _obs(archetype="build_loop", elapsed_ms=DEFAULT_ARCHETYPE_SLA_MS + 1)
    result = NodeRuntimeSweep().handle(
        RuntimeSweepRequest(
            workflow_observations=[obs],
            archetype_sla_ms={"build_loop": DEFAULT_ARCHETYPE_SLA_MS * 10},
        )
    )
    assert result.by_type.get(EnumFindingType.STRANDED_WORKFLOW.value, 0) == 0


@pytest.mark.unit
def test_watchdog_terminal_clears_strand() -> None:
    """A tripped watchdog counts as terminal evidence (reached_terminal=True)."""
    result = NodeRuntimeSweep().handle(
        RuntimeSweepRequest(
            workflow_observations=[
                _obs(reached_terminal=True, has_routable_next=False)
            ],
        )
    )
    assert result.by_type.get(EnumFindingType.STRANDED_WORKFLOW.value, 0) == 0
