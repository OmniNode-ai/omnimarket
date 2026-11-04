# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The request path of the agent review dispatch, through fake ports, under both profiles."""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from typing import Any

import pytest

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_agent_review_dispatch_orchestrator.models import (
    EnumAgentReviewOutcome,
    EnumAgentReviewReason,
    ModelAgentFinding,
)
from omnimarket.nodes.node_agent_review_dispatch_orchestrator.overlay import (
    load_agent_review_config,
)

from .support import (
    HEAD,
    KEY,
    MOVED,
    OVERLAY,
    Harness,
    SimulatedCrash,
    World,
    build_harness,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def make(profile: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    def _make(**kwargs: Any) -> Harness:
        return build_harness(profile, monkeypatch, tmp_path, **kwargs)

    return _make


def _overlay(**changes: Any) -> dict[str, Any]:
    fields = copy.deepcopy(OVERLAY)
    for dotted, value in changes.items():
        target = fields
        *parents, leaf = dotted.split("__")
        for name in parents:
            target = target[name]
        target[leaf] = value
    return fields


async def test_a_new_head_ends_completed_with_one_stored_receipt_and_one_post(
    make: Any,
) -> None:
    h: Harness = make()
    terminal = await h.run(h.request())
    assert terminal.outcome is EnumAgentReviewOutcome.COMPLETED
    assert terminal.historical is False
    stored = h.world.stored[KEY]
    assert terminal.receipt_id == stored.id
    assert h.world.posts == [stored.id]
    assert [v.identity for v in stored.voters] == ["voter-one", "voter-two"]
    assert all(v.delegation_event_id and v.rung == "lab" for v in stored.voters)
    assert stored.roster_snapshot.overlay_revision == "test-revision-1"
    assert stored.quorum_verdict == "passed"
    assert stored.reviewer_id == "test-reviewer"


async def test_two_simultaneous_requests_store_one_receipt_and_post_its_id(
    make: Any,
) -> None:
    world = World(lab_gate=asyncio.Event())
    h: Harness = make(world=world)
    first, second = await asyncio.gather(
        h.run(h.request("cmd-1")), h.run(h.request("cmd-2"))
    )
    assert world.lab_arrivals >= 4, (
        "both requests ran a lab review (a cost, not a hazard)"
    )
    assert world.store_writes == 1
    stored = world.stored[KEY]
    assert {first.receipt_id, second.receipt_id} == {stored.id}
    assert world.posts
    assert set(world.posts) == {stored.id}


async def test_a_head_with_a_stored_receipt_is_not_reviewed_again(make: Any) -> None:
    h: Harness = make()
    await h.run(h.request("cmd-1"))
    stored = h.world.stored[KEY]
    lab_before, sign_before = len(h.world.lab_calls), h.world.sign_calls
    again = await h.run(h.request("cmd-2"))
    assert len(h.world.lab_calls) == lab_before
    assert h.world.sign_calls == sign_before
    assert h.world.store_writes == 1
    assert again.receipt_id == stored.id
    assert set(h.world.posts) == {stored.id}


async def test_a_request_after_a_failed_run_starts_again(make: Any) -> None:
    world = World(lab_delay=1.0)
    h: Harness = make(overlay=_overlay(deadline_seconds=0.05), world=world)
    failed = await h.run(h.request("cmd-1"))
    assert failed.outcome is EnumAgentReviewOutcome.FAILED
    assert failed.reason is EnumAgentReviewReason.DEADLINE_EXCEEDED
    assert world.posts == []
    world.lab_delay = 0.0
    h2: Harness = make(overlay=_overlay(deadline_seconds=5.0), world=world)
    again = await h2.run(h2.request("cmd-2"))
    assert again.outcome is EnumAgentReviewOutcome.COMPLETED
    assert world.store_writes == 1


async def test_the_nodes_own_deadline_ends_failed_and_posts_nothing(make: Any) -> None:
    world = World(lab_delay=1.0)
    h: Harness = make(overlay=_overlay(deadline_seconds=0.05), world=world)
    terminal = await h.run(h.request())
    assert terminal.outcome is EnumAgentReviewOutcome.FAILED
    assert terminal.reason is EnumAgentReviewReason.DEADLINE_EXCEEDED
    assert world.posts == []
    assert world.store_writes == 0


@pytest.mark.parametrize(
    "point",
    [
        "before_lab_review",
        "after_sign_before_store",
        "after_store_before_post",
        "after_post",
    ],
)
async def test_a_crash_at_each_step_then_a_retry_leaves_one_receipt_and_its_post(
    make: Any, point: str
) -> None:
    world = World(crash_at=point)
    h: Harness = make(world=world)
    with pytest.raises(SimulatedCrash):
        await h.handler.handle(h.request("cmd-1"))
    retried = await h.run(h.request("cmd-1"))
    assert retried.outcome is EnumAgentReviewOutcome.COMPLETED
    assert world.store_writes == 1
    stored = world.stored[KEY]
    assert world.posts
    assert set(world.posts) == {stored.id}
    assert retried.receipt_id == stored.id


async def test_a_post_that_committed_before_a_timeout_is_not_posted_twice(
    make: Any,
) -> None:
    world = World(post_commits_then_times_out=True)
    h: Harness = make(world=world)
    terminal = await h.run(h.request())
    assert terminal.outcome is EnumAgentReviewOutcome.COMPLETED
    assert world.posts == [world.stored[KEY].id]


async def test_a_stale_head_at_the_first_read_refuses_and_posts_nothing(
    make: Any,
) -> None:
    world = World(heads=[MOVED])
    h: Harness = make(world=world)
    terminal = await h.run(h.request())
    assert terminal.outcome is EnumAgentReviewOutcome.REFUSED
    assert terminal.reason is EnumAgentReviewReason.STALE_HEAD
    assert world.lab_calls == []
    assert world.posts == []


async def test_a_stale_head_at_the_reread_refuses_and_posts_nothing(make: Any) -> None:
    world = World(heads=[HEAD, MOVED])
    h: Harness = make(world=world)
    terminal = await h.run(h.request())
    assert terminal.outcome is EnumAgentReviewOutcome.REFUSED
    assert terminal.reason is EnumAgentReviewReason.STALE_HEAD
    assert world.posts == []


async def test_a_delivery_after_a_recorded_terminal_is_historical_and_posts_nothing(
    make: Any,
) -> None:
    h: Harness = make()
    first = await h.run(h.request("cmd-1"))
    calls = h.world.total_calls()
    replay = await h.run(h.request("cmd-1"))
    assert replay.historical is True
    assert replay.model_copy(update={"historical": False}) == first
    assert h.world.total_calls() == calls, "a replay calls no port but the log"


async def test_a_lab_shortfall_refuses_with_lab_rung_insufficient(make: Any) -> None:
    world = World(lab_empty_event={"voter-two"})
    h: Harness = make(world=world)
    terminal = await h.run(h.request())
    assert terminal.outcome is EnumAgentReviewOutcome.REFUSED
    assert terminal.reason is EnumAgentReviewReason.LAB_RUNG_INSUFFICIENT
    assert world.sign_calls == 0
    assert world.store_writes == 0
    assert world.posts == []


async def test_a_voter_that_cannot_be_reached_is_a_lab_shortfall(make: Any) -> None:
    world = World(lab_raises={"voter-one"})
    h: Harness = make(world=world)
    terminal = await h.run(h.request())
    assert terminal.reason is EnumAgentReviewReason.LAB_RUNG_INSUFFICIENT
    assert world.posts == []


async def test_two_voters_raising_the_same_finding_block_the_receipt(make: Any) -> None:
    finding = ModelAgentFinding(path="a.py", line=10, severity="critical", claim="x")
    world = World(lab_findings={"voter-one": (finding,), "voter-two": (finding,)})
    h: Harness = make(world=world)
    terminal = await h.run(h.request())
    assert terminal.outcome is EnumAgentReviewOutcome.COMPLETED
    stored = world.stored[KEY]
    assert stored.quorum_verdict == "blocked"
    assert len(stored.blocking) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"roster__0__provider": "anthropic"},
        {"roster__0__endpoint_class": "review_gateway"},
        {"policy__approved_identities": ["voter-one"]},
        {"roster__1__model": ""},
        {"roster__0__rung": "cloud", "rung_order": ["cloud", "lab"]},
        {"policy__forbidden_hosts": ["192.0.2.12"]},
    ],
    ids=["provider", "gateway", "unknown-identity", "missing-model", "rung", "host"],
)
async def test_a_forbidden_roster_causes_zero_calls(
    make: Any, changes: dict[str, Any]
) -> None:
    fields = copy.deepcopy(OVERLAY)
    for dotted, value in changes.items():
        parts = dotted.split("__")
        target: Any = fields
        for name in parts[:-1]:
            target = target[int(name)] if name.isdigit() else target[name]
        leaf: Any = int(parts[-1]) if parts[-1].isdigit() else parts[-1]
        target[leaf] = value
    world = World()
    h: Harness = make(overlay=fields, world=world)
    terminal = await h.run(h.request())
    assert terminal.outcome is EnumAgentReviewOutcome.REFUSED
    assert terminal.reason is EnumAgentReviewReason.ROSTER_FORBIDDEN
    assert world.total_calls() == 0
    assert world.stored == {}
    assert world.terminals == {}


def test_the_node_refuses_to_load_without_its_overlay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    with pytest.raises(NodeOverlayError):
        load_agent_review_config()
