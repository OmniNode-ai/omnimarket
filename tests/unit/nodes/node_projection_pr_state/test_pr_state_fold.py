# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Definition-B fold: replay, out-of-order arrival and terminal observations."""

from itertools import permutations

import pytest
from pydantic import ValidationError

from omnimarket.events.pr_state import EnumPrState
from omnimarket.nodes.node_projection_pr_state.handlers.pr_state_fold import (
    HandlerProjectionPrState,
    apply_result,
)
from omnimarket.nodes.node_projection_pr_state.models.model_pr_state_fold_request import (
    ModelPrStateFoldRequest,
)
from omnimarket.nodes.node_projection_pr_state.models.model_pr_state_fold_result import (
    ModelPrStateFoldResult,
)
from tests.unit.nodes.node_pr_state_emit_effect.helpers import event

pytestmark = pytest.mark.unit


def test_recorded_observations_converge_in_every_order() -> None:
    log = [
        event(),
        event(head_sha="b" * 40, observed_at="2026-09-28T10:05:00Z"),
        event(
            state=EnumPrState.MERGED,
            merged_at="2026-09-28T10:06:00Z",
            observed_at="2026-09-28T10:06:00Z",
        ),
    ]
    expected = None
    for ordering in permutations(log):
        state: dict[str, ModelPrStateFoldResult] = {}
        for e in (*ordering, *ordering):
            apply_result(
                state,
                HandlerProjectionPrState().handle(ModelPrStateFoldRequest(event=e)),
            )
        if expected is None:
            expected = state
        assert state == expected
        assert state["OmniNode-ai/omnimarket#3050"].event.state is EnumPrState.MERGED
        assert len(state) == 1


def test_equal_time_tie_uses_digest_and_redelivery_is_noop() -> None:
    a, b = sorted([event(), event(armed=True)], key=lambda e: e.digest)
    state: dict[str, ModelPrStateFoldResult] = {}
    for e in (b, a, b):
        result = HandlerProjectionPrState().handle(ModelPrStateFoldRequest(event=e))
        apply_result(state, result)
    assert next(iter(state.values())).event == b


def test_same_digest_at_newer_tick_advances_observation() -> None:
    state: dict[str, ModelPrStateFoldResult] = {}
    for e in (event(), event(observed_at="2026-09-28T10:01:00Z"), event()):
        apply_result(
            state, HandlerProjectionPrState().handle(ModelPrStateFoldRequest(event=e))
        )
    assert next(iter(state.values())).event.observed_at == "2026-09-28T10:01:00Z"


def test_closed_is_kept_and_newer_reopen_is_allowed() -> None:
    state: dict[str, ModelPrStateFoldResult] = {}
    for e in (
        event(state=EnumPrState.CLOSED),
        event(observed_at="2026-09-28T10:01:00Z"),
    ):
        apply_result(
            state, HandlerProjectionPrState().handle(ModelPrStateFoldRequest(event=e))
        )
        assert next(iter(state.values())).event.state == e.state


def test_enriched_wire_payload_validates_but_corruption_does_not() -> None:
    wire = event().model_dump(mode="json") | {"entity_id": "abc", "emitted_at": "later"}
    assert ModelPrStateFoldRequest.model_validate(wire).event == event()
    wire["armed"] = True
    with pytest.raises(ValidationError):
        ModelPrStateFoldRequest.model_validate(wire)
