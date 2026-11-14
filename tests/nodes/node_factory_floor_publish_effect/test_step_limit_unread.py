# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A step whose overlay time-limit key is missing is skipped as unread; every other step still publishes (OMN-24393).

Before, one missing key (limits.step_timeout_seconds.delegation_history, added by a step whose overlay half had not merged) refused
the whole cycle and the board went stale. Now a step that is not required and has no key is skipped with a typed ``unread``
result named in publish-status.json and drawn unread on the pages; a required step with no key still refuses, naming the key.
Golden chain: a cycle without the delegation_history key publishes and says which step is unread. Error chain: a required step
without its key still refuses by name. Named injected break: ``test_control_*`` removes the key of the history step from a copy
of the limits, which is the state of the overlay on 2026-11-14 09:37Z.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_factory_floor_publish import (
    NODE_DIR,
    HandlerFactoryFloorPublish,
    load_runtime,
)
from omnimarket.nodes.node_factory_floor_publish_effect.models.model_factory_floor_publish import (
    ModelFactoryFloorPublishRequest,
    ModelFactoryFloorPublishResult,
    ModelPublishLimits,
    PublishLimitMissingError,
)
from tests.nodes.node_factory_floor_publish_effect.limits_support import TEST_LIMITS
from tests.nodes.node_factory_floor_publish_effect.test_tracker_served_on_refusal import (
    FloorStepRefusing,
    _home,
    _overlay,
)

pytestmark = pytest.mark.unit

MISSING_KEY = "overlay node_factory_floor_publish_effect: key limits.step_timeout_seconds.{} is missing"


def _without(name: str) -> ModelPublishLimits:
    document = TEST_LIMITS.model_dump()
    del document["step_timeout_seconds"][name]
    return ModelPublishLimits.model_validate(document)


def _cycle(
    tmp_path: Path, limits: ModelPublishLimits
) -> tuple[ModelFactoryFloorPublishResult, Path]:
    request = ModelFactoryFloorPublishRequest(
        home=str(_home(tmp_path, previous=None)),
        overlay=_overlay(tmp_path).model_copy(update={"limits": limits}),
    )
    result = HandlerFactoryFloorPublish(FloorStepRefusing(refuse=False)).handle(
        request, environ={}
    )
    return result, tmp_path / "site"


def test_golden_a_missing_step_key_skips_that_step_and_every_other_step_publishes(
    tmp_path: Path,
) -> None:
    result, site = _cycle(tmp_path, _without("delegation_history"))
    by_name = {s.name: s for s in result.steps}
    assert by_name["delegation_history"].status == "unread"
    assert by_name["delegation_history"].detail == MISSING_KEY.format(
        "delegation_history"
    )
    assert by_name["delegation_history"].seconds == 0.0
    # Every other step ran as it does in a cycle with every key: the same results, step for step (the fake runner answers some
    # capture steps with no output, so "as in the full cycle" is the comparison, not "ok").
    full, _ = _cycle(tmp_path / "full", TEST_LIMITS)
    expected = {s.name: s.status for s in full.steps if s.name != "delegation_history"}
    assert expected
    assert {
        n: s.status for n, s in by_name.items() if n != "delegation_history"
    } == expected
    assert any(status == "ok" for status in expected.values())
    status = json.loads((site / load_runtime().names.status).read_text())
    unread = [s for s in status["steps"] if s["status"] == "unread"]
    assert [s["name"] for s in unread] == ["delegation_history"]
    assert unread[0]["detail"] == MISSING_KEY.format("delegation_history")
    tracker = site / load_runtime().names.delegation_tracker_data
    assert tracker.exists()


def test_golden_the_cycle_with_every_key_has_no_unread_step(tmp_path: Path) -> None:
    result, site = _cycle(tmp_path, TEST_LIMITS)
    history = next(s for s in result.steps if s.name == "delegation_history")
    assert history.status != "unread"
    status = json.loads((site / load_runtime().names.status).read_text())
    assert not [s for s in status["steps"] if s["status"] == "unread"]


def test_error_a_required_step_without_its_key_still_refuses_by_name() -> None:
    with pytest.raises(PublishLimitMissingError) as caught:
        load_runtime().with_limits(_without("factory_floor"))
    assert str(caught.value) == MISSING_KEY.format("factory_floor")


def test_error_the_longest_path_of_a_runtime_with_an_unread_step_is_still_bounded() -> (
    None
):
    rt = load_runtime().with_limits(_without("delegation_history"))
    step = next(s for s in rt.steps if s.name == "delegation_history")
    assert not step.limit_resolved
    with pytest.raises(PublishLimitMissingError):
        _ = step.seconds
    assert sum(rt.declared_longest_path().values()) <= rt.cycle.bound_seconds


def test_the_pages_draw_an_unread_step_as_unread_not_as_failed() -> None:
    floor = (NODE_DIR / "page" / "floor" / "index.html").read_text()
    detail = (NODE_DIR / "page" / "detail.html").read_text()
    assert 'st.status === "unread"' in floor
    assert 'st.status === "unread"' in detail
