# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19833: the pure fold of the PR landing projection.

The fold is a function of one event: no clock, no database. These tests pin
what each of the four events asserts about the rows, and that the frozen T2
payload classes are the input seam, taken strictly.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.events.topics import (
    PR_LANDING_MERGED_TOPIC_V1,
    PR_LANDING_TRANSITIONED_TOPIC_V1,
)
from omnimarket.nodes.node_pr_landing_orchestrator.event_topics import (
    PR_LANDING_EVENT_TOPICS,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models.enum_pr_landing_agent_reason import (
    EnumPrLandingAgentReason,
)
from omnimarket.nodes.node_projection_pr_landing.handlers import (
    HandlerProjectionPrLanding,
    PrLandingProjectionWriter,
)
from omnimarket.nodes.node_projection_pr_landing.handlers.handler_pr_landing_writer import (
    TOPIC_EVENT_KIND,
)
from omnimarket.nodes.node_projection_pr_landing.models import (
    EnumPrLandingProjectionEventKind,
    ModelPrLandingProjectionRequest,
    ModelPrLandingProjectionResult,
)
from tests.pr_landing_projection_events import (
    HEAD_A,
    HEAD_B,
    PR,
    REPO,
    S,
    a_reopened_pr_life,
    agent_needed,
    at,
    closed,
    merged,
    transitioned,
)

pytestmark = pytest.mark.unit


def _fold(event: dict[str, Any]) -> ModelPrLandingProjectionResult:
    request = PrLandingProjectionWriter.build_request(str(event["_topic"]), event)
    return HandlerProjectionPrLanding().handle(request)


def test_a_transition_folds_to_one_state_row_and_one_transition_row() -> None:
    result = _fold(
        transitioned(9, S.CHECKS_PENDING, S.READY, "verdict_green", arm=True)
    )
    state, log = result.state_row, result.transition_row
    assert log is not None
    assert (state.repository, state.pr_number, state.seq) == (REPO, PR, 9)
    assert state.state is S.READY
    assert state.trigger == "verdict_green"
    assert state.head_sha == HEAD_A
    assert state.episode is None, "a transition asserts no episode"
    assert state.opens_episode is False
    assert state.event_at == at(9)
    assert (log.seq, log.from_state, log.to_state) == (9, S.CHECKS_PENDING, S.READY)
    assert [intent.kind.value for intent in log.intents] == ["github.arm"]


@pytest.mark.parametrize(
    ("from_state", "to_state", "opens"),
    [
        (S.CLOSED, S.OBSERVED, True),  # reopened, or any newer snapshot (F10, G5)
        (S.CLOSED, S.MERGED, True),  # merged after a reopen, a new episode (G5)
        (S.CLOSED, S.CLOSED, False),  # a newer closed stays in the episode (G5)
        (None, S.OBSERVED, False),  # first sight
        (S.ARMED, S.MERGED, False),
        (S.NEEDS_AGENT, S.OBSERVED, False),
    ],
)
def test_only_a_transition_out_of_closed_opens_an_episode(
    from_state: Any, to_state: Any, opens: bool
) -> None:
    result = _fold(transitioned(4, from_state, to_state, "reopened"))
    assert result.state_row.opens_episode is opens
    assert result.transition_row is not None
    assert result.transition_row.opens_episode is opens


@pytest.mark.parametrize(
    ("event", "state"),
    [(merged(11, 1), S.MERGED), (closed(3, 0), S.CLOSED)],
)
def test_a_terminal_asserts_its_episode_and_appends_nothing(
    event: dict[str, Any], state: Any
) -> None:
    result = _fold(event)
    row = result.state_row
    assert result.transition_row is None
    assert row.state is state
    assert row.episode == event["episode"]
    assert row.terminal_at == row.event_at
    assert row.trigger is None


def test_agent_needed_moves_the_row_to_needs_agent_with_its_reason() -> None:
    result = _fold(agent_needed(6, EnumPrLandingAgentReason.STALLED, detail="READY"))
    row = result.state_row
    assert result.transition_row is None
    assert row.state is S.NEEDS_AGENT
    assert row.agent_reason is EnumPrLandingAgentReason.STALLED
    assert row.agent_detail == "READY"
    assert row.episode is None


def test_folding_the_same_event_twice_is_identical() -> None:
    """Replay determinism at the fold: a function of its input alone."""
    for event in a_reopened_pr_life():
        first = _fold(dict(event)).model_dump(mode="json")
        second = _fold(dict(event)).model_dump(mode="json")
        assert first == second


def test_the_writer_maps_exactly_the_four_orchestrator_topics() -> None:
    """The consumer side of the frozen seam: nothing more, nothing less."""
    assert set(TOPIC_EVENT_KIND) == set(PR_LANDING_EVENT_TOPICS.values())
    assert set(TOPIC_EVENT_KIND.values()) == set(EnumPrLandingProjectionEventKind)


def test_the_runtime_injections_are_stripped_and_nothing_else() -> None:
    event = transitioned(1, None, S.OBSERVED, "pushed")
    assert any(key.startswith("_") for key in event)
    request = PrLandingProjectionWriter.build_request(
        PR_LANDING_TRANSITIONED_TOPIC_V1, event
    )
    assert request.event_kind is EnumPrLandingProjectionEventKind.TRANSITIONED


def test_an_unknown_payload_field_is_refused_not_dropped() -> None:
    """The T2 classes forbid extras, so seam drift fails loudly here."""
    event = merged(11, 1)
    event["episode_hint"] = 2
    with pytest.raises(ValidationError):
        PrLandingProjectionWriter.build_request(PR_LANDING_MERGED_TOPIC_V1, event)


def test_a_topic_the_node_does_not_consume_is_refused() -> None:
    with pytest.raises(ValueError, match="does not consume"):
        PrLandingProjectionWriter.build_request(
            "onex.evt.omnimarket.pr-landing-unknown.v1",  # onex-topic-allow: negative case, deliberately unregistered
            merged(11, 1),
        )


def test_a_payload_on_the_wrong_topic_is_refused() -> None:
    """A merged payload on the transitioned topic does not validate."""
    with pytest.raises(ValidationError):
        PrLandingProjectionWriter.build_request(
            PR_LANDING_TRANSITIONED_TOPIC_V1, merged(11, 1)
        )


def test_the_request_takes_exactly_one_event() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        ModelPrLandingProjectionRequest()


def test_the_intents_are_stored_as_canonical_json() -> None:
    result = _fold(
        transitioned(
            9, S.CHECKS_PENDING, S.READY, "verdict_green", head_sha=HEAD_B, arm=True
        )
    )
    assert result.transition_row is not None
    encoded = result.transition_row.intents_json()
    decoded = json.loads(encoded)
    assert decoded[0]["kind"] == "github.arm"
    assert decoded[0]["head_sha"] == HEAD_B
    assert encoded == json.dumps(decoded, sort_keys=True, separators=(",", ":"))
