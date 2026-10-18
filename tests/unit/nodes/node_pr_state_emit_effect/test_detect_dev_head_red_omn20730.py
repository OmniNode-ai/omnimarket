# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20730: a red verdict on a landing branch head becomes one dev-head-red event on the bus."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.events.topics import DEV_HEAD_RED_TOPIC_V1, GITHUB_BRANCH_HEAD_TOPIC_V1
from omnimarket.models.dev_head_red import (
    ModelDevHeadRedEvent,
    dev_head_red_event_id,
)
from omnimarket.nodes.node_pr_state_emit_effect.handlers.handler_detect_dev_head_red import (
    DEV_HEAD_RED_INDEX,
    HandlerDetectDevHeadRed,
)

pytestmark = pytest.mark.unit

SHA = "a" * 40
CONTRACT = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_pr_state_emit_effect/contract.yaml"
)


@pytest.fixture(autouse=True)
def empty_index() -> Iterator[None]:
    DEV_HEAD_RED_INDEX.clear()
    yield
    DEV_HEAD_RED_INDEX.clear()


def observation(**overrides: Any) -> dict[str, Any]:
    """The wire payload node_github_webhook_ingress_effect publishes, with transport enrichment."""
    payload: dict[str, Any] = {
        "topic": GITHUB_BRANCH_HEAD_TOPIC_V1,
        "kind": "branch-head-status",
        "entity_id": "OmniNode-ai/omnimarket@dev",
        "repo": "OmniNode-ai/omnimarket",
        "branch": "dev",
        "delivery_id": "6f1c2d3e-0000-4000-8000-000000000001",
        "github_event": "check_run",
        "as_of": "2026-10-13T21:42:00Z",
        "sha": SHA,
        "before_sha": None,
        "ci_status": "FAILURE",
        "check_name": "CI Summary",
        "merge_group_ref": None,
        "_topic": GITHUB_BRANCH_HEAD_TOPIC_V1,
        "correlation_id": "6f1c2d3e-0000-4000-8000-0000000000ff",
    }
    payload.update(overrides)
    return payload


async def detect(payload: dict[str, Any]) -> tuple[Any, ...]:
    output = await HandlerDetectDevHeadRed().handle(payload)
    return tuple(output.events)


@pytest.mark.asyncio
async def test_a_red_summary_on_the_landing_branch_emits_one_event() -> None:
    events = await detect(observation())
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, ModelDevHeadRedEvent)
    assert event.repo == "OmniNode-ai/omnimarket"
    assert event.branch == "dev"
    assert event.head_sha == SHA
    assert event.check == "CI Summary"
    assert event.conclusion == "failure"
    assert event.completed_at == "2026-10-13T21:42:00Z"
    assert event.event_id == dev_head_red_event_id(
        "OmniNode-ai/omnimarket", "dev", SHA, "CI Summary"
    )


@pytest.mark.asyncio
async def test_a_redelivered_or_replayed_red_emits_nothing_more() -> None:
    assert len(await detect(observation())) == 1
    assert await detect(observation()) == ()
    # A second delivery of the same verdict (GitHub retries, the relay replays) is the same red.
    again = observation(delivery_id="6f1c2d3e-0000-4000-8000-000000000002")
    assert await detect(again) == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"ci_status": "SUCCESS"},
        {"ci_status": "PENDING"},
        {"ci_status": "CANCELLED"},
        {"ci_status": None},
        {"check_name": None},
        {"branch": "main", "entity_id": "OmniNode-ai/omnimarket@main"},
        {
            "kind": "merge-group-status",
            "merge_group_ref": "gh-readonly-queue/dev/pr-1-" + SHA,
        },
        {"kind": "branch-ref-advanced", "ci_status": None, "check_name": None},
    ],
    ids=[
        "green",
        "pending",
        "cancelled",
        "no-verdict",
        "no-check",
        "not-landing-branch",
        "merge-group",
        "ref-advance",
    ],
)
async def test_anything_but_a_red_landing_head_verdict_emits_nothing(
    overrides: dict[str, Any],
) -> None:
    assert await detect(observation(**overrides)) == ()


@pytest.mark.asyncio
async def test_a_new_head_red_on_the_same_check_is_a_new_event() -> None:
    assert len(await detect(observation())) == 1
    assert len(await detect(observation(sha="b" * 40))) == 1


def test_contract_routes_branch_head_to_the_detector_and_publishes_dev_head_red() -> (
    None
):
    raw = yaml.safe_load(CONTRACT.read_text())
    bus = raw["event_bus"]
    assert GITHUB_BRANCH_HEAD_TOPIC_V1 in bus["subscribe_topics"]
    assert DEV_HEAD_RED_TOPIC_V1 in bus["publish_topics"]
    routes = [
        h
        for h in raw["handler_routing"]["handlers"]
        if h.get("topic") == GITHUB_BRANCH_HEAD_TOPIC_V1
    ]
    assert len(routes) == 1
    assert routes[0]["handler"]["name"] == "HandlerDetectDevHeadRed"
    declared = {row["event_type"]: row["topic"] for row in raw["published_events"]}
    assert declared["DevHeadRedEvent"] == DEV_HEAD_RED_TOPIC_V1
    assert HandlerDetectDevHeadRed.published_event_topics == {
        ModelDevHeadRedEvent: DEV_HEAD_RED_TOPIC_V1
    }
    assert raw["dev_head_red"]["branches"] == ["dev"]
    produced = {row["topic"] for row in raw["externally_produced_topics"]}
    assert GITHUB_BRANCH_HEAD_TOPIC_V1 in produced
